#!/usr/bin/env python3
"""
CTF Swarm — 多 Claude 实例协作 CTF 解题系统。

像使用 Claude 一样交互式描述题目：

  python coordinator.py
  python coordinator.py --tmux   (自动在 tmux 中启动)

启动后进入交互模式，直接描述你要解的题目即可。
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import yaml

from bus import FileMessageBus

PROJECT_DIR = Path(__file__).parent.resolve()
SESSION_RUNNING = False


def log(msg: str) -> None:
    print(f"  [{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ── 配置加载 ──────────────────────────────────────

def load_config() -> dict:
    path = PROJECT_DIR / "config.yaml"
    if not path.exists():
        print("ERROR: config.yaml not found")
        sys.exit(1)
    with open(path) as f:
        return yaml.safe_load(f) or {}


def build_agent_config(config: dict) -> None:
    config["agent_config"] = {}
    for member in config.get("members", []):
        config["agent_config"][member["id"]] = member
    leader = config.get("leader", {})
    if leader:
        config["agent_config"][leader["id"]] = leader


# ── 题目解析（自然语言） ───────────────────────────

def parse_challenge_input(text: str) -> dict:
    info = {"description": text.strip()}
    text_stripped = text.strip()

    urls = re.findall(r'https?://[^\s]+', text_stripped)
    if urls:
        info["url"] = urls[0]
        info["_type"] = "url"
        # 有 URL 时不再尝试本地路径检测
        return info

    # 仅在输入明确指向本地文件时检测（不再从描述文本中 regex 猜测）
    for w in text_stripped.split():
        w = w.strip("'\"(),.。，：:")
        if not w.startswith('/') and not w.startswith('.'):
            continue  # 只处理以 / 或 . 开头的路径
        try:
            p = Path(w).expanduser().resolve()
            if p.exists():
                if p.is_dir():
                    info["dir"] = str(p)
                    info["_type"] = "dir"
                else:
                    info["file"] = str(p)
                    info["_type"] = "file"
                break
        except (OSError, ValueError):
            continue

    return info


def build_challenge_desc(info: dict) -> str:
    parts = []
    if "url" in info:
        parts.append(f"目标 URL: {info['url']}")
    if "file" in info:
        parts.append(f"目标文件: {info['file']}")
    if "dir" in info:
        parts.append(f"挑战目录: {info['dir']}")
        dir_path = Path(info["dir"])
        if dir_path.exists():
            files = list(dir_path.iterdir())[:20]
            if files:
                parts.append("目录文件:")
                for f in files:
                    parts.append(f"  - {f.name}")
    desc = info.get("description", "")
    for key in ("url", "file", "dir"):
        val = info.get(key)
        if val:
            desc = desc.replace(val, "").strip()
    if desc:
        parts.append(f"描述: {desc}")
    return "\n".join(parts) if parts else desc


# ── 提示词生成 ──────────────────────────────────────

def render_prompt(template: str, **kwargs) -> str:
    for key, val in kwargs.items():
        template = template.replace("{{" + key + "}}", str(val))
    return template


def generate_prompts(config: dict, challenge_desc: str, bus_dir: str) -> list[tuple[str, str, Path]]:
    result = []
    all_agents = list(config.get("members", []))
    leader_cfg = config.get("leader", {})
    if leader_cfg:
        all_agents.append(leader_cfg)

    for agent in all_agents:
        agent_id = agent["id"]
        agent_type = "leader" if agent_id == leader_cfg.get("id") else "member"
        template_path = PROJECT_DIR / "prompts" / f"{agent_type}.md"
        template = template_path.read_text(encoding="utf-8")

        prompt = render_prompt(
            template,
            member_id=agent_id,
            challenge_desc=challenge_desc,
            bus_dir=bus_dir,
        )

        prompt_file = PROJECT_DIR / f".prompt_{agent_id}.md"
        prompt_file.write_text(prompt, encoding="utf-8")
        result.append((agent_id, agent_type, prompt_file))

    return result


# ── 启动成员 ──────────────────────────────────────

def check_tmux() -> bool:
    return shutil.which("tmux") is not None


def _capture_pane(session: str, window: str) -> str:
    """Get current text content of a tmux pane (strip ANSI codes)."""
    r = subprocess.run(
        ["tmux", "capture-pane", "-t", f"{session}:{window}", "-p",
         "-S", "-1000"],  # 只取最近 1000 行，防止 pane 缓冲区无限膨胀
        capture_output=True, text=True, timeout=10,
    )
    return re.sub(r'\x1b\[[\d;]*[a-zA-Z]', '', r.stdout)


def _has_any(content: str, patterns: list[str]) -> bool:
    """Check if any pattern matches the content."""
    for p in patterns:
        if re.search(p, content):
            return True
    return False


def _start_one_agent(session: str, agent_id: str, agent_type: str,
                      agent_cfg: dict, prompt_file: Path) -> bool:
    """Set up one agent in a tmux window. Returns True on success."""
    window_name = agent_id
    env_exports = (
        f"export ANTHROPIC_BASE_URL=\"{agent_cfg['base_url']}\" && "
        f"export ANTHROPIC_API_KEY=\"{agent_cfg['api_key']}\" && "
        f"export ANTHROPIC_MODEL=\"{agent_cfg['model']}\""
    )

    # Create window
    subprocess.run(["tmux", "new-window", "-t", session, "-n", window_name],
                   check=True, timeout=10)

    # Start claude
    claude_path = "/home/yy/.nvm/versions/node/v24.15.0/bin/claude"
    startup = f"cd {PROJECT_DIR} && {env_exports} && export PATH=/home/yy/.nvm/versions/node/v24.15.0/bin:$PATH && {claude_path} --bare --permission-mode bypassPermissions"
    subprocess.run(["tmux", "send-keys", "-t", f"{session}:{window_name}",
                    startup, "Enter"], check=True, timeout=10)

    # ── 处理 claude 启动时的所有交互提示 ──
    # 可能出现的提示（顺序不定）：
    #   1. API key:    "Do you want to use this API key?"  → Up+Enter (选 Yes)
    #   2. Trust:      "trust this folder" / "Yes, I trust" → Enter (默认 Yes)
    #   3. Bypass:     "Bypass Permissions" / "No, exit"   → Down+Enter
    #   4. 就绪:       "Claude Code" 或 "❯" prompt
    #
    READY_PATTERNS = [r"Claude Code", r"❯"]
    API_KEY_PATTERN = [r"Do you want to use this API"]
    TRUST_PATTERNS = [r"trust this folder", r"Yes, I trust"]
    # 注意：不要只匹配 "bypassPermissions"——这个字符串也出现在 bash 命令中！
    BYPASS_PATTERNS = [r"WARNING:.*Bypass Permissions", r"No, exit"]

    deadline = time.monotonic() + 50  # total max 50s per agent
    sent_enter_for_trust = False
    sent_up_for_apikey = False
    sent_down_for_bypass = False

    while time.monotonic() < deadline:
        content = _capture_pane(session, window_name)

        # Check if claude is ready
        if _has_any(content, READY_PATTERNS):
            time.sleep(2)  # wait for prompt to fully render
            break

        # Handle prompts (check all in each iteration)
        if not sent_up_for_apikey and _has_any(content, API_KEY_PATTERN):
            subprocess.run(["tmux", "send-keys", "-t", f"{session}:{window_name}",
                            "Up", "Enter"], check=True, timeout=10)
            sent_up_for_apikey = True
            log(f"[{agent_id}] api key accepted")
            time.sleep(1)

        if not sent_enter_for_trust and _has_any(content, TRUST_PATTERNS):
            subprocess.run(["tmux", "send-keys", "-t", f"{session}:{window_name}",
                            "Enter"], check=True, timeout=10)
            sent_enter_for_trust = True
            log(f"[{agent_id}] trust accepted")
            time.sleep(1)

        if not sent_down_for_bypass and _has_any(content, BYPASS_PATTERNS):
            # Send Down first, then Enter separately
            subprocess.run(["tmux", "send-keys", "-t", f"{session}:{window_name}",
                            "Down"], check=True, timeout=10)
            time.sleep(0.3)
            subprocess.run(["tmux", "send-keys", "-t", f"{session}:{window_name}",
                            "Enter"], check=True, timeout=10)
            sent_down_for_bypass = True
            log(f"[{agent_id}] bypass accepted")
            time.sleep(1)

        # None matched, wait and retry
        time.sleep(0.5)
    else:
        log(f"[{agent_id}] WARNING: claude did not become ready in time")
        return False

    # Paste the prompt
    prompt_text = prompt_file.read_text(encoding="utf-8")
    buf_name = f"prompt_{agent_id}"
    subprocess.run(["tmux", "set-buffer", "-b", buf_name, prompt_text],
                   check=True, timeout=10)
    subprocess.run(
        ["tmux", "paste-buffer", "-b", buf_name,
         "-t", f"{session}:{window_name}", "-d"],
        check=True, timeout=10,
    )
    time.sleep(1.0)

    # 检查 paste-buffer 是否被折叠
    content = _capture_pane(session, window_name)
    if "paste again to expand" in content.lower():
        log(f"[{agent_id}] paste-buffer 被折叠，改用 send-keys 逐行输入")
        _send_keys_typed(session, window_name, prompt_text)

    time.sleep(0.5)

    # Submit
    subprocess.run(["tmux", "send-keys", "-t", f"{session}:{window_name}",
                    "Enter"], check=True, timeout=10)

    log(f"✓ {agent_id} [{agent_type}] ({agent_cfg['model']})")
    return True


def tmux_start(config: dict, prompt_info: list, challenge_desc: str) -> None:
    session_name = "ctf-swarm"
    bus_dir = str(PROJECT_DIR / (config.get("bus_dir", "bus")))

    subprocess.run(["tmux", "new-session", "-d", "-s", session_name, "-n", "monitor"],
                   check=True, timeout=5)
    subprocess.run(["tmux", "set-option", "-g", "history-limit", "3000"],
                   capture_output=True, timeout=5)

    monitor_cmd = (
        f"bash {PROJECT_DIR / 'monitor.sh'} {PROJECT_DIR} {bus_dir} '{challenge_desc[:80]}'"
    )
    subprocess.run(["tmux", "send-keys", "-t", f"{session_name}:monitor",
                    monitor_cmd, "Enter"], check=True, timeout=5)

    # 阶段1：启动 3 个成员 + 初始唤醒
    log("启动 3 个成员...")
    _initial_wake_members(session_name, config, prompt_info)

    # 阶段2：等成员 findings 就绪后启动队长
    leader_info = None
    for agent_id, agent_type, prompt_file in prompt_info:
        if agent_type == "leader":
            leader_info = (agent_id, agent_type, prompt_file)
            break

    if leader_info:
        agent_id, agent_type, prompt_file = leader_info
        agent_cfg = config["agent_config"][agent_id]
        log(f"启动 {agent_id} [leader]（成员已有 findings）...")
        _start_one_agent(session_name, agent_id, agent_type, agent_cfg, prompt_file)
        log(f"✓ {agent_id} 就绪")

    subprocess.run(["tmux", "select-window", "-t", f"{session_name}:monitor"],
                   check=True, timeout=5)

    log(f"全部启动完毕！")
    print(f"\n  📌 tmux 会话已启动: tmux attach -t {session_name}")
    print(f"     调度器将自动检测 flag，可在本终端观察进度")


# ── 调度器（永久监控 + 唤醒 agent） ──────────────

_IDLE_CHECK_INTERVAL = 15        # 轮询间隔（秒）
_MIN_WAKEUP_INTERVAL = 30        # 同一 agent 最短唤醒间隔
_STARTUP_GRACE = 30              # 启动后前 N 秒不唤醒
_AGENT_RECOVERY_INTERVAL = 60    # API 错误自动重试最小间隔（秒）
_MAX_RECOVERY_RETRIES = 3        # 单个 agent 最大自动重试次数


def _is_agent_idle(session: str, agent_id: str) -> bool:
    """Scan from bottom of pane — if the first meaningful content is ❯, agent is idle.

    Chrome lines (separators, status bar, tips) are skipped. Active thinking
    indicators (present-continuous verbs, "still thinking", etc.) mean NOT idle.
    Past-tense summaries like "Brewed for" remain in the buffer after ❯ appears
    and are ignored — ❯ is the signal.
    """
    try:
        content = _capture_pane(session, agent_id)
        lines = content.split('\n')
        thinking_indicators = (
            'still thinking', 'thinking more', 'Sprouting', 'Computing',
            'Razzmatazzing', 'Bootstrapping', 'Analyzing',
            'Swirling', 'Composing', 'Billowing', 'Brewing',
            'Schlepping', 'Cogitating', 'Cultivating', 'Churning',
            'Photosynthesizing', 'Whirlpooling', 'Beboppin', 'Sautéed',
        )
        for line in reversed(lines):
            stripped = line.strip()
            if not stripped:
                continue
            # UI chrome — skip
            if stripped.startswith('⏵⏵') or stripped.startswith('⎿') or stripped.startswith('▌'):
                continue
            if set(stripped) == {'─'} or stripped == '---':
                continue
            # Active thinking — not idle
            for indicator in thinking_indicators:
                if indicator.lower() in stripped.lower():
                    return False
            # ❯ in any position means idle (it's the prompt marker)
            if '❯' in stripped:
                return True
            # Any other meaningful content without ❯ means not idle
            return False
        return False
    except Exception:
        return False




_API_ERROR_PATTERNS = (
    'api error',
    'socket connection was closed',
    'socket closed unexpectedly',
    'connection error',
    'insufficient balance',
    'rate limit exceeded',
    'internal server error',
    'bad gateway',
    'service unavailable',
    'timeout occurred',
)


def _has_api_error(content: str) -> bool:
    """Check if an agent pane contains an API error that needs recovery."""
    for pattern in _API_ERROR_PATTERNS:
        if pattern in content.lower():
            return True
    return False


def _recover_agent(session: str, agent_id: str) -> None:
    """Recover an agent from API error by sending Enter to retry."""
    subprocess.run(["tmux", "send-keys", "-t", f"{session}:{agent_id}", "Escape"],
                   capture_output=True, timeout=5)
    time.sleep(0.3)
    subprocess.run(["tmux", "send-keys", "-t", f"{session}:{agent_id}", "Enter"],
                   capture_output=True, timeout=5)
    log(f"🔄 自动恢复 {agent_id}（API 错误后重试）")


# ── 被动捕获：观察 agent 操作 ──────────────────────

_OBSERVATION_NOISE = (
    'skip this response', 'choose response', '(shift+tab to cycle)',
    'esc to interrupt', 'bypass permissions', 'paste again to expand',
    'Press up to edit', 'current work', 'Press up to edit queued messages',
    '/btw', 'Use /btw',
    # curl output noise
    'URL encoding', 'special characters', 'curl -s', 'curl -v', 'curl -i',
    'user_agent', 'Content-Type', 'Accept:', 'text/html', 'text/plain',
    'HTTP/1.1', 'HTTP/2', 'Server:', 'Date:', 'X-Powered-By',
    # command echo noise
    'echo "', "echo '", 'esac', ';;', ';; esac',
    # ===== separator lines
    '===========', '----------', '_________',
)

# Thinking animation words — used in _is_noise but overridden when line has timing info
_THINKING_NOISE = (
    'Scampering', 'Sprouting', 'Computing', 'Frolicking', 'Bootstrapping',
    'Analyzing', 'Swirling', 'Composing', 'Billowing', 'Brewing',
    'Catapulting', 'Flibbertigibbeting', 'Manifesting', 'Twisting',
    'Photosynthesizing', 'Improvising', 'Sock-hopping', 'Razzmatazzing',
    'Nucleating', 'Metamorphosing', 'Crunching', 'Nesting', 'Sprouting',
    'still thinking', 'thinking more', 'thinking some more', 'almost done thinking',
    'Baked for', 'Brewed for',
)


def _is_noise(line: str) -> bool:
    """Check if a line is noise (empty, separator, thinking indicator, UI chrome)."""
    if not line.strip():
        return True
    stripped = line.strip()
    if stripped in ('────────────────────────────────────────────────────────────────────────────────', '---'):
        return True
    if stripped.startswith('⎿') or stripped.startswith('▌'):
        return True
    if stripped == '❯' or stripped.startswith('⏵⏵'):
        return True
    for kw in _OBSERVATION_NOISE:
        if kw.lower() in stripped.lower():
            return True
    for kw in _THINKING_NOISE:
        if kw.lower() in stripped.lower():
            return True
    return False


_OBS_SEEN_CACHE: dict[str, set[str]] = {}  # member_id → set of already-captured line hashes

def _capture_observations(session: str, member_id: str, _unused: dict | None = None) -> tuple[str, bool, list[str]]:
    """捕获 pane 中未记录过的有意义行。

    tmux pane 固定高度（24行），新输出会顶掉旧行，因此不能靠行号增量。
    改用内容去重：只返回未在 observations.txt 中出现过的行。
    返回 (content, has_new, meaningful_lines)。"""
    try:
        content = _capture_pane(session, member_id)
        lines = content.split('\n')

        # 首轮初始化
        if member_id not in _OBS_SEEN_CACHE:
            _OBS_SEEN_CACHE[member_id] = set()
            # 加载已持久化的行，避免重复
            obs_path = PROJECT_DIR / "bus" / member_id / "observations.txt"
            if obs_path.exists():
                for line in obs_path.read_text(encoding='utf-8').split('\n'):
                    line = line.strip()
                    if line:
                        _OBS_SEEN_CACHE[member_id].add(line)

        # 提取当前 pane 中的所有有意义行
        current_meaningful = []
        for line in lines:
            stripped = line.strip()
            if stripped and not _is_noise(stripped):
                # 用行内容做 key（取前 200 字符去重）
                key = stripped[:200]
                if key not in _OBS_SEEN_CACHE[member_id]:
                    _OBS_SEEN_CACHE[member_id].add(key)
                    current_meaningful.append(stripped)

        if current_meaningful:
            obs_path = PROJECT_DIR / "bus" / member_id / "observations.txt"
            timestamp = time.strftime('%H:%M:%S')
            for line in current_meaningful:
                entry = f'[{timestamp}] {line}\n'
                with open(obs_path, 'a') as f:
                    f.write(entry)

            # 限制缓存大小
            if len(_OBS_SEEN_CACHE[member_id]) > 1000:
                _OBS_SEEN_CACHE[member_id] = set(sorted(_OBS_SEEN_CACHE[member_id])[-500:])

            return content, True, current_meaningful

        return content, False, []
    except Exception:
        return '', False, []


def _build_observations_summary(member_id: str, max_lines: int = 15) -> str:
    """Read the last N lines of a member's observations file."""
    obs_path = PROJECT_DIR / "bus" / member_id / "observations.txt"
    if not obs_path.exists():
        return ''
    lines = obs_path.read_text(encoding='utf-8').strip().split('\n')
    recent = lines[-max_lines:]
    return '\n'.join(recent)


def _capture_member_status(session: str, member_id: str) -> str:
    """Capture the current status line from a member's pane (first non-noise line from bottom)."""
    try:
        content = _capture_pane(session, member_id)
        lines = content.split('\n')
        for line in reversed(lines):
            stripped = line.strip()
            if stripped and not _is_noise(stripped):
                return stripped
        return '(unknown)'
    except Exception:
        return '(unknown)'


def _build_wakeup_context(session: str, bus: FileMessageBus,
                          stale_hint: str = "") -> str:
    """构建唤醒消息：包含队员 findings + 实时状态 + 卡死警告。"""
    parts = []
    if stale_hint:
        parts.append(stale_hint)
    parts.append("=== 系统通知 ===\n成员有新进展，请综合以下信息决策：\n")

    for mid in ("member1", "member2", "member3"):
        finding = bus.get_finding(mid)
        if finding:
            preview = finding.strip().split('\n')[:10]
            parts.append(f"\n[{mid}]:")
            parts.extend(f"  {l}" for l in preview)

    # 实时状态（即使队员在长思考也捕获当前行）
    parts.append("\n【当前状态】")
    for mid in ("member1", "member2", "member3"):
        status = _capture_member_status(session, mid)
        if status:
            parts.append(f"  {mid}: {status}")

    # 操作记录（observations.txt 最近 10 行 — 筛选 noise 后的实时活动）
    parts.append("\n【近期活动】")
    for mid in ("member1", "member2", "member3"):
        obs_path = bus.bus_dir / mid / "observations.txt"
        if obs_path.exists():
            try:
                obs_lines = obs_path.read_text(encoding="utf-8").strip().split("\n")
                recent = [l for l in obs_lines if l.strip()][-10:]
                if recent:
                    parts.append(f"  {mid}:")
                    for l in recent:
                        parts.append(f"    {l.strip()}")
            except Exception:
                pass

    parts.append("\n---")
    parts.append("根据情况通过 advice_memberX.txt 进行针对性指挥。")
    parts.append("写入后调度器会强制打断对应成员。")
    return "\n".join(parts)


def _wake_agent(session: str, agent_id: str, message: str) -> None:
    """向 agent 发送唤醒消息 + Enter 提交。先 Esc 清状态再 paste。

    paste-buffer 被折叠时直接回车展开粘贴内容，确保消息完整发送。
    """
    # 1. Esc 确保回到干净 prompt（打断思考 / 清 queued messages）
    subprocess.run(["tmux", "send-keys", "-t", f"{session}:{agent_id}", "Escape"],
                   capture_output=True, timeout=5)
    time.sleep(0.5)

    # 2. Try paste-buffer
    buf_name = f"swarm_{agent_id}"
    subprocess.run(["tmux", "set-buffer", "-b", buf_name, message],
                   capture_output=True, timeout=5)
    subprocess.run(["tmux", "paste-buffer", "-b", buf_name,
                    "-t", f"{session}:{agent_id}", "-d"],
                   capture_output=True, timeout=5)
    time.sleep(0.8)

    # 3. Check if paste was collapsed by Claude Code
    content = _capture_pane(session, agent_id)
    if "paste again to expand" in content.lower():
        # Paste folded → just press Enter to expand + submit as one message
        # This avoids _send_keys_typed splitting the message into separate inputs
        pass
    else:
        # 4. 常规路径：检查是否被 queue，需要 Up 提交
        if "queued messages" in content.lower():
            subprocess.run(["tmux", "send-keys", "-t", f"{session}:{agent_id}", "Up"],
                           capture_output=True, timeout=5)
            time.sleep(0.3)

    # 5. Enter 提交（对于折叠的 paste，这会展开并提交；对于常规路径，提交已输入内容）
    subprocess.run(["tmux", "send-keys", "-t", f"{session}:{agent_id}", "Enter"],
                   capture_output=True, timeout=5)


def _send_keys_typed(session: str, member_id: str, message: str) -> None:
    """逐行用 send-keys 输入消息（paste-buffer 的回退方案）。

    Claude Code 经常把 paste-buffer 折叠成 'paste again to expand'，
    用 send-keys 模拟真实键盘输入虽然慢但 100% 可靠。

    注意：过滤掉空行避免触发 API 400 "text content cannot be empty"。
    """
    lines = [l for l in message.split('\n') if l.strip()]
    for i, line in enumerate(lines):
        subprocess.run(
            ["tmux", "send-keys", "-t", f"{session}:{member_id}", line],
            capture_output=True, timeout=5)
        if i < len(lines) - 1:
            subprocess.run(
                ["tmux", "send-keys", "-t", f"{session}:{member_id}", "Enter"],
                capture_output=True, timeout=5)


def _force_push_message(session: str, member_id: str, msg: str, buf_suffix: str = "fpush") -> None:
    """通用强制推送：检测状态 → 仅必要时打断 → paste 消息 → Enter。

    注意：3x Esc 在 Claude 处于 "Interrupted" 状态时会破坏输入，
    导致 paste 被吞掉。改用状态感知：
    - 思考中 → 1x Esc 打断
    - Interrupted → 直接输入（无需 Esc）
    - 空闲 → 直接输入
    """
    content = _capture_pane(session, member_id)
    needs_interrupt = (
        not _is_agent_idle(session, member_id)
        and "what should claude do instead" not in content.lower()
    )

    if needs_interrupt:
        subprocess.run(["tmux", "send-keys", "-t", f"{session}:{member_id}", "Escape"],
                       capture_output=True, timeout=5)
        time.sleep(0.4)

    buf_name = f"{buf_suffix}_{member_id}"
    subprocess.run(["tmux", "set-buffer", "-b", buf_name, msg],
                   capture_output=True, timeout=5)
    subprocess.run(["tmux", "paste-buffer", "-b", buf_name,
                    "-t", f"{session}:{member_id}", "-d"],
                   capture_output=True, timeout=5)
    time.sleep(0.8)

    # Verify: paste-buffer often gets collapsed to "paste again to expand"
    content = _capture_pane(session, member_id)
    if "paste again to expand" in content.lower():
        _send_keys_typed(session, member_id, msg)
        time.sleep(0.5)

    # 检查是否被 queue，需要 Up 提交
    content = _capture_pane(session, member_id)
    if "queued messages" in content.lower():
        subprocess.run(["tmux", "send-keys", "-t", f"{session}:{member_id}", "Up"],
                       capture_output=True, timeout=5)
        time.sleep(0.3)

    subprocess.run(["tmux", "send-keys", "-t", f"{session}:{member_id}", "Enter"],
                   capture_output=True, timeout=5)


def _force_push_targeted_advice(session: str, member_id: str) -> None:
    """强制打断某个成员：队长有针对性纠偏。直接从文件中读取建议内容内联推送，
    避免成员自己 Read 文件后 Write 副本到本地目录。"""
    advice_path = Path(__file__).parent / "bus" / "leader" / f"advice_{member_id}.txt"
    advice_content = ""
    try:
        if advice_path.exists():
            lines = advice_path.read_text(encoding="utf-8").strip().split("\n")
            advice_content = "\n".join(lines[:30])  # 最多30行
    except Exception:
        pass

    if advice_content:
        _force_push_message(session, member_id,
            "=== ⚠ 紧急 ===\n"
            f"队长强制指示（{member_id}）：\n\n"
            f"{advice_content}",
            buf_suffix="tadv")
    else:
        _force_push_message(session, member_id,
            "=== ⚠ 紧急 ===\n"
            f"队长有强制指示！请立即停止当前工作。\n"
            f"读取 ./bus/leader/advice_{member_id}.txt 获取详细指令。",
            buf_suffix="tadv")


def _force_push_broadcast_advice(session: str) -> None:
    """广播全局策略给所有成员：队长完成了战略部署。直接内联文件内容。"""
    advice_path = Path(__file__).parent / "bus" / "leader" / "advice.txt"
    advice_content = ""
    try:
        if advice_path.exists():
            lines = advice_path.read_text(encoding="utf-8").strip().split("\n")
            advice_content = "\n".join(lines[:30])
    except Exception:
        pass

    for mid in ("member1", "member2", "member3"):
        if advice_content:
            _force_push_message(session, mid,
                "=== 战略部署 ===\n"
                f"队长发布了全局策略：\n\n{advice_content}",
                buf_suffix="badv")
        else:
            _force_push_message(session, mid,
                "=== 战略部署 ===\n"
                "队长发布了全局策略！请立即停止当前工作。\n"
                "读取 ./bus/leader/advice.txt 了解你的任务分配。",
                buf_suffix="badv")


# ── 启动增强：队长延迟 + 初始唤醒 ─────────────────


def _initial_wake_members(session: str, config: dict, prompt_info: list) -> None:
    """先启动所有成员，发初始唤醒要求写 findings，再启动队长。"""
    # 启动 3 个成员
    for agent_id, agent_type, prompt_file in prompt_info:
        if agent_type == "leader":
            continue
        agent_cfg = config["agent_config"][agent_id]
        log(f"启动 {agent_id} [member]...")
        _start_one_agent(session, agent_id, agent_type, agent_cfg, prompt_file)
        log(f"✓ {agent_id} 就绪")

    # 等待所有成员进入 idle 状态后再发唤醒（避免 agent 还在处理初始 prompt）
    log("等待成员就绪...")
    idle_deadline = time.monotonic() + 40
    while time.monotonic() < idle_deadline:
        all_idle = all(
            _is_agent_idle(session, agent_id)
            for agent_id, agent_type, _ in prompt_info
            if agent_type != "leader"
        )
        if all_idle:
            break
        time.sleep(1)
    else:
        log("WARNING: 部分成员未进入 idle 状态，直接发送唤醒")

    # 初始唤醒：要求每个成员写 findings
    log("发送初始唤醒，要求成员写入 findings...")
    for agent_id, agent_type, _ in prompt_info:
        if agent_type == "leader":
            continue
        _wake_agent(session, agent_id,
            "=== 启动初始化 ===\n请立即将你的初始分析方向和计划写入 findings.txt（滚动列表格式），同时追加到 log.txt，然后开始解题。")  # noqa: E501

    # 等待所有成员产生 findings（最多 60s）
    deadline = time.monotonic() + 60
    findings = [False, False, False]
    while time.monotonic() < deadline:
        findings = []
        for mid in ("member1", "member2", "member3"):
            fp = PROJECT_DIR / "bus" / mid / "findings.txt"
            findings.append(fp.exists() and fp.stat().st_size > 10)
        if all(findings):
            log("所有成员已写入 findings.txt")
            return
        # 顺便扫描 flag
        flag, finder = _scan_panes_for_flag(session)
        if flag:
            bus_dir = PROJECT_DIR / "bus"
            meta_path = bus_dir / "meta.json"
            flag_path = bus_dir / "flag.txt"
            # 写入 meta.json
            if meta_path.exists():
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            else:
                meta = {}
            meta["status"] = "solved"
            meta["flag"] = flag
            meta["found_by"] = finder or "unknown"
            meta["found_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
            # 写入 flag.txt
            flag_path.write_text(f"{finder}: {flag}\n", encoding="utf-8")
            log(f"🎯 Flag detected from [{finder}] pane during init: {flag}")
            return
        time.sleep(2)

    ok_count = sum(1 for ok in findings if ok)
    log(f"WARNING: 仅 {ok_count}/3 成员写了 findings.txt（继续启动队长，调度器后续会唤醒滞后的成员）")
def _scan_panes_for_flag(session: str) -> tuple[str | None, str | None]:
    """Scan all agent tmux panes for real CTF flag patterns (fallback for agent hallucination).
    Avoids false positives from test flags, code snippets, or example text."""
    FLAG_PATTERNS = (
        r'ctfshow\{[a-f0-9\-]{20,}\}',                    # ctfshow UUID format
        r'flag(?!\{[^}]*?(?:test|example|local|TODO|xxx))'  # exclude common test patterns
        r'\{[a-zA-Z0-9_\-]{10,}\}',                         # flag{...} with 10+ chars
    )
    for agent_id in ("member1", "member2", "member3", "leader"):
        try:
            content = _capture_pane(session, agent_id)
            for pattern in FLAG_PATTERNS:
                m = re.search(pattern, content, re.IGNORECASE)
                if m:
                    flag = m.group(0)
                    # Double-check: flag content shouldn't contain test keywords
                    inner = flag.split('{', 1)[1].rstrip('}')
                    if re.search(r'(?:test|example|local|placeholder|TODO|xxxx)', inner, re.IGNORECASE):
                        continue
                    return flag, agent_id
        except Exception:
            continue
    return None, None


def _run_scheduler(config: dict, bus: FileMessageBus, session: str) -> None:
    """永久调度循环：监视文件 mtime 变化 → 唤醒对应 agent。不做判断，不做转述。"""
    # ── ObservationBuffer：积累 observations → 适当时机推送给 leader ──
    class ObservationBuffer:
        """积累成员的 observations，在 leader 空闲时推送。

        推送策略（避免频繁打断 leader）：
        - 等待 advice.txt 写入后再开始推送，给 leader 完成战略部署的时间
        - 最小间隔 90 秒，让 leader 有时间思考与写建议
        - 积累 20 条或 6 轮再推送，减少琐碎干扰
        - 内容去重：如果内容与上次推送基本相同，跳过
        - 强制打断阈值提高到 40 条 / 15 轮（避免 leader 无限期忙导致丢消息）
        """
        _MIN_INTERVAL = 90.0       # 最短推送间隔（秒）
        _FLUSH_MIN_ITEMS = 20       # buffer 满多少条就推送
        _FLUSH_MIN_ROUNDS = 6       # 积累多少轮就推送
        _FORCE_MAX_ITEMS = 40       # 堆积到上限强制打断 leader
        _FORCE_MAX_ROUNDS = 15      # 积累上限轮次强制打断

        def __init__(self):
            self._buffer: dict[str, list[tuple[str, str]]] = {}
            self._rounds = 0
            self._last_flush = 0.0
            self._last_body_hash = ''  # 上次推送内容的 hash，用于去重
            self._advice_seen = False  # advice.txt 是否已被调度器检测到

        def add(self, member_id: str, lines: list[str]) -> None:
            ts = time.strftime('%H:%M:%S')
            if member_id not in self._buffer:
                self._buffer[member_id] = []
            for line in lines:
                self._buffer[member_id].append((ts, line.strip()[:120]))

        @property
        def _total(self) -> int:
            return sum(len(v) for v in self._buffer.values())

        def _body(self) -> str:
            """生成不变更内部状态的 body，用于去重比较。"""
            lines_out = []
            for mid in ("member1", "member2", "member3"):
                entries = self._buffer.get(mid, [])
                if entries:
                    lines_out.append(f"[{mid}]: {len(entries)}条")
                    for ts, line in entries[-5:]:
                        lines_out.append(f"  [{ts}] {line[:120]}")
            return '\n'.join(lines_out)

        def mark_advice_seen(self) -> None:
            """标记 advice.txt 已被调度器检测并广播，此后观察推送才启动。"""
            self._advice_seen = True

        def decide(self, now_mono: float, leader_idle: bool) -> str | None:
            """决策是否推送。返回消息字符串(需推送)或 None(继续积累)。"""
            if self._total == 0:
                self._rounds = 0
                return None

            elapsed = now_mono - self._last_flush

            # 强制打断：堆积过多（不受 advice_seen 影响，避免 buffer 无限膨胀）
            if self._total >= self._FORCE_MAX_ITEMS or self._rounds >= self._FORCE_MAX_ROUNDS:
                return self._build(now_mono)

            # 等待 leader 写完 advice.txt 后再推送，避免打断战略部署
            if not self._advice_seen:
                self._rounds += 1
                return None

            # Leader 忙 → 只积累，不推送
            if not leader_idle:
                self._rounds += 1
                return None

            # 还没到最小间隔
            if elapsed < self._MIN_INTERVAL:
                self._rounds += 1
                return None

            # 内容去重：如果 body 跟上一次推的一模一样，跳过
            current_body = self._body()
            if current_body == self._last_body_hash:
                self._rounds += 1
                return None

            self._rounds += 1
            if self._total >= self._FLUSH_MIN_ITEMS or self._rounds >= self._FLUSH_MIN_ROUNDS:
                return self._build(now_mono)

            return None

        def _build(self, now_mono: float) -> str:
            parts = [f"【观测积累 — 共 {self._total} 条新活动】"]
            for mid in ("member1", "member2", "member3"):
                entries = self._buffer.get(mid, [])
                if entries:
                    parts.append(f"  [{mid}]:")
                    for ts, line in entries[-5:]:
                        parts.append(f"    [{ts}] {line[:120]}")
                    self._buffer[mid] = []
            self._last_flush = now_mono
            self._rounds = 0
            self._last_body_hash = ''
            return "\n".join(parts)

    log("调度器启动 — 文件监视模式")

    # 跟踪 mtime
    _findings_mtimes: dict[str, float] = {}
    _advice_txt_mtime: float = 0.0
    _advice_mtimes: dict[str, float] = {}
    _pending_leader_notify = False
    _last_heartbeat_ts = 0.0
    _HEARTBEAT_INTERVAL = 180.0  # 每 180s 向队长同步队员状态
    _STALE_THRESHOLD = 300.0    # 成员 findings 超过 5 分钟无变化视为卡死
    _last_leader_wake = 0.0
    _last_member_wake: dict[str, float] = {}
    _findings_hashes: dict[str, str] = {}  # content hash for stale detection
    _last_stale_warn: dict[str, float] = {}  # last time we warned about each member being stale
    # API 错误自动恢复跟踪
    _agent_error_counts: dict[str, int] = {}
    _last_agent_recovery: dict[str, float] = {}
    # ObservationBuffer：积累 observations → 适当时机推送 leader
    _obs_buffer = ObservationBuffer()
    startup_time = time.monotonic()

    # 初始化：记录当前 mtime（避免启动时误触发）
    advice_txt_path = bus.bus_dir / "leader" / "advice.txt"
    if advice_txt_path.exists():
        _advice_txt_mtime = advice_txt_path.stat().st_mtime
    for mid in ("member1", "member2", "member3"):
        fp = bus.bus_dir / mid / "findings.txt"
        _findings_mtimes[mid] = fp.stat().st_mtime if fp.exists() else 0
        try:
            _findings_hashes[mid] = fp.read_text(encoding='utf-8') if fp.exists() else ''
        except Exception:
            _findings_hashes[mid] = ''
        _last_stale_warn[mid] = time.monotonic()
        ap = bus.bus_dir / "leader" / f"advice_{mid}.txt"
        _advice_mtimes[mid] = ap.stat().st_mtime if ap.exists() else 0
        _agent_error_counts[mid] = 0
        _last_agent_recovery[mid] = 0.0
    _agent_error_counts["leader"] = 0
    _last_agent_recovery["leader"] = 0.0

    def _handle_flag(flag: str, finder: str) -> None:
        """Flag 已找到，收尾。"""
        log(f"🎯 Flag found by {finder}: {flag}")
        time.sleep(3)
        for agent_id in ("member1", "member2", "member3"):
            _wake_agent(session, agent_id,
                "=== 挑战已结束 ===\nFlag 已被找到，解题完成。")
        _wake_agent(session, "leader",
            "Flag 已找到！请汇总 writeup 写入 bus/leader/writeup.md")
        wp_path = bus.bus_dir / "leader" / "writeup.md"
        for _ in range(12):
            time.sleep(5)
            if wp_path.exists() and wp_path.stat().st_size > 0:
                break
        print(f"\n  ✅ FLAG: {flag} (by {finder})")
        if wp_path.exists():
            print(f"\n  📝 Writeup:\n{wp_path.read_text(encoding='utf-8')[:2000]}")

    try:
        while True:
            if time.monotonic() - startup_time < _STARTUP_GRACE:
                time.sleep(_IDLE_CHECK_INTERVAL)
                continue

            # ── Flag 检测 ──
            if bus.is_solved() or (bus.bus_dir / "flag.txt").exists():
                meta = bus.get_meta()
                flag = meta.get("flag", "?")
                finder = meta.get("found_by", "?")
                if flag == "?":
                    flag = "flag{...}"
                _handle_flag(flag, finder)
                return

            flag, finder = _scan_panes_for_flag(session)
            if flag and not bus.is_solved():
                bus.set_flag_found(flag, finder or "unknown")
                _handle_flag(flag, finder or "unknown")
                return

            now_mono = time.monotonic()

            # ── 监测 findings 变化 → 标记待通知 → 唤醒 leader ──
            for mid in ("member1", "member2", "member3"):
                fp = bus.bus_dir / mid / "findings.txt"
                cur = fp.stat().st_mtime if fp.exists() else 0
                prev = _findings_mtimes.get(mid, -1)
                if cur != prev:
                    _findings_mtimes[mid] = cur
                    _pending_leader_notify = True
                    # 更新 content hash 用于卡死检测
                    try:
                        _findings_hashes[mid] = fp.read_text(encoding='utf-8')
                    except Exception:
                        _findings_hashes[mid] = ''
                    _last_stale_warn[mid] = now_mono

            if _pending_leader_notify:
                if now_mono - _last_leader_wake >= _MIN_WAKEUP_INTERVAL:
                    if _is_agent_idle(session, "leader"):
                        _wake_agent(session, "leader",
                            _build_wakeup_context(session, bus))
                        _last_leader_wake = now_mono
                        _pending_leader_notify = False
                        log("唤醒 leader（新发现）")

            # ── 监测 advice.txt 变化 → 广播全局策略给所有成员 ──
            ap_txt = bus.bus_dir / "leader" / "advice.txt"
            cur_txt = ap_txt.stat().st_mtime if ap_txt.exists() else 0
            if cur_txt and cur_txt != _advice_txt_mtime:
                _advice_txt_mtime = cur_txt
                _force_push_broadcast_advice(session)
                _obs_buffer.mark_advice_seen()  # 激活观察推送
                log("广播全局策略（advice.txt）")

            # ── 监测 advice_memberX.txt 变化 → 强制打断对应成员 ──
            for mid in ("member1", "member2", "member3"):
                ap = bus.bus_dir / "leader" / f"advice_{mid}.txt"
                cur = ap.stat().st_mtime if ap.exists() else 0
                prev = _advice_mtimes.get(mid, -1)
                if cur != prev:
                    _advice_mtimes[mid] = cur
                    if now_mono - _last_member_wake.get(mid, 0) >= _MIN_WAKEUP_INTERVAL:
                        _force_push_targeted_advice(session, mid)
                        _last_member_wake[mid] = now_mono
                        log(f"强制打断 {mid}（新建议）")

            # ── 捕获操作记录 → 持久化 + 喂入 ObservationBuffer ──
            for mid in ("member1", "member2", "member3"):
                _, _, new_lines = _capture_observations(session, mid)
                if new_lines:
                    _obs_buffer.add(mid, new_lines)

            # ── ObservationBuffer 决策：是否推送积累的观测给 leader ──
            leader_idle = _is_agent_idle(session, "leader")
            obs_msg = _obs_buffer.decide(now_mono, leader_idle)
            if obs_msg:
                if leader_idle:
                    _wake_agent(session, "leader",
                        _build_wakeup_context(session, bus) + "\n\n" + obs_msg)
                    _last_leader_wake = now_mono
                    log("观测积累推送 leader")
                else:
                    # 强制打断（堆积过多）
                    _wake_agent(session, "leader", obs_msg)
                    _last_leader_wake = now_mono
                    log("观测积累强制打断 leader")

            # ── 状态心跳：定时向队长同步队员动向 ──
            if (now_mono - _last_heartbeat_ts >= _HEARTBEAT_INTERVAL
                    and now_mono - _last_leader_wake >= _MIN_WAKEUP_INTERVAL):
                _last_heartbeat_ts = now_mono
                if _is_agent_idle(session, "leader"):
                    _wake_agent(session, "leader",
                        _build_wakeup_context(session, bus))
                    _last_leader_wake = now_mono
                    log("状态心跳唤醒 leader")

            # ── 卡死检测：成员 findings 长时间未变化 → 预警 leader ──
            stale_alert = []
            for mid in ("member1", "member2", "member3"):
                last_change = _last_stale_warn.get(mid, startup_time)
                if now_mono - last_change >= _STALE_THRESHOLD:
                    fp = bus.bus_dir / mid / "findings.txt"
                    try:
                        cur_hash = fp.read_text(encoding='utf-8')
                        prev_hash = _findings_hashes.get(mid, '')
                        if cur_hash == prev_hash and cur_hash.strip():
                            stale_alert.append(mid)
                    except Exception:
                        pass

            if stale_alert and now_mono - _last_leader_wake >= _MIN_WAKEUP_INTERVAL:
                if _is_agent_idle(session, "leader"):
                    _wake_agent(session, "leader",
                        _build_wakeup_context(session, bus,
                            stale_hint="⚠️ 卡死预警：以下成员长时间无进展：" +
                            ", ".join(stale_alert)))
                    _last_leader_wake = now_mono
                    # Reset stale timers so we don't spam
                    for mid in stale_alert:
                        _last_stale_warn[mid] = now_mono
                    log(f"卡死预警唤醒 leader（{' '.join(stale_alert)}）")

            # ── Agent 健康检测：API 错误自动恢复 ──
            for agent_id in ("leader", "member1", "member2", "member3"):
                if agent_id not in _last_agent_recovery:
                    continue
                try:
                    content = _capture_pane(session, agent_id)
                    if _has_api_error(content):
                        if _agent_error_counts.get(agent_id, 0) >= _MAX_RECOVERY_RETRIES:
                            continue  # 已达最大重试次数，不再干预
                        if now_mono - _last_agent_recovery.get(agent_id, 0) >= _AGENT_RECOVERY_INTERVAL:
                            _recover_agent(session, agent_id)
                            _agent_error_counts[agent_id] = _agent_error_counts.get(agent_id, 0) + 1
                            _last_agent_recovery[agent_id] = now_mono
                    else:
                        # 无错误 → 重置计数（说明 agent 已恢复）
                        _agent_error_counts[agent_id] = 0
                except Exception:
                    continue

            time.sleep(_IDLE_CHECK_INTERVAL)

    except KeyboardInterrupt:
        log("调度器停止")


def print_manual_instructions(config: dict, prompt_info: list) -> None:
    print()
    for agent_id, agent_type, prompt_file in prompt_info:
        role = "队长" if agent_type == "leader" else "成员"
        agent_cfg = config["agent_config"][agent_id]
        print(f"  ┌─ {agent_id} ({role}) — {agent_cfg['model']}")
        print(f"  │ 终端:")
        print(f"  │   export ANTHROPIC_BASE_URL=\"{agent_cfg['base_url']}\"")
        print(f"  │   export ANTHROPIC_API_KEY=\"{agent_cfg['api_key']}\"")
        print(f"  │   export ANTHROPIC_MODEL=\"{agent_cfg['model']}\"")
        print(f"  │   claude")
        print(f"  │ 启动后粘贴: cat {prompt_file.name}")
        print(f"  └──────────────────────────────────")
        print()


# ── 监控 ──────────────────────────────────────────

def monitor_loop(bus: FileMessageBus) -> tuple[str | None, str | None]:
    print("\n  📡 监控中 (Ctrl+C 停止监控)")
    print()
    last_findings: dict[str, str] = {}

    try:
        while True:
            time.sleep(5)

            if bus.is_solved():
                flag = bus.get_flag()
                meta = bus.get_meta()
                finder = meta.get("found_by", "?")
                log(f"🎯 Flag found by {finder}: {flag}")
                return flag, finder

            for mid in ("member1", "member2", "member3"):
                finding = bus.get_finding(mid)
                if finding and finding != last_findings.get(mid):
                    preview = finding[:120].replace("\n", " ")
                    log(f"[{mid}] {preview}")
                    last_findings[mid] = finding

    except KeyboardInterrupt:
        print()
        return None, None


# ── 执行解题 ──────────────────────────────────────

def run_challenge(config: dict, bus: FileMessageBus, user_input: str,
                  use_tmux: bool, background: bool = False) -> None:
    global SESSION_RUNNING

    # 解析
    challenge_info = parse_challenge_input(user_input)
    challenge_desc = build_challenge_desc(challenge_info)
    bus_dir_str = str(PROJECT_DIR / (config.get("bus_dir", "bus")))

    if not challenge_desc:
        print("  ⚠ 未能识别题目信息，请重新描述")
        return

    SESSION_RUNNING = True

    # tmux 模式：先杀掉旧 session，再清总线（防止旧 agent 写脏数据）
    if use_tmux:
        if not check_tmux():
            print("  ERROR: tmux not found. Please install tmux or run without --tmux")
            SESSION_RUNNING = False
            return
        subprocess.run(["tmux", "kill-session", "-t", "ctf-swarm"],
                       capture_output=True, timeout=5)

    # 清空总线并设置新题目
    bus.clear()
    bus.set_challenge(challenge_info)
    bus.set_meta({
        "status": "running",
        "members": ["member1", "member2", "member3"],
        "challenge": challenge_desc[:100],
    })

    print(f"\n  🎯 挑战已设置: {challenge_desc[:200]}")
    print(f"  📁 总线: {bus.bus_dir}")

    # 生成提示词
    prompt_info = generate_prompts(config, challenge_desc, bus_dir_str)

    # 启动
    if use_tmux:
        tmux_start(config, prompt_info, challenge_desc)
        _run_scheduler(config, bus, "ctf-swarm")
    else:
        print(f"\n  📋 请启动 4 个 claude 实例，粘贴对应的提示词：")
        print_manual_instructions(config, prompt_info)
        print("  💡 下次可以用 --tmux 自动启动")
        print()

        confirm = input("  准备好后按 Enter 开始监控 (或输入 n 取消): ").strip()
        if confirm.lower() in ("n", "no"):
            SESSION_RUNNING = False
            return

        flag, finder = monitor_loop(bus)
        if flag:
            print(f"\n  ✅ FLAG: {flag} (by {finder})")
            wp = bus.bus_dir / "leader" / "writeup.md"
            if wp.exists():
                print(f"\n  📝 Writeup:\n{wp.read_text()[:2000]}")

    SESSION_RUNNING = False


def _cleanup_all() -> None:
    """清理 tmux 会话和所有残留 claude 进程。"""
    # 1. 杀掉 tmux session（连带杀死其中的 claude 进程）
    subprocess.run(["tmux", "kill-session", "-t", "ctf-swarm"],
                   capture_output=True, timeout=5)
    # 2. 杀掉所有 stray claude 进程
    killed = _kill_stray_claude()
    print(f"  清理完成（tmux + {killed} 个 claude 进程）")


def _is_project_claude(pid: int) -> bool:
    """Check if a claude process belongs to this project (by cwd)."""
    try:
        cwd = os.readlink(f"/proc/{pid}/cwd")
        return cwd == str(PROJECT_DIR)
    except (OSError, IOError):
        return False


def _kill_stray_claude() -> int:
    """Kill project-related claude processes that survived tmux kill. Returns count killed."""
    current_pid = os.getpid()
    # Build ancestor set
    ancestors = {current_pid}
    ppid = current_pid
    try:
        while ppid > 1:
            with open(f"/proc/{ppid}/stat") as f:
                ppid = int(f.read().split()[3])
            ancestors.add(ppid)
    except (IOError, IndexError, ValueError):
        pass

    pids = []
    try:
        result = subprocess.run(
            ["pgrep", "-x", "claude"],
            capture_output=True, text=True, timeout=5,
        )
        for pid_str in result.stdout.strip().split():
            if not pid_str:
                continue
            pid = int(pid_str)
            if pid in ancestors:
                continue
            if _is_project_claude(pid):
                pids.append(pid)
    except Exception:
        return 0

    if not pids:
        return 0

    # SIGTERM
    killed = 0
    for pid in pids:
        try:
            os.kill(pid, 15)
            killed += 1
        except (ProcessLookupError, PermissionError):
            pass

    # SIGKILL survivors
    time.sleep(1)
    for pid in pids:
        try:
            os.kill(pid, 0)
            os.kill(pid, 9)
        except (ProcessLookupError, PermissionError):
            pass

    return killed


# ── REPL ──────────────────────────────────────────

WELCOME = r"""
╔══════════════════════════════════════════╗
║        🐙 CTF Swarm 已就绪              ║
║  4 个 Agent 等待你的指令                ║
╚══════════════════════════════════════════╝

"""

HELP = """可用命令:
  <任意描述>    描述你要解的题目，例如:
                "https://target.com 文件上传题"
                "/path/to/challenge.elf"
                "帮我解这个 pwn，文件在 ~/challenges/pwn1"
  status        查看当前解题状态
  stop          停止当前解题
  watch         附加到 tmux 观察 agent 运行（Ctrl+B d 返回）
  help          显示此帮助
  exit/quit     退出
"""


def repl(use_tmux: bool, background: bool = False) -> None:
    config = load_config()
    bus_dir = PROJECT_DIR / (config.get("bus_dir", "bus"))
    bus = FileMessageBus(str(bus_dir))
    build_agent_config(config)

    # 启动时清理残留进程
    _cleanup_all()

    print(WELCOME)
    print(f"  Agent 配置:")
    for aid, acfg in config["agent_config"].items():
        print(f"    {aid}: {acfg['model']} @ {acfg['base_url']}")
    print(f"\n  模式: {'tmux 自动启动' if use_tmux else '手动启动'}")
    print()
    print(HELP)

    try:
        while True:
            if not sys.stdin.isatty():
                # 非交互模式（heredoc/管道）：一次性读完所有 stdin
                user_input = sys.stdin.read().strip()
            else:
                # 交互模式：首行判断命令 vs 多行输入
                first = input("> ").strip()
                if not first:
                    break
                if first in ("exit", "quit", "help", "stop", "status"):
                    user_input = first
                else:
                    # 多行：逐行读取，输入 . 单独一行结束
                    print("  (继续输入，完成后输入 . 单独一行结束)")
                    lines = [first]
                    while True:
                        line = input("... ").strip()
                        if line == ".":
                            break
                        lines.append(line)
                    user_input = "\n".join(lines)

            if not user_input:
                break

            if user_input in ("exit", "quit"):
                print("  Bye!")
                break

            if user_input == "help":
                print(HELP)
                continue

            if user_input == "stop":
                if use_tmux:
                    subprocess.run(["tmux", "kill-session", "-t", "ctf-swarm"],
                                   capture_output=True, timeout=5)
                    bus.clear()
                    print("  ⏹ 已停止解题，tmux session 已销毁")
                else:
                    print("  手动模式请自行关闭 claude 进程")
                SESSION_RUNNING = False
                continue

            if user_input == "status":
                if bus.is_solved():
                    meta = bus.get_meta()
                    print(f"  ✅ 已解题. Flag: {meta.get('flag', '?')}")
                else:
                    print("  🔄 解题进行中 (或尚未开始)")
                    for mid in ("member1", "member2", "member3"):
                        finding = bus.get_finding(mid)
                        if finding:
                            print(f"    [{mid}] {finding[:100]}")
                continue

            if user_input == "watch" and use_tmux:
                subprocess.run(["tmux", "attach-session", "-t", "ctf-swarm"])
                continue

            # 否则作为解题描述处理
            run_challenge(config, bus, user_input, use_tmux, background=background)
            # 非交互模式（heredoc）解题完成后退出
            if not sys.stdin.isatty():
                print("\n  ✅ 解题完成！按 Enter 退出...")
                try:
                    with open('/dev/tty', 'r') as tty:
                        tty.readline()
                except:
                    time.sleep(3)
                break

    except (EOFError, KeyboardInterrupt):
        print()
    finally:
        _cleanup_all()
        print("  Bye!")


# ── 入口 ─────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="CTF Swarm — 多 Claude 协作 CTF 解题系统",
        usage="python coordinator.py [--tmux] [--background]",
    )
    parser.add_argument("--tmux", action="store_true",
                        help="自动在 tmux 中启动所有 Agent")
    parser.add_argument("--background", action="store_true",
                        help="后台模式，启动后不 attach tmux（用于脚本测试）")

    args = parser.parse_args()

    # 后台模式强制 tmux 模式
    if args.background and not args.tmux:
        args.tmux = True

    try:
        repl(use_tmux=args.tmux, background=args.background)
    except KeyboardInterrupt:
        print("\n\n  Bye!")


if __name__ == "__main__":
    main()
