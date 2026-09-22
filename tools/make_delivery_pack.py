# -*- coding: utf-8 -*-
"""
生成可交付的验证包：设计图 ↔ 效果图对照图 + 验收清单 + zip 打包。

用法：
    .\\.venv\\Scripts\\python.exe tools\\make_delivery_pack.py
"""
from __future__ import annotations

import json
import pathlib
import sys
import time
import zipfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PIL import Image, ImageDraw, ImageFilter, ImageStat  # noqa: E402

BATCH_PREFIX = "batch_"
DELIVERY = ROOT / "delivery"


def latest_batch_with_effects() -> tuple[pathlib.Path, pathlib.Path]:
    """找同时有生成图和效果图的最新批次。"""
    for batch in sorted((ROOT / "output").glob(f"{BATCH_PREFIX}*"), reverse=True):
        if not batch.is_dir():
            continue
        for store in sorted(batch.iterdir()):
            if not store.is_dir():
                continue
            gen = store / f"{store.name}生成图"
            if not gen.is_dir():
                continue
            real = [p for p in gen.glob("*.png") if p.stat().st_size > 100_000]
            if real:
                return batch, store
    raise SystemExit("未找到含真实生成图的批次")


def metrics(p: pathlib.Path) -> dict:
    with Image.open(p) as im:
        rgb = im.convert("RGB")
        g = rgb.convert("L")
        w, h = rgb.size
        st = ImageStat.Stat(g)
        rst = ImageStat.Stat(rgb)
        r, _, b = rst.mean
        return {
            "w": w, "h": h,
            "mean": round(st.mean[0], 1),
            "contrast": round(st.stddev[0], 1),
            "warm": round(r / max(1.0, b), 3),
            "sat": round(ImageStat.Stat(rgb.convert("HSV")).mean[1], 1),
            "sharp": round(
                ImageStat.Stat(g.filter(ImageFilter.FIND_EDGES)).stddev[0], 1
            ),
            "kb": round(p.stat().st_size / 1024),
        }


def build_pairs_sheet(pairs: list[tuple[pathlib.Path, pathlib.Path]], out: pathlib.Path,
                      cell: int = 300) -> None:
    """上排设计图、下排效果图，逐列对照。"""
    cols = len(pairs)
    pad, lh = 8, 24
    W = cols * cell + (cols + 1) * pad
    H = 2 * cell + lh * 3 + pad * 3
    sheet = Image.new("RGB", (W, H), (243, 245, 248))
    d = ImageDraw.Draw(sheet)
    d.text((pad, pad // 2 + 4), "上排：设计图（生成）      下排：效果图（合成到玻璃）",
           fill=(60, 70, 85))
    for i, (gen, eff) in enumerate(pairs):
        x = pad + i * (cell + pad)
        for row, src in enumerate((gen, eff)):
            y = lh + pad + row * (cell + lh + pad)
            if src and src.exists():
                with Image.open(src) as im:
                    im = im.convert("RGB")
                    im.thumbnail((cell, cell), Image.Resampling.LANCZOS)
                    sheet.paste(im, (x + (cell - im.width) // 2,
                                     y + (cell - im.height) // 2))
            tag = "设计图" if row == 0 else "效果图"
            d.rectangle([x, y, x + 52, y + 19], fill=(30, 41, 59))
            d.text((x + 6, y + 4), tag, fill=(255, 255, 255))
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out, quality=93)
    print(f"对照图：{out.relative_to(ROOT)}  ({W}×{H})")


def main() -> int:
    batch, store = latest_batch_with_effects()
    gen_dir = store / f"{store.name}生成图"
    eff_dir = ROOT / "output_local_effect_final" / batch.name / store.name
    store_name = store.name.split("_", 1)[-1].replace("门店", "")

    print("=" * 76)
    print("交付包生成")
    print("=" * 76)
    print(f"批次 : {batch.name}")
    print(f"门店 : {store.name}")

    sources = sorted(p for p in gen_dir.glob("*.png") if p.stat().st_size > 100_000)
    if not sources:
        print("❌ 无可用生成图")
        return 1

    DELIVERY.mkdir(parents=True, exist_ok=True)
    dest_gen = DELIVERY / "设计图"
    dest_eff = DELIVERY / "效果图"
    dest_gen.mkdir(exist_ok=True)
    dest_eff.mkdir(exist_ok=True)

    pairs, rows = [], []
    for src in sources:
        eff = eff_dir / src.name
        if not eff.is_file():
            continue
        import shutil
        shutil.copy2(src, dest_gen / src.name)
        shutil.copy2(eff, dest_eff / src.name)
        pairs.append((src, eff))
        rows.append({
            "file": src.name,
            "设计图": metrics(src),
            "效果图": metrics(eff),
        })

    print(f"已复制 {len(pairs)} 组到 {DELIVERY.relative_to(ROOT)}/")

    if pairs:
        build_pairs_sheet(pairs, DELIVERY / "_对照图_设计图vs效果图.jpg")

    # ---------------- 验收清单 ----------------
    if rows:
        eff_avg = {
            k: round(sum(r["效果图"][k] for r in rows) / len(rows), 1)
            for k in ("mean", "contrast", "warm", "sat", "sharp")
        }
        reference = {"亮度": 77.0, "对比度": 56.4, "暖度": 2.400, "饱和": 152.8, "清晰度": 67.1}
        checklist = f"""# 效果图验收清单

生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}
批次：`{batch.name}`
门店：**{store_name}**（{len(pairs)} 张）

---

## 一、交付内容

| 目录 | 内容 |
|------|------|
| `设计图/` | 贴纸设计稿（可印刷） |
| `效果图/` | 合成到门店玻璃的效果图 |
| `_对照图_设计图vs效果图.jpg` | 逐张对照，便于快速评估 |

## 二、本批量化指标（效果图）

| 指标 | 本批均值 | 拼多多同类商品 | 你的真实产品照 |
|------|---------|---------------|---------------|
| 亮度 | {eff_avg['mean']} | 77.0 | 85.1 |
| 对比度 | {eff_avg['contrast']} | 56.4 | 64.4 |
| 暖度 (R/B) | {eff_avg['warm']} | 2.40 | 1.63 |
| 饱和度 | {eff_avg['sat']} | 152.8 | 110.5 |
| 清晰度 | {eff_avg['sharp']} | 67.1 | 39.9 |

## 三、建议你重点看这几项

请按下面的清单逐项确认，任何一项不符合就记下来，我据此调整：

- [ ] **文字是否正确**：主标题「{store_name}」与副标题是否逐字正确、无错字/多余字
- [ ] **版式是否合用**：主标题够不够大、副标题横幅是否清晰、整体层级是否一眼看懂
- [ ] **配色是否符合行业**：这套是蓝色系（房屋中介惯例）；若你的客户更认暖色，需切换
- [ ] **贴纸形状**：异形模切轮廓是否好裁切、有没有过细的突起
- [ ] **效果图的真实感**：贴纸像不像真的贴在玻璃上（重点看边缘与反光）
- [ ] **背景是否合用**：当前用的是 AI 生成的门店背景（**不是实拍**），
      若你有自己的门店实拍照片，效果图会更真实
- [ ] **清晰度**：缩小到手机屏幕尺寸后，主标题是否仍然清楚

## 四、重要说明（合规）

- 效果图的背景为 **AI 生成**（`ai_generated_background`），**不是真实门店实拍**。
  批次清单里已明确标记，请勿把它当作实拍图对外宣传。
- 若要用于正式上架，建议**上传你自己的门店实拍照片**重新合成，
  那样背景为 `real_photo`，亮度与反光都会天然正确。

## 五、下一步

拿到实际反馈后，我可以据此调整：
- 「效果图微调」里的 17 个参数（网页上实时预览）
- 生成图提示词模板（`app/prompt_profiles.py`）
- AI 背景提示词（`app/effect_background.py`）
"""
        (DELIVERY / "验收清单.md").write_text(checklist, encoding="utf-8")
        print(f"验收清单：{(DELIVERY / '验收清单.md').relative_to(ROOT)}")

    # ---------------- 打包 ----------------
    zpath = DELIVERY / f"门店贴纸验证包_{store_name}_{time.strftime('%Y%m%d')}.zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(DELIVERY.rglob("*")):
            if p.is_file() and p != zpath and p.suffix != ".zip":
                z.write(p, p.relative_to(DELIVERY))
    print(f"压缩包：{zpath.relative_to(ROOT)}  ({round(zpath.stat().st_size/1024/1024,1)} MB)")
    print("=" * 76)
    return 0


if __name__ == "__main__":
    sys.exit(main())
