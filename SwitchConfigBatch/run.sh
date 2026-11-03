#!/usr/bin/env bash
set -e
cd -- "$(dirname -- "$0")"
if [ -x .venv/bin/python ]; then
  exec .venv/bin/python app.py "$@"
elif [ -x ../SwitchBatchCLI/.venv/bin/python ]; then
  exec ../SwitchBatchCLI/.venv/bin/python app.py "$@"
else
  echo '请创建 .venv 并安装 requirements.txt'
  exit 1
fi
