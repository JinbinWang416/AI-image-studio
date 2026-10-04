# 交接说明（给接手的开发者 / AI 助手）

> 这份文档的目标：让你在**不读完整对话历史**的情况下，快速理解项目现状、
> 知道哪些已经做完、哪些还没做、以及**踩过哪些坑**。
>
> 请先读 `AGENTS.md`（项目协作约定，含不可破坏的行为），再读本文。

---

## 1. 项目是什么

Windows 上运行的门店玻璃贴纸生成工具：选门店 + 比例 → 生成彩色贴纸设计图 →
再合成到门店玻璃实拍照片上形成「效果图」。以**网页版**为主要交付形态
（`main.py web` → `http://127.0.0.1:8000`）。

- Python 3.12 + FastAPI + 原生 JS 前端（无构建步骤）
- 服务商：阿里云百炼（千问）、OpenAI GPT Image、Gemini、字节 Seedream、本地 mock
- 有完整的账号 / 角色 / 权限体系（argon2id + TOTP + RBAC）

---

## 2. 目录结构要点

```
app/                    Python 代码
  web/server.py         HTTP 接口（最重要，改接口先看这里）
  web/static/           前端（index.html / app.js / app.css / topbar.js）
  providers/            各服务商适配器 + catalog.py（模型与能力定义）
  settings.py           配置读写（含损坏防护与测试隔离）
  batches.py            批次 ID 校验
  print_export/         印刷 TIF 导出（含中文字体处理）
tests/                  unittest（当前 364 项）
tools/                  运维与校验脚本
config/settings.json    **运行时配置，含 API Key —— 不要提交、不要外发**
data/security/         **账号、密码哈希、会话、备份密钥 —— 绝不外发**
data/stores.json       门店定义（只读资源，需要随包分发）
output/                **用户出图与门店照片 —— 绝不外发**
docs/                  设计与说明文档
评审/                   历次外部评审报告（含已知问题清单）
Dockerfile             容器部署
docker-compose.yml     一键起容器
DEPLOY.md              部署指南（三种方式）
render.yaml            Render 平台配置
```

---

## 3. 怎么跑起来

```powershell
# 本机（推荐先这样验证）
.\.venv\Scripts\python.exe main.py web --host 127.0.0.1 --port 8000

# 跑测试（当前基线：364 项通过）
.\.venv\Scripts\python.exe -m unittest discover -s tests

# 语法检查
.\.venv\Scripts\python.exe -m compileall -q app main.py
node --check app\web\static\app.js
```

**Docker**：

```bash
docker compose up -d --build     # 约 5~15 分钟，镜像约 2 GB
```

细节见 `DEPLOY.md`。

---

## 4. 已完成的工作（历次评审的修复，按时间顺序）

| 编号 | 问题 | 处理 |
|---|---|---|
| P0-01 | 配置备份泄露 API Key | 已从 git 历史清除（filter-branch），Key 已更换 |
| P0-03 | 13 处硬编码管理员密码 | 改为读环境变量 `SHS_ADMIN_PW`，密码已更换 |
| P1-01 | 设置接口越权 | `POST /api/settings` 改为**按字段**校验权限 |
| P1-02 / F-01 | 续跑未冻结服务商与模型 | 批次快照记录并恢复 provider/model，**并按快照服务商重新解析 api_key/base_url** |
| P1-03 | 批次 ID 允许点目录穿越 | `is_safe_batch_id()` 拒绝纯点、Windows 保留名等 |
| P1-04 / F-02 | 损坏配置被默认值覆盖 | `save()` 在 corrupt 时**拒绝写入**并抛 `ConfigCorruptError`；`reset()` 需显式确认 |
| P1-05 | 新批次先落盘后校验 | 校验前移（配置、范围、服务商都先验） |
| P1-06 / F-03 | 密钥扫描器漏检 | 重写 `tools/check_git_secrets.py`，真正读 git blob 内容 |
| P2-01 | 锁文件缺依赖 | `requirements.lock.txt` 补齐（35 → 62 项） |
| P2-03 / F-04 | GBK 控制台自检崩溃 | 新增 `tools/_console.py`，`--windowed` 下也为 stdout/stderr 兜底 |
| — | 桌面版启动器 | 恢复 `run_desktop.py` 并修掉 `--windowed` 下 uvicorn 崩溃 |
| — | 顶栏服务商混淆 | 区分「当前设置」与「批次服务商」，不一致时给提示 |
| — | Docker 部署 | 新增 `Dockerfile` / `docker-compose.yml` / `.dockerignore` / `DEPLOY.md`，并修掉 Linux 下中文字体缺失 |

**当前测试基线**：`Ran 364 tests ... OK`（默认 GBK 控制台下也是绿的）。

---

## 5. ⚠️ 还没做完 / 待处理

### 5.1 OpenAI 出图返回 429（**当前卡点**）

**现象**：设置页「测试连接」通过（200），但真正出图时 6 张全部失败：

```
[X] 房屋中介门店 / 01_楼房线稿 最终失败：OpenAI 图像请求触发限流或额度限制，请稍后重试或检查账单
```

**已确认**：

- 模型名 `gpt-image-2.5-flare` **是正确的**（OpenAI 官方 API 文档里有，
  同系列还有 `gpt-image-2.5-sunburst`）
- 错误分类逻辑正确：`429` → `RATE_LIMIT`、`400/404/422` → `INVALID_REQUEST`
- 服务端确实收到了 429

**问题在于**：`app/providers/openai.py` 在抛 429 错误时**丢掉了上游的 message**：

```python
message = cls._message(resp) or f"HTTP {resp.status_code}"   # 取到了
...
if resp.status_code == 429:
    raise ProviderError("OpenAI 图像请求触发限流或额度限制，请稍后重试或检查账单", ...)  # 没用它
```

而 OpenAI 的 429 至少有三种含义，**处理方式相反**：

| 上游 message 大意 | 真实含义 | 该做什么 |
|---|---|---|
| `You exceeded your current quota` | 余额/额度不足 | 充值，重试无用 |
| `Rate limit reached for ...` | 短时限流 | 等一下重试 |
| 需要组织验证 | 权限未开 | 去后台完成验证 |

**建议的改动**（很小）：把 `message` 拼进提示，例如
`OpenAI 图像请求被拒绝（429）：{message}`。
另外检查 `app/providers/gemini.py:102` 有没有同样丢 message 的问题。

### 5.2 P0-02：Render 公网部署的两个阻断项

1. **免费层磁盘是临时的** —— 冷启动后 `config/settings.json` 被清空，
   管理员账号、API Key、服务商选择全丢
2. **首访抢注管理员** —— 在「还没有任何账号」的窗口期，任何能访问到端口的人
   都能创建管理员。`render.yaml` 的 `--i-know-its-public` 就是这个原因加上的，
   **建号后应去掉**

### 5.3 其他

- `render.yaml` 里**不要**设 `PROVIDER` 环境变量 —— 它的优先级高于
  `settings.json`，会把网页里选的服务商**永久压掉**（实测踩过：
  设了 `PROVIDER=mock` 后，网页选千问完全不生效）
- 桌面版 exe 的数据落在 `%LOCALAPPDATA%\门店贴纸图片生成智能体\`，
  与源码目录隔离 —— 排查问题时**别只看项目根目录的 `config/`**

---

## 6. 🔥 踩过的坑（请务必读，能省你很多时间）

### 6.1 测试绝不能写真实 `config/settings.json`

**真实发生过两次**：跑完测试后用户配置里的 `active_provider` 从 `qwen` 变成 `mock`。

- 触发链：测试直接调真实端点 → `get_store().save()` → 单例指向**真实配置**
- 二次踩到的原因：守卫用 `SETTINGS_FILE` 这个**模块全局**判定，
  `mock.patch.object(mod, "SETTINGS_FILE", tmp)` 一句就能绕过
- **现有四道防线**（改动前先看 `tests/test_settings_guard.py`，12 条断言锁死）：
  测试进程内 `get_store()` 自动落临时目录、写真实路径直接抛错、
  真实路径用模块加载时固化的 `_REAL_SETTINGS_PATH`（**别改成 `SETTINGS_FILE`**）、
  `logs/settings-writes.log` 审计每次写入
- **排查手段**：把 `config/settings.json` 设成只读再跑测试，
  绕过的写入会撞锁暴露；配合「测试前 hash → 跑测试 → 后 hash」放同一条命令里

### 6.2 `git filter-branch -- --all` **不会重写 tag**

除非显式给 `--tag-name-filter`。漏了这个参数导致 `v2.1.0` 一直指向
filter **之前**的提交，15 个含旧密码的 blob 因此保持可达 ——
而密钥扫描器当时又恰好漏扫，两件事叠在一起，很久都没发现。

### 6.3 `git cat-file --batch` 的游标必须严格前移

响应流里**非 blob 对象（tree/commit）的内容同样占字节**。
若在类型不符时直接 `continue` 而不 `pos += size + 1`，后续解析全部错位 ——
实测应读 379 个 blob 只返回 311 个。**只把 blob 的 SHA 送进 `--batch`**。

### 6.4 PowerShell 的坑

- `>` 默认写 **UTF-16LE**，Python 按 UTF-8 读会每两字节夹一个 `\x00`，
  417 个路径全部"不存在"，扫描静默变成 0 个文件、结论完全无效
- `Get-Content` 显示中文时可能因 GBK 解码把多行**显示成一行**，
  看起来像"注释和包名粘连"，实际文件是好的 —— **用 Python 读文件核实**
- `Get-Content` 与 read 工具的行号可能对不上，定位代码请用 grep 工具

### 6.5 改架构版代码前先确认是不是「转发模块」

架构版经过 Phase 1 分包，`app/*.py` 里有一些只是转发：

```python
from .validation.professional import *  # noqa: F401,F403
```

**直接覆盖这类文件会出问题**（我踩过：把生产版的完整实现覆盖到了转发模块上）。
真实现在 `app/state/`、`app/web/`、`app/validation/`、`app/core/` 等分包里。

### 6.6 PyInstaller `--windowed` 下 `sys.stdout` 是 `None`

uvicorn 的日志格式器会调 `sys.stdout.isatty()` → `AttributeError` →
`ValueError: Unable to configure formatter 'default'` → **双击图标毫无反应**，
而且 `--windowed` 模式下**没有任何输出可看**。
修法：import 阶段就给 `sys.stdout`/`sys.stderr` 接上 `os.devnull`。

### 6.7 本机控制台是 GBK

打印 `✅` 之类会 `UnicodeEncodeError` 并让**整个脚本以退出码 1 结束**
（看起来像"检查失败"，其实只是打印失败）。
`tools/_console.py` 提供了 `enable_safe_output()`。
**验证时要在不设 `PYTHONIOENCODING` 的默认控制台下也跑一遍** ——
之前一直设着 UTF-8，导致这个问题一直没被发现。

---

## 7. 安全约束（硬性）

- **不要把 API Key / 密码写进**：源码、日志、测试夹具、文档、提交信息
- **不要外发**：`config/settings.json`、`data/security/`、`output/`、`logs/`、`.env`
- **测试只用 mock 服务商**，不得调用付费图像 API
- batch manifest 不能存 Key、Base64 图片或完整请求密文
- 参考图只能放 `<输出根目录>\_references`，门店玻璃背景只能放
  `<输出根目录>\_effect_backgrounds`，都要按哈希去重

---

## 8. 重要约定

### 8.1 大段需求：先规划，等命令，再动手

收到成段需求 / 技术方案 / 功能清单时，先做**只读**现状核查 →
输出分析报告 + 开发规划（含关键决策点、风险、验收标准）→
**等明确的执行命令**后才改代码。

### 8.2 排查问题：先给结论，不要边猜边改

遇到 bug 先用只读手段定位根因（读代码、命中测试、审计日志），
拿到确凿证据再改。**不要把"猜测 + 试改"的过程直接推给用户。**

> 反面教材就在本项目里：我曾凭"`gpt-image-2.5-flare` 这名字不像官方模型"
> 就怀疑模型名配错了，查了官方文档才发现它**确实是真实的模型名** ——
> 差点让用户去改一个本来正确的配置。

### 8.3 改过代码后要提醒更新发布包

GitHub Release 上的附件是**某次打包时的快照**，不会随代码自动更新。
改了 `app/` / `tools/` / `tests/` / `requirements*.txt` 就要提醒重新打包。

### 8.4 打包后必须给出压缩包的完整绝对路径

独立一行、便于复制，不要只说"已打包完成"。

---

## 9. 当前 git 状态

| 分支 | 说明 |
|---|---|
| `main` | 生产版：`E:\1_Software\6_AI工具\deepseek\2_开发\图片生成` |
| `arch-refactor` | 架构版：`E:\1_Software\6_AI工具\deepseek\2_开发\AI生图架构版` |

远端：`https://github.com/JinbinWang416/AI-image-studio.git`（**公开仓库**）

⚠️ **两个 checkout 是独立目录、独立 git 仓库**，但推同一个远端的两个分支。
改动需要**分别同步**（不能假设改一处另一处自动生效）。

⚠️ 直连 `github.com:443` 不稳定，本机通过代理 `http://127.0.0.1:7897` 访问，
两个仓库的 `.git/config` 里都已配 `http.proxy` / `https.proxy`。

⚠️ `gh` CLI 未持久化登录（git 凭据里的 token 缺 `read:org`），
要用时先从 git 凭据管理器取：`git credential fill` → 设为 `GH_TOKEN`。

---

## 10. 建议的上手顺序

1. 读 `AGENTS.md` 与本文
2. 跑一次测试，确认基线是绿的（364 项）
3. 读 `评审/` 里最新的那份报告，了解已知问题
4. 从 **5.1（OpenAI 429 的错误信息改进）** 入手 —— 改动小、价值明确
5. 再决定是否处理 5.2（Render 公网部署）
