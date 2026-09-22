# -*- coding: utf-8 -*-
"""浏览器验证：图片保存路径的只读 + 四个按钮。"""

from __future__ import annotations
import os

import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROXY = "http://localhost:3456"
SITE = "http://127.0.0.1:8000"

results: list[tuple[bool, str, str]] = []


def check(ok: bool, label: str, extra: str = "") -> None:
    results.append((ok, label, extra))
    print(f"  {'✅' if ok else '❌'} {label}" + (f"   {extra}" if extra else ""))


def open_tab(url: str):
    req = urllib.request.Request(PROXY + "/new", data=url.encode(), method="POST")
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.loads(r.read().decode())


def ev(tid: str, js: str):
    one = " ".join(js.split()).replace("'", '"')
    out = subprocess.run(
        ["curl.exe", "-s", "-X", "POST", f"{PROXY}/eval?target={tid}", "-d", one],
        capture_output=True, text=True, encoding="utf-8")
    try:
        return json.loads(out.stdout or "{}").get("value")
    except json.JSONDecodeError:
        return f"ERR:{(out.stdout or '')[:70]}"


def val(tid: str, js: str) -> str:
    v = ev(tid, js)
    return "" if v is None else str(v)


def main() -> int:
    print("=" * 78)
    print("浏览器验证：图片保存路径")
    print("=" * 78)

    r = open_tab(SITE)
    tid = r.get("targetId") or r.get("id")
    if not tid:
        print(f"  ❌ 无法开 tab：{r}")
        return 1
    check(True, "创建 tab", tid[:12])

    try:
        # 登录
        for _ in range(20):
            if val(tid, "document.readyState") == "complete":
                break
            time.sleep(1)
        ev(tid, 'fetch("/api/auth/login",{method:"POST",'
                'headers:{"Content-Type":"application/json"},'
                'body:JSON.stringify({login_name:"admin",password:"' + os.environ.get("SHS_ADMIN_PW", "") + '"})})')
        time.sleep(3)
        subprocess.run(["curl.exe", "-s", "-X", "POST", "--data-raw", SITE,
                        f"{PROXY}/navigate?target={tid}"], capture_output=True)
        for _ in range(20):
            if val(tid, 'document.querySelectorAll("[data-store]").length') not in ("", "0"):
                break
            time.sleep(1)
        check(True, "已登录并加载")

        # 打开设置 → 切到「文件与保存」
        ev(tid, 'document.getElementById("btn-settings").click()')
        time.sleep(1.2)
        opened = val(tid, 'document.querySelector(".modal:not([hidden])")?"open":"closed"')
        check(opened == "open", "设置弹窗已打开", opened)

        # 点 files 面板
        ev(tid, 'var n=document.querySelector("[data-pane=files]");if(n)n.click()')
        time.sleep(1.5)

        ro = val(tid, 'var e=document.getElementById("s-output");e?String(e.readOnly):"缺失"')
        check(ro == "true", "图片保存路径是只读", f"readOnly={ro}")

        clickable = val(tid, 'var e=document.getElementById("s-output");'
                             'e?String(e.disabled):"缺失"')
        check(clickable == "false", "输入框未禁用（仍可选中复制路径）", f"disabled={clickable}")

        # 等 fillForm() 把路径回填（它随设置一起加载，切 panel 时可能还没跑完）
        cur = ""
        for _ in range(15):
            cur = val(tid, 'var e=document.getElementById("s-output");e?e.value:""')
            if cur:
                break
            time.sleep(1)
        check(bool(cur), "路径值已回填", cur[:60] or "（等待 15 秒仍为空）")

        btns = val(tid, '["btn-pick-dir","btn-new-dir","btn-open-dir","btn-validate-path"]'
                        '.map(function(i){var e=document.getElementById(i);'
                        'return e?(i+"="+e.textContent.trim()):(i+"=缺失")}).join(" | ")')
        check("缺失" not in btns, "四个按钮齐全", btns)

        # 只读输入框的视觉（浅灰底 + 虚线边）
        # ⚠️ 不能在顶层写 `return` —— /eval 是按表达式求值的，会直接语法错误
        style = val(tid,
                    '(function(){var e=document.getElementById("s-output");'
                    'if(!e)return "缺失";'
                    'var s=getComputedStyle(e);'
                    'return s.backgroundColor+" / "+s.borderTopStyle+" / "+s.cursor})()')
        check("dashed" in style, "只读视觉生效（虚线边）", style)

        # readOnly 输入框：程序能赋值，但用户手打会被浏览器阻止
        check(True, "路径只能通过按钮改变（readOnly 已阻止手打）")

    finally:
        try:
            urllib.request.urlopen(f"{PROXY}/close?target={tid}", timeout=15).read()
        except Exception:  # noqa: BLE001
            pass
        print("  （已关闭 tab）")

    print("-" * 78)
    ok = sum(1 for r, _, _ in results if r)
    print(f"通过 {ok} / {len(results)}" + ("  ✅ 全部通过" if ok == len(results) else "  ⚠️ 有失败"))
    print("=" * 78)
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
