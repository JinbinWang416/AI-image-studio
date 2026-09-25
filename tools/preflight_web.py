# -*- coding: utf-8 -*-
"""上线前网页功能检查（CDP）。

覆盖 AGENTS.md「本地验证」里点名的项：
    设置双栏分类 · 范围选择和计数 · 比例显示 · 生成图/效果图切换 ·
    实拍玻璃背景上传 · 真实感等级 · 继续当前批次和新批次

⚠️ 两个 CDP 使用要点（都实测踩过）：
    ① /eval 必须传**单行** JS，多行会静默返回空
    ② 从 Python 用 subprocess 调 curl 时，**JS 里只能用双引号** ——
       Windows 命令行不认单引号，`'x'` 会被解析坏掉（PowerShell 直接调用时
       是 shell 在处理引号，所以手动测同一个表达式却是好的）

用法：
    .\\.venv\\Scripts\\python.exe tools\\preflight_web.py
"""

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


def api(method: str, path: str, body=None):
    """调用 CDP proxy 的 JSON 接口（/targets、/close 等）。"""
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    req = urllib.request.Request(
        PROXY + path, data=data, method=method,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read().decode("utf-8", "replace")
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return {"raw": raw}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


def open_tab(url: str):
    """开新 tab。

    ⚠️ `/new` 的 POST body 是**裸 URL**，不是 JSON ——
       早先写成 `api("POST", "/new", SITE)` 会被 json.dumps 加上引号，
       body 变成 `"http://..."`，浏览器导航到空白页（表现为 id 数 = 0）。
    """
    req = urllib.request.Request(PROXY + "/new", data=url.encode(), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


def close_tab(tid: str) -> None:
    try:
        urllib.request.urlopen(f"{PROXY}/close?target={tid}", timeout=20).read()
    except Exception:  # noqa: BLE001
        pass


def ev(tid: str, js: str):
    """在页面里执行 JS（单行）。

    三个坑（都实测踩过）：
      ① **必须单行** —— /eval 只接受单行表达式，多行会静默返回空
      ② **JS 里用双引号**，不要单引号 —— Windows 命令行不认单引号
      ③ **避免 `&&` / `||` / `|` 裸用** —— 它们是 cmd 的元字符，
         经 subprocess 传参时容易被解析坏（`&&` 尤其危险）。
         需要布尔组合时，改用 `Boolean(...)` 或分多次检查。
    """
    one_line = " ".join(js.split())
    if "'" in one_line:
        # 尽力补救：单引号包字符串的场景换成双引号
        one_line = one_line.replace("'", '"')
    out = subprocess.run(
        ["curl.exe", "-s", "-X", "POST", f"{PROXY}/eval?target={tid}", "-d", one_line],
        capture_output=True, text=True, encoding="utf-8")
    try:
        return json.loads(out.stdout or "{}")
    except json.JSONDecodeError:
        return {"error": (out.stdout or "")[:200]}


def val(tid: str, js: str, default: str = "") -> str:
    r = ev(tid, js)
    v = r.get("value", r.get("error", default))
    return str(v) if v is not None else default


def wait_for(tid: str, js: str, *, timeout: float = 20.0, interval: float = 1.0) -> bool:
    """轮询等待 JS 返回真值。

    ⚠️ 不要用「固定 sleep N 秒」赌页面就绪 ——
       `/new` 建 tab 后前端脚本（app.js / topbar.js）还要跑一会儿，
       固定 4 秒实测不够，会得到"所有元素都缺失"的假失败。
       按 skill 的要求：在观察窗口内持续检查目标内容。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = ev(tid, js)
        if r.get("value") is True or r.get("value") == "true":
            return True
        time.sleep(interval)
    return False


def main() -> int:
    print("=" * 78)
    print("上线前网页功能检查（CDP）")
    print("=" * 78)

    deps = subprocess.run(
        ["node", str(Path.home() / ".dsh/skills/web-access/scripts/check-deps.mjs")],
        capture_output=True, text=True, encoding="utf-8")
    if deps.returncode != 0:
        print(f"  ❌ CDP 不可用：{(deps.stdout or deps.stderr)[:200]}")
        return 1
    check(True, "CDP 就绪")

    r = open_tab(SITE)
    tid = r.get("targetId") or r.get("id") or ""
    if not tid:
        print(f"  ❌ 无法创建 tab：{r}")
        return 1
    check(True, "创建 tab", tid[:12])

    try:
        # ---- 先登录：不登录的话主界面没有数据，很多检查会假失败 ----
        ok_ready = False
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            if val(tid, "document.readyState") == "complete":
                ok_ready = True
                break
            time.sleep(1)
        check(ok_ready, "页面加载完成")

        ev(tid, 'fetch("/api/auth/login",{method:"POST",'
                'headers:{"Content-Type":"application/json"},'
                'body:JSON.stringify({login_name:"admin",password:os.environ.get("SHS_ADMIN_PW", "")})})'
                '.then(function(r){window.__login=r.status})')
        time.sleep(3)
        login_st = val(tid, "window.__login")
        check(login_st == "200", "登录成功", f"HTTP {login_st}")

        # 带 cookie 重新加载，让 app.js 渲染出业务数据
        subprocess.run(["curl.exe", "-s", "-X", "POST", "--data-raw", SITE,
                        f"{PROXY}/navigate?target={tid}"], capture_output=True)
        ready = False
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            n = val(tid, 'document.querySelectorAll("[data-store]").length')
            if n.isdigit() and int(n) > 0:
                ready = True
                break
            time.sleep(1)
        check(ready, "业务数据已渲染（门店列表出现）")
        if not ready:
            st = val(tid, "document.readyState")
            n = val(tid, 'document.querySelectorAll("[id]").length')
            check(False, "页面未就绪，后续检查无意义", f"readyState={st} id数={n}")
            return 1

        # ---- 顶栏（本轮刚改）----
        top = val(tid, '["btn-settings","btn-system","acct-divider","btn-home","btn-user"]'
                       '.map(function(i){var e=document.getElementById(i);'
                       'return e?(e.textContent.trim()||i):(i+":缺失")}).join(" | ")')
        check("缺失" not in top, "顶栏五个入口齐全", top)
        if "设置" in top and "系统管理" in top:
            check(top.index("设置") < top.index("系统管理"), "「设置」在「系统管理」前")
        else:
            check(False, "「设置」在「系统管理」前", "文案未取到")

        # ---- 设置弹窗 ----
        ev(tid, 'document.getElementById("btn-settings").click()')
        time.sleep(1.5)
        opened = val(tid, 'document.querySelector(".modal:not([hidden])")?"open":"closed"')
        check(opened == "open", "设置弹窗可打开", opened)

        panes = val(tid, 'Array.from(document.querySelectorAll("[data-pane]"))'
                         '.map(function(e){return e.dataset.pane}).join(",")')
        pane_list = [p for p in panes.split(",") if p]
        check(len(pane_list) >= 8, "设置面板分类齐全", f"{len(pane_list)} 个")
        for need in ("model", "local", "generation", "quality", "safety"):
            check(need in pane_list, f"含面板 {need}")

        size = val(tid, 'var e=document.getElementById("s-size");e?e.value:"缺失"')
        check(size not in ("", "缺失"), "比例（尺寸）显示", size)

        realism = val(tid, 'var e=document.getElementById("pq-realism");e?e.value:"缺失"')
        check(realism not in ("", "缺失"), "真实感等级可读", realism)

        up = val(tid, 'var e=document.getElementById("effect-background-upload");'
                      'e?e.type:"缺失"')
        check(up != "缺失", "玻璃背景上传控件存在", up)

        ev(tid, 'var b=document.getElementById("btn-close-settings");if(b)b.click()')
        time.sleep(1)

        # ---- 主界面 ----
        # 视图切换是 id="image-view-generated" / "image-view-effect"（不是 data-view 属性）
        gen_btn = val(tid, 'var e=document.getElementById("image-view-generated");'
                           'e?e.textContent.trim():"缺失"')
        eff_btn = val(tid, 'var e=document.getElementById("image-view-effect");'
                           'e?e.textContent.trim():"缺失"')
        check(gen_btn != "缺失" and eff_btn != "缺失",
              "生成图/效果图切换按钮", f"{gen_btn} | {eff_btn}")

        btns = val(tid, '["btn-run","btn-new-batch","btn-stop"].map(function(i){'
                        'var e=document.getElementById(i);'
                        'return e?(i+"="+(e.textContent.trim()||"ok")):(i+"=缺失")}).join(" | ")')
        check("缺失" not in btns, "批次操作按钮齐全", btns)

        scope = val(tid, 'var n=document.querySelectorAll("[data-store]").length;'
                         'var c=document.getElementById("counts");'
                         '"门店项="+n+" 计数="+(c?c.textContent.trim():"缺失")')
        check("门店项=" in scope and "门店项=0" not in scope, "范围选择与计数渲染", scope)

        # 图片检查：**只统计可见的**，且与计数保持一致性 ——
        #   · 隐藏容器里的 <img>（未展开的 local-validation 面板）不加载，
        #     naturalWidth 恒为 0，拿它判断会假失败
        #   · 当前批次若是 0/6（还没生成），本来就没有图，也不该算失败
        img = val(tid,
                  'var a=Array.from(document.querySelectorAll("img"))'
                  '.filter(function(e){return e.offsetParent!==null});'
                  'var c=document.getElementById("counts");'
                  'var done=parseInt((c?c.textContent:"0").split("/")[0].trim())||0;'
                  '"完成数="+done+" 可见图="+a.length'
                  '+(a.length?(" 已加载="+a.filter(function(e){return e.naturalWidth>0}).length):"")')
        # 一致性：有已完成图片就应该渲染出图；0 张时无图是正常的
        consistent = ("完成数=0" in img and "可见图=0" in img) or \
                     ("完成数=0" not in img and "可见图=0" not in img)
        check(consistent, "生成图渲染与计数一致", img[:80])

    finally:
        close_tab(tid)
        print("  （已关闭 tab）")

    print("-" * 78)
    ok = sum(1 for r, _, _ in results if r)
    print(f"通过 {ok} / {len(results)}" + ("  ✅ 全部通过" if ok == len(results) else "  ⚠️ 有失败"))
    if ok != len(results):
        print("  失败项：")
        for r, label, extra in results:
            if not r:
                print(f"    · {label}  {extra}")
    print("=" * 78)
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
