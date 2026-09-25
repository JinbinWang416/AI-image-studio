# -*- coding: utf-8 -*-
"""密钥扫描门禁：检查 git **对象内容**（含历史、含已删除文件）里的疑似凭据。

⚠️ 只输出「位置 + 字段名 + 值长度」，**绝不打印值本身**。

## 为什么重写了

上一版有两个致命缺陷，导致它**报不出真实泄露**：

1. **正则太窄**：字段名白名单只有 `api_key|access_token|password|...`，
   而且要求值**长度 ≥ 16**。而真实的管理员密码叫 `PW` / `ADMIN_PW`、只有 8 位 ——
   全部被静默过滤。（当时 13 处硬编码管理员密码，它一处都没报出来。）
2. **`--git-all` 是假的**：它只从 `git rev-list --objects` 里取**路径**，
   然后去读**当前工作树**的文件。已删除的文件、只在历史里存在的版本，
   压根没被检查过。

现在改为：用 `git cat-file --batch` **流式读取真实 blob 内容**，逐字节扫描。

## 用法

    # 当前已跟踪文件
    python tools/check_git_secrets.py --git

    # 全部历史对象（含已删除文件），这是发布门禁该用的模式
    python tools/check_git_secrets.py --git-all

    # 自检：用合成样本验证检出率（不含任何真实凭据）
    python tools/check_git_secrets.py --selftest

退出码：0 = 干净，1 = 发现疑似凭据，2 = 本次结论无效（别当成「干净」）。
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

# ⚠️ Windows 默认控制台是 GBK，本文件的 `print("  [OK ] …")` 之类会抛
#    UnicodeEncodeError 并以退出码 1 结束 —— 看起来像「扫描失败」，
#    实际只是打印失败。（第一次修 P2-03 时只改了 run_selfcheck.py，
#     忘了这个新写的工具，导致默认终端下测试挂 1 项。）
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _console import enable_safe_output  # noqa: E402

enable_safe_output()

# ---------------------------------------------------------------- 凭据识别
# ⚠️ 字段名必须覆盖**缩写**：真实项目里管理员密码就叫 `PW` / `ADMIN_PW`。
#    早先只认 `password` 全称，于是 13 处硬编码一个都没报出来。
_CRED_WORD = (
    r"api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|"
    r"secret|token|credential|"
    r"password|passwd|pwd|pass|pw"
)

# ① 赋值 / 键值：  PW = "值"   ADMIN_PW = "值"   "password": "值"   password:"值"
# ⚠️ `field` 必须把**前导标识符**一起吃掉（`ADMIN_PW` / `FIXTURE_PW` / `TMP_PW`）——
#    早先只捕获到光秃秃的 `PW`，既看不懂变量全名，也无法据此判断它是不是测试夹具。
_KV = re.compile(
    rf"""(?ix)
    (?P<field>["']?[A-Za-z_]*?(?:{_CRED_WORD})["']?)     # 字段名（含前缀，引号可有可无）
    \s*[:=]\s*
    (?P<q>["'])                               # 值的引号
    (?P<val>[^"'\n]{{4,200}})                 # 值：**长度下限降到 4**
    (?P=q)
    """
)

# ② argparse 默认值：ap.add_argument("--password", default="值")
_ARG_DEFAULT = re.compile(
    rf"""(?ix)
    add_argument\([^)]*?(?:{_CRED_WORD})[^)]*?
    default\s*=\s*
    (?P<q>["'])(?P<val>[^"'\n]{{4,200}})(?P=q)
    """
)

# 明显是占位 / 掩码 / 标记的值，不算泄露
_PLACEHOLDER = re.compile(
    r"""(?ix)^(
        sk-?x{4,}|x{4,}|a{8,}|\*{2,}|\.{3,}|_{3,}|-{3,}|
        your|test|demo|example|sample|placeholder|changeme|todo|fixme|
        none|null|true|false|changeme
    )"""
)
# 纯符号/单字符（`PASS = "✅"` 这种进度标记常量）
_MARKER = re.compile(r"^[^\w]{0,3}$")

# 变量名里带这些词的，基本可判定为测试夹具（结构化识别，不靠人肉看）
_FIXTURE_HINT = re.compile(r"(?i)(fixture|dummy|sample|fake|stub|test|tmp|temp|probe|example)")


def looks_real(value: str) -> bool:
    """判断一个字面量是否「像真实凭据」而非占位符/标记。"""
    if len(value) < 4:
        return False
    if _PLACEHOLDER.match(value) or _MARKER.match(value):
        return False
    if set(value) <= set("*xX0. "):            # 全是掩码字符
        return False
    # 至少要有一定字符多样性；纯重复串（aaaaaa）不算
    return len(set(value)) >= 3


def _severity(path: str, field: str = "") -> tuple[str, str]:
    """按路径与变量名给命中分级。

    测试目录里的字面量、以及 `FIXTURE_PW` / `TMP_PW` 这类**按命名就能看出是夹具**
    的变量，都不该和 `app/` 里的真凭据混在一起报 —— 否则每次跑都一片红，
    人就会开始无视这个工具。分级后真实泄露才显眼。
    """
    p = path.replace("\\", "/").lower()
    if _FIXTURE_HINT.search(field or ""):
        return "[?] ", "（变量名含 fixture/tmp/test 之类，多为测试夹具）"
    if "/tests/" in p or p.startswith("tests/") or "_test." in p or p.endswith("_test.py"):
        return "[?] ", "（测试目录，多为夹具，请确认是否匹配真实账号）"
    if p.endswith((".md", ".txt", ".json", ".mjs")):
        return "[?] ", "（文档/脚本，请确认是否示例）"
    return "[!!]", ""


def scan_text(text: str, where: str, *, is_test_path: bool = False) -> list[str]:
    """扫一段文本，返回命中描述（不含值本身）。"""
    hits: list[str] = []
    seen: set[tuple[int, str]] = set()

    def add(line_no: int, field: str, val: str) -> None:
        key = (line_no, field.lower())
        if key in seen:
            return
        seen.add(key)
        tag, note = ("[?] ", "") if is_test_path else _severity(where, field)
        hits.append(f"  {tag} {where}:{line_no}  字段={field}  长度={len(val)}{note}")

    for m in _KV.finditer(text):
        val = m.group("val")
        if looks_real(val):
            add(text[: m.start()].count("\n") + 1, m.group("field").strip("\"'"), val)

    for m in _ARG_DEFAULT.finditer(text):
        val = m.group("val")
        if looks_real(val):
            add(text[: m.start()].count("\n") + 1, "add_argument", val)

    return hits


# ---------------------------------------------------------------- git 读取
def _git(*args: str) -> bytes:
    return subprocess.run(
        ["git", "-c", "core.quotepath=false", *args], capture_output=True
    ).stdout


def iter_tracked() -> list[tuple[str, str]]:
    """当前已跟踪文件 → [(名称, 文本内容)]。"""
    out: list[tuple[str, str]] = []
    for raw in _git("ls-files", "-z").split(b"\0"):
        rel = raw.decode("utf-8", errors="replace").strip()
        if not rel:
            continue
        data = _git("show", f"HEAD:{rel}")
        if not data:
            continue
        out.append((rel, data.decode("utf-8", errors="ignore")))
    return out


def iter_all_blobs() -> list[tuple[str, str]]:
    """**全部历史对象**里的 blob → [(sha:路径, 文本内容)]。

    ⚠️ 两个必须守住的点（都实测踩过）：

    **① 必须读 blob 内容，不能读工作树。**
       早先的实现只从 `rev-list --objects` 取**路径**，再去读当前工作树的文件 ——
       已删除的文件、只在历史里存在的版本压根没被检查，「历史扫描」名不副实。

    **② 只能把 blob 的 SHA 送进 `--batch`。**
       `git cat-file --batch` 的响应流里，非 blob 对象（tree / commit）的
       **内容照样占字节**。如果对它们直接 `continue` 而不前移游标，后续解析
       全部错位 —— 实测应读 379 个 blob 只返回 311 个，而**漏掉的 68 个里
       恰好包含全部 15 个含旧密码的对象**，让这个门禁变成「永远绿灯」。
       所以先用 `--batch-check` 拿类型，**只把 blob 挑出来**再读内容。
    """
    listing = _git("rev-list", "--objects", "--all")
    entries: list[tuple[str, str]] = []
    for line in listing.decode("utf-8", errors="replace").splitlines():
        parts = line.split(" ", 1)
        if len(parts) == 2:
            entries.append((parts[0], parts[1]))
    if not entries:
        return []

    # ---- 第一趟：--batch-check 拿类型，只留 blob ----
    chk = subprocess.run(
        ["git", "cat-file", "--batch-check"],
        input="".join(sha + "\n" for sha, _ in entries).encode(),
        capture_output=True,
    )
    kinds: dict[str, str] = {}
    for line in chk.stdout.decode("utf-8", errors="replace").splitlines():
        seg = line.split()
        if len(seg) >= 2:
            kinds[seg[0]] = seg[1]

    blobs = [(sha, path) for sha, path in entries if kinds.get(sha) == "blob"]
    if not blobs:
        return []

    # ---- 第二趟：只喂 blob，严格按 size 前移游标 ----
    proc = subprocess.run(
        ["git", "cat-file", "--batch"],
        input="".join(sha + "\n" for sha, _ in blobs).encode(),
        capture_output=True,
    )
    out: list[tuple[str, str]] = []
    buf = proc.stdout
    pos = 0
    for sha, path in blobs:
        nl = buf.find(b"\n", pos)
        if nl < 0:
            break
        seg = buf[pos:nl].decode("utf-8", errors="replace").split()
        pos = nl + 1
        if len(seg) != 3:
            # 走到这里说明上游给了非 blob，理论上不会发生
            continue
        try:
            size = int(seg[2])
        except ValueError:
            continue
        content = buf[pos: pos + size]
        pos += size + 1                  # 跳过内容 + 尾随换行
        if size > 4 * 1024 * 1024:       # 超大二进制不扫
            continue
        out.append((f"{sha[:8]}:{path}", content.decode("utf-8", errors="ignore")))
    return out


# ---------------------------------------------------------------- 自检
SELFTEST_SAMPLES = [
    # (样本, 是否应当报出)
    ('PW = "Str0ng!Passw0rd"', True),                       # 缩写字段名 + 8~15 位
    ('ADMIN_PW = "abcdefgh"', True),                        # 8 位短密码（旧版会漏）
    ('{"login":"admin","password":"hunter2xyz"}', True),    # JSON
    ('password:"noquote12345"', True),                      # 键无引号
    ('ap.add_argument("--password", default="secret123")', True),   # argparse
    ('api_key = "sk-1234567890abcdef"', True),
    ('PASS = "✅"', False),                                 # 进度标记常量
    ('PASSWORD = ""', False),                               # 空串
    ('password = "***"', False),                            # 掩码
    ('api_key = "your-api-key-here"', False),               # 占位
    ('pwd = "xxx"', False),                                 # 占位
]


def selftest() -> int:
    print("=" * 74)
    print("自检：合成样本检出率（不含任何真实凭据）")
    print("=" * 74)
    ok = True
    for sample, should_hit in SELFTEST_SAMPLES:
        hits = scan_text(sample, "sample")
        got = bool(hits)
        mark = "OK " if got == should_hit else "FAIL"
        if got != should_hit:
            ok = False
        expect = "应报出" if should_hit else "应放过"
        print(f"  [{mark}] {expect}  {sample[:56]}")
    print("=" * 74)
    print("  全部符合预期 ✅" if ok else "  有不符合预期的样本 ❌")
    return 0 if ok else 1


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    arg = sys.argv[1]

    if arg == "--selftest":
        return selftest()

    if arg == "--git":
        items = iter_tracked()
        origin = "git 已跟踪文件（读对象内容）"
    elif arg == "--git-all":
        items = iter_all_blobs()
        origin = "git 全部历史 blob（含已删除文件）"
    else:
        print(__doc__)
        return 2

    hits: list[str] = []
    for where, text in items:
        hits.extend(scan_text(text, where))

    print(f"[{origin}] 扫描 {len(items)} 个对象：")
    if not items:
        print("  [!!] 一个对象都没读到 —— 本次结论无效，别当成「干净」")
        return 2
    if hits:
        print("\n".join(hits))
        print(f"\n[!!] 发现 {len(hits)} 处疑似凭据 —— 绝不能推送")
        print("     （提示：测试夹具也会被列出，请人工确认是否匹配真实账号）")
        return 1
    print("  [OK] 未发现疑似凭据")
    return 0


if __name__ == "__main__":
    sys.exit(main())
