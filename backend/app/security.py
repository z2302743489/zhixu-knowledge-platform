"""Anonymous browser ownership, operation-mode gates, and encrypted service keys.

Role selection is deliberately not authentication. Deploy only in a trusted network.
"""
import base64
import hashlib
import ipaddress
import os
import secrets
import socket
from pathlib import Path
from urllib.parse import urlsplit
from cryptography.fernet import Fernet, InvalidToken
from fastapi import Header, HTTPException, Request
from .config import settings


def _fernet():
    secret = settings.secret_key
    if not secret:
        target = settings.data_dir / ".secret_key"
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as handle:
                handle.write(secrets.token_urlsafe(48))
        except FileExistsError:
            pass
        secret = target.read_text().strip()
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest()))


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode() if value else ""


def decrypt(value: str) -> str:
    try:
        return _fernet().decrypt(value.encode()).decode() if value else ""
    except InvalidToken as exc:
        raise ValueError("服务密钥无法解密，请检查部署加密密钥或重新配置服务密钥") from exc


def role(x_role: str = Header(default="user")) -> str:
    if x_role not in {"admin", "manager", "user"}:
        raise HTTPException(400, "无效角色模式")
    return x_role


def require_manager(x_role: str = Header(default="user")):
    if x_role not in {"admin", "manager"}:
        raise HTTPException(403, "请切换为管理员或知识库管理人员模式")
    return x_role


def require_admin(x_role: str = Header(default="user")):
    if x_role != "admin":
        raise HTTPException(403, "此操作需要管理员模式")
    return x_role


def owner(request: Request) -> str:
    return request.state.owner


def validate_provider_url(url: str, *, resolve: bool = False) -> str:
    """Validate explicit endpoints. Private hosts require operator allowlisting.

    No redirects are followed by provider clients. Network administrators should
    additionally restrict egress; DNS validation alone is not an egress firewall.
    """
    parsed = urlsplit(url)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("服务地址必须为无用户名、查询参数和片段的 HTTP(S) URL")
    host = parsed.hostname.lower()
    allow = {item.strip().lower() for item in settings.allowed_private_hosts.split(",") if item.strip()}
    if parsed.scheme == "http" and host not in allow:
        raise ValueError("云端服务必须使用 HTTPS；内网 HTTP 主机需在 ZHIXU_ALLOWED_PRIVATE_HOSTS 中显式允许")
    if host in allow:
        return url.rstrip("/")
    if host in {"localhost", "metadata.google.internal"} or host.endswith(".localhost"):
        raise ValueError("服务地址不能指向本机或元数据服务")
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        if not resolve:
            return url.rstrip("/")
        try:
            addresses = [ipaddress.ip_address(info[4][0]) for info in socket.getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM)]
        except socket.gaierror as exc:
            raise ValueError("服务地址 DNS 解析失败") from exc
    if any(not addr.is_global for addr in addresses):
        raise ValueError("服务地址不能指向未显式允许的内网、回环或保留地址")
    return url.rstrip("/")
