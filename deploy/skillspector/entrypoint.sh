#!/bin/sh
# ============================================================================
# SkillSpector (Jenkins) 容器入口
# ============================================================================
# 从 WITTYHUB_CONFIG 指向的 config.yaml 读取部署参数与大模型复核参数，注入为
#   JENKINS_OPTS / JENKINS_ADMIN_PASS / JENKINS_NUM_EXECUTORS /
#   JENKINS_QUIET_PERIOD / WITTYHUB_REPOSITORY_ROOT
#   SKILLSPECTOR_* / OPENAI_*（大模型复核）
# 环境变量后启动 Jenkins。
#
# 生产部署不读取 compose.yaml 的 environment，参数以 config.yaml 为准；
# 容器环境变量仅在 config.yaml 对应值为空时兜底（例如 OPENAI_API_KEY）。
# 若 config.yaml 不存在，则直接使用容器自身提供的环境变量/默认值。
# ============================================================================

set -eu

CONFIG_PATH="${WITTYHUB_CONFIG:-/app/config.yaml}"

if [ -f "${CONFIG_PATH}" ]; then
    eval "$(SKILLSPECTOR_CONFIG="${CONFIG_PATH}" python3 <<'PY'
import os
import sys

try:
    import yaml
except ImportError:
    print("[wittyhub-entrypoint] ERROR: pyyaml is required to read config", file=sys.stderr)
    sys.exit(1)

config_path = os.environ["SKILLSPECTOR_CONFIG"]
with open(config_path) as f:
    data = yaml.safe_load(f) or {}

security = data.get("security", {}) or {}
model = data.get("model", {}) or {}


def sec(field, default=""):
    value = security.get(field, default)
    return default if value is None else value


def model_field(field, default=""):
    """当前生效 provider 的模型配置，语义与 ModelConfig.resolve() 一致。"""
    provider = str(model.get("provider", "") or "")
    if provider:
        entry = (model.get("providers", {}) or {}).get(provider) or {}
        value = entry.get(field, default)
    else:
        value = model.get(field, default)
    return default if value is None else value


def first(*values):
    for value in values:
        if value is not None and str(value).strip() != "":
            return str(value)
    return ""


def flag(field, default):
    value = sec(field, default)
    if isinstance(value, bool):
        return "1" if value else "0"
    return "1" if str(value).strip().lower() in {"1", "true", "yes", "on"} else "0"


def emit(key, value, overwrite=True):
    text = "" if value is None else str(value)
    # 配置值为空时保留容器已有环境变量，避免把显式 -e 传入的凭据清空
    if not overwrite and text.strip() == "":
        return
    escaped = text.replace("'", "'\\''")
    print("export {key}='{value}'".format(key=key, value=escaped))


# Jenkins 部署参数：config.yaml 为唯一来源（与既有行为保持一致）
for key, value in {
    "JENKINS_OPTS": "--httpPort={}".format(sec("skillspector_jenkins_http_port", "8083")),
    "JENKINS_ADMIN_PASS": sec("skillspector_jenkins_token", ""),
    "JENKINS_NUM_EXECUTORS": str(sec("skillspector_jenkins_num_executors", "10")),
    "JENKINS_QUIET_PERIOD": str(sec("skillspector_jenkins_quiet_period", "5")),
    "WITTYHUB_REPOSITORY_ROOT": sec(
        "skillspector_repository_root", "/opt/wittyhub/skill-repositories"
    ),
}.items():
    emit(key, value)

# 大模型复核参数：模型/地址/密钥留空时复用 model.providers 当前 provider
for key, value in {
    "SKILLSPECTOR_LLM_NODES": str(sec("skillspector_llm_nodes", "meta_analyzer")),
    "SKILLSPECTOR_MODEL": first(sec("skillspector_model", ""), model_field("name")),
    "OPENAI_BASE_URL": first(sec("skillspector_base_url", ""), model_field("base_url")),
    "OPENAI_API_KEY": first(sec("skillspector_api_key", ""), model_field("api_key")),
    "SKILLSPECTOR_TEXT_ONLY": flag("skillspector_text_only", True),
    "SKILLSPECTOR_RULE_SCOPE": flag("skillspector_rule_scope", True),
    "SKILLSPECTOR_WINDOW_LINES": str(sec("skillspector_window_lines", "20")),
    "SKILLSPECTOR_WINDOW_HEADER_LINES": str(sec("skillspector_window_header_lines", "40")),
    "SKILLSPECTOR_RPM": str(sec("skillspector_rpm", "1000")),
    "SKILLSPECTOR_TPM": str(sec("skillspector_tpm", "80000")),
    "SKILLSPECTOR_MAX_OUTPUT_TOKENS": str(sec("skillspector_max_output_tokens", "4096")),
    "SKILLSPECTOR_DISABLE_THINKING": flag("skillspector_disable_thinking", True),
    "SKILLSPECTOR_LOG_LEVEL": str(sec("skillspector_log_level", "INFO")),
    "SKILLSPECTOR_MAX_CONCURRENCY": str(sec("skillspector_max_concurrency", "4")),
    "SKILLSPECTOR_BATCH_TIMEOUT": str(sec("skillspector_batch_timeout", "300")),
    "SKILLSPECTOR_LLM_TIMEOUT": str(sec("skillspector_llm_timeout", "90")),
    "SKILLSPECTOR_LLM_BUDGET_SECONDS": str(sec("skillspector_llm_budget_seconds", "540")),
}.items():
    emit(key, value, overwrite=False)
PY
)"
fi

echo "[wittyhub-entrypoint] JENKINS_OPTS=${JENKINS_OPTS:-}"
echo "[wittyhub-entrypoint] JENKINS_ADMIN_PASS set: $([ -n "${JENKINS_ADMIN_PASS:-}" ] && echo yes || echo no)"
echo "[wittyhub-entrypoint] JENKINS_NUM_EXECUTORS=${JENKINS_NUM_EXECUTORS:-}"
echo "[wittyhub-entrypoint] JENKINS_QUIET_PERIOD=${JENKINS_QUIET_PERIOD:-}"
echo "[wittyhub-entrypoint] WITTYHUB_REPOSITORY_ROOT=${WITTYHUB_REPOSITORY_ROOT:-}"
echo "[wittyhub-entrypoint] SKILLSPECTOR_LLM_NODES=${SKILLSPECTOR_LLM_NODES:-}"
echo "[wittyhub-entrypoint] SKILLSPECTOR_MODEL=${SKILLSPECTOR_MODEL:-}"
echo "[wittyhub-entrypoint] OPENAI_BASE_URL=${OPENAI_BASE_URL:-}"
echo "[wittyhub-entrypoint] OPENAI_API_KEY set: $([ -n "${OPENAI_API_KEY:-}" ] && echo yes || echo no)"
echo "[wittyhub-entrypoint] SKILLSPECTOR_MAX_CONCURRENCY=${SKILLSPECTOR_MAX_CONCURRENCY:-}"
echo "[wittyhub-entrypoint] SKILLSPECTOR_LLM_TIMEOUT=${SKILLSPECTOR_LLM_TIMEOUT:-}"
echo "[wittyhub-entrypoint] SKILLSPECTOR_LLM_BUDGET_SECONDS=${SKILLSPECTOR_LLM_BUDGET_SECONDS:-}"

if [ -z "${OPENAI_API_KEY:-}" ]; then
    echo "[wittyhub-entrypoint] WARNING: OPENAI_API_KEY 未配置，SkillSpector 大模型复核将不可用" >&2
fi

exec /usr/local/bin/jenkins.sh "$@"
