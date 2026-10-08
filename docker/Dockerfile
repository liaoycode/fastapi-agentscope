# ── base ──────────────────────────────────────────────────────────────
# 不用 `# syntax=docker/dockerfile:1.x` directive:它会让 BuildKit 去
# auth.docker.io 拉解析器镜像,内网/被墙环境拉不到。本 Dockerfile 没
# 用任何 1.7 独有语法(都是 FROM/COPY/RUN/ENV/ENTRYPOINT/HEALTHCHECK 等
# 老语法),BuildKit 内置解析器就够了。
# python:3.11-slim 对齐 .venv 里跑着的 Python 版本(uv.lock 里锁的也是 3.11)。
# 用 debian-slim 而不是 alpine,避开 glibc/musl 不兼容导致 aiodocker/asyncpg 装不上。
FROM python:3.11-slim

# 设置时区和编码
# 不要 UV_SYSTEM_PYTHON=1——它会让 uv sync 不建 .venv,把包装到系统 Python,
# 但 PATH 又指向 /app/.venv/bin(本镜像默认 .venv),exec 时找不到二进制。
ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy



# vene运行python环境
WORKDIR /agent/venv
# uv:跟项目里 uv.lock 配套装依赖;COPY from ghcr.io 直接拿官方 binary,
# 不用 pip/venv 那一套,层缓存也更干净。
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /uvx /usr/local/bin/
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
# Python 国内源
ENV UV_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
# 先复制 lock + manifest,让 uv 装依赖这一层独立缓存。
# 改业务代码不会让 uv 重新解析/下载。
RUN uv sync --frozen --no-install-project --no-dev

ENV PATH="/agent/venv/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy

# 工作目录
WORKDIR /agent/code
# 复制源码
COPY app ./app
COPY main.py ./
COPY skills ./skills

EXPOSE 8000


CMD ["uvicorn", "main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "1", \
     "--limit-concurrency", "100", \
     "--limit-max-requests", "1000"]

