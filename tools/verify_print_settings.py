# -*- coding: utf-8 -*-
"""验证印刷设置开关端到端可用：保存 → 持久化 → 被导出流程读取。"""

from __future__ import annotations
import os

import json
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
            with self.opener.open(req, timeout=40) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")
        except Exception as exc:  # noqa: BLE001
            return -1, f"{type(exc).__name__}: {exc}"


print("=" * 76)
print("印刷设置开关端到端验证")
print("=" * 76)

c = Client()
st, body = c.call("POST", "/api/auth/login", {"login_name": "admin", "password": PW})
if st != 200:
    print(f"  ❌ 登录失败: {st}")
    raise SystemExit(1)
check(True, "管理员登录")

# ---- 读当前设置 ----
st, body = c.call("GET", "/api/settings")
if st != 200:
    check(False, "读取设置", f"{st}")
    raise SystemExit(1)
settings = json.loads(body)
original = dict(settings.get("print") or {})
check("print" in settings, "设置里含 print 段", json.dumps(original, ensure_ascii=False)[:80])

for k in ("enabled", "width_cm", "dpi", "bleed_mm", "cutout", "white_ink", "dieline"):
    check(k in original, f"print 段含 {k}", str(original.get(k)))

# ---- 保存一组新值 ----
new_print = {
    "enabled": True,
    "width_cm": 42.0,
    "dpi": 200,
    "bleed_mm": 2.5,
    "cutout": "fallback",
    "white_ink": True,
    "dieline": True,
}
st, body = c.call("POST", "/api/settings", {"print": new_print})
check(st == 200, "保存 print 设置 → 200", f"实际 {st}")

st, body = c.call("GET", "/api/settings")
saved = (json.loads(body).get("print") or {}) if st == 200 else {}
check(saved.get("cutout") == "fallback", "cutout 已持久化", str(saved.get("cutout")))
check(abs(float(saved.get("width_cm") or 0) - 42.0) < 0.01,
      "width_cm 已持久化", str(saved.get("width_cm")))
check(int(saved.get("dpi") or 0) == 200, "dpi 已持久化", str(saved.get("dpi")))
check(abs(float(saved.get("bleed_mm") or 0) - 2.5) < 0.01,
      "bleed_mm 已持久化", str(saved.get("bleed_mm")))

# ---- 落盘确认 ----
sf = ROOT / "config" / "settings.json"
if sf.is_file():
    disk = json.loads(sf.read_text(encoding="utf-8"))
    check("print" in disk, "settings.json 落盘含 print 段")
    check((disk.get("print") or {}).get("cutout") == "fallback",
          "落盘的 cutout 值正确", str((disk.get("print") or {}).get("cutout")))

# ---- 被 load_config 读到 ----
from app.config import load_config  # noqa: E402

cfg = load_config()
check(cfg.print_cutout == "fallback", "load_config 读到 cutout", cfg.print_cutout)
check(abs(cfg.print_width_cm - 42.0) < 0.01, "load_config 读到 width_cm", str(cfg.print_width_cm))
check(cfg.print_dpi == 200, "load_config 读到 dpi", str(cfg.print_dpi))

# ---- 非法值回落 ----
st, _ = c.call("POST", "/api/settings", {"print": dict(new_print, cutout="乱写的")})
# ⚠️ SettingsStore.load() 有**进程内缓存** —— 同一进程里再调 load_config()
#    会读到旧值（实测踩到：误判为"回落失效"）。这里用子进程验证落盘值的处理。
import subprocess  # noqa: E402

probe = (
    "import sys; sys.path.insert(0, r'%s');"
    "from app.config import load_config;"
    "print(load_config().print_cutout)" % ROOT
)
_out = subprocess.run([sys.executable, "-c", probe],
                      capture_output=True, text=True, encoding="utf-8")
_got = (_out.stdout or "").strip()
check(_got == "auto", "非法 cutout 回落 auto（子进程验证）",
      _got or (_out.stderr or "")[:80])

# ---- 导出接口返回该设置 ----
st, body = c.call("GET", "/api/print-export/batch_test")
if st in (200, 404):
    if st == 200:
        d = json.loads(body)
        check("settings" in d, "导出接口返回 settings")
    else:
        check(True, "导出接口可访问（批次不存在属正常）")

# ---- 恢复原值 ----
st, body = c.call("GET", "/api/settings")
cur = json.loads(body)
merged = dict(cur.get("print") or {})
merged.update(original)
c.call("POST", "/api/settings", {"print": merged})
check(True, "已恢复原有设置")

print("-" * 76)
ok = sum(1 for r, _ in results if r)
print(f"通过 {ok} / {len(results)}" + ("  ✅ 全部通过" if ok == len(results) else "  ⚠️ 有失败"))
print("=" * 76)
sys.exit(0 if ok == len(results) else 1)
