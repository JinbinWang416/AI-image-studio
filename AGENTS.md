# 门店贴纸图片自动生成智能体：协作说明

## 项目目标

维护一个 Windows 本地运行的门店玻璃贴纸生成应用。用户选择门店和图片比例后，应用生成彩色贴纸设计图；每张设计图同时可以合成到用户上传的门店玻璃实拍照片中，形成“效果图”。

## 先读这些文件

1. `README.md`：运行和目录总览。
2. `PROJECT_CONTEXT.json`：机器可读约束、入口和验证命令。
3. `app/web/server.py`：HTTP 接口和批次快照。
4. `app/orchestrator.py`：常规批量任务。
5. `app/prompt_profiles.py`：质量模板、中文文字锁定和真实感等级。
6. `app/effect_renderer.py`：设计图到玻璃门店效果图的本地合成。

## 不可破坏的行为

- “开始/继续当前批次”只执行当前批次快照中的未完成任务；它不能被后来修改的范围、模型或比例改变。
- “重新生成（新批次）”使用当前设置创建新目录，绝不覆盖旧批次。
- 当前选择的门店决定主界面展示范围，也决定新批次创建的任务范围。
- 贴纸设计图与效果图分别写入 `<门店>生成图`、`<门店>效果图`，并保留各自 manifest。
- 真实效果图只能基于用户本机上传的门店玻璃照片。缺少照片时只能输出并明确标记为“模拟门店背景”。不要把模拟图描述为实拍图。
- V1/V2/V3 真实感规则必须保留；新批次可逐级提高，不能超过 V3。

## 安全与数据

- `config/settings.json`、`.env`、`output/` 中可能有 API Key、参考图或用户门店照片。不要在输出、日志、测试夹具、文档或提交内容中暴露它们。
- API Key 仅允许在后端遮罩保存和使用；batch manifest 不能存储 Key、Base64 图片或完整请求密文。
- 测试必须使用 mock 服务商、本机虚拟图片或 HTTP 模拟；不得调用付费图像 API。
- 参考图只能放在 `<输出根目录>\_references`；门店玻璃背景只能放在 `<输出根目录>\_effect_backgrounds`。两种资产都必须按哈希去重。

### ⚠️ 测试进程绝不能写 `config/settings.json`（2026-09-25 两次踩到）

**症状**：跑完测试后，用户配置里的 `active_provider` 从 `qwen` 变成 `mock`，界面退回模拟模式。
逐个测试文件复跑又完全不复现（23/23「未改」），因为触发点在「测试直接调用真实端点」这条路上。

**链条**：`tests/test_openai_regeneration.py` → `server.api_openai_house_regenerate()`
→ `get_store().save({"prompt_quality": {...}})` → 单例此前一律指向**真实配置**；
而 `default_settings()` 的默认 `active_provider` 恰好是 `"mock"`，一落盘就污染。

**二次踩到的真正原因**：守卫和审计都用 `SETTINGS_FILE` 这个**模块全局**做判定，
`mock.patch.object(mod, "SETTINGS_FILE", tmp)` 一句话就能把它换掉 ——
换掉之后，指向真实配置的 store 会「看起来不像真实配置」，守卫提前 `return`、
审计静默跳过，写入畅通无阻（文件 mtime 对得上，审计日志里却一条记录都没有）。

**现有四道防线**（改动前先看 `tests/test_settings_guard.py`，12 条断言锁死）：

1. 测试进程内 `get_store()` 与 `SettingsStore()` 都自动落到临时目录（结构上拿不到真实配置）；
2. 测试上下文里写真实路径 → 直接抛 `RuntimeError`；
3. 真实路径判定用模块加载时固化的 **`_REAL_SETTINGS_PATH`** ——
   **不要**图省事改用 `SETTINGS_FILE`，那个能被 `mock.patch` 换掉；
4. `logs/settings-writes.log` 审计每次真实写入（含被拦截的），记调用者与测试来源。

**排查同类问题的有效手段**：把 `config/settings.json` 设成只读再跑测试 ——
绕过的写入会撞锁报错而暴露；配合「测试前 hash → 跑测试 → 测试后 hash」放在同一条命令里比对。
取证工具：`tools/restore_provider.py --check`（只读）/ `tools/restore_provider.py qwen`（还原并留存现场）。

## 开发范围（2026-09-20 起）

- **以网页版为唯一交付目标**：`main.py web` → `http://127.0.0.1:8000`，前端在 `app/web/static/`。
- **桌面版产物已移除**（2026-09-22，按用户要求）：`desktop/`、根目录 `*.exe`、`delivery/`
  已删除，共释放约 457 MB。**构建能力仍保留**，需要时可重新生成：
  - `tools/build_desktop_installer.ps1`（桌面安装包）
  - `tools/make_portable_pack.py`（可移植包）
  - `tools/make_delivery_pack.py`（门店验证包）
- 新功能的验收标准只针对网页路径。
- 历史：桌面版曾是独立交付物 → 2026-09-20 冻结 → 2026-09-22 移除产物。

## 开发约定

- Python 代码在 `app/`，前端在 `app/web/static/`。改变接口时，同时检查 server、前端状态、批次 snapshot 和测试。
- 新增服务商须在 `app/providers/` 与 `app/providers/catalog.py` 中定义能力。图生图/多图生图必须显式声明是否支持，不能静默降级。
- 服务商的余额不足、鉴权、限流、超时、内容拒绝和非 PNG 返回必须保留未完成任务，并给可恢复提示；不能进行无意义重试。
- 修改输出格式时兼容历史 batch 的 manifest；历史恢复不能依赖当前设置。
- 默认中文界面。生成提示词必须锁定中文主标题和副标题，但不要承诺模型一定能完美排字。

## 交付约定

- **每次打包（可移植包 / 交付包 / 任何 zip）之后，必须直接给出压缩包的完整文件地址**：
  独立一行、绝对路径、便于直接复制，不要只说"已打包完成"或只给相对路径。
- 一次回复里若产生了多个压缩包，逐个列出地址，并标注各自用途。
- 生成压缩包的工具：`tools/make_portable_pack.py`（可移植包）、
  `tools/make_delivery_pack.py`（门店验证包）。

### ⚠️ 改过代码后必须主动提醒更新发布包

**Release 附件是「某次打包时的快照」，不会随代码自动更新。**
只要动了 `app/`、`tools/`、`tests/`、`requirements*.txt` 等源码，
就要**主动提醒用户**：GitHub Release 上的包已经落后于当前代码，
需要重新打包并替换，否则别人下载到的还是旧版本。

实测踩过：`v2.1.0` 的附件是 10:59 打的包，而当天 12:40 还在推修复
（F-01~F-04），中间好几轮改动都没进包。

重新发布的步骤：

1. 重新打包（会顺带脱敏 `config/settings.json`）：
   `.\.venv\Scripts\python.exe tools\make_portable_pack.py`
2. 验证新包确实含最新代码（抽查几个标志性字符串），并确认 `api_key` 已清空
3. **文件要改成 ASCII 名再上传** —— 实测 gh 在 Windows 下会把中文名写成 `_._xxx.zip`
4. `gh release delete-asset` 删旧附件 → `gh release upload` 传新附件
5. 清理 `_portable_stage/` 与临时副本（各约 700 MB）

⚠️ `gh` 未持久化登录（git 凭据里的 token 缺 `read:org` scope），每次要用先从
git 凭据管理器取：`git credential fill` → 设为 `GH_TOKEN` → 走 `7897` 代理。

## 协作方式（重要）

### 大段需求：先规划，等命令，再动手

收到**成段的需求描述 / 技术方案 / 功能清单**时：

1. **先做现状核查**（读相关代码、确认依赖与环境，**只读不改**）
2. **输出分析报告 + 开发规划**，必须包含：
   - 现状核查结论（含与需求不符或互相矛盾之处）
   - **关键决策点**（列出可选项、我的建议、影响）
   - 模块设计与文件清单
   - 实施顺序与工作量
   - 风险、降级方案与回滚方法
   - 验收标准
3. **等待明确的执行命令**（如「开始」「执行」）后才动代码

**判断标准**：需求文本含多个编号条目，或涉及新模块 / 新依赖 / 新接口 / 数据格式变更时，
一律视为「大段需求」，走上述流程。

**在收到执行命令前，不得**：
- 安装或卸载依赖（会改环境）
- 创建 / 修改 / 删除任何源文件
- 改动 `config/settings.json`、`output/` 或任何用户数据

**例外**：用户明确说「直接改」「开始」「执行」时，才直接动手。

### 排查问题：先给结论，不要边猜边改

遇到 bug 或异常时，**先用只读手段定位根因**（读代码、查计算样式、命中测试、颜色标记等），
拿到确凿证据后再改。不要把「猜测 + 试改」的过程直接推给用户。

---

## 本地验证

在项目根目录运行：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m compileall -q app main.py
node --check app\web\static\app.js
.\.venv\Scripts\python.exe main.py web --host 127.0.0.1 --port 8000
```

网页检查至少覆盖：设置双栏分类、范围选择和计数、比例显示、生成图/效果图切换、实拍玻璃背景上传、真实感等级、继续当前批次和新批次。
