# -*- coding: utf-8 -*-
"""
验证可移植包的安全性：确认不含密钥、隐私数据与无关大件。

用法：
    .\\.venv\\Scripts\\python.exe tools\\verify_portable_pack.py
"""
from __future__ import annotations

import json
import pathlib
import re
import sys
import zipfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
DELIVERY = ROOT / "delivery"

# 明令禁止出现在包内的内容（路径片段 → 原因）
# 注意：用「路径片段」判断时容易被子串误伤（如 `.env` 命中 `.env.example`），
#       因此这里区分「必须完全等于某文件名」与「不得作为路径段出现」两类。
FORBIDDEN_EXACT_NAMES: list[tuple[str, str]] = [
    (".env", "环境变量文件，可能含密钥"),
]

FORBIDDEN_PATH_SEGMENTS: list[tuple[str, str]] = [
    ("/_references/", "用户自有素材与第三方图片（隐私/版权）"),
    ("/_references", "用户自有素材与第三方图片（隐私/版权）"),
    ("/.venv/", "虚拟环境，应重建"),
    ("/desktop/", "桌面版构建产物（已冻结）"),
    ("/_archive", "历史归档"),
    ("/output_local_", "开发期验证产物"),
    ("/__pycache__/", "Python 缓存"),
    ("/logs/", "日志"),
    ("/data/security/", "⚠️ 用户账号与会话数据（密码哈希 / 登录令牌）"),
]

FORBIDDEN_SUFFIXES: list[tuple[str, str]] = [
    (".exe", "桌面安装包"),
]

# 密钥形态的特征（用于扫描文本内容）
SECRET_PATTERNS = [
    (re.compile(r"sk-[A-Za-z0-9]{16,}"), "OpenAI 风格密钥"),
    (re.compile(r"sk-ant-[A-Za-z0-9\-_]{20,}"), "Anthropic 风格密钥"),
    (re.compile(r"\b[0-9a-f]{32,}\b"), "疑似长十六进制密钥"),
    (re.compile(r"Bearer\s+[A-Za-z0-9\-_.]{20,}"), "Bearer 令牌"),
]


def main() -> int:
    packs = sorted(DELIVERY.glob("*可移植包*.zip"))
    if not packs:
        print("未找到可移植包")
        return 1
    pack = packs[-1]

    print("=" * 78)
    print(f"可移植包安全验证：{pack.name}")
    print(f"大小：{pack.stat().st_size/1024/1024:.1f} MB")
    print("=" * 78)

    problems: list[str] = []
    with zipfile.ZipFile(pack) as z:
        names = z.namelist()
        print(f"\n【包内文件数】{len(names)}")

        # ① 禁止路径
        print("\n【① 禁止内容检查】")
        norm = [("/" + n.replace("\\", "/").lstrip("/")) for n in names]

        for fname, why in FORBIDDEN_EXACT_NAMES:
            hits = [n for n in norm if n.rsplit("/", 1)[-1] == fname]
            if hits:
                problems.append(f"包含禁止文件 {fname}（{why}）")
                print(f"  ❌ {fname:<20} 命中 {len(hits)} 项 —— {why}")
            else:
                print(f"  ✅ {fname:<20} 未出现（.example 等模板不算）")

        for frag, why in FORBIDDEN_PATH_SEGMENTS:
            hits = [n for n in norm if frag in n]
            if hits:
                problems.append(f"包含禁止内容 {frag}（{why}）：{len(hits)} 项")
                print(f"  ❌ {frag:<20} 命中 {len(hits)} 项 —— {why}")
            else:
                print(f"  ✅ {frag:<20} 未出现")

        for suffix, why in FORBIDDEN_SUFFIXES:
            hits = [n for n in norm if n.lower().endswith(suffix)]
            if hits:
                problems.append(f"包含禁止内容 *{suffix}（{why}）：{len(hits)} 项")
                print(f"  ❌ *{suffix:<19} 命中 {len(hits)} 项 —— {why}")
            else:
                print(f"  ✅ *{suffix:<19} 未出现")

        # ② settings.json 脱敏
        print("\n【② 配置脱敏检查】")
        cfg_name = next((n for n in names if n.endswith("config/settings.json")), None)
        if not cfg_name:
            problems.append("缺少 config/settings.json")
            print("  ❌ 未找到 config/settings.json")
        else:
            data = json.loads(z.read(cfg_name).decode("utf-8"))
            leaked = []
            for name, cfg in (data.get("providers") or {}).items():
                if isinstance(cfg, dict) and cfg.get("api_key"):
                    leaked.append(name)
            for key in ("prompt_optimizer_api_key", "deepseek_api_key"):
                if data.get(key):
                    leaked.append(key)
            if leaked:
                problems.append(f"settings.json 仍含密钥：{leaked}")
                print(f"  ❌ 仍含密钥：{leaked}")
            else:
                print("  ✅ 所有 api_key 已清空")
            print(f"     服务商条目：{len(data.get('providers') or {})} 个")

        # ③ 文本内容扫密钥
        print("\n【③ 文件内容密钥扫描】")
        scanned = 0
        suspicious: list[str] = []
        for n in names:
            if not n.lower().endswith((".py", ".json", ".md", ".txt", ".ps1", ".js", ".html")):
                continue
            try:
                text = z.read(n).decode("utf-8", errors="ignore")
            except Exception:  # noqa: BLE001
                continue
            scanned += 1
            for pat, label in SECRET_PATTERNS:
                for m in pat.finditer(text):
                    frag = m.group(0)
                    low = frag.lower()
                    # 排除明显的占位/示例
                    if any(x in low for x in ("example", "your", "xxx", "placeholder", "test")):
                        continue
                    # 排除字母表顺序串（abcdefghij / 1234567890 这类测试夹具）
                    body = frag.split("-", 1)[-1]
                    if body[:10].lower() in ("abcdefghij", "0123456789"):
                        continue
                    if "abcdefghijklmnop" in low or "1234567890" in low:
                        continue
                    suspicious.append(f"{n}: {label} → {frag[:24]}…")
        print(f"  已扫描 {scanned} 个文本文件")
        if suspicious:
            # 十六进制长串可能只是哈希，单独提示
            hard = [s for s in suspicious if "长十六进制" not in s]
            soft = [s for s in suspicious if "长十六进制" in s]
            for s in hard:
                problems.append(f"疑似密钥：{s}")
                print(f"  ❌ {s}")
            if soft:
                print(f"  ⚠️  {len(soft)} 处长十六进制串（可能是哈希，需人工确认）")
                for s in soft[:5]:
                    print(f"      {s}")
            if not hard and not soft:
                print("  ✅ 未发现密钥特征")
        else:
            print("  ✅ 未发现密钥特征")

        # ④ 结构确认
        print("\n【④ 关键内容确认】")
        for need, label in [
            ("main.py", "入口"),
            ("app/web/server.py", "服务端"),
            ("app/effect_renderer.py", "效果图渲染器"),
            ("app/effect_background.py", "背景解析（统一入口）"),
            ("data/stores.json", "门店数据"),
            ("AGENTS.md", "协作约定"),
            ("PROJECT_CONTEXT.json", "机器可读约束"),
            ("PORTABLE_README.md", "包说明"),
            ("requirements.txt", "依赖清单"),
        ]:
            ok = any(n.endswith(need) for n in names)
            print(f"  {'✅' if ok else '❌'} {label:<24} {need}")
            if not ok:
                problems.append(f"缺少 {need}")

        # ⑤ 背景库与示例批次
        bg = [n for n in names if "_effect_backgrounds" in n and n.endswith(".png")]
        sample = [n for n in names if n.endswith("_效果图.png")]
        print(f"\n【⑤ 附带资源】")
        print(f"  {'✅' if bg else '⚠️'} AI 背景库：{len(bg)} 张")
        print(f"  {'✅' if sample else '⚠️'} 示例效果图：{len(sample)} 张")

    print("\n" + "=" * 78)
    if problems:
        print(f"❌ 发现 {len(problems)} 个问题：")
        for p in problems:
            print(f"   - {p}")
    else:
        print("✅ 全部检查通过：无密钥、无隐私数据、结构完整")
    print("=" * 78)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
