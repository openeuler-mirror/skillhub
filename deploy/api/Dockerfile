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
    git \
    libpq-dev \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src ./src
COPY scripts ./scripts
COPY migrations ./migrations
COPY alembic.ini ./
COPY deploy/api/entrypoint.sh /usr/local/bin/wittyhub-api-entrypoint
COPY skillcrawler ./skillcrawler

# PyPI 源可按构建环境切换：境内默认华为云（实测同 wheel 下载 1.5s，
# 阿里云 295s），境外构建加 --build-arg PIP_INDEX_URL=https://pypi.org/simple
ARG PIP_INDEX_URL=https://repo.huaweicloud.com/repository/pypi/simple
RUN pip install -i "${PIP_INDEX_URL}" --no-cache-dir --default-timeout=500 -e .

RUN groupadd --gid "${APP_GID}" wittyhub \
    && useradd --uid "${APP_UID}" --gid wittyhub --create-home --shell /usr/sbin/nologin wittyhub \
    && mkdir -p /opt/wittyhub \
    && chown -R wittyhub:wittyhub /app /opt/wittyhub \
    && chmod 755 /usr/local/bin/wittyhub-api-entrypoint

ENV PYTHONUNBUFFERED=1
ENV PORT=8081

EXPOSE 8081

HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD python -c "import httpx; httpx.get('http://localhost:8081/api/v1/health').raise_for_status()"

USER wittyhub

ENTRYPOINT ["wittyhub-api-entrypoint"]
CMD ["uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "8081"]
