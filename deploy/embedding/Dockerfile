FROM python:3.11-slim

ARG APP_UID=1000
ARG APP_GID=1000

WORKDIR /app

# Debian 源按构建环境切换：境内默认华为云（实测 Packages.gz 下载 0.6s，
# 阿里云 3.1s、官方 20s+），境外构建加 --build-arg APT_MIRROR=deb.debian.org
ARG APT_MIRROR=repo.huaweicloud.com
RUN sed -i "s/deb.debian.org/${APT_MIRROR}/g" /etc/apt/sources.list 2>/dev/null; \
    sed -i "s/deb.debian.org/${APT_MIRROR}/g" /etc/apt/sources.list.d/*.sources 2>/dev/null; \
    apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# PyPI 源可按构建环境切换：境内默认华为云（实测同 wheel 下载 1.5s，
# 阿里云 295s），境外构建加 --build-arg PIP_INDEX_URL=https://pypi.org/simple
ARG PIP_INDEX_URL=https://repo.huaweicloud.com/repository/pypi/simple
RUN pip install --no-cache-dir --default-timeout=600 --retries 3 \
    --index-url "${PIP_INDEX_URL}" \
    --extra-index-url https://download.pytorch.org/whl/cpu \
    "torch==2.13.0+cpu" \
    fastapi \
    uvicorn \
    sentence-transformers \
    numpy \
    pyyaml \
    huggingface_hub

RUN groupadd --gid "${APP_GID}" embedding \
    && useradd --uid "${APP_UID}" --gid embedding --create-home --shell /usr/sbin/nologin embedding \
    && mkdir -p /models-cache \
    && chown -R embedding:embedding /app /models-cache

COPY --chown=embedding:embedding deploy/embedding/app.py /app/app.py

ENV PYTHONUNBUFFERED=1
ENV MODEL_NAME=BAAI/bge-base-zh-v1.5
ENV DIMENSION=768
ENV PORT=8082

EXPOSE 8082

USER embedding

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8082"]
