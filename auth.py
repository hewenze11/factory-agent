"""
auth.py - Token 鉴权

TOKEN_PATH: /opt/factory/.factory/agent.token
- 不存在时自动生成 32 字节随机 token（十六进制，0600 权限）
- 所有请求必须携带 Authorization: Bearer {token}，缺失或错误返回 401
"""

import os
import secrets
import stat
import threading
import logging

logger = logging.getLogger(__name__)

TOKEN_PATH = os.environ.get("FACTORY_TOKEN_PATH", "/opt/factory/.factory/agent.token")

_cached_token: str | None = None
_token_lock = threading.Lock()


def _load_or_create_token() -> str:
    """加载或创建 token，返回 token 字符串。"""
    token_path = TOKEN_PATH

    if os.path.exists(token_path):
        with open(token_path, "r", encoding="utf-8") as f:
            token = f.read().strip()
        if token:
            logger.info("Loaded existing token from %s", token_path)
            return token
        # 文件存在但内容为空，重新生成
        logger.warning("Token file is empty, regenerating: %s", token_path)

    # 生成新 token
    token = secrets.token_hex(32)  # 32 字节 → 64 字符十六进制

    # 确保目录存在
    token_dir = os.path.dirname(token_path)
    os.makedirs(token_dir, exist_ok=True)

    # 写入文件
    fd = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, token.encode("utf-8"))
    finally:
        os.close(fd)

    # 确保权限为 0600
    os.chmod(token_path, stat.S_IRUSR | stat.S_IWUSR)
    logger.info("Generated new token at %s", token_path)
    return token


def get_token() -> str:
    """获取 token（带缓存，双重检查锁防竞态）。"""
    global _cached_token
    if _cached_token is None:
        with _token_lock:
            if _cached_token is None:  # 二次检查，防止持锁前被其他线程初始化
                _cached_token = _load_or_create_token()
    return _cached_token


def verify_request(environ: dict) -> bool:
    """
    校验 HTTP 请求的 Authorization header。

    Args:
        environ: WSGI environ dict

    Returns:
        True 表示鉴权通过，False 表示拒绝
    """
    auth_header = environ.get("HTTP_AUTHORIZATION", "")
    if not auth_header.startswith("Bearer "):
        return False
    provided = auth_header[len("Bearer "):].strip()
    # 使用 hmac 比较防止时序攻击
    import hmac
    expected = get_token()
    return hmac.compare_digest(provided, expected)
