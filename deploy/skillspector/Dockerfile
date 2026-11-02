# ============================================================================
# SkillSpector + Jenkins 镜像 (自包含版本)
# ============================================================================
# 用法:
#   docker build -f deploy/skillspector/Dockerfile -t skillspector-jenkins:latest .
#   docker run -d --name skillspector-jenkins \
#     -p 8083:8083 -p 50000:50000 \
#     -e OPENAI_API_KEY=sk-xxx \
#     -e SKILLSPECTOR_LLM_NODES=meta_analyzer \
#     -v jenkins_home:/var/jenkins_home \
#     skillspector-jenkins:latest
#
#   大模型调用范围由容器环境变量决定（compose 部署见 deploy/compose.yaml）：
#     SKILLSPECTOR_LLM_NODES=meta_analyzer  仅 meta 复核节点调用（剔除静态误报 + 富化）
#     OPENAI_API_KEY / OPENAI_BASE_URL / SKILLSPECTOR_MODEL   凭证与模型
#
# 配置:
#   WITTYHUB_CONFIG    配置文件路径 (默认 /app/config.yaml)，容器入口启动时读取，
#                      将其中的 security.skillspector_* 参数注入为 JENKINS_* 环境变量
# 构建参数:
#   GIT_REPO_URL        SkillSpector 仓库地址
#   GIT_REF             分支 / 标签 / commit SHA
#                       (默认钉在上游 main 的 b445640，即 patches/ 里 patch 的基准提交)
# 说明:
#   构建时会把 deploy/skillspector/patches/*.patch 打进 SkillSpector 源码，使其支持
#   按节点控制大模型调用（SKILLSPECTOR_LLM_NODES）。上游一旦漂移，patch 会应用失败并
#   中断构建，此时需更新 patch 或调整 GIT_REF。
# ============================================================================

# ---- Stage 1: 构建 SkillSpector wheels ----
FROM python:3.13-slim AS skillspector-builder

ARG GIT_REPO_URL=https://github.com/JunchengDwain/SkillSpector.git
# 钉死提交：patches/ 里的 patch 基于该提交产出
ARG GIT_REF=b4456402350af8757f975bab033dc6aacf8e6c0a

RUN sed -i 's|http://deb.debian.org|https://repo.huaweicloud.com|g' /etc/apt/sources.list.d/debian.sources && \
    apt-get update && \
    apt-get install -y --no-install-recommends git && \
    rm -rf /var/lib/apt/lists/*

# git clone --branch 不接受 commit SHA，故用 fetch + checkout，可同时支持分支/标签/SHA
RUN git init /tmp/skillspector && \
    git -C /tmp/skillspector remote add origin "${GIT_REPO_URL}" && \
    git -C /tmp/skillspector fetch --depth 1 origin "${GIT_REF}" && \
    git -C /tmp/skillspector checkout -q FETCH_HEAD

# wittyhub 侧的 patch（按序应用）：
#   0001 按节点开关大模型（A 方案 = 只有 meta_analyzer 调用，做静态误报剔除 + 富化）
#   0002 硅基流动 Qwen3.5 适配：原生 json_schema 结构化输出、temperature=0、
#        关闭 thinking、客户端 RPM/TPM 限流与统一重试
#   0003 成本/上下文控制：TEXT_ONLY 跳过二进制、RULE_SCOPE 规则×文件类别裁剪、
#        命中窗口切片（WINDOW_LINES/HEADER_LINES）
COPY deploy/skillspector/patches /tmp/patches
RUN git -C /tmp/skillspector apply --verbose /tmp/patches/*.patch

WORKDIR /tmp/skillspector
RUN pip config set global.timeout 120 && \
    pip config set global.index-url https://repo.huaweicloud.com/repository/pypi/simple && \
    pip install pip --upgrade && \
    pip wheel . -w /tmp/wheels

# ---- Stage 2: Jenkins 镜像 ----
FROM jenkins/jenkins:lts

USER root

RUN sed -i 's|http://deb.debian.org|https://repo.huaweicloud.com|g' /etc/apt/sources.list.d/debian.sources && \
    apt-get update && \
    apt-get install -y --no-install-recommends \
        python3 python3-pip git curl ca-certificates tzdata && \
    rm -rf /var/lib/apt/lists/*

# 从 Stage 1 复制预构建的 wheels
COPY --from=skillspector-builder /tmp/wheels /tmp/wheels

# 安装 SkillSpector
RUN pip config set global.timeout 120 && \
    pip config set global.index-url https://repo.huaweicloud.com/repository/pypi/simple && \
    pip install --no-cache-dir --break-system-packages --ignore-installed /tmp/wheels/*.whl pyyaml && \
    rm -rf /tmp/wheels

# 验证 SkillSpector 安装
RUN skillspector --version

# Jenkins 插件
RUN jenkins-plugin-cli --plugins \
    "workflow-aggregator:latest" \
    "git:latest" \
    "credentials-binding:latest" \
    "pipeline-stage-view:latest" \
    "build-token-root:latest" \
    "timestamper:latest"

# .override 确保已有 Jenkins 数据卷也使用最新初始化脚本
COPY deploy/skillspector/init.groovy /usr/share/jenkins/ref/init.groovy.d/01-init.groovy.override
COPY deploy/skillspector/pipeline.groovy /usr/share/jenkins/ref/jenkins-pipeline.groovy
COPY deploy/skillspector/checkout.sh /usr/local/bin/wittyhub-skillspector-checkout
RUN chmod 755 /usr/local/bin/wittyhub-skillspector-checkout

# 容器入口：读取 config.yaml 注入 JENKINS_* 环境变量后启动 Jenkins
COPY deploy/skillspector/entrypoint.sh /usr/local/bin/wittyhub-entrypoint.sh
RUN chmod 755 /usr/local/bin/wittyhub-entrypoint.sh
ENTRYPOINT ["/usr/local/bin/wittyhub-entrypoint.sh"]

ENV SKILLSPECTOR_LOG_LEVEL=WARNING
ENV SKILLSPECTOR_PROVIDER=openai

USER jenkins
