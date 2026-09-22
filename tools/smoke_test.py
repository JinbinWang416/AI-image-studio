# -*- coding: utf-8 -*-
"""
Web 服务冒烟测试（标准库实现，不依赖 requests）。

用法：
    python main.py web          # 另开终端启动服务
    python tools/smoke_test.py  # 运行本测试

或指定地址：python tools/smoke_test.py http://127.0.0.1:8000
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"

passed = 0
failed = 0
section = ""


def head(title: str) -> None:
    global section
    section = title
    print(f"\n── {title} " + "─" * max(0, 52 - len(title)))


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  [PASS] {name}" + (f" — {detail}" if detail else ""))
    else:
        failed += 1
        print(f"  [FAIL] {name}" + (f" — {detail}" if detail else ""))


def get(path: str, raw: bool = False):
    with urllib.request.urlopen(BASE + path, timeout=20) as r:
        body = r.read()
        return r.status, (body if raw else body.decode("utf-8"))


def post(path: str, payload: dict | None = None):
    data = json.dumps(payload or {}).encode("utf-8")
    req = urllib.request.Request(
        BASE + path, data=data, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8") or "{}")


def main() -> int:
    print("=" * 60)
    print(f"Web 服务冒烟测试 → {BASE}")
    print("=" * 60)

    # ---------------------------------------------------------- 基础
    head("基础与静态资源")
    try:
        status, html = get("/")
        check("首页可访问", status == 200, f"HTTP {status}")
        check("首页含标题", "门店贴纸图片自动生成智能体" in html)
        check("首页含设置入口", 'id="btn-settings"' in html)
        check("首页含设置弹窗", 'id="settings-modal"' in html)
        check("弹窗含 6 个分组", html.count('class="sgroup') >= 6,
              f"实际 {html.count('class=\"sgroup')} 个")
        check("首页已移除旧的服务商下拉", 'id="provider"' not in html)
        check("含导出按钮", 'id="btn-export-titles"' in html and 'id="btn-export-package"' in html)
        check("含提示词版本选择", 'id="s-prompt-version"' in html)
    except Exception as e:
        check("首页可访问", False, str(e))

    for path in ("/static/style.css", "/static/app.js"):
        try:
            status, body = get(path)
            check(f"静态资源 {path}", status == 200 and len(body) > 500,
                  f"HTTP {status}, {len(body)} 字节")
        except Exception as e:
            check(f"静态资源 {path}", False, str(e))

    # ---------------------------------------------------------- 状态
    head("状态接口")
    state = None
    try:
        status, text = get("/api/state")
        state = json.loads(text)
        check("/api/state 正常", status == 200)
        check("门店数量为 23", len(state["stores"]) == 23, f"实际 {len(state['stores'])}")
        check("总张数为 138", state["progress"]["total"] == 138)
        check("每套 6 张", all(len(x["items"]) == 6 for x in state["stores"]))
        first = state["stores"][0]
        check("第 1 套标题正确",
              first["main_title"] == "房屋中介" and first["sub_title"] == "房源咨询 欢迎到店")
        check("拼多多标题为 30 字", len(first["pdd_title"]) == 30)
        check("顶栏需要 run_limit 字段", "run_limit" in state["config"])
        check("服务商摘要含 6 家", len(state["providers"]) == 6,
              ", ".join(state["providers"].keys()))
        check("进度字段完整", "percent" in state["progress"],
              f"{state['progress']['done']}/{state['progress']['total']}")
    except Exception as e:
        check("/api/state 正常", False, str(e))

    # ---------------------------------------------------------- 清单
    head("服务商 / 模型清单")
    catalog = None
    try:
        status, text = get("/api/catalog")
        catalog = json.loads(text)
        check("/api/catalog 正常", status == 200)
        check("含 6 个服务商", len(catalog) == 6, ", ".join(catalog.keys()))
        qwen = catalog.get("qwen", {})
        check("阿里含模型列表", len(qwen.get("models", [])) >= 5,
              f"{len(qwen.get('models', []))} 个模型")
        top = qwen["models"][0]
        check("模型带价格标注", "price" in top and top["price"] > 0, f"¥{top.get('price')}")
        check("模型带负向词标注", "negative" in top, f"negative={top.get('negative')}")
        check("模型带限流标注", "rpm" in top, f"rpm={top.get('rpm')}")
        kling = catalog.get("kling", {})
        check("可灵含踩坑警告", len(kling.get("warnings", [])) >= 2,
              f"{len(kling.get('warnings', []))} 条")
        check("含自定义 OpenAI 兼容", "custom" in catalog)
        check("含本地模拟", "mock" in catalog)
        check("含 FLUX 本地验证", "flux_local" in catalog and catalog["flux_local"].get("kind") == "local_flux")
    except Exception as e:
        check("/api/catalog 正常", False, str(e))

    # ---------------------------------------------------------- 设置
    head("设置接口")
    settings = None
    try:
        status, text = get("/api/settings")
        settings = json.loads(text)
        check("/api/settings 正常", status == 200)
        check("含 active_provider", "active_provider" in settings)
        check("含 5 组配置",
              all(k in settings for k in
                  ("providers", "generation", "output", "qc", "guard")),
              ", ".join(settings.keys()))
        check("含 6 个服务商配置", len(settings.get("providers", {})) == 6)
        check("API Key 已打码或为空",
              all("****" in (p.get("api_key") or "") or not p.get("api_key")
                  for p in settings["providers"].values()))
        check("含 has_key 标记",
              all("has_key" in p for p in settings["providers"].values()))
    except Exception as e:
        check("/api/settings 正常", False, str(e))

    # 保存（幂等：写回当前值，不破坏配置）
    try:
        status, d = post("/api/settings", {
            "active_provider": (settings or {}).get("active_provider", "mock"),
            "generation": {"limit": (settings or {}).get("generation", {}).get("limit", 0)},
        })
        check("保存设置（幂等回写）", status == 200 and d.get("ok"), f"HTTP {status}")
        check("保存后返回摘要", "summary" in d and "provider_label" in d["summary"])
        check("保存后无配置问题", not d.get("problems"), "；".join(d.get("problems", [])))
    except Exception as e:
        check("保存设置（幂等回写）", False, str(e))

    # 测试连接（mock 无需 Key）
    try:
        status, d = post("/api/settings/test", {"provider": "mock"})
        check("测试连接 · 本地模拟", status == 200 and d.get("ok"),
              d.get("message", ""))
    except Exception as e:
        check("测试连接 · 本地模拟", False, str(e))

    # 测试连接（无 Key 应返回明确提示）
    try:
        status, d = post("/api/settings/test", {"provider": "kling", "api_key": ""})
        check("测试连接 · 缺 Key 有明确提示",
              d.get("status") == "no_key", d.get("message", ""))
    except Exception as e:
        check("测试连接 · 缺 Key 有明确提示", False, str(e))

    try:
        status, text = get("/api/local-validation/state")
        d = json.loads(text)
        check("本地验证状态接口", status == 200 and d.get("scope", {}).get("store") == "01")
        check("本地验证固定 V8 / 6 张 / 单并发", d.get("scope", {}).get("prompt_version") == "v8" and d.get("scope", {}).get("images") == 6 and d.get("scope", {}).get("concurrency") == 1)
    except Exception as e:
        check("本地验证状态接口", False, str(e))

    try:
        status, text = get("/api/professional-validation/state")
        d = json.loads(text)
        scope = d.get("scope", {})
        check("V9 专业贴纸状态接口", status == 200 and scope.get("store") == "01")
        check("V9 固定 6 主题 / 每主题 3 候选 / 单并发", scope.get("images") == 6 and scope.get("candidates_per_theme") == 3 and scope.get("concurrency") == 1)
    except Exception as e:
        check("V9 专业贴纸状态接口", False, str(e))

    try:
        status, d = post("/api/run", {"provider": "flux_local"})
        check("FLUX 被限制为专用验证入口", status == 400 and "固定" in d.get("detail", ""), d.get("detail", ""))
    except Exception as e:
        check("FLUX 被限制为专用验证入口", False, str(e))

    # 路径校验
    try:
        status, d = post("/api/settings/validate-path", {"path": "output"})
        check("路径校验 · 有效路径", d.get("ok"), d.get("message", ""))
        status, d = post("/api/settings/validate-path", {"path": ""})
        check("路径校验 · 空路径被拒", not d.get("ok"), d.get("message", ""))
    except Exception as e:
        check("路径校验", False, str(e))

    # 导出抽检清单
    try:
        status, d = post("/api/export/checklist")
        check("导出人工抽检清单", d.get("ok") and d.get("rows") == 138,
              f"{d.get('rows')} 行 → {d.get('path')}")
    except Exception as e:
        check("导出人工抽检清单", False, str(e))

    # ---------------------------------------------------------- 提示词版本
    head("提示词版本管理")
    try:
        status, text = get("/api/prompts/versions")
        pv = json.loads(text)
        check("/api/prompts/versions 正常", status == 200)
        check("含 current 版本", bool(pv.get("current")), pv.get("current"))
        vers = pv.get("versions", [])
        avail = [v for v in vers if v.get("available")]
        check("可用版本 >= 8", len(avail) >= 8,
              ", ".join(v["version"] for v in avail))
        check("当前为参考图驱动 V8", pv.get("current") == "v8", pv.get("current"))
        check("版本带说明文字", all(v.get("note") for v in avail[:5]))
        check("含提示词预览样本", bool(pv.get("sample")),
              f"{len(pv.get('sample', {}))} 条")
        sample_now = (pv.get("sample") or {}).get(pv.get("current"), "")
        check("当前版本提示词含彩色要求", "彩色" in sample_now)
        check("当前版本提示词含异形轮廓要求", "自由曲线圆角异形外轮廓" in sample_now)
    except Exception as e:
        check("/api/prompts/versions 正常", False, str(e))

    # ---------------------------------------------------------- 导出
    head("导出功能")
    try:
        status, d = post("/api/export/titles")
        check("导出拼多多标题 CSV", d.get("ok") and d.get("rows") == 23,
              f"{d.get('rows')} 行")
    except Exception as e:
        check("导出拼多多标题 CSV", False, str(e))

    try:
        status, d = post("/api/export/checklist")
        check("导出人工抽检清单", d.get("ok") and d.get("rows") == 138,
              f"{d.get('rows')} 行")
    except Exception as e:
        check("导出人工抽检清单", False, str(e))

    try:
        status, d = post("/api/export/package")
        check("打包图片 ZIP", d.get("ok"), f"{d.get('images')} 张 / {d.get('size_mb')} MB")
    except Exception as e:
        check("打包图片 ZIP", False, str(e))

    # ---------------------------------------------------------- 单张重生成
    head("单张重生成（仅参数校验，不实际调用 API）")
    try:
        status, d = post("/api/regenerate", {})
        check("缺参数时返回 400", status == 400, str(d.get("detail", ""))[:60])
    except Exception as e:
        check("缺参数时返回 400", False, str(e))

    # ---------------------------------------------------------- 运维面板
    head("版本对比 / 待处理项 / 运行历史")
    try:
        status, text = get("/api/failures")
        f = json.loads(text)
        check("/api/failures 正常", status == 200)
        check("含 failed / missing 字段",
              "failed" in f and "missing" in f and "total_pending" in f,
              f"失败 {f.get('failed_count')} / 未生成 {f.get('missing_count')}")
    except Exception as e:
        check("/api/failures 正常", False, str(e))

    try:
        status, text = get("/api/runs")
        r = json.loads(text)
        check("/api/runs 正常", status == 200)
        check("含 summary 统计", "summary" in r and "runs" in r,
              f"累计 {r.get('summary', {}).get('runs')} 次运行")
    except Exception as e:
        check("/api/runs 正常", False, str(e))

    try:
        target = None
        if state:
            for st in state["stores"]:
                for it in st["items"]:
                    if it["exists"]:
                        target = (st["output_dir"], it["file_name"])
                        break
                if target:
                    break
        if target:
            path = ("/api/history?store=" + urllib.parse.quote(target[0])
                    + "&file=" + urllib.parse.quote(target[1]))
            status, text = get(path)
            h = json.loads(text)
            check("/api/history 正常", status == 200 and "items" in h,
                  f"{h.get('count')} 个历史版本")
        else:
            print("  [SKIP] /api/history — 尚无已生成的图片")
    except Exception as e:
        check("/api/history 正常", False, str(e))

    # ---------------------------------------------------------- 图片与安全
    head("图片与安全")
    try:
        if state:
            target = None
            for store in state["stores"]:
                for it in store["items"]:
                    if it["exists"]:
                        target = (store["output_dir"], it["file_name"])
                        break
                if target:
                    break
            if target:
                path = ("/api/image?store=" + urllib.parse.quote(target[0])
                        + "&file=" + urllib.parse.quote(target[1]))
                status, data = get(path, raw=True)
                check("图片端点返回合法 PNG",
                      status == 200 and data[:8] == b"\x89PNG\r\n\x1a\n",
                      f"{target[1]} ({len(data)} 字节)")
            else:
                print("  [SKIP] 图片端点 — 尚无已生成的图片")
    except Exception as e:
        check("图片端点返回合法 PNG", False, str(e))

    try:
        get("/api/image?store=..&file=../main.py")
        check("路径穿越被拦截", False, "未拦截！")
    except urllib.error.HTTPError as e:
        check("路径穿越被拦截", e.code in (403, 404), f"HTTP {e.code}")
    except Exception as e:
        check("路径穿越被拦截", False, str(e))

    print("\n" + "=" * 60)
    print(f"通过 {passed} 项，失败 {failed} 项")
    print("=" * 60)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
