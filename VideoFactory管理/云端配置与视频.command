#!/bin/bash
set -u
cd "$(dirname "$0")" || exit 1
if [ -x /usr/local/bin/python3 ]; then PYTHON=/usr/local/bin/python3; else PYTHON=$(command -v python3 || true); fi
[ -n "$PYTHON" ] || { echo '请安装包含 Tk 的 Python 3。'; exit 1; }
exec "$PYTHON" -B "$(pwd)/cloud_manager.py"
