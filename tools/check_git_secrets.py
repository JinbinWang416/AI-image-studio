# -*- coding: utf-8 -*-
"""扫描 git **已跟踪**的文件，找出任何非空密钥字面量。

⚠️ 只输出「文件名:行号 字段名 长度」，**绝不打印密钥内容**。
⚠️ 用法（两段式，避免 Python 直接调 git 时的编码/沙箱问题）：

    git ls-files > %TEMP%\\tracked.txt
    python tools/check_git_secrets.py %TEMP%\\tracked.txt

    # 也可顺带扫历史里出现过的所有 blob：
    git rev-list --objects --all | git cat-file --batch-check="%(objectname) %(rest)" > %TEMP%\\allobj.txt
    python tools/check_git_secrets.py %TEMP%\\allobj.txt

退出码：发现疑似真实密钥返回 1（可直接串进推送前的 gate）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# 值长度 >= 16 才算「像真的」；测试里的 sk-abcdef 之类会被长度过滤掉
PATTERN = re.compile(
    r'["\']?(api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|password)["\']?'
    r'\s*[:=]\s*'
    r'["\']([^"\']{16,})["\']',
    re.IGNORECASE,
)

# 这些明显是占位/示例，不算泄露
PLACEHOLDER = re.compile(
    r"^(sk-)?(x{4,}|a{8,}|\*{4,}|your|test|demo|example|placeholder|changeme|todo)",
    re.IGNORECASE,
)

TEXT_EXT = {
    ".py", ".js", ".ts", ".json", ".md", ".txt", ".yml", ".yaml",
    ".toml", ".ini", ".cfg", ".html", ".css", ".ps1", ".sh", ".env", ".bat",
}
MAX_BYTES = 2 * 1024 * 1024


def looks_real(value: str) -> bool:
    if PLACEHOLDER.match(value):
        return False
    if set(value) <= {"*", "x", "X", "0"}:     # 全是掩码字符
        return False
    # 至少要有一定字符多样性，纯重复串不算
    return len(set(value)) >= 8


def is_text_candidate(p: Path) -> bool:
    """判断是否值得当文本扫。

    ⚠️ 不能只看 `p.suffix in TEXT_EXT` —— 备份文件的扩展名是 `.bak-printreset`
       这种复合形式（`Path("settings.json.bak-printreset").suffix` 得到
       `.bak-printreset`），会被白名单静默跳过。而**恰恰是这类备份文件**
       最容易带着真实 Key 混进仓库（实测就是它泄到了 GitHub）。
    """
    name = p.name.lower()
    if p.suffix.lower() in TEXT_EXT:
        return True
    # 名字里带这些标记的，一律按文本处理
    return any(mark in name for mark in (".json", ".env", ".bak", ".yaml", ".yml", ".ini", ".cfg", ".txt", ".py"))


def _names_from_git(all_objects: bool) -> list[str]:
    """直接调 git 取文件名清单。

    ⚠️ 不要在 PowerShell 里用 `git ls-files > file.txt` 再交给 Python ——
       PS 5.1 的 `>` 默认写 **UTF-16LE**，Python 按 UTF-8 读出来每两字节夹一个
       `\\x00`，417 个路径会全部「不存在」，扫描静默变成 0 个文件（结论完全无效，
       实测踩到）。要落盘也必须 `Out-File -Encoding utf8` 或走本函数这条路。
    """
    import subprocess

    cmd = ["git", "-c", "core.quotepath=false", "ls-files"]
    if all_objects:
        cmd = ["git", "-c", "core.quotepath=false", "rev-list", "--objects", "--all"]
    raw = subprocess.run(cmd, capture_output=True).stdout
    lines = raw.decode("utf-8", errors="replace").splitlines()
    if all_objects:
        # "sha path" → 取 path；没有空格的（tree/commit）跳过
        lines = [ln.split(" ", 1)[1] for ln in lines if " " in ln]
    seen: dict[str, None] = {}
    for ln in lines:
        ln = ln.strip()
        if ln:
            seen[ln] = None
    return list(seen)


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2

    arg = sys.argv[1]
    if arg in ("--git", "--git-all"):
        names = _names_from_git(all_objects=arg == "--git-all")
        origin = "git 已跟踪文件" if arg == "--git" else "git 全部历史对象"
    else:
        listing = Path(arg)
        names = [ln.strip() for ln in listing.read_text(encoding="utf-8-sig", errors="ignore").splitlines() if ln.strip()]
        origin = f"清单文件 {listing.name}"

    hits: list[str] = []
    scanned = 0
    missing = 0

    for rel in names:
        p = Path(rel)
        if not p.is_file():
            missing += 1
            continue
        if not is_text_candidate(p):
            continue
        try:
            if p.stat().st_size > MAX_BYTES:
                continue
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        scanned += 1
        for m in PATTERN.finditer(text):
            if looks_real(m.group(2)):
                line_no = text[: m.start()].count("\n") + 1
                hits.append(f"  [!!] {rel}:{line_no}  {m.group(1)}  长度={len(m.group(2))}")

    print(f"[{origin}] 条目 {len(names)} 个，实际扫描文本 {scanned} 个（跳过 {missing} 个不存在）：")
    if scanned == 0:
        print("  [!!] 一个文件都没扫到 —— 清单或编码有问题，本次结论无效，别当成「干净」")
        return 2
    if hits:
        print("\n".join(hits))
        print(f"\n[!!] 发现 {len(hits)} 处疑似真实密钥 —— 绝不能推送")
        return 1
    print("  [OK] 未发现真实密钥字面量")
    return 0


if __name__ == "__main__":
    sys.exit(main())
