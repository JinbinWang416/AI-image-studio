# -*- coding: utf-8 -*-
"""验证参考图缩略图接口与前端反馈。"""

from __future__ import annotations
import os

import base64
import io
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
            with self.opener.open(req, timeout=60) as r:
                return r.status, r.read(), r.headers.get("Content-Type", "")
        except urllib.error.HTTPError as e:
            return e.code, e.read(), ""
        except Exception as exc:  # noqa: BLE001
            return -1, str(exc).encode(), ""


print("=" * 78)
print("参考图缩略图接口验证")
print("=" * 78)

from PIL import Image  # noqa: E402


def make_png(w: int, h: int) -> bytes:
    im = Image.new("RGB", (w, h))
    px = im.load()
    for x in range(0, w, 2):
        for y in range(0, h, 2):
            px[x, y] = ((x * 5) % 256, (y * 7) % 256, 128)
    buf = io.BytesIO()
    im.save(buf, format="PNG", compress_level=1)
    return buf.getvalue()


anon = Client()
st, _, _ = anon.call("GET", "/api/reference-assets/aaaaaaaaaaaaaaaa/raw")
check(st in (401, 403), "未登录访问 → 401/403", f"实际 {st}")

c = Client()
st, body, _ = c.call("POST", "/api/auth/login", {"login_name": "admin", "password": PW})
if st != 200:
    print(f"  ❌ 登录失败 {st}: {body[:120]}")
    raise SystemExit(1)
check(True, "管理员登录")

# 上传一张参考图
raw = make_png(900, 700)
url = "data:image/png;base64," + base64.b64encode(raw).decode()
st, body, _ = c.call("POST", "/api/reference-assets",
                     {"data_url": url, "file_name": "thumb-test.png"})
if st != 200:
    check(False, "上传参考图 → 200", f"{st} {body[:150]}")
    raise SystemExit(1)
d = json.loads(body)
asset = d.get("asset") or {}
aid = asset.get("id", "")
check(bool(aid), "上传成功并返回资产 ID", aid)
check("compressed" not in asset or not asset.get("compressed"),
      "900×700 未触发压缩（未超 1536）")

# 取缩略图
st, content, ctype = c.call("GET", f"/api/reference-assets/{aid}/raw")
check(st == 200, "获取缩略图 → 200", f"实际 {st}")
check(content[:8] == b"\x89PNG\r\n\x1a\n", "返回的是有效 PNG", f"{len(content)} 字节")
check("image/png" in ctype, "Content-Type 正确", ctype)

# 安全：非法 ID
for bad in ("../../etc/passwd", "zzzz", "a" * 20, "..%2F..%2Fetc"):
    st, _, _ = c.call("GET", f"/api/reference-assets/{bad}/raw")
    check(st in (400, 403, 404), f"非法 ID 被拒：{bad[:20]}", f"实际 {st}")

# 不存在的合法 ID
st, _, _ = c.call("GET", "/api/reference-assets/0123456789abcdef/raw")
check(st == 404, "不存在的资产 → 404", f"实际 {st}")

# 列表接口仍然正常（且不含 Base64）
st, body, _ = c.call("GET", "/api/reference-assets")
if st == 200:
    d = json.loads(body)
    check("assets" in d, "列表接口正常", f"{len(d.get('assets') or [])} 张")
    text = json.dumps(d, ensure_ascii=False)
    check("base64" not in text.lower(), "列表不含 Base64 内容")
else:
    check(False, "列表接口", f"{st}")

# ---- 清理：删除本次上传的测试资产，避免污染 output/_references ----
# （之前漏了这步，测试跑完会在用户资产库里留下 thumb-test.png）
try:
    cfg_root = ROOT / "output"
    ref_dir = cfg_root / "_references"
    meta = ref_dir / f"{aid}.json"
    if meta.is_file():
        m = json.loads(meta.read_text(encoding="utf-8"))
        if m.get("file_name") == "thumb-test.png":
            meta.unlink()
            img = ref_dir / f"{aid}.png"
            if img.is_file():
                img.unlink()
            check(True, "已清理测试资产", aid)
        else:
            check(False, "测试资产文件名不符，未删除", str(m.get("file_name")))
except Exception as exc:  # noqa: BLE001
    check(False, "清理测试资产失败", f"{type(exc).__name__}")

print("-" * 78)
ok = sum(1 for r, _ in results if r)
print(f"通过 {ok} / {len(results)}" + ("  ✅ 全部通过" if ok == len(results) else "  ⚠️ 有失败"))
print("=" * 78)
sys.exit(0 if ok == len(results) else 1)
