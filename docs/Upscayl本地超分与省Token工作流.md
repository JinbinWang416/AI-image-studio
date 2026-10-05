# Upscayl 本地超分与省费工作流

把 Upscayl（upscayl-ncnn CLI，Real-ESRGAN 引擎，本地 Vulkan GPU 推理，**免费**）
接进生成与印刷两条链路，用「低价档生成 + 本地免费放大」替代「买高价尺寸档」。

---

## 1. 省费逻辑

23 店 × 6 贴纸 = **138 张/轮**（以 qwen-image ¥0.25/张 计）：

| 付费点 | 次数/轮 | 现状 | 集成后 | 手段 |
|---|---|---|---|---|
| 贴纸生成 | 138 | ¥34.5 | ¥0~5.5 | 低价/免费档打底 + 本地放大 |
| 效果图背景 | 23 | ¥5.75 | ¥0.92 | 背景走低价档 |
| 专业验证候选 | 18 | ¥4.5 | ¥0.24 | `candidates_per_theme` 3→1 |
| 清晰度重跑 | 不定 | 隐性 | 0 | effective_dpi 提上去后不再触发警告 |
| **合计** | | **≈¥48** | **≈¥1.7~7.2** | **省 85%~97%** |

**关键**：省费靠的是「低价档 + 本地放大」，**不是**去开高价尺寸档
（`server.py` 的 `_ALLOWED_SIZES` 白名单**刻意保持不动**）。

---

## 2. 架构

```
app/upscayl/
├── __init__.py     对外导出
├── engine.py       UpscaylEngine：定位二进制 / 构造命令 / 执行 / 降级
├── alpha.py        RGBA 拆合：alpha 单独放大并二值化
└── models.py       模型清单 + 默认值

tools/upscayl/      二进制 + 模型（随项目分发，51.56 MB）
tools/upscayl_compare.py   文字边缘验收门禁（人工审查用对比图）
```

**两条链路**：

| 链路 | 接入点 | 产物 | 开关 |
|---|---|---|---|
| 生成图 | `generation/orchestrator._maybe_upscale()` | `<stem>_upNx.png`（**母版不动**） | `upscayl.upscale_generated` |
| 印刷 | `print_export._upscale_rgba()` + `resolve_print_source()` | 有放大版时优先用它当源 | `upscayl.upscale_print` |

**总开关**：`upscayl.enabled=False` 一键回到改动前的行为（默认就是关）。

---

## 3. 安装

`tools/upscayl/` 已随项目就位，**无需额外安装**。包含：

- `upscayl-bin.exe`（20251207-174704，7.7 MB）+ `LICENSE`
- `models/`：5 组模型（`.bin` + `.param` 成对）

**前置**：显卡需支持 **Vulkan**。设置页点「测试」会真跑一次小图，
缺驱动/显存不足会当场报错，而不是等批次跑到一半。

---

## 4. ⚠️ 模型选型（实测结论，与初版文档不同）

3 张真实贴纸 × 4 模型，tile=128：

| 模型 | 平均 SSIM | 锐度比 | 判定 |
|---|---|---|---|
| **`realesr-animevideov3-x4`** | **0.9784** | 2.5 | ✅ **生成图默认** |
| `realesrgan-x4plus` | 0.9692 | 4.5 | ✅ **印刷默认** |
| `realesrgan-x4plus-anime` | 0.9605 | 6.1 | ✅ 可用 |
| `realesr-animevideov3-x2` | **0.5747** | 8.2 | ❌ **不可用** |

### 🔴 `realesr-animevideov3-x2` 会产出严重平铺伪影

同一块内容被复制多次、颜色错乱、笔画重复错位。换 tile（128/256/512）
**都一样坏**，且 `.bin` 与同系列 x4 大小相同 —— 所以不是下载损坏，
是**这一个模型**本身的问题。已在 `models.py` 里标注并从默认值移除。

### ⚠️ 「锐度比」这个指标会误判

x2 的锐度比高达 **8.2**（门槛只要 0.90），看起来"最清晰" ——
那是**平铺伪影制造的虚假高频**。

**只信锐度比会把它选成最佳模型。** 必须同时看 SSIM，或直接看对比图。

---

## 5. 两个必须知道的坑

### 坑 1：`-t 0`（auto tile）输出**全黑**图

2048px 输入 + `-t 0` 触发 `vkQueueSubmit failed -4`，
输出**尺寸正常但 mean=0 / stddev=0**。比报错更危险 ——
报错会被发现，全黑图可能一路流到印刷。

**防线**（三层）：`UpscaylEngine._build_cmd()` 强制纠正、
`_validate_upscayl_payload()` 在 API 入口拒绝、前端 `Math.max(32, ...)`。

### 坑 2：alpha 的处理

初版文档记录「CLI 丢弃 alpha」，但**本次实测相反**：
透明背景贴纸 44.7% → 44.4%，alpha 完整保留，甚至生成了抗锯齿边缘。

不过印刷链路（`cutout()` 之后）确实带 alpha，两种行为都无法保证，
所以**仍走 `alpha.py` 的拆合方案**：白底合成出 RGB → 超分 →
alpha 单独 LANCZOS 放大 + **128 阈值二值化** → 合成回 RGBA。

二值化是关键：超分会造出渐变半透明像素，印在玻璃上是**灰边**。

---

## 6. 失败一律降级（硬性约束）

缺二进制 / 无 Vulkan / 超时(120s) / OOM / 非零退出码 ——
**全部回落 LANCZOS，批次绝不中断**，只在 manifest 记
`ErrorCode.UPSCAYL_FAILED` + warning。

> 实测这个设计救过两次场：一次是 `__init__.py` 漏导出，
> 一次是相对导入多写了一层（`...upscayl` 应为 `..upscayl`）——
> 两次都是**导出照常成功**，错误只体现在 `error_code` 上。

---

## 7. FAQ

**Q：开了之后 effective_dpi 还是没到 300？**
`effective_dpi = 源图宽 / (宽cm/2.54)`。1024px 印 60cm 只有 43 DPI；
4 倍放大后 4096px 是 173 DPI。要真正到 300 需要生成时就出更大尺寸，
或提高 `print_max_pixels`。超分能显著改善，但不是无限的。

**Q：放大版会覆盖原图吗？**
**不会。** 另存 `<stem>_upNx.png`，母版一个字节都不动
（AGENTS.md 硬规则：批次目录文件绝不覆盖）。幂等 —— 重跑只覆盖同名放大版。

**Q：能只对印刷生效、不影响生成图吗？**
能。`upscale_generated` 与 `upscale_print` 是两个独立开关。

**Q：为什么锐度比那么高还说模型坏？**
见 §4 —— 平铺伪影会制造虚假高频。以 SSIM 和**人眼看对比图**为准。

**Q：怎么回滚？**
`upscayl.enabled=False`（默认值）。改设置页总开关即可，无需改代码。

---

## 8. 验收命令

```powershell
# 模块单测（全 mock，不真调 CLI）
.\.venv\Scripts\python.exe -m unittest tests.test_upscayl

# 真跑一次可用性自检
.\.venv\Scripts\python.exe -c "from app.upscayl import UpscaylEngine as E; print(E().selftest())"

# 文字边缘验收（产出对比图，必须人工看一眼中文笔画）
.\.venv\Scripts\python.exe tools\upscayl_compare.py --model realesr-animevideov3-x4
.\.venv\Scripts\python.exe tools\upscayl_compare.py --model realesrgan-x4plus
```

对比图输出到 `output/_upscayl_smoke/`，`_side.png` 是并排，
`_zoom.png` 是文字区域局部放大。
