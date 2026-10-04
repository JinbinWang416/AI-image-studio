# -*- coding: utf-8 -*-
"""打「开发者交接包」：把源码与文档打包，交给别人继续开发。

⚠️ 与 `make_portable_pack.py` 的区别：

   · `make_portable_pack.py` 打的是**给最终用户运行**的包（含示例批次与出图）
   · 本脚本打的是**给开发者/AI 接手**的包 —— 只要源码、文档和部署文件，
     **绝不包含任何密钥、账号凭据或用户数据**

## 排除策略

采用**白名单 + 黑名单双重**：

1. 只收录明确列出的顶层条目（白名单）—— 新增目录不会被悄悄带进去
2. 再对白名单里的每一项做黑名单过滤（`config/settings.json`、`data/security/` 等）
3. 打包后**自检**：逐条扫描包内文本，命中疑似凭据就报错并删除产物

用法：

    .\\.venv\\Scripts\\python.exe tools\\make_handover_pack.py [--root <项目目录>]
"""
from __future__ import annotations

import argparse
import re
import sys
import zipfile
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------- 收录白名单
# 只收这些顶层条目。新增目录需显式加进来 —— 避免不小心把 output/ 之类带出去。
INCLUDE_TOP = [
    "app",              # 源码
    "tools",            # 运维脚本
    "tests",            # 测试
    "docs",             # 设计文档
    "评审",              # 历次评审报告（含已知问题清单）
    "main.py",
    "requirements.txt",
    "requirements.lock.txt",
    "runtime.txt",
    # 文档
    "README.md",
    "AGENTS.md",
    "PROJECT_CONTEXT.json",
    "HANDOVER.md",
    "DEPLOY.md",
    # 部署
    "Dockerfile",
    "docker-compose.yml",
    ".dockerignore",
    "render.yaml",
    ".env.example",
    ".gitignore",
    # 只读资源（应用启动必需：门店定义）
    "data/stores.json",
]

# ---------------------------------------------------------------- 外部补充材料
# 项目目录**之外**的、但交接时必须带上的东西。
# 每项是 (源路径, 包内目标路径)，源路径相对项目根。
EXTERNAL_DIRS: list[tuple[str, str]] = [
    # 历次外部评审报告 —— 接手方最需要的就是「已知问题清单」，
    # 而它按项目约定放在与本项目**平级**的 `评审/` 目录里。
    ("../评审", "评审"),
]


# ---------------------------------------------------------------- 排除黑名单
# 路径片段命中即跳过。**这几条是安全底线，不要为"包完整一点"而放宽。**
EXCLUDE_PARTS = {
    "__pycache__", ".venv", "venv", ".git", ".idea", ".vscode",
    "node_modules", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "desktop", "dist", "build", "delivery", "_portable_stage",
    "output", "logs", "security",
}
EXCLUDE_SUFFIX = {".pyc", ".pyo", ".zip", ".exe", ".spec", ".log", ".tmp"}
EXCLUDE_NAME_PATTERNS = [
    re.compile(r"^settings\.json"),        # 含 API Key（含 .bak-* / .corrupt.*）
    re.compile(r"^\.env$"),                # 真实环境变量（.env.example 保留）
    re.compile(r"\.bak-"),                 # 各种备份
    re.compile(r"^\.backup_key$"),
    re.compile(r"^sessions\.json"),
]


def should_skip(rel: Path) -> bool:
    """是否排除这个相对路径。"""
    if any(part in EXCLUDE_PARTS for part in rel.parts):
        return True
    if rel.suffix.lower() in EXCLUDE_SUFFIX:
        return True
    if any(p.match(rel.name) for p in EXCLUDE_NAME_PATTERNS):
        return True
    return False


def collect(root: Path) -> list[tuple[Path, Path]]:
    """收集要打包的文件，返回 `(磁盘路径, 包内相对路径)` 列表。"""
    picked: list[tuple[Path, Path]] = []

    def add(src: Path, rel: Path) -> None:
        if should_skip(rel):
            return
        # 包内路径只保留相对部分（外部材料的 `..` 不能带进 zip）
        safe_rel = Path(*[p for p in rel.parts if p not in ("..", ".")])
        picked.append((src, safe_rel))

    for name in INCLUDE_TOP:
        target = root / name
        if not target.exists():
            print(f"  [--] 跳过（不存在）: {name}")
            continue
        if target.is_file():
            add(target, Path(name))
            continue
        for p in sorted(target.rglob("*")):
            if p.is_file():
                add(p, p.relative_to(root))

    for src_rel, dst_rel in EXTERNAL_DIRS:
        src_dir = (root / src_rel).resolve()
        if not src_dir.is_dir():
            print(f"  [--] 跳过（不存在）: {src_rel}")
            continue
        n = 0
        for p in sorted(src_dir.rglob("*")):
            if not p.is_file():
                continue
            add(p, Path(dst_rel) / p.relative_to(src_dir))
            n += 1
        print(f"  [++] 外部材料: {dst_rel}/  ({n} 个文件)")

    return picked


# ---------------------------------------------------------------- 打包后自检
SCAN_SUFFIX = {".py", ".js", ".ts", ".json", ".md", ".txt", ".yml", ".yaml",
               ".toml", ".ini", ".cfg", ".html", ".css", ".ps1", ".sh", ".env",
               ".example", ".bat", ""}

# ⚠️ 这个文件本身必然含「凭据样本」（它的 `SELFTEST_SAMPLES` 就是干这个的），
#    扫描时豁免掉，否则每次打包都会被自己拦下。
_SCANNER_SELF = "check_git_secrets.py"


def audit(zip_path: Path) -> list[str]:
    """复用项目自己的密钥扫描器，对包内文本做**分级**审计。

    ⚠️ 为什么不在这里自己写一套正则：第一版就是这么干的，结果把
       `tools/check_git_secrets.py` 里 `SELFTEST_SAMPLES` 的**合成样本**
       当成了真实凭据，直接拦下了打包。

       而 `tools/check_git_secrets.py` 是项目专门维护的扫描器，
       它已经区分了「源码路径里的凭据」(`[!!]`) 与
       「测试夹具 / 合成样本 / 文档示例」(`[?]`) —— 复用它，
       两边的判断标准才不会漂移。

    只把 `[!!]` 级命中当作阻断项；`[?]` 级打印出来供人工确认。
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        import check_git_secrets as scanner
    except ImportError:
        return ["  [!!] 无法导入密钥扫描器，本次审计无效"]

    blocking: list[str] = []
    warnings: list[str] = []
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            if info.is_dir() or info.file_size > 2 * 1024 * 1024:
                continue
            if Path(info.filename).suffix.lower() not in SCAN_SUFFIX:
                continue
            if Path(info.filename).name == _SCANNER_SELF:
                continue
            try:
                text = z.read(info).decode("utf-8", errors="ignore")
            except (OSError, UnicodeDecodeError):
                continue
            for hit in scanner.scan_text(text, info.filename):
                line = "  " + hit.strip()
                if hit.strip().startswith("[!!]"):
                    blocking.append(line)
                elif hit.strip().startswith("[?]"):
                    warnings.append(line)

    if warnings:
        print(f"  以下 {len(warnings)} 处为测试夹具/示例（不阻断，请人工确认）：")
        for w in warnings[:15]:
            print("  " + w.strip()[:130])
        print()
    return blocking


def main() -> int:
    ap = argparse.ArgumentParser(description="打开发者交接包")
    ap.add_argument("--root", default=None, help="项目根目录（默认脚本上一级）")
    ap.add_argument("--out", default=None, help="输出 zip 路径")
    args = ap.parse_args()

    root = Path(args.root).resolve() if args.root else Path(__file__).resolve().parent.parent
    stamp = datetime.now().strftime("%Y%m%d")
    out = Path(args.out) if args.out else root / "delivery" / f"AI生图_开发者交接包_{stamp}.zip"
    out.parent.mkdir(parents=True, exist_ok=True)

    print("=" * 74)
    print("打开发者交接包（源码 + 文档，不含密钥与用户数据）")
    print("=" * 74)
    print(f"  源目录: {root}")
    print(f"  输出  : {out}")
    print()

    files = collect(root)
    if not files:
        print("  [!!] 没收集到任何文件，终止")
        return 2

    print(f"  收集 {len(files)} 个文件，开始压缩…")
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for src, rel in files:
            z.write(src, rel.as_posix())

    size_mb = out.stat().st_size / 1024 / 1024
    print(f"  压缩完成：{size_mb:.1f} MB")
    print()

    print("=" * 74)
    print("安全自检（扫描包内是否混入凭据）")
    print("=" * 74)
    findings = audit(out)
    if findings:
        print("\n".join(findings[:40]))
        print(f"\n  [!!] 发现 {len(findings)} 处疑似凭据 —— 已删除产物，请检查后重试")
        out.unlink(missing_ok=True)
        return 1
    print("  [OK] 未发现疑似凭据 ✅")

    # 额外断言：几个绝不该出现的路径
    #
    # ⚠️ 用「精确匹配文件名」而不是 `startswith(".env")` —— 后者会把
    #    `.env.example`（应该分发，里面只有占位说明）也误判成泄露。
    with zipfile.ZipFile(out) as z:
        names = z.namelist()
    forbidden_names = {".env", "settings.json", "sessions.json", ".backup_key"}
    forbidden_prefixes = ("config/settings.json", "data/security/", "output/", "logs/")
    leaked = [
        n for n in names
        if any(n.startswith(p) for p in forbidden_prefixes)
        or Path(n).name in forbidden_names
    ]
    if leaked:
        print(f"  [!!] 包内出现禁止路径：{leaked[:5]}")
        out.unlink(missing_ok=True)
        return 1
    print("  [OK] 未包含 settings.json / data/security / .env / output / logs ✅")

    print()
    print("=" * 74)
    print(f"交接包：{out}")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    sys.exit(main())
