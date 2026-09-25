# -*- coding: utf-8 -*-
"""实测「路径可编辑 + 换盘新建目录」三处改动。"""

from __future__ import annotations
import os

import http.cookiejar
import json
import shutil
import urllib.error
import urllib.request
from pathlib import Path

PW = os.environ.get("SHS_ADMIN_PW", "")
results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, extra: str = "") -> None:
    results.append((ok, name, extra))
    print(f"  {'✅' if ok else '❌'} {name}" + (f"   {extra}" if extra else ""))


def client(base: str):
    jar = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    def call(method, path, body=None):
        data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
        req = urllib.request.Request(base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with op.open(req, timeout=120) as r:
                return r.status, json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read().decode() or "{}")
            except Exception:  # noqa: BLE001
                return e.code, {}
        except Exception as exc:  # noqa: BLE001
            return -1, {"error": str(exc)}

    return call


def main() -> int:
    print("=" * 78)
    print("「路径可编辑 + 换盘新建目录」实测")
    print("=" * 78)

    for name, base in (("原版", "http://127.0.0.1:8000"), ("架构版", "http://127.0.0.1:8001")):
        print(f"\n【{name}】{base}")
        call = client(base)
        st, _ = call("POST", "/api/auth/login", {"login_name": "admin", "password": PW})
        if st != 200:
            check(False, "登录", f"HTTP {st}")
            continue
        check(True, "登录")

        # ① 纯目录名 → 同级创建
        st, d = call("POST", "/api/settings/new-dir",
                     {"name": "DSH测试_同级", "base": str(Path.cwd() / "output")})
        check(st == 200 and d.get("ok"), "① 纯目录名 → 同级创建",
              d.get("message", "")[:60])
        p1 = Path(d.get("path", "")) if d.get("path") else None

        # ② 绝对路径（换盘）→ 直接创建
        target2 = Path("E:/DSH换盘测试/贴纸输出")
        st, d2 = call("POST", "/api/settings/new-dir", {"name": str(target2)})
        check(st == 200 and d2.get("ok"), "② 绝对路径 → 换盘创建",
              d2.get("message", "")[:60])
        p2 = Path(d2.get("path", "")) if d2.get("path") else None
        if p2:
            check(p2.is_dir(), "② 目录真的建出来了", str(p2))

        # ③ 校验接口能接受可编辑的路径
        st, d3 = call("POST", "/api/settings/validate-path", {"path": str(target2)})
        check(st == 200 and d3.get("ok"), "③ 校验接口可用",
              str(d3.get("message", ""))[:50])

        # ④ 非法路径应被拒
        st, d4 = call("POST", "/api/settings/new-dir", {"name": 'E:/bad<name>'})
        check(not d4.get("ok"), "④ 非法路径被拒", str(d4.get("message", ""))[:50])

        # 清理
        for p in (p1, p2):
            if p and p.is_dir():
                try:
                    shutil.rmtree(p, ignore_errors=True)
                except Exception:  # noqa: BLE001
                    pass
        tmp = Path("E:/DSH换盘测试")
        if tmp.is_dir():
            shutil.rmtree(tmp, ignore_errors=True)
        check(True, "清理测试目录")

    # ⑤ 前端：路径框已可编辑
    print()
    for name, path in (("原版", Path(r"E:\1_Software\6_AI工具\deepseek\2_开发\图片生成\app\web\static\index.html")),
                       ("架构版", Path(r"E:\1_Software\6_AI工具\deepseek\2_开发\AI生图架构版\app\web\static\index.html"))):
        html = path.read_text(encoding="utf-8")
        readonly = 'id="s-output"' in html and 'readonly' in html.split('id="s-output"')[1][:200]
        check(not readonly, f"⑤ {name}：路径框 readonly 已移除")

    print("\n" + "=" * 78)
    ok = sum(1 for r, _, _ in results if r)
    print(f"通过 {ok} / {len(results)}" + ("  ✅ 全部通过" if ok == len(results) else "  ⚠️ 有失败"))
    print("=" * 78)
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
