# -*- coding: utf-8 -*-
"""
代码冗余自检：找出未使用的导入、疑似废弃的函数、遗留的 TODO。

用法：
    .\\.venv\\Scripts\\python.exe tools\\selfcheck_code.py
"""
from __future__ import annotations

import ast
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
APP = ROOT / "app"
TESTS = ROOT / "tests"


def scan_unused_imports(path: pathlib.Path) -> list[str]:
    """用 AST 找出导入但未在源码其它位置出现的名字。"""
    src = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(src)
    except SyntaxError as exc:
        return [f"语法错误：{exc}"]

    imported: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                name = (a.asname or a.name).split(".")[0]
                imported.append((name, node.lineno))
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                if a.name == "*":
                    continue
                imported.append((a.asname or a.name, node.lineno))

    # 去掉导入行本身后，在正文里找名字
    lines = src.splitlines()
    body = "\n".join(
        line for i, line in enumerate(lines, 1)
        if not any(i == ln for _, ln in imported)
    )
    out = []
    for name, lineno in imported:
        if name in ("annotations",):
            continue
        if not re.search(rf"\b{re.escape(name)}\b", body):
            out.append(f"第 {lineno} 行：导入 `{name}` 未被使用")
    return out


def scan_unused_functions(path: pathlib.Path, all_src: str) -> list[str]:
    """找出在本文件与整个 app/ 中都没有被引用的模块级函数。"""
    src = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    out = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        name = node.name
        if name.startswith("__"):
            continue
        # 在本文件与全项目其它文件中的引用次数（排除定义行）
        hits = len(re.findall(rf"\b{re.escape(name)}\b", all_src))
        if hits <= 1:
            out.append(f"第 {node.lineno} 行：函数 `{name}()` 无任何引用")
    return out


def main() -> int:
    files = sorted(APP.rglob("*.py")) + sorted(TESTS.rglob("*.py"))
    all_src = "\n".join(
        p.read_text(encoding="utf-8") for p in sorted(APP.rglob("*.py"))
    )

    print("=" * 78)
    print("代码冗余自检")
    print("=" * 78)

    total = 0
    for p in files:
        issues = scan_unused_imports(p)
        if p.parent == APP or APP in p.parents:
            issues += scan_unused_functions(p, all_src)
        if issues:
            total += len(issues)
            print(f"\n📄 {p.relative_to(ROOT)}")
            for i in issues:
                print(f"    {i}")

    # TODO / FIXME / 调试残留
    print("\n" + "-" * 78)
    print("调试残留扫描")
    print("-" * 78)
    leftovers = []
    for p in files:
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"\b(TODO|FIXME|XXX|HACK|breakpoint\(\)|pdb\.set_trace)\b", line):
                leftovers.append(f"{p.relative_to(ROOT)}:{i}  {line.strip()[:90]}")
    if leftovers:
        for x in leftovers:
            print(f"  {x}")
        total += len(leftovers)
    else:
        print("  ✅ 无 TODO / FIXME / 调试断点残留")

    print("\n" + "=" * 78)
    print(f"共发现 {total} 处待处理")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
