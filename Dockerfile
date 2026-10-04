# syntax=docker/dockerfile:1
#
# 门店贴纸图片自动生成智能体 —— 生产镜像
#
# 构建：
#     docker build -t store-sticker-ai .
#
# 运行（把数据存到宿主机，容器重启不丢）：
#     docker run -d --name store-sticker -p 8000:8000 \
#       -v "$PWD/docker-data/config:/app/config" \
#       -v "$PWD/docker-data/data:/app/data" \
#       -v "$PWD/docker-data/logs:/app/logs" \
#       -v "$PWD/docker-data/output:/app/output" \
#       store-sticker-ai
#
# 打开 http://127.0.0.1:8000 ，首次访问会引导创建管理员账号。
#
# 说明：
#   · 基础镜像是 python:3.12-slim，与本机开发环境（3.12.10）一致
#   · 镜像体积较大（约 2 GB）：onnxruntime / opencv / rembg 都是重量级依赖，
#     这是功能决定的，不是可以随便裁掉的
#   · ⚠️ 见文件末尾关于 --i-know-its-public 的安全提示

FROM python:3.12-slim

# ---------------------------------------------------------------- 系统依赖
# · libglib2.0-0    opencv-python-headless 的运行期依赖
# · libgomp1        onnxruntime / numpy 的 OpenMP 运行时
# · fonts-noto-cjk  **中文字体**
#       ⚠️ 关键：应用要排版中文标题，Windows 下用的是微软雅黑
#          （C:\Windows\Fonts\msyh.ttc），Linux 上没有这个文件。
#          不装的话 `app/professional_local.py` 会抛「缺少中文字体」。
# · curl            健康检查用
# · tzdata          时区
RUN apt-get update && apt-get install -y --no-install-recommends \
        libglib2.0-0 \
        libgomp1 \
        fonts-noto-cjk \
        curl \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------- 时区
ENV TZ=Asia/Shanghai
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# ---------------------------------------------------------------- 非 root 运行
# 容器里不该用 root 跑应用：一旦有漏洞，影响面小得多。
RUN useradd -m -u 1000 -s /bin/bash appuser

WORKDIR /app

# ---------------------------------------------------------------- 依赖层
# 先只拷 requirements.txt 再装 —— 这样「只改代码」时这一层能被缓存命中，
# 不用每次重新装那几个几百 MB 的包。
COPY requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# ---------------------------------------------------------------- 应用代码
COPY . .

# ---------------------------------------------------------------- 运行时目录
# ⚠️ 这四个目录**必须持久化**，否则容器一重启：
#      config  → 管理员账号、API Key、所有设置全丢
#      data    → 安全存储（argon2 哈希、TOTP 密钥）全丢
#      output  → 已生成的图全丢
#      logs    → 审计日志（含 settings-writes.log）全丢
RUN mkdir -p /app/config /app/data /app/logs /app/output \
    && chown -R appuser:appuser /app

VOLUME ["/app/config", "/app/data", "/app/logs", "/app/output"]

# ---------------------------------------------------------------- 中文字体自检
# 构建时就验证字体可用 —— 免得等到用户点「生成」才发现中文渲染不了。
RUN python -c "import sys; sys.path.insert(0, '.'); \
from app.professional_local import FONT_TITLE, FONT_BODY; \
assert FONT_TITLE.is_file(), f'标题字体缺失: {FONT_TITLE}'; \
assert FONT_BODY.is_file(), f'正文字体缺失: {FONT_BODY}'; \
print(f'字体就绪: {FONT_TITLE.name} / {FONT_BODY.name}')"

USER appuser

ENV APP_HOST=0.0.0.0 \
    APP_PORT=8000 \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${APP_PORT}/" >/dev/null || exit 1

# ---------------------------------------------------------------- 启动
# ⚠️ 关于 `--i-know-its-public`：
#    容器首次启动时**还没有任何账号**，不加这个参数服务会拒绝监听 0.0.0.0
#    （见 main.py 的启动护栏）。所以初始必须带上。
#
#    **建好管理员账号之后**，建议二选一：
#      ① 去掉该参数并重启容器（此后无账号时不再自动放行）；
#      ② 保持现状，但用防火墙 / 反向代理 / VPN 限制谁能访问这个端口。
#    否则任何能访问到端口的人都可以在首次访问时抢注管理员。
CMD ["sh", "-c", "python main.py web --host 0.0.0.0 --port ${APP_PORT} --i-know-its-public"]
