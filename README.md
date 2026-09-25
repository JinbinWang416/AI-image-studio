# 门店贴纸图片自动生成智能体

为 23 类门店生成彩色玻璃静电贴设计图，支持把设计图合成到门店玻璃照片中查看安装效果，
并可按需导出可直接送印的 CMYK 分色 TIF。网页版默认运行在 `http://127.0.0.1:8000`。

## 快速开始

```powershell
cd "E:\1_Software\6_AI工具\deepseek\2_开发\图片生成"
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py web --host 127.0.0.1 --port 8000
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
- **CMYK 转换优先用印刷厂的 ICC profile**，查找顺序：
  ① 设置里显式指定的路径 → ② **项目 `config/icc/` 目录**（放 `*.icc` / `*.icm`，推荐做法）→
  ③ 系统颜色目录 `C:\Windows\System32\spool\drivers\color` 下名为 `RSWOP.icm`、
  `USWebCoatedSWOP.icc`、`ISOcoated_v2_eci.icc`、`CoatedFOGRA39.icc`、`default_cmyk.icc` 的文件。
  三处都找不到时会降级为朴素转换，并**在 manifest 里写入警告**（颜色会有偏差）
- ⚠️ ICC profile 通常有版权，**不要提交到仓库**（`config/icc/*.icc` 已在 `.gitignore` 中排除）
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
- **自检**：四方权限码一致性自检（`tools/run_selfcheck.py`）

密钥仅以掩码形式返回前端。`.env`、`config/settings.json*`、`data/security/` 均已在 `.gitignore` 中排除。

## 目录说明

```text
app/                         Python 服务、服务商适配、提示词、效果图渲染
app/print_export/            印刷导出（CMYK / 白墨 / 刀模 / 合并预览）
app/web/static/              网页界面与管理后台
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
```

## 验证

以下命令**不调用付费图像 API**：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v      # 294 个测试
.\.venv\Scripts\python.exe -m compileall -q app main.py
node --check app\web\static\app.js
.\.venv\Scripts\python.exe tools\run_selfcheck.py                # 权限一致性自检
.\.venv\Scripts\python.exe tools\check_secrets.py --glob config  # 推送前密钥扫描
```

## 开发范围

- **以网页版为唯一交付目标**：`main.py web` → `http://127.0.0.1:8000`，前端在 `app/web/static/`。
- **桌面版产物已移除**（2026-09-22）：`desktop/`、根目录 `*.exe`、`delivery/`，共释放约 457 MB。
  构建能力仍保留在 `tools/build_desktop_installer.ps1`、`tools/make_portable_pack.py`、
  `tools/make_delivery_pack.py`，需要时可重新生成。
- **架构重构版在 `arch-refactor` 分支**：模块化单体 + package-by-feature。
  Phase 0 把 Config 的 57 个平铺字段拆成 12 个子配置并保留 57 个兼容 `@property`；
  Phase 1 把 21 个文件迁入 10 个业务包，旧位置保留转发模块。两版**行为等价**，
  用户可见行为一致，新功能仍以 `main` 分支为准。

## 给其他 AI 的入口

- Codex：读取根目录 [AGENTS.md](AGENTS.md)。
- DeepSeek：阅读 [DEEPSEEK_HANDOFF.md](DEEPSEEK_HANDOFF.md)，并把其中的「接续任务提示词」连同需求发送给 DeepSeek。
- 任意工具：读取机器可读的 [PROJECT_CONTEXT.json](PROJECT_CONTEXT.json)。
