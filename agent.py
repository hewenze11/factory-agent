"""
agent.py - AI 软件工厂 用户服务器 Agent 主入口

架构：
- 单 Python 进程，多线程
- HTTP 服务：Waitress
- 监听 127.0.0.1:{port}，默认 34567，顺序探测 34567-34577 找可用端口
"""

import base64
import json
import logging
import os
import re
import socket
import sys
import threading
from urllib.parse import parse_qs, urlparse

import waitress

import auth
import trash_gc as factory_gc
import repo as factory_repo
from path_utils import PathError

# ─────────────────────────────────────────────────────────────────────────────
# 日志配置
# ─────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("agent")

# ─────────────────────────────────────────────────────────────────────────────
# 端口探测
# ─────────────────────────────────────────────────────────────────────────────
PORT_RANGE_START = int(os.environ.get("FACTORY_PORT_START", 34567))
PORT_RANGE_END = int(os.environ.get("FACTORY_PORT_END", 34577))
BIND_HOST = os.environ.get("FACTORY_BIND_HOST", "127.0.0.1")


def _find_free_port(start: int, end: int) -> int:
    """在 [start, end] 范围内顺序探测，返回第一个可用端口，否则抛出 RuntimeError。"""
    for port in range(start, end + 1):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind((BIND_HOST, port))
                return port
        except OSError:
            continue
    raise RuntimeError(
        f"No free port found in range {start}-{end} on {BIND_HOST}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# HTTP 工具
# ─────────────────────────────────────────────────────────────────────────────

def _json_response(
    start_response,
    status: str,
    body: dict | list,
) -> list[bytes]:
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    start_response(status, [
        ("Content-Type", "application/json; charset=utf-8"),
        ("Content-Length", str(len(payload))),
    ])
    return [payload]


def _error(start_response, code: int, message: str) -> list[bytes]:
    status_map = {
        400: "400 Bad Request",
        401: "401 Unauthorized",
        404: "404 Not Found",
        405: "405 Method Not Allowed",
        409: "409 Conflict",
        413: "413 Payload Too Large",
        500: "500 Internal Server Error",
    }
    status = status_map.get(code, f"{code} Error")
    return _json_response(start_response, status, {"error": message})


_MAX_BODY_BYTES = 10 * 1024 * 1024  # 10 MB 请求体上限


def _read_body(environ, max_bytes: int = _MAX_BODY_BYTES) -> bytes:
    """读取 request body，超过 max_bytes 抛 OverflowError（调用方返回 413）。"""
    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
    except ValueError:
        length = 0
    if length > max_bytes:
        raise OverflowError(f"Request body too large: {length} > {max_bytes}")
    wsgi_input = environ.get("wsgi.input")
    if wsgi_input is None:
        return b""
    # 流式读取，防止 chunked/未声明 Content-Length 时绕过上限
    chunks = []
    remaining = max_bytes + 1
    while remaining > 0:
        chunk = wsgi_input.read(min(8192, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    data = b"".join(chunks)
    if len(data) > max_bytes:
        raise OverflowError(f"Request body too large: {len(data)} > {max_bytes}")
    return data


def _parse_json_body(environ) -> dict:
    """读取并解析 JSON body，失败抛 ValueError。"""
    raw = _read_body(environ)
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise ValueError(f"Invalid JSON body: {e}") from e


# ─────────────────────────────────────────────────────────────────────────────
# 路由正则
# ─────────────────────────────────────────────────────────────────────────────

# project_id 只允许字母、数字、-、_
_PROJECT_ID_RE = r"[a-zA-Z0-9_-]+"

ROUTES: list[tuple[re.Pattern, str, callable]] = []  # (pattern, method, handler)


def _route(pattern: str, method: str):
    """装饰器：注册路由。"""
    def decorator(fn):
        ROUTES.append((re.compile(pattern), method, fn))
        return fn
    return decorator


# ─────────────────────────────────────────────────────────────────────────────
# 路由处理函数
# ─────────────────────────────────────────────────────────────────────────────

@_route(rf"^/repo/(?P<project_id>{_PROJECT_ID_RE})/init$", "POST")
def handle_init(environ, start_response, project_id: str):
    try:
        result = factory_repo.repo_init(project_id)
        return _json_response(start_response, "200 OK", result)
    except ValueError as e:
        return _error(start_response, 400, str(e))
    except Exception as e:
        logger.exception("repo_init error")
        return _error(start_response, 500, str(e))


@_route(rf"^/repo/(?P<project_id>{_PROJECT_ID_RE})/write$", "POST")
def handle_write(environ, start_response, project_id: str):
    try:
        body = _parse_json_body(environ)
    except OverflowError as e:
        return _error(start_response, 413, str(e))
    except ValueError as e:
        return _error(start_response, 400, str(e))

    files = body.get("files")
    # 兼容单文件格式：{"path": "...", "content": "..."} 或 {"path": "...", "content_b64": "..."}
    if files is None and ("path" in body):
        entry = {"path": body["path"]}
        if "content_b64" in body or "content_base64" in body:
            entry["content_b64"] = body.get("content_b64") or body.get("content_base64")
        else:
            entry["content"] = body.get("content", "")
        files = [entry]
    if not isinstance(files, list):
        return _error(start_response, 400, "'files' must be a list, or provide 'path'+'content'/'content_b64'")

    # 解码 content_b64/content_base64 → content（repo_write 只接受 str）
    decoded_files = []
    for item in files:
        if "content_b64" in item or "content_base64" in item:
            b64 = item.get("content_b64") or item.get("content_base64")
            try:
                content = base64.b64decode(b64).decode("utf-8")
            except Exception as exc:
                return _error(start_response, 400, f"Invalid base64 content: {exc}")
            decoded_files.append({"path": item.get("path", ""), "content": content})
        else:
            decoded_files.append(item)

    try:
        result = factory_repo.repo_write(project_id, decoded_files)
        return _json_response(start_response, "200 OK", result)
    except OverflowError as e:
        return _error(start_response, 413, str(e))
    except PathError as e:
        return _error(start_response, 400, f"Path error: {e}")
    except FileNotFoundError as e:
        return _error(start_response, 404, str(e))
    except ValueError as e:
        return _error(start_response, 400, str(e))
    except Exception as e:
        logger.exception("repo_write error")
        return _error(start_response, 500, str(e))


@_route(rf"^/repo/(?P<project_id>{_PROJECT_ID_RE})/commit$", "POST")
def handle_commit(environ, start_response, project_id: str):
    try:
        body = _parse_json_body(environ)
    except (OverflowError, ValueError) as e:
        return _error(start_response, 400, str(e))

    message = body.get("message", "")
    if not isinstance(message, str) or not message.strip():
        return _error(start_response, 400, "'message' must be a non-empty string")

    try:
        result = factory_repo.repo_commit(project_id, message)
        return _json_response(start_response, "200 OK", result)
    except FileNotFoundError as e:
        return _error(start_response, 404, str(e))
    except ValueError as e:
        return _error(start_response, 400, str(e))
    except Exception as e:
        logger.exception("repo_commit error")
        return _error(start_response, 500, str(e))


@_route(rf"^/repo/(?P<project_id>{_PROJECT_ID_RE})/log$", "GET")
def handle_log(environ, start_response, project_id: str):
    try:
        result = factory_repo.repo_log(project_id)
        return _json_response(start_response, "200 OK", result)
    except FileNotFoundError as e:
        return _error(start_response, 404, str(e))
    except Exception as e:
        logger.exception("repo_log error")
        return _error(start_response, 500, str(e))


@_route(rf"^/repo/(?P<project_id>{_PROJECT_ID_RE})/export$", "GET")
def handle_export(environ, start_response, project_id: str):
    qs = parse_qs(environ.get("QUERY_STRING", ""))
    commit_hash = qs.get("commit", [None])[0]
    fmt = qs.get("format", ["zip"])[0]

    if fmt not in ("zip", "cursor", "idea"):
        return _error(start_response, 400, f"Invalid format: {fmt!r}. Must be zip|cursor|idea")

    zip_name = f"{project_id}.zip"
    try:
        # repo_export 先做大小预检（内部抛 OverflowError），再流式生成
        chunks = factory_repo.repo_export(project_id, commit_hash, fmt)
        # 流式传输：不在内存中聚合全量数据，防止 200MB OOM
        start_response("200 OK", [
            ("Content-Type", "application/zip"),
            ("Content-Disposition", f'attachment; filename="{zip_name}"'),
            ("Transfer-Encoding", "chunked"),
        ])
        return chunks  # Waitress 会迭代 generator 流式发送
    except OverflowError as e:
        return _error(start_response, 413, str(e))
    except FileNotFoundError as e:
        return _error(start_response, 404, str(e))
    except Exception as e:
        logger.exception("repo_export error")
        return _error(start_response, 500, str(e))


@_route(rf"^/repo/(?P<project_id>{_PROJECT_ID_RE})$", "DELETE")
def handle_delete(environ, start_response, project_id: str):
    try:
        factory_repo.repo_delete(project_id)
        # 幂等：无论是否存在，统一返回 204 No Content
        start_response("204 No Content", [])
        return [b""]
    except ValueError as e:
        return _error(start_response, 400, str(e))
    except Exception as e:
        logger.exception("repo_delete error")
        return _error(start_response, 500, str(e))


@_route(r"^/metrics$", "POST")
def handle_metrics(environ, start_response):
    """接收 factory-api 推送的监控数据，只做简单记录。"""
    try:
        body = _parse_json_body(environ)
        logger.info("metrics received: %s", json.dumps(body, ensure_ascii=False)[:512])
        return _json_response(start_response, "200 OK", {"status": "ok"})
    except Exception as e:
        logger.warning("metrics parse error: %s", e)
        return _json_response(start_response, "200 OK", {"status": "ok"})  # 不因监控失败影响业务


# ─────────────────────────────────────────────────────────────────────────────
# WSGI Application
# ─────────────────────────────────────────────────────────────────────────────

def application(environ, start_response):
    method = environ.get("REQUEST_METHOD", "").upper()
    path = environ.get("PATH_INFO", "/")

    # ── 鉴权 ──────────────────────────────────────────────────────────────
    if not auth.verify_request(environ):
        return _error(start_response, 401, "Unauthorized: missing or invalid token")

    # ── 路由匹配 ──────────────────────────────────────────────────────────
    for pattern, expected_method, handler in ROUTES:
        m = pattern.match(path)
        if m:
            if method != expected_method:
                return _error(start_response, 405, f"Method {method} not allowed")
            groups = m.groupdict()
            # 过滤掉 None（可选捕获组）
            kwargs = {k: v for k, v in groups.items() if v is not None}
            return handler(environ, start_response, **kwargs)

    return _error(start_response, 404, f"Not found: {path}")


# ─────────────────────────────────────────────────────────────────────────────
# 主入口
# ─────────────────────────────────────────────────────────────────────────────

def main():
    # 确保 repos 目录存在
    os.makedirs(factory_repo.REPOS_DIR, exist_ok=True)

    # 加载/生成 token（启动时就报告）
    token = auth.get_token()
    logger.info("Agent token loaded (path=%s)", auth.TOKEN_PATH)

    # 启动 GC 调度器
    factory_gc.start_gc_scheduler()

    # 探测可用端口
    port = _find_free_port(PORT_RANGE_START, PORT_RANGE_END)
    logger.info(
        "Starting factory agent on %s:%d (threads=8)",
        BIND_HOST, port
    )

    # 写入端口文件（供外部进程发现）
    port_file = os.environ.get(
        "FACTORY_PORT_FILE",
        "/opt/factory/.factory/agent.port"
    )
    try:
        os.makedirs(os.path.dirname(port_file), exist_ok=True)
        with open(port_file, "w") as f:
            f.write(str(port))
        logger.info("Port written to %s", port_file)
    except OSError as e:
        logger.warning("Could not write port file %s: %s", port_file, e)

    # 启动 Waitress
    waitress.serve(
        application,
        host=BIND_HOST,
        port=port,
        threads=8,
        channel_timeout=120,
        cleanup_interval=30,
        connection_limit=256,
    )


if __name__ == "__main__":
    main()
