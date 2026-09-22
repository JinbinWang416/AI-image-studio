# DeepSeek 开发交接说明

此文件用于让 DeepSeek 接续本项目开发。请先阅读 `README.md`、`PROJECT_CONTEXT.json` 和根目录 `AGENTS.md`；它们与本说明共同构成当前工程上下文。

## 当前已完成状态

- 本地 FastAPI 网页应用可在 `http://127.0.0.1:8000` 运行。
- 用户可在设置中选择 23 个门店；主界面仅显示已选择门店，新批次仅创建所选门店的六张任务。
- 生成比例会写入批次快照；历史批次不会被后续设置改写。
- 已支持提示词质量模板与真实感 V1/V2/V3。新批次逐级提高真实感，最高 V3。
- 每张输出包含独立的“生成图”和“效果图”目录与 manifest。
- 上传手机拍摄的门店玻璃照片后，效果图以该照片为背景进行本地合成；不上传时明确标记为模拟门店背景。
- 支持多服务商配置；所有离线测试都使用 mock，不调用付费 API。

## 接续任务提示词

将下面内容和用户的新需求一起发送给 DeepSeek：

```text
你正在维护 Windows 本地项目“门店贴纸图片自动生成智能体”。请先读取根目录 README.md、AGENTS.md、PROJECT_CONTEXT.json 和本文件。遵守 AGENTS.md 的不可破坏行为、安全约束和验证命令。

项目必须保留：当前批次的范围/比例/模型/提示词快照；新批次不覆盖旧批次；主界面只显示选择的门店；生成图和效果图分别保存；真实门店效果图只可使用本机上传的玻璃照片，未上传时必须明确标记为模拟；不得输出或读取明文 API Key；测试不调用付费图像 API。

请先定位涉及的 Python、前端、manifest 和测试文件。完成修改后运行：
1) .\.venv\Scripts\python.exe -m unittest discover -s tests -v
2) .\.venv\Scripts\python.exe -m compileall -q app main.py
3) node --check app\web\static\app.js
然后报告修改文件、验证结果、未覆盖风险。不要只给方案，直接完成代码修改。
```

## 文件定位

| 目标 | 优先修改位置 |
| --- | --- |
| 网页接口、设置保存、批次快照 | `app/web/server.py` |
| 批量生成和停止/恢复 | `app/orchestrator.py` |
| 单门店 6 图重新生成 | `app/openai_regeneration.py` |
| 提示词模板、变量、真实感 | `app/prompt_profiles.py` |
| 玻璃门店效果图合成 | `app/effect_renderer.py` |
| 参考图/玻璃背景资产 | `app/reference_assets.py` |
| 服务商与能力声明 | `app/providers/`、`app/providers/catalog.py` |
| 页面结构和交互 | `app/web/static/index.html`、`app/web/static/app.js` |
| 离线验证 | `tests/` |

## 交付要求

1. 先保留现有用户数据和历史批次；不删除 `output/`、`config/settings.json` 或用户照片。
2. 任何新的设置都要在当前批次 snapshot 中固化，并且可在历史中追溯。
3. 任何错误都要区分可恢复和不可恢复；额度不足后充值、测试连接成功时，用户应可继续原批次未完成任务。
4. 运行时、日志和最终说明绝不显示 API Key、Base64 参考图或用户路径以外的敏感内容。
5. 完成修改后给出准确的文件清单和测试结果。
