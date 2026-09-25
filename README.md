# 门店贴纸图片自动生成智能体

为 23 类门店生成彩色玻璃静电贴设计图，并可把设计图合成到门店玻璃照片中，方便查看实际安装效果。网页版默认运行在 `http://127.0.0.1:8000`。

## 快速开始

```powershell
cd "E:\1_Software\6_AI工具\deepseek\2_开发\图片生成"
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py web --host 127.0.0.1 --port 8000
```

也可以双击 `启动图片生成智能体.vbs`。首次使用时，在网页“设置 → 模型服务”中填写并保存所选服务商的 API Key；密钥只保存在本机 `config/settings.json`，不得提交、复制或写入批次清单。

## 主要能力

- 在“生成范围”选择门店，主界面只展示当前选择的门店；新批次只生成已选择的图片。
- 支持千问、OpenAI GPT Image、Gemini、Seedream、可灵、智谱与本地 FLUX（具体功能随服务商能力显示）。
- 支持 1:1、4:5、3:4、16:9、9:16 等生成比例；批次会冻结比例、范围、模型、提示词和参考图版本。
- 支持文生图、图生图、多图生图；参考图保存到本机并按哈希去重。
- 提示词模板保证中文主标题、副标题、行业主体、白色不透明背景和可制作贴纸构图；真实感分为 V1/V2/V3，后续新批次自动逐级提升，最高 V3。
- 每个门店输出“生成图”和“效果图”两部分。效果图保留原始贴纸设计，再合成至玻璃门店场景。

## 真实门店效果图

在“设置 → 文件与保存”上传手机拍摄的门店玻璃照片。照片只保存在：

```text
<输出根目录>\_effect_backgrounds
```

生成效果图时系统以该照片为背景，并保留贴纸的轮廓、文字和图案。未上传照片时，系统使用“模拟门店背景”，页面与清单会明确标记为模拟，不能当作真实门店实拍效果。

## 目录说明

```text
app/                         Python 服务、服务商、提示词、效果图渲染
app/web/static/              网页界面
config/settings.json         本机设置和 API Key（敏感，禁止共享）
data/stores.json             门店与六张主题数据
output/batch_*/              每个不可变的生成批次
  <门店>/ <门店>生成图/       原始贴纸 PNG 与门店 manifest
  <门店>/ <门店>效果图/       安装效果 PNG 与效果 manifest
output/_references/          图生图参考素材
output/_effect_backgrounds/  本机门店玻璃照片
tests/                       离线单元和集成测试
desktop/                     桌面应用与安装包构建脚本
```

## 验证

以下命令不调用付费图像 API：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m compileall -q app main.py
node --check app\web\static\app.js
```

当前桌面安装包为 `门店贴纸图片生成智能体桌面安装包_v2.0_20260919.exe`；部署说明见 [LOCAL-DEPLOYMENT.md](LOCAL-DEPLOYMENT.md)，多模型环境说明见 [DEPLOYMENT-MODELS.md](DEPLOYMENT-MODELS.md)。

> **开发范围说明（2026-09-20 起）**：后续以**网页版为唯一交付目标**，**不再更新桌面安装包**。
> 上面的安装包冻结为历史版本，仍可正常安装使用，但新功能只保证网页版可用。
> 详见 [AGENTS.md](AGENTS.md) 的「开发范围」一节。

## 给其他 AI 的入口

- Codex：读取根目录 [AGENTS.md](AGENTS.md)。
- DeepSeek：阅读 [DEEPSEEK_HANDOFF.md](DEEPSEEK_HANDOFF.md)，并把其中的“接续任务提示词”连同需求发送给 DeepSeek。
- 任意工具：读取机器可读的 [PROJECT_CONTEXT.json](PROJECT_CONTEXT.json)。

这些文件不含 API Key、请求内容或本机个人图片。
