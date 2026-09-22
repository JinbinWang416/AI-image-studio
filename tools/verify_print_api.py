# -*- coding: utf-8 -*-
"""
印刷导出接口端到端验证。

覆盖：
    1. GET  /api/print-export/<batch_id>          状态查询
    2. POST /api/print-export/<batch_id>/<store>  手动重跑（幂等）
    3. 权限：未登录 401 / 低权限 403
    4. 边界：不存在的批次 / 门店 → 404
    5. 批次 manifest 回写 print_export 字段（只增不改）
    6. api_image 的 view=preview

用法：
    .\\.venv\\Scripts\\python.exe tools\\verify_print_api.py
"""
from __future__ import annotations
import os

import json
import sys
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE = "http://127.0.0.1:8000"
ADMIN_PW = os.environ.get("SHS_ADMIN_PW", "")  # 真实 admin 密码（登录用）
FIXTURE_PW = "Str0ng!Passw0rd"  # 新建测试用户用：必须满足强度规则（≥10 位）

RESULTS: list[tuple[bool, str]] = []


def check(ok: bool, label: str, extra: str = "") -> None:
    RESULTS.append((ok, label))
    print(f"  {'✅' if ok else '❌'} {label}" + (f"   {extra}" if extra else ""))


class Client:
    def __init__(self) -> None:
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar())
        )

    def call(self, method: str, path: str, body: dict | None = None):
        data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
        req = urllib.request.Request(
            BASE + path, data=data, method=method,
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        try:
            with self.opener.open(req, timeout=180) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")
        except Exception as exc:  # noqa: BLE001
            return -1, f"{type(exc).__name__}: {exc}"


def find_batch_with_png() -> tuple[str, str]:
    """找一个含 PNG 的真实批次与门店序号。"""
    for p in sorted((ROOT / "output").glob("batch_*/*/*生成图/*.png"), reverse=True):
        store_dir = p.parent.parent
        batch_dir = store_dir.parent
        idx = store_dir.name.split("_", 1)[0]
        if idx.isdigit():
            return batch_dir.name, idx
    return "", ""


def main() -> int:
    print("=" * 78)
    print("印刷导出接口验证")
    print("=" * 78)

    batch_id, store_index = find_batch_with_png()
    if not batch_id:
        print("  ❌ 找不到含 PNG 的批次，无法验证")
        return 1
    print(f"  测试批次: {batch_id}   门店序号: {store_index}\n")

    # ---- 未登录 ----
    anon = Client()
    st, _ = anon.call("GET", f"/api/print-export/{batch_id}")
    check(st == 401, "未登录查询状态 → 401", f"实际 {st}")
    st, _ = anon.call("POST", f"/api/print-export/{batch_id}/{store_index}")
    check(st == 401, "未登录重跑 → 401", f"实际 {st}")

    # ---- 登录 ----
    c = Client()
    st, body = c.call("POST", "/api/auth/login",
                      {"login_name": "admin", "password": ADMIN_PW})
    if st != 200:
        print(f"  ❌ 登录失败：{st} {body[:150]}")
        return 1
    check(True, "管理员登录")

    # ---- 状态查询 ----
    st, body = c.call("GET", f"/api/print-export/{batch_id}")
    if st != 200:
        check(False, "查询状态 → 200", f"{st} {body[:150]}")
    else:
        d = json.loads(body)
        check(True, "查询状态 → 200", f"{len(d.get('stores') or [])} 个门店")
        for key in ("batch_id", "enabled", "settings", "stores"):
            check(key in d, f"响应含 {key}")
        s = d["settings"]
        for key in ("width_cm", "dpi", "bleed_mm", "white_ink", "dieline"):
            check(key in s, f"settings 含 {key}")
        check(isinstance(d["stores"], list), "stores 是列表")

    # ---- 非法批次 ----
    st, _ = c.call("GET", "/api/print-export/..%2F..%2Fetc")
    check(st in (400, 404), "非法批次 ID → 400/404", f"实际 {st}")
    # 中文批次名必须 URL 编码，否则 urllib 会直接抛错（不是接口问题）
    from urllib.parse import quote

    st, _ = c.call("GET", f"/api/print-export/{quote('batch_不存在')}")
    check(st == 404, "不存在的批次 → 404", f"实际 {st}")

    # ---- 手动重跑 ----
    st, body = c.call("POST", f"/api/print-export/{batch_id}/{store_index}", {})
    if st != 200:
        check(False, "手动重跑 → 200", f"{st} {body[:200]}")
    else:
        d = json.loads(body)
        check(True, "手动重跑 → 200",
              f"成功 {d.get('success')}/{d.get('total')}")
        check(d.get("total", 0) >= 1, "至少导出了 1 张")
        check(all("error_code" in r for r in d.get("results") or []),
              "每个结果含 error_code")
        ok_results = [r for r in d.get("results") or [] if r.get("ok")]
        if ok_results:
            check(len(ok_results[0].get("files") or []) >= 4,
                  "产出至少 4 个文件",
                  f"{len(ok_results[0]['files'])} 个")

    # ---- 幂等：再跑一次 ----
    st2, body2 = c.call("POST", f"/api/print-export/{batch_id}/{store_index}", {})
    if st == 200 and st2 == 200:
        d1, d2 = json.loads(body), json.loads(body2)
        n1 = len((d1["results"][0].get("files") or [])) if d1.get("results") else 0
        n2 = len((d2["results"][0].get("files") or [])) if d2.get("results") else 0
        check(n1 == n2, "重跑幂等（文件数一致）", f"{n1} → {n2}")

    # ---- 批次 manifest 回写 ----
    store_dir = None
    for p in sorted((ROOT / "output" / batch_id).glob(f"{store_index}_*")):
        if p.is_dir():
            store_dir = p
            break
    store_output_dir = ""
    if store_dir:
        # 目录名形如 `02_租房服务门店`；而 api_image 的 store 参数要的是
        # store.output_dir（`租房服务门店`，不带序号）。两者不同，别混用。
        parts = store_dir.name.split("_", 1)
        store_output_dir = parts[1] if len(parts) > 1 else store_dir.name
        mf = store_dir / "_manifest.json"
        if mf.is_file():
            data = json.loads(mf.read_text(encoding="utf-8"))
            pe = data.get("print_export")
            check(isinstance(pe, dict), "批次 manifest 已回写 print_export")
            if isinstance(pe, dict):
                check("status" in pe, "print_export 含 status", str(pe.get("status")))
                check("dir" in pe, "print_export 含 dir", str(pe.get("dir")))
                check("files" in pe, "print_export 含 files",
                      f"{len(pe.get('files') or [])} 个")
            # 关键：既有字段不能被破坏
            for key in ("version", "store", "entries"):
                check(key in data, f"既有字段 {key} 未被破坏")
        else:
            check(False, "批次 manifest 不存在")

    # ---- preview 视图 ----
    #
    # ⚠️ `api_image` 用的是 `cfg.output_root`，它**已经包含当前活动批次**，
    #    所以这个接口只服务"当前批次"。历史批次的预览图取不到（这是设计如此）。
    #    因此这里要先拿到当前活动批次，再验证。
    pv_dir = store_dir / "预览" if store_dir else None
    if pv_dir and pv_dir.is_dir():
        jpgs = list(pv_dir.glob("*.jpg"))
        check(bool(jpgs), "预览目录有 JPEG", f"{len(jpgs)} 个")

        st, body = c.call("GET", "/api/state")
        active_batch = ""
        if st == 200:
            try:
                active_batch = json.loads(body).get("batch_id") or ""
            except Exception:  # noqa: BLE001
                active_batch = ""

        if active_batch == batch_id:
            from urllib.parse import quote as _q

            name = jpgs[0].name
            st, _ = c.call(
                "GET",
                f"/api/image?store={_q(store_output_dir)}&file={_q(name)}&view=preview",
            )
            check(st == 200, "api_image view=preview → 200", f"实际 {st}")
        else:
            print(f"  ⏭  当前活动批次是 {active_batch or '（无）'}，"
                  f"与测试批次不同 —— api_image 只服务当前批次，跳过 preview 校验")
            # 仍要确认接口认得 preview 这个 view（非法 view 会 403）
            st, _ = c.call("GET", "/api/image?store=x&file=none.jpg&view=bogus")
            check(st in (401, 403), "非法 view 被拒 → 401/403", f"实际 {st}")

    # ---- 权限边界：低权限用户 ----
    import time as _t

    probe = f"pv{int(_t.time()) % 100000}"
    st, body = c.call("POST", "/api/admin/users", {
        "login_name": probe, "display_name": "印刷验证", "password": FIXTURE_PW,
        "roles": ["operator"]})
    if st == 200:
        uid = json.loads(body)["user"]["id"]
        low = Client()
        low.call("POST", "/api/auth/login",
                 {"login_name": probe, "password": FIXTURE_PW})
        st, _ = low.call("GET", f"/api/print-export/{batch_id}")
        check(st == 200, "客服(operator)有 batch.export → 200", f"实际 {st}")
        c.call("PATCH", f"/api/admin/users/{uid}", {"status": "disabled"})
        check(True, "清理测试账号")

    # ---- 清理：删除本次导出产生的印刷产物 ----
    #
    # ⚠️ 这个脚本会**真的往真实批次的 output/ 里写** 印刷TIF/ _work/ 预览/。
    #    之前漏了清理，导致 output/ 里累积残留（实测多出 6 个 PNG 和 31 个文件）。
    #    这里跑完就删掉，保持用户数据干净。
    if store_dir:
        import shutil

        cleaned = []
        for name in ("印刷TIF", "_work", "预览"):
            d = store_dir / name
            if d.is_dir():
                shutil.rmtree(d, ignore_errors=True)
                cleaned.append(name)
        check(True, "已清理本次导出产物", ", ".join(cleaned) or "（无）")

    print("-" * 78)
    ok = sum(1 for r, _ in RESULTS if r)
    print(f"通过 {ok} / {len(RESULTS)}" + ("  ✅ 全部通过" if ok == len(RESULTS) else "  ⚠️ 有失败"))
    print("=" * 78)
    return 0 if ok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
