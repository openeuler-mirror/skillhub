#!/bin/sh
# ============================================================================
# SkillSpector (Jenkins) 容器入口
# ============================================================================
# 从 WITTYHUB_CONFIG 指向的 config.yaml 读取 Jenkins 部署参数，注入为
#   JENKINS_OPTS / JENKINS_ADMIN_PASS / JENKINS_NUM_EXECUTORS /
#   JENKINS_QUIET_PERIOD / WITTYHUB_REPOSITORY_ROOT
# 环境变量后启动 Jenkins（参数均取自 config.yaml 的 security.skillspector_*）。
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
    print("[skillhub-entrypoint] ERROR: pyyaml is required to read config", file=sys.stderr)
    sys.exit(1)

config_path = os.environ["SKILLSPECTOR_CONFIG"]
with open(config_path) as f:
    data = yaml.safe_load(f) or {}


def sec(field, default=""):
    return data.get("security", {}).get(field, default)


exports = {
    "JENKINS_OPTS": "--httpPort={}".format(sec("skillspector_jenkins_http_port", "8083")),
    "JENKINS_ADMIN_PASS": sec("skillspector_jenkins_token", ""),
    "JENKINS_NUM_EXECUTORS": str(sec("skillspector_jenkins_num_executors", "10")),
    "JENKINS_QUIET_PERIOD": str(sec("skillspector_jenkins_quiet_period", "5")),
    "WITTYHUB_REPOSITORY_ROOT": sec("skillspector_repository_root", "/opt/wittyhub/skill-repositories"),
}

for key, value in exports.items():
    escaped = value.replace("'", "'\\''")
    print("export {key}='{value}'".format(key=key, value=escaped))
PY
)"
fi

echo "[skillhub-entrypoint] JENKINS_OPTS=${JENKINS_OPTS:-}"
echo "[skillhub-entrypoint] JENKINS_ADMIN_PASS set: $([ -n "${JENKINS_ADMIN_PASS:-}" ] && echo yes || echo no)"
echo "[skillhub-entrypoint] JENKINS_NUM_EXECUTORS=${JENKINS_NUM_EXECUTORS:-}"
echo "[skillhub-entrypoint] JENKINS_QUIET_PERIOD=${JENKINS_QUIET_PERIOD:-}"
echo "[skillhub-entrypoint] WITTYHUB_REPOSITORY_ROOT=${WITTYHUB_REPOSITORY_ROOT:-}"

exec /usr/local/bin/jenkins.sh "$@"
