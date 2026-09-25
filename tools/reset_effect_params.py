# -*- coding: utf-8 -*-
"""把效果图参数重置为**代码中的当前默认值**。

背景：`config/settings.json` 里的 `effect.params` 会覆盖代码默认值，
      所以在代码里改了默认参数后，若不同步重置设置，改动不会生效。

用法：
    .\\.venv\\Scripts\\python.exe tools\\reset_effect_params.py
"""
from __future__ import annotations

import json
import pathlib
import sys
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
BASE = "http://127.0.0.1:8000"


def main() -> int:
    # 优先走接口（服务在跑时最省事）
    try:
        with urllib.request.urlopen(f"{BASE}/api/effect-params", timeout=20) as r:
            cur = json.loads(r.read().decode("utf-8"))
        defaults = cur["defaults"]
        data = json.dumps({"params": defaults}, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            f"{BASE}/api/effect-params", data=data, method="POST",
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            saved = json.loads(r.read().decode("utf-8"))
        print("✅ 已通过接口重置为代码默认值：")
        for k, v in saved["params"].items():
            print(f"   {k:<24} = {v}")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"接口不可用（{exc}），改为直接清理 settings.json 的 effect.params")

    # 回退：直接删掉 settings 里的 params 字段
    settings = ROOT / "config" / "settings.json"
    raw = settings.read_bytes()
    crlf = b"\r\n" in raw
    data = json.loads(raw.decode("utf-8"))
    effect = data.get("effect")
    if isinstance(effect, dict) and "params" in effect:
        effect.pop("params")
        out = json.dumps(data, ensure_ascii=False, indent=2)
        settings.write_text(out + ("\r\n" if crlf else "\n"), encoding="utf-8", newline="")
        print("✅ 已移除 settings.json 中的 effect.params（下次加载将使用代码默认值）")
    else:
        print("ℹ️ settings.json 中本就没有 effect.params")
    return 0


if __name__ == "__main__":
    sys.exit(main())
