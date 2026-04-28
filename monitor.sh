#!/bin/bash
# CTF Swarm 监控脚本 — 在 tmux 监控窗口中运行
# 由 coordinator.py --tmux 自动调用

PROJECT_DIR="$1"
BUS_DIR="$2"
CHALLENGE_DESC="$3"

if [ -z "$PROJECT_DIR" ]; then
    echo "Usage: $0 <project_dir> <bus_dir> [challenge_desc]"
    exit 1
fi

cd "$PROJECT_DIR" || exit 1

echo "=== CTF Swarm Monitor ==="
echo "  Challenge: ${CHALLENGE_DESC:-...}"
echo "  Bus: $BUS_DIR"
echo ""
echo "  Watching for flag..."
echo "  Press Ctrl+C in any pane to stop"
echo ""

while true; do
  if [ -f "$BUS_DIR/flag.txt" ]; then
    echo ""
    echo "============================================"
    echo "  🎯 FLAG FOUND!"
    echo "============================================"
    cat "$BUS_DIR/flag.txt"
    echo ""
    if [ -f "$BUS_DIR/leader/writeup.md" ]; then
      echo "--- Writeup ---"
      head -50 "$BUS_DIR/leader/writeup.md"
      echo ""
    fi
    echo "============================================"
    break
  fi

  echo "  --- $(date '+%H:%M:%S') ---"
  for m in member1 member2 member3; do
    f="$BUS_DIR/$m/findings.txt"
    if [ -f "$f" ] && [ -s "$f" ]; then
      first=$(head -1 "$f" 2>/dev/null)
      echo "  [$m] ${first:-(empty)}"
    else
      echo "  [$m] (waiting...)"
    fi
  done

  sleep 5
done
