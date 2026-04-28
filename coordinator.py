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
BUS_DIR = PROJECT_DIR / "bus"
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

    for w in text_stripped.split():
        w = w.strip("'\"(),.。，")
        p = Path(w).expanduser().resolve()
        if p.exists():
            if p.is_dir():
                info["dir"] = str(p)
                info["_type"] = "dir"
            else:
                info["file"] = str(p)
                info["_type"] = "file"
            break

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


def _wait_for_any(session: str, window: str, patterns: list[str],
                   timeout: int = 25, interval: float = 0.5) -> bool:
    """Wait until pane content matches any pattern."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _has_any(_capture_pane(session, window), patterns):
            return True
        time.sleep(interval)
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
    startup = f"cd {PROJECT_DIR} && {env_exports} && claude --permission-mode bypassPermissions"
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
    time.sleep(1.5)

    # Submit
    subprocess.run(["tmux", "send-keys", "-t", f"{session}:{window_name}",
                    "Enter"], check=True, timeout=10)

    log(f"✓ {agent_id} [{agent_type}] ({agent_cfg['model']})")
    return True


def tmux_start(config: dict, prompt_info: list, challenge_desc: str,
               background: bool = False) -> None:
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
_MIN_WAKEUP_INTERVAL = 40        # 同一 agent 最短唤醒间隔
_FINDINGS_STALE = 90             # findings 多久未更新视为过期
_ADVICE_STALE = 120              # advice 多久未更新视为过期
_STARTUP_GRACE = 30              # 启动后前 N 秒不唤醒（短暂等待避免重复唤醒）


def _is_agent_idle(session: str, agent_id: str) -> bool:
    """检测 agent 是否真正 idle（❯ 提示符可见 + 无思考动画）。只检查最近 5 行。"""
    try:
        content = _capture_pane(session, agent_id)
        lines = content.split('\n')
        recent = lines[-5:]
        # 必须看到 ❯ 提示符（可能后面跟着 "Press up to edit"）
        has_prompt = any('❯' in line for line in recent)
        if not has_prompt:
            return False
        # 不能有思考/工作中动画
        thinking_indicators = (
            'still thinking', 'thinking more', 'Sprouting', 'Computing',
            'Razzmatazzing', 'Brewed for', 'Bootstrapping', 'Analyzing',
            'Swirling', 'Composing', 'Billowing', 'Brewing',
        )
        for line in recent:
            for indicator in thinking_indicators:
                if indicator.lower() in line.lower():
                    return False
        return True
    except Exception:
        return False


def _force_push_targeted_advice(session: str, member_id: str, advice: str) -> None:
    """强制推送针对性建议：Esc 打断 → paste → Enter 提交。"""
    # 1. Esc 打断当前思考/任务
    subprocess.run(["tmux", "send-keys", "-t", f"{session}:{member_id}", "Escape"],
                   capture_output=True, timeout=5)
    time.sleep(0.5)

    # 2. Paste 建议
    buf_name = f"tadv_{member_id}"
    msg = f"=== 队长针对性建议 ===\n{advice}\n\n请认真处理队长的建议。"
    subprocess.run(["tmux", "set-buffer", "-b", buf_name, msg],
                   capture_output=True, timeout=5)
    subprocess.run(["tmux", "paste-buffer", "-b", buf_name,
                    "-t", f"{session}:{member_id}", "-d"],
                   capture_output=True, timeout=5)
    time.sleep(0.3)

    # 3. Enter 提交
    subprocess.run(["tmux", "send-keys", "-t", f"{session}:{member_id}", "Enter"],
                   capture_output=True, timeout=5)


def _detect_member_errors(session: str) -> dict[str, list[str]]:
    """轻量检测：扫描成员 pane 输出中的错误模式。
    只取最近的错误行，用于唤醒 leader 时提供上下文。"""
    ERROR_PATTERNS = (r'\bError\b', r'\bFail(?:ed|ure)?\b', r'Segfault',
                      r'Traceback', r'trace/breakpoint', r'Killed',
                      r'coredump', r'stack smashing', r'Timeout')
    flags = {}
    for mid in ("member1", "member2", "member3"):
        try:
            content = _capture_pane(session, mid)
            errors = []
            for line in content.split('\n'):
                stripped = line.strip()
                if not stripped:
                    continue
                for pat in ERROR_PATTERNS:
                    if re.search(pat, stripped, re.IGNORECASE):
                        errors.append(stripped[:100])
                        break
            if errors:
                # 去重 + 取最后 3 条
                seen = set()
                unique = []
                for e in reversed(errors):
                    if e not in seen:
                        seen.add(e)
                        unique.append(e)
                    if len(unique) >= 3:
                        break
                flags[mid] = list(reversed(unique))
        except Exception:
            pass
    return flags


def _build_wakeup_context(bus: FileMessageBus, agent_id: str, session: str = "") -> str:
    """构建唤醒消息：汇总队友最新发现+队长建议+错误标记，简明扼要。"""
    if agent_id == "leader":
        parts = ["=== 调度唤醒 队长 ===", "各成员进度:"]
        error_flags = _detect_member_errors(session) if session else {}
        for mid in ("member1", "member2", "member3"):
            f = bus.get_finding(mid)
            summary = f.strip().split('\n')[0][:80] if f else "(无发现)"
            activity = _get_member_recent_activity(session, mid) if session else ""
            flags = []
            if mid in error_flags:
                flags.append("⚠ 疑似卡住（连续错误）")
            if activity:
                parts.append(f"  [{mid}] {summary}")
                parts.append(f"    最近: {activity}")
            else:
                parts.append(f"  [{mid}] {summary}")
            if flags:
                parts.append(f"    {' '.join(flags)}")
        parts.append("\n【全局建议】写入 leader/advice.txt（所有成员被动读取）")
        parts.append("【针对性建议】写入 leader/advice_memberX.txt（调度器强制打断+推送）")
        return '\n'.join(parts)

    parts = ["=== 调度唤醒 ==="]

    advice = bus.get_advice()
    if advice:
        line = advice.strip().split('\n')[0][:120]
        parts.append(f"[队长] {line}")

    for mid in ("member1", "member2", "member3"):
        if mid == agent_id:
            continue
        f = bus.get_finding(mid)
        if f:
            first = f.strip().split('\n')[0][:100]
            parts.append(f"[{mid}] {first}")

    parts.append("\n请按以下步骤执行：\n1. 将当前进展写入 bus/" + agent_id + "/findings.txt（滚动列表格式，新内容加最前面，保留最近5条），同时追加到 bus/" + agent_id + "/log.txt\n2. 阅读以上情报，继续解题")  # noqa: E501
    return '\n'.join(parts)


def _wake_agent(session: str, agent_id: str, message: str) -> None:
    """向 agent 发送唤醒消息 + Enter 提交。先 Esc 清状态再 paste。"""
    # 1. Esc 确保回到干净 prompt（打断思考 / 清 queued messages）
    subprocess.run(["tmux", "send-keys", "-t", f"{session}:{agent_id}", "Escape"],
                   capture_output=True, timeout=5)
    time.sleep(0.4)

    # 2. Paste 消息
    buf_name = f"swarm_{agent_id}"
    subprocess.run(["tmux", "set-buffer", "-b", buf_name, message],
                   capture_output=True, timeout=5)
    subprocess.run(["tmux", "paste-buffer", "-b", buf_name,
                    "-t", f"{session}:{agent_id}", "-d"],
                   capture_output=True, timeout=5)
    time.sleep(0.5)

    # 3. 检查是否被 queue，需要 Up 提交
    content = _capture_pane(session, agent_id)
    if "queued messages" in content.lower():
        subprocess.run(["tmux", "send-keys", "-t", f"{session}:{agent_id}", "Up"],
                       capture_output=True, timeout=5)
        time.sleep(0.3)

    # 4. Enter 提交
    subprocess.run(["tmux", "send-keys", "-t", f"{session}:{agent_id}", "Enter"],
                   capture_output=True, timeout=5)


# ── 启动增强：队长延迟 + 初始唤醒 ─────────────────

def _get_member_recent_activity(session: str, member_id: str) -> str:
    """从 tmux pane 提取最近工具调用摘要。"""
    try:
        content = _capture_pane(session, member_id)
        lines = content.split('\n')
        # 从后往前找最近 3 个含 ●（工具调用）或 ⎿（结果）的行
        hits = []
        for line in reversed(lines):
            s = line.strip()
            if s.startswith('●'):
                hits.append(s[:90])
            if len(hits) >= 2:
                break
        if not hits:
            # 退一步：找任意非空非状态行
            for line in reversed(lines):
                s = line.strip()
                if s and not s.startswith('─') and not s.startswith('⏵') and not s.startswith('❯') and not s.startswith('Press'):
                    hits.append(s[:90])
                if len(hits) >= 2:
                    break
        return ' | '.join(reversed(hits)) if hits else "(无)"
    except Exception:
        return "(?)"


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
    EXCLUDE_PATTERNS = (r'test', r'example', r'local_test', r'placeholder', r'TODO', r'xxxx')
    # Only match: ctfshow with UUID, or flag{...} with meaningful content (no test/example)
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
    """永久调度循环：检测 idle → 唤醒 → 轮询。"""
    log("调度器启动 — 每 15s 轮询 agent 状态")
    last_wake: dict[str, float] = {}
    startup_time = time.monotonic()

    def _file_age(rel_path: str) -> float:
        p = bus.bus_dir / rel_path
        return time.time() - p.stat().st_mtime if p.exists() else 9999

    # 追踪针对性建议文件的 mtime
    _advice_mtimes: dict[str, float] = {}
    for mid in ("member1", "member2", "member3"):
        p = bus.bus_dir / "leader" / f"advice_{mid}.txt"
        if p.exists():
            _advice_mtimes[mid] = p.stat().st_mtime

    try:
        while True:
            # 启动宽限期：不打扰第一轮执行
            if time.monotonic() - startup_time < _STARTUP_GRACE:
                time.sleep(_IDLE_CHECK_INTERVAL)
                continue

            # 检测针对性建议文件（leader 写的新 advice_memberX.txt）→ 强制推送
            for mid in ("member1", "member2", "member3"):
                p = bus.bus_dir / "leader" / f"advice_{mid}.txt"
                cur_mtime = p.stat().st_mtime if p.exists() else 0
                prev_mtime = _advice_mtimes.get(mid, 0)
                if cur_mtime and cur_mtime != prev_mtime:
                    content = p.read_text(encoding="utf-8").strip()
                    if content:
                        log(f"检测到 → 强制推送针对性建议给 {mid}")
                        _force_push_targeted_advice(session, mid, content)
                        last_wake[mid] = time.time()
                    _advice_mtimes[mid] = cur_mtime

            # 从 tmux pane 检测 flag（兜底 agent 幻觉）
            flag_txt_path = bus.bus_dir / "flag.txt"
            flag, finder = _scan_panes_for_flag(session)
            if flag and not bus.is_solved() and not flag_txt_path.exists():
                bus.set_flag_found(flag, finder or "unknown")
                flag_txt_path.write_text(f"{finder}: {flag}\n", encoding="utf-8")
                log(f"🎯 Flag detected from [{finder}] pane: {flag}")
                solved = True
            else:
                # 检查 flag（支持两种方式：meta.json 或 flag.txt）
                solved = bus.is_solved() or flag_txt_path.exists()

            if solved and not bus.is_solved() and flag_txt_path.exists():
                raw = flag_txt_path.read_text(encoding="utf-8").strip()
                bus.set_flag_found(raw, "unknown")

            if solved:
                meta = bus.get_meta()
                flag = meta.get("flag", "?")
                finder = meta.get("found_by", "?")
                # 如果 meta 中没有 flag，从 flag.txt 读取
                if flag == "?" and flag_txt_path.exists():
                    raw = flag_txt_path.read_text(encoding="utf-8").strip()
                    # 格式可能是 "member2: flag{...}" 或 "flag{...}"
                    m = re.search(r'flag\{[^}]+\}', raw)
                    if m:
                        flag = m.group(0)
                    else:
                        flag = raw
                log(f"🎯 Flag found by {finder}: {flag}")

                # 给找到 flag 的成员一点时间写 findings.txt
                time.sleep(3)

                # 通知所有成员停止解题
                for agent_id in ("member1", "member2", "member3"):
                    _wake_agent(session, agent_id,
                        "=== 挑战已结束 ===\nFlag 已被找到（" + finder + "），解题完成。请停止工作，无需继续分析。")  # noqa: E501
                log("已通知所有成员停止解题")

                # 唤醒 leader 生成 writeup
                log("唤醒 leader 生成 writeup...")
                _wake_agent(session, "leader",
                    "Flag 已找到！请立即读取所有成员的 findings.txt，汇总生成完整的 writeup，写入 bus/leader/writeup.md")  # noqa: E501

                # 等待 writeup（最多等 60s）
                wp_path = bus.bus_dir / "leader" / "writeup.md"
                for _ in range(12):
                    time.sleep(5)
                    if wp_path.exists() and wp_path.stat().st_size > 0:
                        break

                print(f"\n  ✅ FLAG: {flag} (by {finder})")
                if wp_path.exists():
                    content = wp_path.read_text(encoding="utf-8")[:2000]
                    print(f"\n  📝 Writeup:\n{content}")
                else:
                    print("  (leader 未生成 writeup)")
                return

            now = time.time()

            for agent_id in ("member1", "member2", "member3"):
                if not _is_agent_idle(session, agent_id):
                    continue
                if now - last_wake.get(agent_id, 0) < _MIN_WAKEUP_INTERVAL:
                    continue
                age = _file_age(f"{agent_id}/findings.txt")
                if age < _FINDINGS_STALE:
                    continue  # 近期有更新，不唤醒

                msg = _build_wakeup_context(bus, agent_id, session)
                _wake_agent(session, agent_id, msg)
                last_wake[agent_id] = now
                log(f"唤醒 {agent_id}")

            # 队长检查
            if _is_agent_idle(session, "leader"):
                if now - last_wake.get("leader", 0) >= _MIN_WAKEUP_INTERVAL:
                    age = _file_age("leader/advice.txt")
                    if age >= _ADVICE_STALE:
                        msg = _build_wakeup_context(bus, "leader", session)
                        _wake_agent(session, "leader", msg)
                        last_wake["leader"] = now
                        log("唤醒 leader")

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
        tmux_start(config, prompt_info, challenge_desc, background=background)
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
