#!/usr/bin/env bash
set -e
cd -- "$(dirname -- "$0")"
if [ ! -x .venv/bin/python ]; then
  echo '请先创建 .venv 并安装 requirements.txt，详见 USAGE.md'
  exit 1
fi
exec .venv/bin/python app.py "$@"
