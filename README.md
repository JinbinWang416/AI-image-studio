# 门店贴纸图片自动生成智能体（架构重构版）

为 23 类门店生成彩色玻璃静电贴设计图，支持把设计图合成到门店玻璃照片中查看安装效果，
并可按需导出可直接送印的 CMYK 分色 TIF。

> **本目录是架构重构版**，对应仓库的 `arch-refactor` 分支。
> 生产版在同一仓库的 `main` 分支。两版**行为等价、用户可见行为完全一致**，
> 差别只在内部代码组织（本版为模块化单体 + package-by-feature）。
> 新功能仍以 `main` 为准，本版用于验证重构不改变行为。

网页版默认运行在 `http://127.0.0.1:8001`（生产版用 `8000`，便于两版并行对照）。

## 快速开始

```powershell
cd "E:\1_Software\6_AI工具\deepseek\2_开发\AI生图架构版"
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py web --host 127.0.0.1 --port 8001
```

也可以双击 `启动图片生成智能体.vbs`。

首次使用时，在网页「设置 → 模型服务」中填写并保存所选服务商的 API Key。
密钥只保存在本机 `config/settings.json`，**不得提交、复制或写入批次清单**。

## 主要能力

- **范围与批次**：在「生成范围」选择门店，主界面只展示当前选择的门店；新批次只生成已选择的图片。
  批次会冻结比例、范围、模型、提示词和参考图版本，后续改设置不会影响它。
- **三种生成模式**：文生图、图生图、多图生图；参考图保存在本机并按哈希去重。
- **生成比例**：1:1、4:5、3:4、16:9、9:16 等。
- **提示词模板**：保证中文主标题、副标题、行业主体、白色不透明背景和可制作贴纸构图；
  真实感分 V1/V2/V3，新批次自动逐级提升，最高 V3。
- **双份输出**：每个门店同时产出「生成图」与「效果图」；效果图保留原始贴纸设计，再合成到玻璃场景。
- **印刷导出**：按门店把生成图导出为四层印刷 TIF（见下方专节）。
- **失败可恢复**：余额不足、鉴权失败、限流、超时、内容拒绝等都会保留未完成任务并给出可恢复提示，
  不做无意义重试。

## 服务商支持

| 服务商 | 标识 | 状态 |
|---|---|---|
| 阿里云百炼（千问） | `qwen` | ✅ 已实现，默认 `qwen-image-3.0` |
| OpenAI GPT Image | `openai` | ✅ 已实现 |
| Google Gemini | `gemini` | ✅ 已实现 |
| 字节 Seedream | `seedream` | ✅ 已实现 |
| FLUX.2 klein 4B | `flux_local` | ✅ 本机验证用 |
| 本地模拟 | `mock` | ✅ 离线测试用，不调用任何外部 API |
| 自定义 OpenAI 兼容 | `custom` | ✅ 复用 OpenAI 适配器（图生图能力同样生效） |
| 快手可灵 | `kling` | ⚠️ 已预留配置，**适配器尚未实现** |
| 智谱 GLM | `zhipu` | ⚠️ 已预留配置，**适配器尚未实现** |

前端可调用 `GET /api/providers/capabilities` 查询各服务商的实时能力
（是否支持图生图 / 多图生图、最大参考图数量、可用模型列表），不会静默降级。

## 印刷 TIF 导出

对已生成的批次按门店导出四层 300 DPI TIF：

| 文件 | 用途 |
|---|---|
| `<门店>_<序号>_CMYK.tif` | 彩色层，印刷主版。走 **ICC** 做 sRGB → CMYK，并对白底 / 亮蓝 / 大红三类做偏色保护 |
| `<门店>_<序号>_白墨.tif` | 白墨层（玻璃静电贴必印）。单通道 `L`，**255 = 印白墨** |
| `<门店>_<序号>_刀模.tif` | 刀模线层。白底 + 红色描边、**不填色**，含出血，供模切机沿轮廓切割 |
| `<门店>_<序号>_合并预览.tif` | 三层套准预览：棋盘格底 + 彩色层 + 白墨边界（绿）+ 刀模线（红） |

另有 JPEG 缩略图，便于在网页上快速核对。

- 接口：`GET /api/print-export/{batch_id}` 查状态，`POST /api/print-export/{batch_id}/{store_index}` 执行导出
- CMYK 转换依赖系统颜色目录里的 ICC profile（`C:\Windows\System32\spool\drivers\color`），
  文件名需为下列之一（按优先级）：`RSWOP.icm`、`USWebCoatedSWOP.icc`、`ISOcoated_v2_eci.icc`、
  `CoatedFOGRA39.icc`、`default_cmyk.icc`。找不到时会降级为朴素转换，
  并**在 manifest 里写入警告**（颜色会有偏差，建议向印刷厂索取匹配的 profile）
- ⚠️ 白墨极性按**印刷惯例**（不透明区印白墨）。这一项做反会导致整批报废，
  工艺需要「满版白墨 + 图案镂空」时用 `print_white_ink_invert` 反转
- 导出**不修改**任何原始 PNG，只在门店目录下新增 `印刷TIF/` 与 `预览/` 子目录，并写入 `print_manifest.json`

## 真实门店效果图

在「设置 → 文件与保存」上传手机拍摄的门店玻璃照片。照片只保存在：

```text
<输出根目录>\_effect_backgrounds
```

生成效果图时系统以该照片为背景，并保留贴纸的轮廓、文字和图案。
未上传照片时使用「模拟门店背景」，页面与清单会**明确标记为模拟**，不能当作真实门店实拍效果。

## 管理后台与安全

访问 `/admin`（默认账号 `admin`）。已实现：

- **账号与权限**：argon2id 密码哈希 + 角色 / 权限码，`require_permission` 作为唯一授权入口
- **MFA**：TOTP 双因素认证
- **审计**：敏感字段自动脱敏后才落日志
- **备份**：AES-256-GCM 加密备份与还原
- **配置写入防线**：测试进程内不碰真实 `config/settings.json`（见 `tests/test_settings_guard.py`）
- **自检**：四方权限码一致性自检（`tools/run_selfcheck.py`）

密钥仅以掩码形式返回前端。`.env`、`config/settings.json*`、`data/security/` 均已在 `.gitignore` 中排除。

## 目录说明（分包结构）

```text
app/core/                    配置分层（12 子配置 + 57 个兼容 @property）与路径
app/state/                   设置存储、批次 manifest、运行记录
app/security/                账号、权限、会话、审计、加密备份
app/generation/              编排器（批量生成主流程）
app/providers/               服务商适配器（qwen / openai / gemini / seedream / flux_local / mock）
app/prompt/                  提示词模板、质量档位、真实感等级、优化器
app/effect/                  效果图合成与背景匹配
app/print_export/            印刷导出（CMYK / 白墨 / 刀模 / 合并预览）
app/validation/              本地与专业验证流程
app/local_service/           本地 FLUX 服务
app/web/                     HTTP 接口与网页静态资源（含管理后台）
app/*.py                     22 个转发模块（from .pkg.mod import *），旧导入路径不破坏

config/settings.json         本机设置与 API Key（敏感，已 gitignore）
data/stores.json             23 个门店与六张主题数据
data/security/               账号、会话与备份密钥（含密码哈希，已 gitignore）
output/batch_*/              每个不可变的生成批次
  <门店>/ <门店>生成图/       原始贴纸 PNG 与门店 manifest
  <门店>/ <门店>效果图/       安装效果 PNG 与效果 manifest
  <门店>/印刷TIF/             四层印刷文件与 print_manifest.json
  <门店>/预览/                合并预览 JPEG 缩略图
output/_references/          图生图参考素材（跨批次共享）
output/_effect_backgrounds/  本机门店玻璃照片（跨批次共享）
tests/                       离线单元与集成测试
tools/                       自检、验证、打包与运维脚本
_backup_phase0/              Phase 0 重构前的 config.py 原件（已 gitignore）
_backup_phase1/              Phase 1 分包前的原始 app/ 副本（已 gitignore）
```

## 验证

以下命令**不调用付费图像 API**：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v      # 320 个测试
.\.venv\Scripts\python.exe -m compileall -q app main.py
node --check app\web\static\app.js
.\.venv\Scripts\python.exe tools\run_selfcheck.py                # 权限一致性自检
.\.venv\Scripts\python.exe tools\check_secrets.py --glob config  # 推送前密钥扫描
```

比生产版多出的 26 个测试集中在 `tests/test_config_mapping.py`：逐字段断言
「12 个子配置 + 兼容 `@property`」与旧版 57 个平铺字段完全等价。

## 架构重构说明

**Phase 0 —— Config 分层**：把 57 个平铺字段拆成 12 个子配置，
并保留 57 个兼容 `@property`，因此 `cfg.provider`、`cfg.output_root` 等旧写法全部照常工作。
子配置字段名用 `providers`（复数），因为 `cfg.provider` 已被占用为字符串。
另提供 `with_config()` / `make_config()` 用于生成改动后的副本。

**Phase 1 —— 分包**：把 21 个文件迁入 10 个业务包，旧位置保留转发模块，
外部导入路径（含测试与工具脚本）无需改动。

⚠️ 两个已知坑（都已踩过并记录在代码注释里）：

1. `from .pkg.mod import *` **不带下划线开头的私有名**，且模块级变量是**副本** ——
   `patch.object(转发模块, "变量")` 无效，必须 patch 真实现。
2. `app/core/paths.py` 的 `_package_root()` **锚定 `app` 包**，不再用
   `Path(__file__).parent.parent`。分包后文件深度会变，按深度上溯会把
   `data/`、`output/`、`config/` 全部指错（实测触发 24 个测试失败）。

## 给其他 AI 的入口

- Codex：读取根目录 [AGENTS.md](AGENTS.md)。
- DeepSeek：阅读 [DEEPSEEK_HANDOFF.md](DEEPSEEK_HANDOFF.md)，并把其中的「接续任务提示词」连同需求发送给 DeepSeek。
- 任意工具：读取机器可读的 [PROJECT_CONTEXT.json](PROJECT_CONTEXT.json)。

这些文件不含 API Key、请求内容或本机个人图片。
