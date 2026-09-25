# -*- coding: utf-8 -*-
"""
SSRF 防护测试（文档 §13 用例 1~2）。

覆盖：
    必须拒绝 —— 内网、云元数据、回环（默认）、非 http(s)、带凭证的 URL
    必须放行 —— 正常外部 https、显式允许的回环

用法：
    .\\.venv\\Scripts\\python.exe tools\\test_url_guard.py
"""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.security.url_guard import UrlGuardError, validate_outbound_url  # noqa: E402

# (URL, 是否应放行, 说明)
CASES: list[tuple[str, bool, str]] = [
    # ── 必须拒绝 ──
    ("http://169.254.169.254/latest/meta-data/", False, "云元数据端点（AWS/阿里云）"),
    ("http://127.0.0.1:22", False, "本机 SSH 端口"),
    ("http://127.0.0.1:8000/api/state", False, "本机自身服务"),
    ("http://localhost:3306", False, "localhost 写法"),
    ("http://10.0.0.5/", False, "A 类私有"),
    ("http://192.168.1.1/", False, "C 类私有"),
    ("http://172.16.0.1/", False, "B 类私有"),
    ("http://[::1]:8080/", False, "IPv6 回环"),
    ("http://[fe80::1]/", False, "IPv6 链路本地"),
    ("http://0.0.0.0/", False, "通配地址"),
    ("ftp://example.com/", False, "非 http(s) 协议"),
    ("file:///etc/passwd", False, "file 协议"),
    ("http://user:pass@example.com/", False, "URL 内夹带凭证"),
    ("http://", False, "缺少主机名"),
    ("", False, "空字符串"),
    # ── 必须放行 ──
    ("https://api.deepseek.com", True, "正常外部服务"),
    ("https://dashscope.aliyuncs.com/api/v1", True, "阿里云百炼"),
    ("https://api.openai.com/v1", True, "OpenAI"),
    ("http://127.0.0.1:8189", True, "本地 FLUX 服务（显式 allow_loopback）"),
]


def main() -> int:
    print("=" * 74)
    print("SSRF 防护测试")
    print("=" * 74)
    print(f"{'URL':<46}{'预期':<8}{'实际':<8}{'结果'}")
    print("-" * 74)

    passed = failed = 0
    for url, should_pass, why in CASES:
        # 本地回环那条需要显式开启
        loopback = "allow_loopback" in why
        try:
            validate_outbound_url(url, allow_loopback=loopback)
            ok, got = True, "放行"
        except UrlGuardError:
            ok, got = False, "拒绝"
        except Exception as exc:  # noqa: BLE001
            ok, got = False, f"异常"

        good = ok == should_pass
        passed += good
        failed += not good
        mark = "✅" if good else "❌"
        print(f"{url[:44]:<46}{'放行' if should_pass else '拒绝':<8}{got:<8}{mark} {why}")

    print("-" * 74)
    print(f"通过 {passed} / {len(CASES)}" + (f"，失败 {failed}" if failed else " ✅ 全部通过"))
    print("=" * 74)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
