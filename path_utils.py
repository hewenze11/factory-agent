"""
path_utils.py - 路径安全校验（7步铁律）

每次写入前必须调用 safe_join(base_dir, rel_path)。
成功返回绝对路径字符串，失败抛出 PathError。
"""

import os
from urllib.parse import unquote


class PathError(ValueError):
    """路径非法，上层捕获后返回400。"""
    pass


def safe_join(base_dir: str, rel_path: str) -> str:
    """
    对 rel_path 执行7步路径安全校验，返回 base_dir 下的绝对路径。

    Args:
        base_dir: 已解析的绝对目录路径（repo 根目录）
        rel_path: 客户端传入的相对路径

    Returns:
        安全的绝对路径字符串

    Raises:
        PathError: 任何步骤检测到非法路径
    """
    # ── 步骤 1：URL 解码（strict 模式，非法 % 序列直接报错）──────────────
    try:
        decoded = unquote(rel_path, errors="strict")
    except Exception as exc:
        raise PathError(f"URL decode failed: {exc}") from exc

    # ── 步骤 2：解码后仍含 % → 双重编码攻击 ─────────────────────────────
    if "%" in decoded:
        raise PathError("Double-encoding detected: '%' found after URL decode")

    # ── 步骤 3：拒绝前后空白（不自动修正，直接拒绝），再检查空/trivial路径 ──
    # 必须用 stripped 做后续所有检查，防止前导空格绕过 isabs 等检测
    if decoded != decoded.strip():
        raise PathError(f"Leading/trailing whitespace in path not allowed: {decoded!r}")
    stripped = decoded  # 此时 decoded 已无前后空白，统一用同一变量
    if not stripped or stripped in (".", "/", "\\"):
        raise PathError(f"Path is empty or trivially relative: {decoded!r}")

    # ── 步骤 4：绝对路径检测（基于已确认无空白的 decoded）─────────────────
    if os.path.isabs(decoded):
        raise PathError(f"Absolute path not allowed: {decoded!r}")

    # ── 步骤 5：逐段检查 ".."、控制字符、空段 ───────────────────────────
    # 统一分隔符后再分段
    normalized_for_split = decoded.replace("\\", "/")
    segments = normalized_for_split.split("/")
    for seg in segments:
        if seg == "..":
            raise PathError(f"Path traversal '..' detected in: {decoded!r}")
        if seg == "":
            # 允许 "a//b" 形式的空段只在中间出现（不允许前导/后置/连续空段），
            # 实际上空段意味着 // 或开头 /，前两步已挡住开头 /，
            # 中间连续空段（a//b）也拒绝，更安全。
            raise PathError(f"Empty path segment detected in: {decoded!r}")
        for ch in seg:
            code = ord(ch)
            # 0x20（空格）也禁止：Windows/macOS 下文件名末尾空格会被静默截断，可绕过检查
            if code <= 0x20 or code == 0x7F:
                raise PathError(
                    f"Control character or space (0x{code:02X}) in path segment {seg!r}"
                )

    # ── 步骤 6：os.lstat 检查每级父目录，发现 symlink → 400 ─────────────
    parts = decoded.replace("\\", "/").split("/")
    cumulative = base_dir
    for part in parts[:-1]:  # 检查中间每一级目录
        cumulative = os.path.join(cumulative, part)
        try:
            st = os.lstat(cumulative)
        except FileNotFoundError:
            break  # 目录还不存在，后续 write 会创建，安全
        except OSError:
            break
        else:
            import stat as _stat
            if _stat.S_ISLNK(st.st_mode):
                raise PathError(f"Symlink detected in path: {cumulative!r}")

    # ── 步骤 7：realpath + commonpath 最终兜底 ──────────────────────────
    candidate = os.path.normpath(os.path.join(base_dir, decoded))
    real_base = os.path.realpath(base_dir)
    real_candidate = os.path.realpath(candidate)
    try:
        common = os.path.commonpath([real_base, real_candidate])
    except ValueError:
        # Windows 上跨驱动器时抛 ValueError
        raise PathError(f"Path escapes base directory: {decoded!r}")
    if common != real_base:
        raise PathError(
            f"Path escapes base directory: {decoded!r} "
            f"(resolved to {real_candidate!r}, base is {real_base!r})"
        )

    return candidate
