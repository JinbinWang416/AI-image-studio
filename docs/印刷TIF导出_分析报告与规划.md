# 印刷 TIF 自动导出 —— 分析报告与开发规划

> 依据：用户需求（目录重组 / 6 步流水线 / 6 个模块 / 2 个接口 / 8 条测试）
> 状态：**规划阶段，等开发命令**（未改动任何文件）
> 上一版规划：`docs/印刷TIF导出_开发规划.md`（本文件为**修订版**，纳入新需求）

---

## 一、现状核查结论

### 文件与机制（全部确认存在）

| 项 | 实情 |
|---|------|
| `app/storage.py` | `Storage` 类，有 `store_dir()` / `generated_dir()` / `effect_dir()` / `save_image()` —— **印刷模块应复用它的路径方法** |
| 批次结构 | `output/<批次>/<门店>/` + `<门店>生成图/` + `<门店>效果图/` + `_manifest.json` |
| **`_manifest.json` 实际字段** | `['version', 'store', 'created_at', 'updated_at', 'summary', 'entries']` |
| **前端取图** | `/api/image?store=…&file=…&view=generated\|effect` → `Storage.generated_dir()` / `effect_dir()` |
| `access_rules.py` | 41 条规则，已有 `("*", "/api/export/", "batch.export")` |
| **批次完成挂载点** | `orchestrator.py:303` 发 `run_finished` 事件处（循环结束、`finished_at` 已设） |
| Pillow | 12.3.0，**已内置 `ImageCms`（lcms2 绑定）+ CMYK 模式** |
| 已装依赖 | numpy 2.5.3 · opencv-python-headless 5.0.0.93 · scipy · scikit-image · numba · PyMatting |
| **rembg** | ⚠️ 2.0.84 **空壳**，缺 `onnxruntime`，导入即报错 |

---

## 二、🔴 必须确认（3 个高危项）

### Q1：`_work/` 移动 PNG 与「不修改现有 output/」**直接冲突**

**需求里有两句互相矛盾的话**：

| 出处 | 原文 |
|------|------|
| 核心原则 | 「不修改、不覆盖任何现有 PNG、效果图和批次 manifest」 |
| 目录结构 | 「PNG 移到内部 `_work/` 目录，印刷厂和用户都看不到」 |

**"移动" = 原位置文件消失**，这与"不修改"冲突。

**而且会连带两个后果**：

1. **前端立刻 404**
   `api_image(view="generated")` 读的是 `Storage.generated_dir(store)`。
   PNG 一旦移走，**现有前端所有缩略图全部裂图**。

2. **历史批次怎么办？**
   现在已有 **20+ 个历史批次**。若一并迁移 = **大规模改动用户数据**（AGENTS.md 明令禁止）；
   若不迁移 = **新旧批次两套结构并存**，后续所有读图代码都要兼容两种。

**三个方案**：

| 方案 | 做法 | 优点 | 缺点 |
|------|------|------|------|
| **A（最稳，推荐）** | **PNG 原地不动**；`_work/` 只放**母版副本或硬链接** | 零破坏，历史批次安全，前端不用改 | 占双倍空间（硬链接可避免） |
| **B（按需求字面）** | 真移动到 `_work/`，前端改读 `预览/*.jpg` | 完全符合需求描述 | **改动现有结构**；历史批次要么迁移要么兼容 |
| **C（折中，推荐）** | **新批次移动，历史批次不动**；`api_image` 加 `view=preview` 并回退旧路径 | 兼顾 | 两套结构短期并存 |

**我倾向 A 或 C**。**请确认**。

> 补充：Windows 硬链接（`os.link`）**不需要管理员权限**（同分区内），
> 可让 `_work/` 与原目录指向同一份数据，**空间不翻倍**，是 A 方案的理想实现。
> 符号链接才需要开发者模式。

---

### Q2：**白墨层极性**（上一版就提出，需求里仍是"透明区=全白"）

**需求原文**：
> 透明区=全白、不透明区按轮廓出白墨版

**印刷惯例是相反的**：

```
白墨层 = "哪里要印白墨" 的版
玻璃静电贴必须在图案下垫白墨，否则彩墨印在透明玻璃上不显色

  不透明区（图案本身）→ 实色    ← 印白墨
  透明区（图案之外）  → 空白    ← 不印
```

**这一项做反 = 整批印刷报废**。

| 选项 | 含义 |
|------|------|
| **A（推荐）** | 不透明区=印白墨，透明区=不印 |
| **B（字面）** | 透明区全白，不透明区出轮廓（适用于"满版白墨+图案镂空"工艺） |
| **C** | 两者都出，settings 开关切换 |

**请确认**。

---

### Q3：**色彩转换不需要 `pillow-lcms2` / `python-lcms2`**

需求要求装 `pillow-lcms2 或 python-lcms2`。实际情况：

| 包 | 状态 |
|----|------|
| `pillow-lcms2` | **PyPI 上不存在这个包名** |
| `python-lcms2` | 存在，但**是独立库**，与 Pillow 功能重叠 |
| **`PIL.ImageCms`** | ✅ **Pillow 12.3.0 已内置**，就是 lcms2 的官方绑定 |

**建议**：**不装额外包**，直接用 `PIL.ImageCms`。
（ICC profile 仍按要求放 `config/icc/`，系统里有 `RSWOP.icm` 可作默认 CMYK。）

**请确认**。

---

## 三、🟠 需要明确（3 个中危项）

### Q4：「印刷确认」动作**需求里没有定义**

需求原文：
> settings 加开关 `print_auto_clean_work`（默认 false）；开启后按"**印刷确认**"动作清理 `_work`

**"印刷确认"是什么？** 三种可能：

| 选项 | 说明 |
|------|------|
| **A** | 新增接口 `POST /api/print-export/<batch>/<store>/confirm` |
| **B** | 前端在批次完成态加一个「印刷已确认」按钮 |
| **C** | 暂不实现，只留 settings 开关（**推荐，先做主体**） |

---

### Q5：分辨率物理限制（7087px vs 源图 1024px）

| 参数 | 数值 |
|------|------|
| 60 cm @ 300 DPI | **7087 px** |
| AI 源图 | **1024 px** |
| 放大倍数 | **6.9 倍** |
| 单张内存 | **约 200 MB**（7087² × 4 通道） |

**插值不会增加真实细节** —— 尺寸达标，但清晰度等效约 **43 DPI**。

**建议**：manifest 里**同时记录标称 DPI 与有效 DPI**，并在报告里列为已知限制。

**请确认是否接受**（若不接受，可选"按源图分辨率导出 + 建议成品尺寸"）。

---

### Q6：`_manifest.json` 的字段挂载位置

实际字段是 `['version','store','created_at','updated_at','summary','entries']`，**没有 `status`**。

需求说"在批次 `_manifest.json` 标 `print_export=failed`"。

**建议**：**加在顶层**（与 `summary` 平级）：

```json
{
  "version": 1,
  "store": {...},
  "print_export": {
    "status": "success|failed|skipped",
    "exported_at": "2026-09-20T21:30:00+08:00",
    "dir": "印刷TIF",
    "files": ["...CMYK.tif", "...白墨.tif", "...刀模.tif", "...合并预览.tif", "print_manifest.json"],
    "error": {"code": "...", "message": "..."},
    "version_tag": "print-v1"
  }
}
```

**请确认**。

---

## 四、🟡 其余需确认项

| # | 问题 | 建议 |
|---|------|------|
| **Q7** | **rembg 是否安装**（需 `onnxruntime` ~50MB + 首次下载 u2net 模型 ~176MB，**要联网**） | **可选安装 + 自动降级纯色去背** |
| **Q8** | **历史批次是否回填导出** | 只对新批次自动导出；历史批次用 `POST` 接口手动重跑 |
| **Q9** | **补专色通道**（需求："默认关闭，settings 开关"） | 本期**只留开关不实现**，避免范围膨胀 |
| **Q10** | 依赖处理：上轮误装的 rembg 空壳 + 20+ 传递依赖 | 若 Q7 选"不装"则一并卸载 |

---

## 五、模块设计（按需求，6 个文件）

```
app/print_export/
├── __init__.py          对外入口：export_store() / export_batch() / status()
├── print_export.py      流水线编排 · 批次挂接 · 失败重试
├── layers.py            去背 · 1px 收边 · 白墨层 · Alpha 处理
├── cmyk.py              ICC 加载 · sRGB→CMYK · 偏色保护（亮蓝/大红/白底）
├── dieline.py           OpenCV 轮廓 · 3mm 出血外扩 · 刀模线层
├── preview.py           合并预览 TIF · JPEG 缩略图（q85，最长边 1200）
└── manifest.py          print_manifest.json 读写 · 批次 manifest 回写
```

**复用而非重写**：
- 路径全部走 `app/storage.py` 的 `Storage.store_dir()` 等方法
- 磁盘错误复用 `app/security/store.py` 的 `StorageFullError`（已有 `code=storage_full`）
- 日志走现有 `app/logger.py`

---

## 六、集成点（4 处）

| 位置 | 改动 | 说明 |
|------|------|------|
| `app/config.py` | +8 字段 | `print_export_enabled`(True) · `print_width_cm`(60) · `print_dpi`(300) · `print_bleed_mm`(3) · `print_icc_path` · `print_white_ink`(True) · `print_dieline`(True) · `print_auto_clean_work`(False) |
| `app/orchestrator.py:303` 附近 | 挂 `run_finished` 后 | **try/except 包裹，失败不阻断** |
| `app/web/server.py` | +2 接口 + `api_image` 加 `view=preview` | 权限 `batch.export` |
| `app/web/access_rules.py` | +2 行 | `("POST", "/api/print-export/", "batch.export")`<br>`("GET", "/api/print-export/", "batch.export")` |

---

## 七、实施顺序（10 步，约 10~12 h）

| 步 | 内容 | 依赖确认项 | 预估 |
|----|------|-----------|------|
| 1 | `config.py` 字段 + `manifest.py` | Q6 | 60 min |
| 2 | `layers.py`（去背 + 收边 + 白墨层） | **Q2 / Q7** | 110 min |
| 3 | `cmyk.py`（ImageCms + 偏色保护） | **Q3** | 70 min |
| 4 | `dieline.py`（轮廓 + 出血 + 闭合校验） | - | 70 min |
| 5 | `preview.py`（合并 TIF + JPEG） | - | 50 min |
| 6 | `print_export.py`（编排 + 目录重组） | **Q1** | 110 min |
| 7 | orchestrator 挂接（开关 + 不阻断） | - | 40 min |
| 8 | 2 接口 + `api_image` 扩展 + 前端徽标 | **Q4** | 90 min |
| 9 | `tests/test_print_export.py`（8 条） | - | 90 min |
| 10 | 三项命令 + 真实批次试跑 + 报告 | - | 60 min |

---

## 八、风险与降级

| # | 风险 | 对策 |
|---|------|------|
| R1 | **白墨极性做反 → 整批报废** | Q2 确认 + settings 开关 + **首件人工核对** |
| R2 | **移动 PNG → 前端裂图 + 历史批次混乱** | Q1 选 A/C；`api_image` 加回退路径 |
| R3 | 6.9 倍插值 → 印刷发虚 | manifest 记录有效 DPI；列为已知限制 |
| R4 | 7087px 内存 200MB | **串行处理** + `print_max_pixels` 上限 + 及时 `close()` |
| R5 | rembg 模型下载失败 | 降级纯色去背（Q7） |
| R6 | CMYK 偏色 | 偏色保护 + manifest 警告 + **首件人工校色** |
| R7 | ICC 缺失 | 回落系统 `RSWOP.icm`；再缺则朴素转换 + 警告 |
| R8 | 磁盘满 | 复用 `StorageFullError` → `code=storage_full`，主流程不崩 |
| R9 | 批量导出阻塞 | 批次后**异步**执行，可中断 |

---

## 九、验收标准

```
✅ 印刷TIF/ 下 4 个 TIF + print_manifest.json
✅ CMYK 模式正确、LZW 压缩
✅ 刀模闭合 + 出血 3mm（按 DPI 换算像素）
✅ 白墨层与彩层像素尺寸一致
✅ 预览 JPEG 最长边 ≤ 1200px
✅ manifest 字段齐全（源 PNG/尺寸/DPI/ICC/出血/版本号）
✅ 降级：源图缺失 / ICC 缺失 / 磁盘满 → 不崩、有错误码
✅ 重跑幂等：重复执行不产生脏文件
✅ 现有 output/ PNG 与效果图**字节级不变**（除非 Q1 选 B）
✅ 现有 217 测试全绿
✅ .\.venv\Scripts\python.exe -m unittest discover -s tests -v
✅ .\.venv\Scripts\python.exe -m compileall -q app main.py
✅ node --check app\web\static\app.js
```

---

## 十、等你确认

| # | 问题 | 我的建议 |
|---|------|---------|
| **Q1** | `_work/` 移动 PNG（与"不改 output"冲突 + 前端 404 + 历史批次） | **A**（原地不动，`_work` 放硬链接）或 **C**（新批次移动、历史兼容） |
| **Q2** | 白墨层极性 | **A**（印刷惯例）或 **C**（两者都要） |
| **Q3** | `pillow-lcms2` / `python-lcms2` | **不装**，用 Pillow 内置 `ImageCms` |
| **Q4** | 「印刷确认」动作 | **C** 本期只留开关 |
| **Q5** | 7087px vs 1024px | 接受，manifest 标注有效 DPI |
| **Q6** | `print_export` 字段位置 | 加在 `_manifest.json` **顶层** |
| **Q7** | rembg | **可选安装 + 降级** |
| **Q8** | 历史批次 | 只对新批次自动导出 |
| **Q9** | 专色通道 | 本期只留开关 |
| **Q10** | 上轮误装的依赖 | 按 Q7 决定是否卸载 |

**说「开始」+ Q1~Q10 的选择（可直接说"全按你建议"），我再动手。**

**当前未改动任何文件；`config/settings.json`、`output/` 均未触碰。**
