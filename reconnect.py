#!/usr/bin/env python3
"""Reconnect scheduler to existing tmux session without killing agents."""
import sys
import time
sys.path.insert(0, '/home/yy/my-ctf-swarm')

from pathlib import Path
from bus import FileMessageBus
from coordinator import _run_scheduler, log

bus = FileMessageBus(Path('/home/yy/my-ctf-swarm/bus'))

# Check bus state
meta = bus.get_meta()
print(f"  Session: ctf-swarm")
print(f"  Status: {meta.get('status', '?')}")
print(f"  Challenge: {meta.get('challenge', '?')[:60]}")

log("重新连接调度器到现有 tmux 会话")
_run_scheduler({}, bus, 'ctf-swarm')
