"""
文件消息总线 — 多持久 Agent 通过文件系统交换信息。

结构:
  bus/
  ├── challenge.json         # 题目信息（固定，只读）
  ├── meta.json              # 运行状态、flag 等
  ├── member1/
  │   ├── findings.txt       # 最新发现（覆盖写）
  │   └── log_001.txt        # 历史记录（追加）
  ├── member2/
  │   ├── findings.txt
  │   └── log_001.txt
  ├── member3/
  │   ├── findings.txt
  │   └── log_001.txt
  └── leader/
      ├── advice.txt         # 最新建议（覆盖写）
      └── writeup.md         # 最终解题报告
"""

import json
import os
import time
from pathlib import Path
from typing import Optional


class FileMessageBus:
    """消息总线 — 专门为持久 Claude 进程设计。"""

    def __init__(self, bus_dir: str = "bus"):
        self.bus_dir = Path(bus_dir)
        self.bus_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_default_dirs()

    def _ensure_default_dirs(self) -> None:
        for name in ("member1", "member2", "member3", "leader"):
            (self.bus_dir / name).mkdir(parents=True, exist_ok=True)

    # ── 题目信息 ──────────────────────────────────────

    def set_challenge(self, info: dict) -> None:
        self._write_json("challenge.json", info)

    def get_challenge(self) -> dict:
        return self._read_json("challenge.json") or {}

    # ── 元信息 ────────────────────────────────────────

    def set_meta(self, meta: dict) -> None:
        self._write_json("meta.json", meta)

    def get_meta(self) -> dict:
        return self._read_json("meta.json") or {}

    def set_flag_found(self, flag: str, by: str) -> None:
        meta = self.get_meta()
        meta["status"] = "solved"
        meta["flag"] = flag
        meta["found_by"] = by
        meta["found_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        self.set_meta(meta)
        # 也写一个独立的 flag 文件方便成员检测
        (self.bus_dir / "flag.txt").write_text(f"{by}: {flag}\n", encoding="utf-8")

    def is_solved(self) -> bool:
        return self.get_meta().get("status") == "solved"

    def get_flag(self) -> Optional[str]:
        return self.get_meta().get("flag")

    # ── 成员发现（文件方式，适合 claude 直接读写）──────

    def post_finding(self, member_id: str, finding: str) -> None:
        """写 findings.txt：覆盖写 + 追加日志."""
        member_dir = self.bus_dir / member_id
        member_dir.mkdir(parents=True, exist_ok=True)
        # 覆盖写最新发现
        (member_dir / "findings.txt").write_text(finding, encoding="utf-8")
        # 追加日志
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        log_file = member_dir / "log.txt"
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(f"[{timestamp}]\n{finding}\n\n---\n\n")

    def get_finding(self, member_id: str) -> str:
        """获取某成员的最新发现。"""
        f = self.bus_dir / member_id / "findings.txt"
        if f.exists():
            return f.read_text(encoding="utf-8").strip()
        return ""

    def get_all_findings(self) -> dict[str, str]:
        """获取所有成员的最新发现。"""
        return {
            mid: self.get_finding(mid)
            for mid in ("member1", "member2", "member3")
        }

    # ── 队长建议（全局） ──────────────────────────────

    def post_advice(self, advice: str) -> None:
        leader_dir = self.bus_dir / "leader"
        leader_dir.mkdir(parents=True, exist_ok=True)
        (leader_dir / "advice.txt").write_text(advice, encoding="utf-8")

    def get_advice(self) -> str:
        f = self.bus_dir / "leader" / "advice.txt"
        if f.exists():
            return f.read_text(encoding="utf-8").strip()
        return ""

    # ── 队长针对性建议（按成员） ──────────────────────

    def post_targeted_advice(self, member_id: str, advice: str) -> None:
        leader_dir = self.bus_dir / "leader"
        leader_dir.mkdir(parents=True, exist_ok=True)
        (leader_dir / f"advice_{member_id}.txt").write_text(advice, encoding="utf-8")

    def get_targeted_advice(self, member_id: str) -> str:
        f = self.bus_dir / "leader" / f"advice_{member_id}.txt"
        if f.exists():
            return f.read_text(encoding="utf-8").strip()
        return ""

    def clear_targeted_advice(self, member_id: str) -> None:
        f = self.bus_dir / "leader" / f"advice_{member_id}.txt"
        if f.exists():
            f.unlink()

    def get_all_targeted_advice_mtimes(self) -> dict[str, float]:
        result = {}
        for mid in ("member1", "member2", "member3"):
            f = self.bus_dir / "leader" / f"advice_{mid}.txt"
            if f.exists():
                result[mid] = f.stat().st_mtime
        return result

    # ── Writeup ───────────────────────────────────────

    def write_writeup(self, content: str) -> None:
        (self.bus_dir / "leader" / "writeup.md").write_text(content, encoding="utf-8")

    # ── 清空 ──────────────────────────────────────────

    def clear(self) -> None:
        import shutil
        for item in self.bus_dir.iterdir():
            if item.is_dir():
                shutil.rmtree(item)
            else:
                item.unlink()
        self._ensure_default_dirs()

    # ── 内部 ──────────────────────────────────────────

    def _write_json(self, path: str, data: dict) -> None:
        p = self.bus_dir / path
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _read_json(self, path: str) -> Optional[dict]:
        p = self.bus_dir / path
        if not p.exists():
            return None
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
