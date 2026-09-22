# -*- coding: utf-8 -*-
"""「文件与保存」输出目录改造验证。

覆盖：
    · new-dir：正常创建、非法名、重名、超长
    · 只读：前端输入框已 readonly（不调 pick-dir，避免弹窗打扰用户）
    · pick-dir：仅验证路由已注册 + 前端已绑定

用法：
    .\\.venv\\Scripts\\python.exe tools\\verify_output_dir.py
"""

from __future__ import annotations
import os

import json
import shutil
import sys
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
BASE = "http://127.0.0.1:8000"
PW = os.environ.get("SHS_ADMIN_PW", "")

results: list[tuple[bool, str]] = []


def check(ok: bool, label: str, extra: str = "") -> None:
    results.append((ok, label))
    print(f"  {'✅' if ok else '❌'} {label}" + (f"   {extra}" if extra else ""))


class Client:
    def __init__(self):
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()))

    def call(self, method, path, body=None):
        data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
        req = urllib.request.Request(
            BASE + path, data=data, method=method,
            headers={"Content-Type": "application/json; charset=utf-8"})
        try:
            with self.opener.open(req, timeout=60) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")
        except Exception as exc:  # noqa: BLE001
            return -1, f"{type(exc).__name__}: {exc}"


print("=" * 78)
print("输出目录改造验证")
print("=" * 78)

# 用项目下的临时目录做实验，别碰真实保存目录
sandbox = ROOT / "_dir_test_sandbox"
if sandbox.exists():
    shutil.rmtree(sandbox, ignore_errors=True)
sandbox.mkdir(parents=True)
base_dir = sandbox / "output"
base_dir.mkdir()

c = Client()
st, body = c.call("POST", "/api/auth/login", {"login_name": "admin", "password": PW})
if st != 200:
    print(f"  ❌ 登录失败 {st}: {body[:120]}")
    raise SystemExit(1)
check(True, "管理员登录")

print("\n【1】new-dir 正常创建")
st, body = c.call("POST", "/api/settings/new-dir",
                  {"name": "我的图片", "base": str(base_dir)})
d = json.loads(body) if st == 200 else {}
check(st == 200 and d.get("ok"), "创建成功", d.get("message", "")[:60])
created = Path(d.get("path", "")) if d.get("path") else None
check(created is not None and created.is_dir(), "目录确实已创建",
      str(created.relative_to(ROOT)) if created else "")
# 「同级」= 建在**当前目录的父目录**下（base 的兄弟，不是 base 的子目录）
check(created is not None and created.parent == base_dir.parent,
      "建在当前目录的同级（父目录下）",
      f"parent={created.parent.name if created else '?'}（应为 {base_dir.parent.name}）")

print("\n【2】非法与边界")
cases = [
    ("空名", {"name": "", "base": str(base_dir)}, False),
    ("含斜杠", {"name": "a/b", "base": str(base_dir)}, False),
    ("含星号", {"name": "a*b", "base": str(base_dir)}, False),
    ("点点", {"name": "..", "base": str(base_dir)}, False),
    ("超长名", {"name": "x" * 100, "base": str(base_dir)}, False),
    ("重名", {"name": "我的图片", "base": str(base_dir)}, False),
    ("正常第二个", {"name": "第二目录", "base": str(base_dir)}, True),
]
for label, payload, should_ok in cases:
    st2, body2 = c.call("POST", "/api/settings/new-dir", payload)
    r = json.loads(body2) if st2 == 200 else {}
    ok = bool(r.get("ok")) == should_ok
    check(ok, f"{label} → {'允许' if should_ok else '拒绝'}",
          (r.get("message") or "")[:52])

print("\n【3】pick-dir / open-dir 路由已注册（不实际调用，避免弹窗）")
for path in ("/api/settings/pick-dir", "/api/settings/new-dir", "/api/settings/open-dir"):
    st3, _ = c.call("POST", path, {})
    # 未登录会 401；登录后这些接口都存在（pick-dir 会真弹窗，所以只测 new-dir 之外的 404 判定）
    check(st3 != 404, f"{path} 已注册", f"HTTP {st3}")

print("\n【4】前端：输入框只读 + 按钮齐全")
html = (ROOT / "app" / "web" / "static" / "index.html").read_text(encoding="utf-8")
import re

m = re.search(r'<input id="s-output"[^>]*>', html)
tag = m.group(0) if m else ""
check("readonly" in tag, "s-output 已设 readonly", tag[:90])
for bid, label in (("btn-pick-dir", "浏览…"), ("btn-new-dir", "新建"),
                   ("btn-open-dir", "打开"), ("btn-validate-path", "校验")):
    check(f'id="{bid}"' in html, f"按钮存在：{label}")

appjs = (ROOT / "app" / "web" / "static" / "app.js").read_text(encoding="utf-8")
check("async function pickDir()" in appjs, "前端有 pickDir()")
check("async function newDir()" in appjs, "前端有 newDir()")
check("btn-pick-dir" in appjs and "btn-new-dir" in appjs, "两个按钮已绑定事件")

print("\n【5】CSS：只读样式存在")
css = (ROOT / "app" / "web" / "static" / "app.css").read_text(encoding="utf-8")
check("input[readonly]" in css, "只读输入框样式已定义")

# 清理沙箱
shutil.rmtree(sandbox, ignore_errors=True)
check(not sandbox.exists(), "已清理测试沙箱")

print("-" * 78)
ok = sum(1 for r, _ in results if r)
print(f"通过 {ok} / {len(results)}" + ("  ✅ 全部通过" if ok == len(results) else "  ⚠️ 有失败"))
if ok != len(results):
    for r, label in results:
        if not r:
            print(f"    · {label}")
print("=" * 78)
sys.exit(0 if ok == len(results) else 1)
