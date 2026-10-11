# syntax=docker/dockerfile:1
FROM python:3.12-slim

# UTF-8 + 无缓冲输出：中文日志与 emoji 在任何宿主上一致
# （代码侧已有 make_console_safe，这里是运行环境侧的双保险）
ENV PYTHONUTF8=1 \
    PYTHONUNBUFFERED=1 \
    TZ=UTC

WORKDIR /app

# 只带运行时代码。tests/ evals/ .git/ data/ 与本地机密由 .dockerignore 显式排除 ——
# 镜像里没有任何凭据与运行数据，镜像外泄也不丢隐私。
COPY ops/ ./ops/
COPY tools/ ./tools/
COPY targets/ ./targets/
COPY config/ ./config/
COPY README.md DESIGN.md LICENSE ./

# 运行数据（SQLite / 报告 / 日志）一律落卷：镜像无状态，删容器不丢数据
VOLUME /app/data

ENTRYPOINT ["python", "-m", "ops"]
CMD ["--list"]

# ------------------------------------------------------------
# 用法（本项目的触发哲学是"无状态、可重入的 --once 单轮"，
# 容器就是它天然的载体 —— 外部 cron / K8s CronJob 直接调）：
#
#   单轮任务:
#     docker run --rm -v ./data:/app/data -v ./config:/app/config:ro \
#       ops-pipeline --task api_watch --once
#
#   只读面板（8899）:
#     docker run --rm -d -p 8899:8899 -v ./data:/app/data \
#       -v ./config:/app/config:ro --entrypoint python \
#       ops-pipeline tools/dashboard.py --host 0.0.0.0 --port 8899
#
#   自检:
#     docker run --rm -v ./config:/app/config:ro ops-pipeline --doctor --doctor-offline
# ------------------------------------------------------------
