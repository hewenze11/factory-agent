"""
repo.py - Git 操作：init / write / commit / log / export / delete
"""

import json
import logging
import os
import shutil
import time
import zipfile  # 仅用于 ZIP_DEFLATED 常量
from typing import Generator

import git  # gitpython
import zipstream  # zipstream-new: true streaming zip generation

import trash_gc
from path_utils import PathError, safe_join

logger = logging.getLogger(__name__)

# 仓库根目录
REPOS_DIR = os.environ.get("FACTORY_REPOS_DIR", "/opt/factory/repos")

# 单次写入限制
MAX_FILES_PER_WRITE = 200
MAX_BYTES_PER_WRITE = 10 * 1024 * 1024  # 10 MB

# export 限制
MAX_EXPORT_BYTES = 200 * 1024 * 1024  # 200 MB


# ─────────────────────────────────────────────────────────────────────────────
# 工具函数
# ─────────────────────────────────────────────────────────────────────────────

def _repo_path(project_id: str) -> str:
    """返回 project_id 对应的仓库绝对路径。"""
    if not project_id or not all(c.isalnum() or c in "-_" for c in project_id):
        raise ValueError(f"Invalid project_id: {project_id!r}")
    return os.path.join(REPOS_DIR, project_id)


def _open_repo(project_id: str) -> git.Repo:
    """打开已有仓库，不存在时抛出 FileNotFoundError。"""
    path = _repo_path(project_id)
    if not os.path.isdir(os.path.join(path, ".git")):
        raise FileNotFoundError(f"Repo not initialized: {project_id!r}")
    return git.Repo(path)


# ─────────────────────────────────────────────────────────────────────────────
# 接口实现
# ─────────────────────────────────────────────────────────────────────────────

def repo_init(project_id: str) -> dict:
    """
    POST /repo/{project_id}/init
    幂等：已初始化则跳过，返回 {"status": "ok", "initialized": bool}
    """
    path = _repo_path(project_id)
    os.makedirs(path, exist_ok=True)
    git_dir = os.path.join(path, ".git")
    if os.path.isdir(git_dir):
        logger.info("repo_init: already initialized %s", project_id)
        return {"status": "ok", "initialized": False, "path": path}

    repo = git.Repo.init(path)
    # 设置默认分支名为 main
    try:
        repo.git.checkout("-b", "main")
    except git.GitCommandError:
        pass  # 已有分支时忽略
    logger.info("repo_init: initialized %s at %s", project_id, path)
    return {"status": "ok", "initialized": True, "path": path}


def repo_write(project_id: str, files: list[dict]) -> dict:
    """
    POST /repo/{project_id}/write
    body: {"files": [{"path": "...", "content": "..."}]}
    - 单次上限 200 文件 / 10 MB，超限抛 OverflowError（调用方返回 413）
    - 路径安全校验通过 safe_join
    """
    if len(files) > MAX_FILES_PER_WRITE:
        raise OverflowError(
            f"Too many files: {len(files)} > {MAX_FILES_PER_WRITE}"
        )

    repo_dir = _repo_path(project_id)
    if not os.path.isdir(os.path.join(repo_dir, ".git")):
        raise FileNotFoundError(f"Repo not initialized: {project_id!r}")

    # 预校验：路径安全 + 计算总字节
    total_bytes = 0
    validated: list[tuple[str, str]] = []
    for item in files:
        rel = item.get("path", "")
        content = item.get("content", "")
        if not isinstance(rel, str) or not isinstance(content, str):
            raise ValueError("Each file entry must have string 'path' and 'content'")

        abs_path = safe_join(repo_dir, rel)  # raises PathError on bad path
        content_bytes = content.encode("utf-8")
        total_bytes += len(content_bytes)
        if total_bytes > MAX_BYTES_PER_WRITE:
            raise OverflowError(
                f"Total content exceeds {MAX_BYTES_PER_WRITE // 1024 // 1024} MB limit"
            )
        validated.append((abs_path, content))

    # 写入文件
    written = []
    for abs_path, content in validated:
        parent = os.path.dirname(abs_path)
        os.makedirs(parent, exist_ok=True)
        with open(abs_path, "w", encoding="utf-8") as f:
            f.write(content)
        written.append(os.path.relpath(abs_path, repo_dir))

    logger.info("repo_write: wrote %d files to %s", len(written), project_id)

    # 自动提交（write 写完即 commit，减少调用方负担）
    repo = _open_repo(project_id)
    repo.git.add(A=True)
    with repo.config_writer() as cfg:
        if not cfg.has_option("user", "email"):
            cfg.set_value("user", "email", "factory-agent@cayan.ai")
        if not cfg.has_option("user", "name"):
            cfg.set_value("user", "name", "Factory Agent")
    # 只有在有变更时才 commit
    if repo.is_dirty(untracked_files=True):
        commit_msg = f"write: {len(written)} file(s)"
        commit = repo.index.commit(commit_msg)
        logger.info("repo_write: auto-committed %s", commit.hexsha[:8])
        return {"status": "ok", "written": len(written), "commit_hash": commit.hexsha}
    else:
        return {"status": "ok", "written": len(written), "commit_hash": None}


def repo_commit(project_id: str, message: str) -> dict:
    """
    POST /repo/{project_id}/commit
    提交所有变更，返回 {"commit_hash": "..."}
    """
    if not message or not message.strip():
        raise ValueError("Commit message must not be empty")

    repo = _open_repo(project_id)

    # git add -A
    repo.git.add(A=True)

    # 配置提交者（容器内无全局 git config 时需要）
    with repo.config_writer() as cfg:
        if not cfg.has_option("user", "email"):
            cfg.set_value("user", "email", "factory-agent@cayan.ai")
        if not cfg.has_option("user", "name"):
            cfg.set_value("user", "name", "Factory Agent")

    # 检查是否有变更可提交
    try:
        head_commit = repo.head.commit
        # 有 HEAD：检查 staged diff
        if not repo.index.diff(head_commit) and not repo.untracked_files:
            # 再用 porcelain status 双重确认
            status = repo.git.status("--porcelain")
            if not status.strip():
                logger.info("repo_commit: nothing to commit in %s", project_id)
                return {
                    "status": "ok",
                    "commit_hash": head_commit.hexsha,
                    "note": "nothing to commit",
                }
    except ValueError:
        pass  # 空仓库（无 HEAD），直接提交

    commit = repo.index.commit(message.strip())
    logger.info("repo_commit: %s in %s", commit.hexsha, project_id)
    return {"status": "ok", "commit_hash": commit.hexsha}


def repo_log(project_id: str) -> dict:
    """
    GET /repo/{project_id}/log
    返回最近20条 commit 列表。
    """
    repo = _open_repo(project_id)

    try:
        commits = list(repo.iter_commits(max_count=20))
    except git.GitCommandError:
        # 空仓库（无任何 commit）
        return {"status": "ok", "commits": []}

    result = []
    for c in commits:
        result.append({
            "hash": c.hexsha,
            "short_hash": c.hexsha[:8],
            "message": c.message.strip(),
            "author": str(c.author),
            "authored_date": c.authored_datetime.isoformat(),
            "committed_date": c.committed_datetime.isoformat(),
        })

    return {"status": "ok", "commits": result}


def repo_export(
    project_id: str,
    commit_hash: str | None,
    fmt: str,
) -> Generator[bytes, None, None]:
    """
    GET /repo/{project_id}/export?commit={hash}&format={zip|cursor|idea}
    流式 zip 生成器。超 200MB 抛 OverflowError。

    cursor / idea 格式目前和 zip 相同，但会在 zip 内加入对应的配置文件占位。
    """
    repo = _open_repo(project_id)

    # 确定 commit
    try:
        if commit_hash:
            commit = repo.commit(commit_hash)
        else:
            commit = repo.head.commit
    except (git.BadName, git.GitCommandError, ValueError) as e:
        raise FileNotFoundError(f"Commit not found: {commit_hash!r}") from e

    # 大小预检：遍历 tree 统计原始字节，超限提前拒绝（避免占用内存）
    total_size = 0
    blobs = []
    for blob in commit.tree.traverse():
        if blob.type != "blob":
            continue
        total_size += blob.size
        if total_size > MAX_EXPORT_BYTES:
            raise OverflowError(
                f"Export exceeds {MAX_EXPORT_BYTES // 1024 // 1024} MB limit"
            )
        blobs.append(blob)

    # 真正流式生成 zip（zipstream-new），逐 blob 写入后即时 yield，不聚合整个 zip 到内存
    # zipstream-new API: zipstream.ZipFile（与标准库 zipfile.ZipFile 同接口），可迭代产出 bytes
    zs = zipstream.ZipFile(mode="w", compression=zipstream.ZIP_DEFLATED, allowZip64=True)

    # IDE 配置文件（小文件，writestr 一次性写入可接受，zipstream-new 兼容标准库 API）
    if fmt == "cursor":
        zs.writestr(".cursor/settings.json",
                    json.dumps({"project": project_id}).encode())
    elif fmt == "idea":
        zs.writestr(
            f".idea/{project_id}.iml",
            f'<?xml version="1.0"?>\n<module type="PYTHON_MODULE" version="4"/>\n'.encode()
        )

    # 逐 blob 写入：用 write_iter 流式喂数据，每个 blob 只在内存中临时放一份
    def _blob_iter(b):
        """将 blob 数据流转为 generator，支持 write_iter 流式写入。"""
        chunk_size = 65536
        stream = b.data_stream
        while True:
            chunk = stream.read(chunk_size)
            if not chunk:
                break
            yield chunk

    for blob in blobs:
        arc_name = f"{project_id}/{blob.path}"
        zs.write_iter(arc_name, _blob_iter(blob))

    # 流式 yield 给 Waitress，每次产出一个 zip chunk
    yield from zs


def repo_delete(project_id: str) -> bool:
    """
    DELETE /repo/{project_id}
    软删除：移入 /opt/factory/trash/{project_id}.bak.{unix_ts}
    已删除（不存在）时幂等返回 False（调用方返回 204）。
    """
    repo_dir = _repo_path(project_id)

    if not os.path.isdir(repo_dir):
        logger.info("repo_delete: %s not found, idempotent", project_id)
        return False  # 已删除，幂等

    trash_dir = trash_gc.TRASH_DIR
    os.makedirs(trash_dir, exist_ok=True)
    ts = int(time.time())
    trash_target = os.path.join(trash_dir, f"{project_id}.bak.{ts}")

    with trash_gc.trash_lock:
        if not os.path.isdir(repo_dir):
            return False  # 并发删除，幂等
        shutil.move(repo_dir, trash_target)

    logger.info("repo_delete: moved %s → %s", repo_dir, trash_target)
    return True  # 成功删除
