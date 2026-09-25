# -*- coding: utf-8 -*-
"""
验证「效果图参数微调」接口。

用 Python 直接调用（避免 PowerShell 传中文 JSON 时的编码问题），检查：
  1. GET  /api/effect-params            参数与可调范围
  2. POST /api/effect-params/preview    默认参数预览（背景应为自动匹配的 AI 背景）
  3. POST /api/effect-params/preview    调参预览（贴纸放大 / 关反光 / 更暖）
  4. POST /api/effect-params            保存参数并复读

用法：
    .\\.venv\\Scripts\\python.exe tools\\verify_effect_params.py
"""
from __future__ import annotations

import json
import pathlib
import sys
import urllib.request

BASE = "http://127.0.0.1:8000"
ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "logs"


def get(path: str) -> dict:
    with urllib.request.urlopen(BASE + path, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def post(path: str, payload: dict) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        BASE + path, data=data, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode("utf-8"))


def copy_preview(name: str) -> None:
    src = ROOT / "output" / "_effect_preview" / "preview.png"
    if src.is_file():
        OUT.mkdir(exist_ok=True)
        (OUT / name).write_bytes(src.read_bytes())


def main() -> int:
    print("=" * 74)
    print("效果图参数微调接口验证")
    print("=" * 74)

    # ---------------- 1) 参数 ----------------
    p = get("/api/effect-params")
    spec = p["spec"]
    print(f"可调参数 : {len(spec)} 个")
    groups: dict[str, list[str]] = {}
    for key, s in spec.items():
        groups.setdefault(s["group"], []).append(s["label"])
    for g, labels in groups.items():
        print(f"  [{g}] {' / '.join(labels)}")
    print(f"预览源可用: {p['has_source']}")

    # ---------------- 2) 默认参数预览 ----------------
    print("\n" + "-" * 74)
    d = post("/api/effect-params/preview", {"store_name": "房屋中介"})
    print(f"预览 A（默认） : {d['width']}×{d['height']}  背景={d['background_mode']}")
    print(f"  源图         : {d['source']}")
    copy_preview("preview_A_default.png")

    # ---------------- 3) 调参预览 ----------------
    tuned = {
        "sticker_width_ratio": 0.92,
        "vertical_center": 0.40,
        "reflection_strength": 0.0,
        "warm_strength": 0.30,
        "background_blur_ratio": 0.045,
        "vignette_strength": 0.22,
    }
    d2 = post("/api/effect-params/preview", {"store_name": "房屋中介", "params": tuned})
    print(f"预览 B（调参） : 背景={d2['background_mode']}")
    for k, v in tuned.items():
        print(f"  {k:<24} → {d2['params'][k]}")
    copy_preview("preview_B_tuned.png")

    # ---------------- 4) 保存与复读 ----------------
    saved = post("/api/effect-params", {"params": tuned})
    again = get("/api/effect-params")["current"]
    ok_save = all(abs(again[k] - v) < 1e-6 for k, v in tuned.items())
    print(f"\n保存参数     : {'✅ 复读一致' if ok_save else '❌ 复读不一致'}")

    # 复位为默认（避免影响后续批量）
    post("/api/effect-params", {"params": p["defaults"]})
    back = get("/api/effect-params")["current"]
    ok_reset = all(abs(back[k] - v) < 1e-6 for k, v in p["defaults"].items())
    print(f"恢复默认     : {'✅ 已复位' if ok_reset else '❌ 复位失败'}")

    # ---------------- 5) 预设 ----------------
    presets = p.get("presets") or {}
    print(f"\n预设         : {len(presets)} 组")
    preset_ok = True
    for key, preset in presets.items():
        print(f"  [{key}] {preset['label']}")
        print(f"        {preset['note']}")
        try:
            d3 = post("/api/effect-params/preview",
                      {"store_name": "房屋中介", "params": preset["params"]})
            copy_preview(f"preview_preset_{key}.png")
            applied = all(
                abs(d3["params"][k] - v) < 1e-6 for k, v in preset["params"].items()
            )
            print(f"        → 预览已生成，参数{'已生效' if applied else '未完全生效'}")
            preset_ok = preset_ok and applied
        except Exception as exc:  # noqa: BLE001
            print(f"        → ❌ {exc}")
            preset_ok = False

    # ---------------- 结论 ----------------
    print("\n" + "=" * 74)
    ok = (
        len(spec) >= 13
        and d["background_mode"] == "ai_generated"
        and d2["background_mode"] == "ai_generated"
        and ok_save and ok_reset and preset_ok
    )
    print(f"结论：{'✅ 全部通过' if ok else '⚠️ 存在问题，请检查上面输出'}")
    if d["background_mode"] != "ai_generated":
        print("  注意：预览背景不是 ai_generated，说明自动匹配未在接口路径生效")
    print("=" * 74)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
