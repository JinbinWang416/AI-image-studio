# 云端上线部署指南（免费平台测试）

本项目是 **FastAPI + Uvicorn** 全栈应用（AI 门店贴纸生图平台），需能运行 Python 后端的平台。
下面以 **Render 免费 Web Service** 为主方案，**Railway** 为备选。

---

## 一、上线前的仓库准备

> 当前目录**还不是 Git 仓库**（无 `.git`），需先初始化并推到 GitHub。

1. 初始化并提交（`.gitignore` 已正确排除 `.env`、`config/settings.json`、`output/`、`logs/`、`data/security/`）：

   ```bash
   git init
   git add .
   git commit -m "feat: AI 图片生成平台首次提交"
   ```

   > ⚠️ 提交前确认：`git status` 里**不应出现** `.env`、`config/settings.json`、`data/security/security.json`、
   > `output/`、`logs/`。若出现了，说明 `.gitignore` 未生效，先排查再提交，避免泄露密钥/密码哈希。

2. 在 GitHub 新建一个**私有**（或公开）仓库，**不要**勾选自动生成 README/.gitignore。
3. 推送：

   ```bash
   git remote add origin https://github.com/<你的用户名>/<仓库名>.git
   git branch -M main
   git push -u origin main
   ```

---

## 二、Render 一键部署（推荐）

1. 打开 https://dashboard.render.com → **New** → **Blueprint**。
2. 连接上一步的 GitHub 仓库，Render 会自动读取根目录的 **`render.yaml`** 并创建服务。
3. 确认配置（一般无需改）：
   - **Runtime**：Python（由 `runtime.txt` 锁定 3.12）
   - **Build Command**：`pip install -r requirements.txt`
   - **Start Command**：`python main.py web --host 0.0.0.0 --port $PORT --i-know-its-public`
   - **Plan**：Free
4. 点击 **Create Web Service**。首次构建会安装 `opencv / numpy / onnxruntime / rembg` 等较重依赖，
   **可能耗时 5~15 分钟**，属正常。
5. 构建完成后，Render 给出一个 `https://ai-image-studio-xxxx.onrender.com` 公开地址。

### 环境变量（Render 控制台 → Environment）
| Key | 值 | 说明 |
|-----|----|------|
| `PROVIDER` | `mock` | 默认离线模拟，**零 API Key 即可测试**界面与生成流程 |
| `PORT` | （自动注入） | 不要手动定值 |
| `DASHSCOPE_API_KEY` / `OPENAI_API_KEY` | 你的 Key | 仅真实出图时需要，可选 |

---

## 三、首次访问：创建管理员（重要）

1. 打开 Render 提供的 URL。由于初始无账号，应用会自动弹出**「创建管理员」**界面。
2. 填写登录名、显示名、密码，提交即创建首个管理员并进入系统。
   - 之后所有访问都需登录，公网不再裸奔。
3. 在首页「设置」选择服务商、填写 API Key（真实出图时）；仅测试可保持 `mock`。

> ⚠️ 免费层磁盘是**临时盘**：容器重启/休眠后，`data/security/`（账号）、`output/`、`logs/` 会丢失，
> 下次访问需**重新创建管理员**。属于测试可接受范围；生产请用付费层 + 持久化磁盘。

---

## 四、验证能否跑通（mock 模式）

在首页点击「开始/继续」或「重新生成（新批次）」，mock 服务商会在本地生成模拟 PNG，
验证前端、进度、日志、效果图合成等流程是否正常。无需任何外部 API。

---

## 五、备选：Railway

1. https://railway.app → New Project → Deploy from GitHub repo。
2. Railway 会自动检测 `requirements.txt` 并用 Nixpacks 构建。
3. **必须**设置启动命令为：`python main.py web --host 0.0.0.0 --port $PORT --i-know-its-public`
   （Railway 同样注入 `$PORT`）。
4. 在 Variables 里加 `PROVIDER=mock`。
5. 免费层有用量额度（每月 $5），超额需绑卡；同样有休眠与临时磁盘。

---

## 六、风险与注意

- **临时磁盘**：免费层容器重启会清空 `output/`、`logs/`、账号数据。测试 OK；生产需持久化。
- **冷启动慢**：免费 Web Service 休眠后首次访问需 30~60s 唤醒，可能一次超时，刷新即可。
- **重依赖构建**：`rembg` 首次运行会下载 `u2net.onnx`（约 167MB）到用户目录；在临时盘上每次冷启动都会重下，
  导致首次真实去背很慢。默认去背为 `auto`（无 AI 去背则降级纯色去背，对纯净背景足够），mock 测试不受影响。
- **公网安全**：`--i-know-its-public` 在创建管理员前是无认证的，务必**启动后立即创建管理员**；
  测试完毕建议停掉服务或改用付费层 + 防火墙限制来源。
- **若启动报缺 `python-multipart`**：本项目登录/setup 走 JSON，理论上不需要；万一遇到
  `422/Unsupported media type` 或导入错误，把 `python-multipart==0.0.20` 加进 `requirements.txt` 重新部署即可。

---

## 七、本地对照（可选）

```bash
# 本地已验证的启动方式（仅本机）
.\.venv\Scripts\python.exe main.py web          # 默认 127.0.0.1:8000
```
