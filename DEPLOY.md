# 部署指南

本文档说明如何把这个项目跑在服务器上。三种方式任选，**Docker 是最省心的**。

> ⚠️ 本项目是**单机工具**（原本设计为本机 `127.0.0.1` 运行），没有做过多租户隔离。
> 放到公网前请先读完文末的「安全清单」。

---

## 方式一：Docker Compose（推荐）

**前置**：装了 Docker Desktop（Windows/macOS）或 docker + docker-compose-plugin（Linux）。

```bash
docker compose up -d --build     # 构建并启动（首次约 5~15 分钟，镜像约 2 GB）
docker compose logs -f           # 看日志
docker compose down              # 停止（数据保留在 ./docker-data）
```

然后打开 <http://127.0.0.1:8000>。

**数据在哪**：`./docker-data/` 下的四个子目录（`config` / `data` / `logs` / `output`）。
这就是你的全部家当 —— **备份它等于备份整个系统**。

**只装 Docker、不用 Compose**：

```bash
docker build -t store-sticker-ai .
docker run -d --name store-sticker -p 8000:8000 \
  -v "$PWD/docker-data/config:/app/config" \
  -v "$PWD/docker-data/data:/app/data" \
  -v "$PWD/docker-data/logs:/app/logs" \
  -v "$PWD/docker-data/output:/app/output" \
  store-sticker-ai
```

---

## 方式二：Render（公网，免费层可试）

仓库里已有 `render.yaml`。Render 控制台 → **New → Blueprint** → 连这个仓库即可。

⚠️ **免费层有三个硬限制**（实测踩过）：

1. **磁盘是临时的** —— 15 分钟无流量会休眠，**冷启动后 `config/settings.json` 被清空**，
   管理员账号、API Key、服务商选择**全部丢失**，得重新建/重新选。
   要持久化必须升级到付费层并挂载 Disk。
2. **首次访问谁都能抢注管理员**（见下方安全清单第 2 条）。
3. **出图慢** —— 免费实例 CPU 弱，加上冷启动 30~60 秒。

**环境变量**（Render 控制台 → Environment）：

| Key | 值 | 说明 |
|---|---|---|
| `DASHSCOPE_API_KEY` | 你的百炼 Key | 密钥走环境变量，不落盘 |
| ~~`PROVIDER`~~ | **不要设** | ⚠️ 设了会**永久压掉**网页里选的服务商，表现为「选了千问却一直显示本地模拟」 |

---

## 方式三：自己的 Linux 服务器（裸机）

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 中文字体 —— 不装的话「专业样图」会报「缺少中文字体」
sudo apt-get install -y fonts-noto-cjk

.venv/bin/python main.py web --host 0.0.0.0 --port 8000 --i-know-its-public
```

建议用 systemd 或 supervisor 托管，前面加一层 Nginx 反向代理 + HTTPS。

---

## 首次使用

1. 打开页面 → 会引导**创建管理员账号**（用户名 + 强密码，至少 10 位）
2. 登录 → **设置 → 模型服务** → 选服务商（如「阿里云百炼」）
3. 填 API Key —— 如果用环境变量提供了 Key，这里可以不填
4. 回到主页 → 选门店和比例 → **开始/继续当前批次**

---

## 数据与备份

| 路径 | 内容 | 丢了会怎样 |
|---|---|---|
| `config/settings.json` | 服务商、Key、输出路径、模板 | 要重新配一遍 |
| `data/security/security.json` | 账号、角色、argon2 密码哈希、TOTP | **没人能登录** |
| `data/security/.backup_key` | 备份加密密钥 | 已加密的备份**解不开** |
| `data/stores.json` | 门店与主题定义 | 应用无法启动 |
| `output/` | 生成图、效果图、批次 manifest | 历史出图丢失 |
| `logs/` | 审计日志 | 丢审计线索 |

**备份**：应用内置了加密备份功能（设置 → 系统管理 → 备份）。
也可以直接打包 `docker-data/`，但注意里面有明文 API Key，**别放公共网盘**。

⚠️ `.backup_key` 必须和 `security.json` 一起备份 —— 少了它，加密备份就是一堆废字节。

---

## 安全清单（公网部署前逐条确认）

1. **`--i-know-its-public` 是临时措施。**
   容器首次启动还没有账号，不加它服务会拒绝监听 `0.0.0.0`。
   **建好管理员账号后**请二选一：去掉该参数并重启，或用防火墙 / VPN 限制访问端口。

2. **首访抢注风险。**
   在「还没有任何账号」的窗口期，**任何能访问到端口的人都能创建管理员**。
   所以：部署完立刻建号，别把没建号的服务挂在公网。

3. **API Key 别写进 `settings.json` 再提交。**
   Key 优先从环境变量读（`app/config.py` 的取值顺序是「环境变量 → settings.json」），
   走环境变量更安全，也不会被打包带走。

4. **别把 `config/`、`output/` 提交到 git。**
   `.gitignore` 已经覆盖，但换机器/换仓库时要再确认一遍。

5. **公网必须上 HTTPS。**
   用 Nginx / Caddy 反代并签证书；直接暴露 HTTP 会让登录密码和会话明文传输。

6. **限制端口暴露面。** 内部工具的话，绑 `127.0.0.1` + VPN 比暴露公网安全得多。

---

## 故障排查

**「缺少中文字体」/ 图上中文变方框**
镜像里已装 `fonts-noto-cjk`。裸机部署要自己装。换字体：
```bash
APP_FONT_TITLE=/path/to/title.ttf APP_FONT_BODY=/path/to/body.ttf
```

**网页里选的服务商不生效，一直显示「本地模拟 · mock-v1」**
有人在环境变量里设了 `PROVIDER`。它优先级高于 `settings.json`，会永久压掉网页选择。
删掉这个环境变量再重启。

**容器起来就退出**
```bash
docker compose logs app        # 看真实报错
```
最常见的是端口被占（改 `ports` 左侧），或挂载目录权限不对
（`chown -R 1000:1000 docker-data`）。

**出图很慢 / 卡住**
`onnxruntime` + `opencv` 吃 CPU。免费/低配实例上「AI 去背」会明显拖慢：
可以在代码里退回纯色去背（`app/layers.py` 自动降级），或换更强的实例。

**改了代码但容器里没变**
```bash
docker compose up -d --build   # 必须 --build，否则用的还是旧镜像
```

---

## 这个镜像里有什么

- 基础：`python:3.12-slim`（与本机开发环境 3.12.10 一致）
- 系统包：`libglib2.0-0`（opencv 依赖）、`libgomp1`（onnxruntime 依赖）、`fonts-noto-cjk`（中文字体）
- Python 依赖：见 `requirements.txt`（fastapi / uvicorn / httpx / Pillow / cryptography / numpy / opencv-python-headless / rembg / onnxruntime / argon2-cffi / PyOTP）
- 以**非 root 用户** `appuser`（uid 1000）运行
- 构建时会**自检中文字体**，字体不对会直接构建失败 —— 免得等用户点生成才发现
