#!/usr/bin/env bash
# WittyHub CLI 一键安装脚本
#
# 用法:
#   curl -fsSL https://skillhub.openeuler.org/install/install.sh | bash
#
# 可选环境变量:
#   WITTYHUB_VERSION        指定安装版本（默认 latest）
#   WITTYHUB_NPM_REGISTRY   指定 npm 源（默认 https://registry.npmmirror.com，CN 加速）
set -euo pipefail

MIN_NODE_MAJOR=18
NPM_REGISTRY="${WITTYHUB_NPM_REGISTRY:-https://registry.npmmirror.com}"
TARGET_VERSION="${WITTYHUB_VERSION:-latest}"

# 1. 检查 Node.js（与 wittyhub package.json 的 engines 保持一致）
if ! command -v node >/dev/null 2>&1; then
  echo "错误: 未检测到 Node.js。wittyhub 需要 Node.js >= ${MIN_NODE_MAJOR}（https://nodejs.org/）" >&2
  exit 1
fi

node_major="$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0)"
if [ "${node_major}" -lt "${MIN_NODE_MAJOR}" ]; then
  echo "错误: Node.js 版本过低（当前 v${node_major}，需要 >= ${MIN_NODE_MAJOR}）" >&2
  exit 1
fi

# 2. 全局安装 wittyhub
echo "正在安装 wittyhub@${TARGET_VERSION} ..."
npm install -g "wittyhub@${TARGET_VERSION}" --registry "${NPM_REGISTRY}"

# 3. 验证安装
echo ""
echo "验证安装:"
wittyhub --version
echo ""
echo "wittyhub 安装完成。使用 \"wittyhub --help\" 查看全部命令。"