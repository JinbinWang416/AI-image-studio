# -*- coding: utf-8 -*-
"""验证 GET /api/providers/capabilities。"""

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
            with self.opener.open(req, timeout=30) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")
        except Exception as exc:  # noqa: BLE001
            return -1, f"{type(exc).__name__}: {exc}"


print("=" * 78)
print("服务商能力接口验证")
print("=" * 78)

anon = Client()
st, _ = anon.call("GET", "/api/providers/capabilities")
check(st == 401, "未登录 → 401", f"实际 {st}")

c = Client()
st, body = c.call("POST", "/api/auth/login", {"login_name": "admin", "password": PW})
if st != 200:
    print(f"  ❌ 登录失败 {st}: {body[:120]}")
    raise SystemExit(1)
check(True, "管理员登录")

st, body = c.call("GET", "/api/providers/capabilities")
if st != 200:
    check(False, "接口 → 200", f"{st} {body[:150]}")
    raise SystemExit(1)
d = json.loads(body)
check(True, "接口 → 200")

for k in ("providers", "current", "current_caps", "notes"):
    check(k in d, f"响应含 {k}")

provs = d.get("providers") or {}
check(len(provs) >= 5, "服务商数量 ≥ 5", f"{len(provs)} 个")

print()
print("  能力矩阵:")
print(f"  {'服务商':<24} {'图生图':<8} {'多图':<8} {'上限':<6} 支持模式")
print("  " + "-" * 72)
for name, cap in provs.items():
    yi = "✅" if cap.get("supports_image") else "❌"
    mi = "✅" if cap.get("supports_multi_image") else "❌"
    label = cap.get("label", name)[:10]
    print(f"  {name + '（' + label + '）':<24} {yi:<8} {mi:<8} "
          f"{cap.get('max_references', 0):<6} {' / '.join(cap.get('modes') or [])}")

print()
# 关键断言
flux = provs.get("flux_local") or {}
check(not flux.get("supports_image"), "flux_local 声明不支持图生图")
check(flux.get("modes") == ["text"], "flux_local 只有 text 模式", str(flux.get("modes")))

qwen = provs.get("qwen") or {}
check(bool(qwen.get("supports_image")), "qwen 声明支持图生图")
check(bool(qwen.get("supports_multi_image")), "qwen 声明支持多图生图")
check(int(qwen.get("max_references") or 0) == 3, "qwen 上限 3 张",
      str(qwen.get("max_references")))
check("multi" in (qwen.get("modes") or []), "qwen 支持 multi 模式")

oa = provs.get("openai") or {}
check(not oa.get("supports_multi_image"), "openai 声明不支持多图生图")
check(int(oa.get("max_references") or 0) == 1, "openai 上限 1 张")

check(d.get("current") in provs, "current 指向已知服务商", str(d.get("current")))
check(bool(d.get("current_caps")), "current_caps 非空")

# 每个服务商都要有 modes，且 text 恒在
bad = [n for n, c in provs.items() if "text" not in (c.get("modes") or [])]
check(not bad, "所有服务商都含 text 模式", str(bad))

# 一致性：声明 supports_image 就必须有 image 模式
inconsistent = [
    n for n, c in provs.items()
    if bool(c.get("supports_image")) != ("image" in (c.get("modes") or []))
]
check(not inconsistent, "能力与模式列表一致", str(inconsistent))

print("-" * 78)
ok = sum(1 for r, _ in results if r)
print(f"通过 {ok} / {len(results)}" + ("  ✅ 全部通过" if ok == len(results) else "  ⚠️ 有失败"))
print("=" * 78)
sys.exit(0 if ok == len(results) else 1)
