# 印刷 TIF 自动导出流水线 —— 开发规划

> 依据：用户需求（7 项技术要求 + 工程约束）
> 状态：**规划阶段，等开始命令**
> 已完成的准备工作：依赖安装（部分）、环境核查

---

## 〇、环境核查结论（改变方案的事实）

| 项 | 状态 | 影响 |
|---|------|------|
| Pillow 12.3.0 | ✅ 含 `ImageCms` + CMYK 模式 | **无需再装 littlecms** —— `ImageCms` 就是 lcms2 的 Python 绑定 |
| numpy 2.5.3 | ✅ 已装 | 满足 |
| opencv-python-headless 5.0.0.93 | ✅ 已装 | 满足刀模轮廓需求 |
| scipy 1.18.1 / PyMatting | ✅ 随 rembg 装入 | 满足 |
| **rembg 2.0.84** | ⚠️ **装了但不可用** | **缺 `onnxruntime`** —— 导入即报错：<br>`No onnxruntime backend found` |
| **项目现有去背能力** | ❌ **没有** | 只有 `effect_renderer._feather_alpha()` 做边缘羽化，**不是抠图** |
| **系统 ICC** | ✅ 找到 `RSWOP.icm`（CMYK）+ `sRGB Color Space Profile.icm` | 可直接用作默认色彩配置 |

### ⚠️ rembg 的实际状态

```
>>> import rembg
No onnxruntime backend found.
Please install rembg with CPU or GPU support:
    pip install "rembg[cpu]"
```

**它已经装进了 20+ 个传递依赖**（scipy / scikit-image / numba / llvmlite / pymatting / tqdm…），但**核心推理后端缺失**，等于装了个空壳。

---

## 一、需求梳理

### 输出结构

```
output/<批次>/<门店>/
├── <门店>生成图/          ← 现有 RGB PNG（**不动**）
├── <门店>效果图/          ← 现有合成图（**不动**）
└── 印刷TIF/               ← 【新增】
    ├── <门店>_CMYK.tif       彩色层（印刷主版）
    ├── <门店>_白墨.tif       白墨层（玻璃贴必印）
    ├── <门店>_刀模.tif       刀模线层（不印色）
    ├── <门店>_合并预览.tif   三层合并（人工核对）
    └── print_manifest.json   尺寸/DPI/ICC/出血/色卡记录
```

### 7 项技术要求的落地方式

| # | 要求 | 方案 | 依赖 |
|---|------|------|------|
| 1 | 去背抠图 + 1px 收边 | rembg(u2net) 或降级纯色去背 → Alpha 腐蚀 1px | rembg **或** Pillow |
| 2 | 白墨层 | 由 Alpha 生成，**极性待确认（见 D1）** | Pillow |
| 3 | sRGB→CMYK + 偏色保护 | `ImageCms` + ICC profile | Pillow（已具备） |
| 4 | 刀模 + 3mm 出血 | OpenCV 找轮廓 → 外扩 → 红色描边 | opencv（已装） |
| 5 | 300 DPI 重采样 | LANCZOS，**但需讨论（见 D2）** | Pillow |
| 6 | 打包 TIF | LZW 压缩、CMYK 模式 | Pillow |
| 7 | print_manifest.json | 记录全部参数 | - |

---

## 二、四个关键决策点（需要你确认）

### 🔴 D1：白墨层的**极性**（最容易做反的一点）

你的描述：

> 透明区域=全白、不透明区域按原图轮廓出白墨版

**这与印刷厂惯例相反**，需要确认是哪种：

**理解 A（印刷惯例，我推荐）**

> 白墨层是"**哪里要印白墨**"的版。
> 玻璃静电贴需要**在整个图案下面垫一层白墨**，否则彩色墨水在透明玻璃上不显色。
>
> ```
> 不透明区域（图案本身）→ 实色（K=100 / 白）   ← 这里印白墨
> 透明区域（图案之外）  → 空白（不印）
> ```

**理解 B（字面理解）**

> 透明区域全白、不透明区域出轮廓线 —— 这更像"**反相版**"或"**留白版**"，
> 一般用于"满版白墨 + 图案处镂空"的工艺。

**我的建议**：**按 A 实现**，但在 `print_manifest.json` 里记录 `white_ink_polarity: "positive"`，
并在 `settings` 里提供 `print_white_ink_invert` 开关，**两种都能出**。

**请确认走 A / B / 两者都要**。

---

### 🔴 D2：目标分辨率 —— 这里有个**物理限制**

| 参数 | 数值 |
|------|------|
| 贴纸实际宽度 | 60 cm（默认，可配） |
| 目标 DPI | 300 |
| **需要的像素宽** | 60 ÷ 2.54 × 300 = **7087 px** |
| **AI 源图实际** | **1024 px** |
| **放大倍数** | **6.9 倍** |

**问题**：插值放大**不会增加真实细节**。7087px 的 TIF 尺寸达标，但**印刷出来的清晰度取决于源图的 1024px**（等效约 43 DPI）。

**三个选项**：

| 选项 | 做法 | 优点 | 缺点 |
|------|------|------|------|
| **A** | 硬放大到 7087px | 满足印刷厂"300DPI"的形式要求 | 6.9 倍插值，边缘发虚，**治标不治本** |
| **B** | 按源图原始分辨率导出，manifest 建议成品尺寸 | 诚实，不损失 | 不满足"300DPI"字面要求 |
| **C（推荐）** | **A + 在 manifest 里同时记录"标称 DPI"与"有效 DPI"** | 尺寸满足要求，**且明确告知真实清晰度** | 需要在 manifest 里解释 |

**C 的具体做法**：

```json
{
  "nominal": { "width_cm": 60, "dpi": 300, "pixels": 7087 },
  "effective": { "source_pixels": 1024, "effective_dpi": 43.3 },
  "note": "模板按 300DPI 输出；因源图 1024px，实际清晰度等效约 43DPI。若要真正 300DPI，需生成时选择更高分辨率模型或启用超分。"
}
```

**另外**：7087×7087×4 通道 ≈ **200 MB 内存/张**。批量导出需要**串行 + 及时释放**。
可加 `print_max_pixels` 上限（默认 8000）防止极大尺寸把内存打爆。

**请确认走 A / B / C**。

---

### 🟠 D3：rembg 是否安装（涉及联网下载）

**当前状态**：装了空壳，缺 `onnxruntime`。

**要让它工作**需要：
1. `pip install "rembg[cpu]"` → 拉 **onnxruntime**（约 50 MB）
2. **首次运行自动下载 u2net 模型**（约 **176 MB**，需联网，缓存在 `~/.u2net/`）

**三个选项**：

| 选项 | 说明 |
|------|------|
| **A** | 装上（联网下载 ~226 MB），抠图质量最好 |
| **B（推荐）** | **装，但做成可选**：有 rembg 就用，没有就降级 |
| **C** | 不装，用 Pillow 纯色去背 |

**降级方案（B/C 共用）**：AI 生成的设计图通常**背景纯净**（白底或浅色底），
用「**边缘取样 + 容差 + 连通域**」的纯 Pillow 算法可处理大部分情况。
**质量不如 u2net，但没有依赖、离线可用**。

**请确认 A / B / C**。

---

### 🟠 D4：CMYK ICC profile 用哪个

| 选项 | 说明 |
|------|------|
| **A（推荐）** | 用系统自带的 **`RSWOP.icm`**（美国轮转胶印标准，Windows 自带，无授权问题） |
| **B** | 下载 **ISOcoated_v2_eci.icc**（ECI 免费发布，欧洲胶印标准），放 `config/icc/` |
| **C** | 两者都支持：`config/icc/` 有就优先，没有就用系统 RSWOP |

**都建议**：再加一层**偏色保护**（你要求的第 3 点）：

```
转换后的"亮蓝 / 大红 / 纯白"如果偏离预期阈值 → 局部微调 + 在 manifest 记录警告
```

**请确认 A / B / C**。

---

## 三、模块设计

```
app/print_export/
├── __init__.py          导出入口（export_store / export_batch）
├── config.py            PrintConfig：尺寸/DPI/出血/ICC/开关，从 settings 读
├── layers.py            去背 + 1px 收边 + 白墨层生成
├── cmyk.py              sRGB→CMYK（ImageCms）+ 偏色保护
├── dieline.py           OpenCV 轮廓 + 出血外扩 + 刀模线绘制
└── print_export.py      主编排：串起 4 层 + 写 manifest
```

### 各模块职责

| 模块 | 关键函数 | 说明 |
|------|---------|------|
| `config.py` | `PrintConfig.from_settings()` | 默认 60cm / 300DPI / 3mm 出血 / LZW；全部可被 `settings` 覆盖 |
| `layers.py` | `cutout()` / `tighten_alpha()` / `make_white_ink()` | 去背 → 1px 腐蚀收边 → 白墨层 |
| `cmyk.py` | `to_cmyk()` / `protect_colors()` | ICC 转换 + 亮蓝/大红/白底阈值保护 |
| `dieline.py` | `find_outline()` / `expand()` / `draw_dieline()` | 轮廓 → 外扩 3mm → 红色描边（**闭合校验**） |
| `print_export.py` | `export_store()` | 编排 + 输出 4 个 TIF + manifest |

---

## 四、集成点

| 位置 | 改动 | 说明 |
|------|------|------|
| `app/config.py` | 加字段 | `print_export_enabled`（默认 True）· `print_width_cm` · `print_dpi` · `print_bleed_mm` · `print_icc_path` · `print_white_ink` · `print_dieline` |
| `app/orchestrator.py` | 批次完成后调用 | **失败不阻断**，记日志 + batch manifest 标 `print_export=failed` |
| `app/web/server.py` | 2 个接口 | `POST /api/print-export/<batch_id>/<store_index>`<br>`GET /api/print-export/<batch_id>` |
| `app/web/access_rules.py` | 1 行 | `("*", "/api/print-export", "batch.export")` |
| `batch manifest` | 加字段 | `print_export: {status, dir, files, ...}` |
| `requirements.txt` | 3 行 | `opencv-python-headless==5.0.0.93` · `numpy==2.5.3` · `rembg==2.0.84`（可选） |

---

## 五、实施顺序

| 步 | 内容 | 依赖决策 | 预估 |
|----|------|---------|------|
| 1 | `config.py` + settings 字段 | - | 30 min |
| 2 | `layers.py`（去背 + 降级算法 + 白墨层） | **D1 / D3** | 90 min |
| 3 | `cmyk.py`（ICC + 偏色保护） | **D4** | 60 min |
| 4 | `dieline.py`（轮廓 + 出血 + 闭合校验） | - | 60 min |
| 5 | `print_export.py`（编排 + manifest） | **D2** | 70 min |
| 6 | orchestrator 集成（开关 + 失败不阻断） | - | 40 min |
| 7 | 2 个接口 + 权限 | - | 40 min |
| 8 | 测试（正常 5 项 + 降级 3 项） | - | 70 min |
| 9 | 验收（三项必跑 + 真实批次试跑） | - | 40 min |
| | **合计** | | **~8 h** |

---

## 六、测试计划

### 正常用例（用 tests/ 里已有合成 PNG）

| # | 用例 | 断言 |
|---|------|------|
| 1 | 生成 4 个 TIF | 文件都存在 |
| 2 | CMYK 模式正确 | `Image.mode == "CMYK"` |
| 3 | 白墨层尺寸一致 | 与彩色层同宽高 |
| 4 | 刀模轮廓闭合 | 首尾点距离 < 阈值 |
| 5 | manifest 字段齐全 | 7 类字段都在 |
| 6 | **像素尺寸符合预期** | `px == cm / 2.54 * dpi`（容差 ±2） |
| 7 | 1px 收边生效 | Alpha 边缘向内收缩 |
| 8 | 不改动源 PNG | 源文件 mtime 与 hash 不变 |

### 降级用例

| # | 用例 | 期望 |
|---|------|------|
| 9 | 源 PNG 缺失 | 优雅报错，不抛栈 |
| 10 | ICC 文件缺失 | 降级为朴素转换 + manifest 记警告 |
| 11 | 无 rembg | 降级纯色去背，仍产出 4 个 TIF |
| 12 | 极大尺寸 | 受 `print_max_pixels` 限制，不 OOM |

---

## 七、风险与对策

| # | 风险 | 对策 |
|---|------|------|
| R1 | **6.9 倍插值 → 印刷发虚** | manifest 记录有效 DPI（D2-C）；建议后续接超分模型 |
| R2 | **白墨极性做反 → 整批报废** | D1 确认 + settings 开关 + **首张人工核对** |
| R3 | 7087px × 4 通道 ≈ 200MB 内存 | 串行处理、及时 `close()`、`print_max_pixels` 上限 |
| R4 | rembg 模型下载失败 | 降级纯色去背（D3-B） |
| R5 | CMYK 转换后偏色 | 偏色保护 + manifest 记录警告 + **首件人工校色** |
| R6 | 输出目录污染现有数据 | **只写 `印刷TIF/` 子目录**，绝不触碰生成图/效果图 |
| R7 | 批量导出拖慢主流程 | 批次完成后异步执行 + 失败不阻断 |

---

## 八、验收标准

```
✅ 输出 4 个 TIF + print_manifest.json
✅ CMYK 模式正确，LZW 压缩
✅ 刀模轮廓闭合、红色描边、3mm 出血
✅ 白墨层与彩色层像素尺寸一致
✅ manifest 字段齐全（源路径/尺寸/DPI/ICC/出血/开关/时间/版本号）
✅ 现有 PNG 与效果图**字节级不变**
✅ 批次 manifest 记录 print_export 状态
✅ 2 个接口可用（权限 batch.export）
✅ 12 条测试通过
✅ .\.venv\Scripts\python.exe -m unittest discover -s tests -v
✅ .\.venv\Scripts\python.exe -m compileall -q app main.py
✅ node --check app\web\static\app.js
```

---

## 九、等你确认

| # | 问题 | 选项 |
|---|------|------|
| **D1** | 白墨层极性 | **A** 印刷惯例（不透明区=印白墨）（推荐）<br>**B** 字面理解（透明区=全白）<br>**C** 两者都要（settings 开关） |
| **D2** | 目标分辨率 | **A** 硬放大到 7087px<br>**B** 按源图原始分辨率<br>**C** A + manifest 标注有效 DPI（推荐） |
| **D3** | rembg | **A** 装上（联网 ~226MB）<br>**B** 可选安装 + 降级（推荐）<br>**C** 不装，纯 Pillow 去背 |
| **D4** | CMYK ICC | **A** 系统 RSWOP.icm（推荐）<br>**B** 下载 ISOcoated_v2_eci<br>**C** 两者都支持（推荐） |

**说「开始」+ D1~D4 的选择，我就动手。**

另：当前已装了 `numpy` / `opencv-python-headless` / `rembg`（空壳）及其 20+ 传递依赖。
若 D3 选 C，我可以把 rembg 及其专属依赖卸载干净，避免污染环境。
