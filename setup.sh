#!/bin/bash
# CTF Swarm 设置脚本 — 创建虚拟环境
set -e

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
echo "=== CTF Swarm Setup ==="
echo "Project: $PROJECT_DIR"
echo ""

for name in claude_leader claude_number1 claude_number2 claude_number3; do
    VENV_DIR="$PROJECT_DIR/venvs/$name"
    if [ -d "$VENV_DIR" ]; then
        echo "[✓] $name already exists"
    else
        echo "[...] Creating $name..."
        python3 -m venv "$VENV_DIR"
        echo "[✓] $name created"
    fi
done

echo ""
echo "Done. Virtual environments created in $PROJECT_DIR/venvs/"
