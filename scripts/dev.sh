#!/usr/bin/env bash
# 一键准备开发环境并启动。等价于手写：
#   python -m venv .venv && source .venv/bin/activate
#   pip install -r requirements.txt && cp .env.example .env
#   python run.py serve
set -euo pipefail

cd "$(dirname "$0")/.."

if [ ! -d .venv ]; then
  echo "[1/3] 创建虚拟环境 .venv"
  python -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate

echo "[2/3] 安装依赖"
pip install -q -r requirements.txt

if [ ! -f .env ]; then
  echo "[3/3] 生成 .env（请填写 DATABASE_URL）"
  cp .env.example .env
  echo "      已生成 .env，改完 DATABASE_URL 后重新执行本脚本。"
  exit 0
fi

echo "[3/3] 启动服务"
exec python run.py "$@"
