# -*- coding: utf-8 -*-
"""检查安全模块所需的第三方依赖是否就绪。"""

from __future__ import annotations

import importlib
import sys

# 每个依赖：模块名、用途、是否必需（否则有降级方案）
REQUIRED = [
    ("argon2", "密码哈希（argon2id，文档 §3.7 要求成熟方案）", True),
    ("pyotp", "管理员 MFA（TOTP，文档 §3.9）", True),
    ("qrcode", "MFA 二维码（可选，无则显示密钥文本）", False),
    ("bcrypt", "argon2 不可用时的备选哈希", False),
]

print("=" * 70)
print("安全模块依赖检查")
print("=" * 70)
missing_required = []
for name, why, required in REQUIRED:
    try:
        mod = importlib.import_module(name)
        ver = getattr(mod, "__version__", "?")
        print(f"  ✅ {name:<12} {ver:<12} {why}")
    except ImportError:
        tag = "必需" if required else "可选"
        print(f"  ❌ {name:<12} {'':<12} [{tag}] {why}")
        if required:
            missing_required.append(name)

print("-" * 70)
if missing_required:
    print(f"缺少必需依赖：{', '.join(missing_required)}")
    print("\n安装命令：")
    print(f"  .\\.venv\\Scripts\\python.exe -m pip install {' '.join(missing_required)}")
else:
    print("✅ 必需依赖齐备")
print("=" * 70)
sys.exit(1 if missing_required else 0)
