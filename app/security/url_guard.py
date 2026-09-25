# -*- coding: utf-8 -*-
"""外部 URL 安全校验 —— 防止 SSRF（文档 §11）。

## 为什么需要

`app/web/server.py` 的「测试连接」允许前端传入 `base_url`，服务端会用它发起请求。
若不校验，攻击者可让服务端访问**内网服务**或**云元数据端点**
（如 `http://169.254.169.254/`），从而探测内网、窃取凭证。

## 防护策略

1. 仅允许 `http` / `https`
2. 解析主机名后，**拒绝解析到内网 / 保留地址**（含 DNS 重绑定场景：解析后再查 IP）
3. 拒绝带用户名密码的 URL（`http://user:pass@host/`）
4. 拒绝非标准端口（可配置白名单）
5. **调用侧必须关闭重定向跟随** —— 否则 302 可绕过校验
6. 允许本地回环 **仅当**显式开启（本地模型如 FLUX 需要 `127.0.0.1:8189`）

## 已知边界

DNS 解析与实际连接之间存在 TOCTOU 窗口。彻底防御需要在连接层做 IP 校验
（socket 级别）。本模块的校验已能阻断绝大多数常见 SSRF 手法；
若将来对安全性要求更高，应在 httpx 的 transport 层再加一层 IP 校验。
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

__all__ = ["UrlGuardError", "validate_outbound_url", "is_private_host"]


class UrlGuardError(ValueError):
    """外部地址未通过安全校验。"""


# 明确禁止的地址段（内网 + 保留 + 链路本地）
_BLOCKED_NETWORKS = [
    ipaddress.ip_network("0.0.0.0/8"),          # 本网络
    ipaddress.ip_network("10.0.0.0/8"),         # 私有
    ipaddress.ip_network("100.64.0.0/10"),      # 运营商级 NAT
    ipaddress.ip_network("127.0.0.0/8"),        # 回环（另有开关）
    ipaddress.ip_network("169.254.0.0/16"),     # 链路本地（含云元数据 169.254.169.254）
    ipaddress.ip_network("172.16.0.0/12"),      # 私有
    ipaddress.ip_network("192.0.0.0/24"),       # IETF 协议分配
    ipaddress.ip_network("192.0.2.0/24"),       # TEST-NET-1
    ipaddress.ip_network("192.168.0.0/16"),     # 私有
    ipaddress.ip_network("198.18.0.0/15"),      # 基准测试
    ipaddress.ip_network("198.51.100.0/24"),    # TEST-NET-2
    ipaddress.ip_network("203.0.113.0/24"),     # TEST-NET-3
    ipaddress.ip_network("224.0.0.0/4"),        # 组播
    ipaddress.ip_network("240.0.0.0/4"),        # 保留
    ipaddress.ip_network("::1/128"),            # IPv6 回环
    ipaddress.ip_network("fc00::/7"),           # IPv6 唯一本地
    ipaddress.ip_network("fe80::/10"),          # IPv6 链路本地
    ipaddress.ip_network("ff00::/8"),           # IPv6 组播
]

# 允许的端口（空集合 = 不限制）
_ALLOWED_PORTS: set[int] = set()

_DEFAULT_PORTS = {"http": 80, "https": 443}


def _resolve(host: str) -> list[str]:
    """解析主机名到 IP 列表（解析失败返回空列表）。"""
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return []
    out: list[str] = []
    for info in infos:
        addr = info[4][0]
        # 去掉 IPv6 的 scope id（fe80::1%eth0）
        addr = addr.split("%", 1)[0]
        if addr not in out:
            out.append(addr)
    return out


def is_private_host(host: str) -> bool:
    """判断主机名是否指向内网/保留地址。

    对**域名**会做真实 DNS 解析，因此能拦住「域名解析到内网」的绕过手法。
    """
    if not host:
        return True
    lowered = host.strip().strip("[]").lower()

    # 直接给 IP
    try:
        ip = ipaddress.ip_address(lowered)
        return any(ip in net for net in _BLOCKED_NETWORKS)
    except ValueError:
        pass

    # localhost 的常见写法
    if lowered in {"localhost", "localhost.localdomain", "ip6-localhost"}:
        return True

    # 域名：解析后逐个检查
    addrs = _resolve(lowered)
    if not addrs:
        return True  # 解析不了，按不安全处理（默认拒绝，§2.5）
    for addr in addrs:
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return True
        if any(ip in net for net in _BLOCKED_NETWORKS):
            return True
    return False


def validate_outbound_url(
    url: str,
    *,
    allow_loopback: bool = False,
    allowed_hosts: set[str] | None = None,
) -> str:
    """校验并返回规范化的外部服务地址。

    Args:
        url: 待校验地址
        allow_loopback: 是否允许回环地址（**本地模型服务**如 FLUX 需要）。
            默认 False —— 默认拒绝（文档 §2.5）。
        allowed_hosts: 额外放行的主机名白名单（精确匹配，不含端口）

    Raises:
        UrlGuardError: 未通过校验

    Returns:
        去掉尾部斜杠的规范化 URL
    """
    raw = str(url or "").strip()
    if not raw:
        raise UrlGuardError("地址不能为空")

    parts = urlsplit(raw)

    # ① 协议
    scheme = (parts.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise UrlGuardError("仅支持 http 或 https 地址")

    # ② 禁止在 URL 里夹带凭证
    if parts.username or parts.password:
        raise UrlGuardError("地址中不允许包含用户名或密码")

    # ③ 主机名
    host = (parts.hostname or "").strip()
    if not host:
        raise UrlGuardError("地址缺少主机名")

    if allowed_hosts and host.lower() in {h.lower() for h in allowed_hosts}:
        return raw.rstrip("/")

    if allow_loopback:
        # 只允许回环，仍然禁止其它内网段
        try:
            ip = ipaddress.ip_address(host.strip("[]"))
            if ip.is_loopback:
                return raw.rstrip("/")
        except ValueError:
            if host.lower() in {"localhost", "localhost.localdomain"}:
                return raw.rstrip("/")
    else:
        if is_private_host(host):
            raise UrlGuardError(
                f"不允许访问内网或保留地址：{host}（如需使用本地模型服务，请显式开启 allow_loopback）"
            )

    # 若 allow_loopback 为 True 但目标不是回环，仍需检查是否为其它内网段
    if allow_loopback and is_private_host(host):
        # is_private_host 对回环返回 True，前面已放行；走到这里说明是别的内网段
        try:
            ip = ipaddress.ip_address(host.strip("[]"))
            if not ip.is_loopback:
                raise UrlGuardError(f"不允许访问内网地址：{host}")
        except ValueError:
            raise UrlGuardError(f"不允许访问内网地址：{host}")

    # ④ 端口
    if parts.port is not None:
        if parts.port <= 0 or parts.port > 65535:
            raise UrlGuardError("端口号无效")
        if _ALLOWED_PORTS and parts.port not in _ALLOWED_PORTS:
            raise UrlGuardError(f"端口 {parts.port} 不在允许范围内")

    return raw.rstrip("/")
