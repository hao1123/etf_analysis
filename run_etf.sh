#!/bin/zsh
set -eu

SCRIPT_DIR="${0:A:h}"

if [[ -n "${ETF_PYTHON_BIN:-}" ]]; then
  PYTHON_BIN="$ETF_PYTHON_BIN"
elif command -v pyenv >/dev/null 2>&1; then
  PYTHON_BIN="$(pyenv which python3)"
else
  PYTHON_BIN="$(command -v python3)"
fi

exec "$PYTHON_BIN" "$SCRIPT_DIR/etf.py" "$@"
