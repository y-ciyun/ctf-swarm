# CTF Swarm

多 Claude 实例协作 CTF 解题系统。**每个成员是一个持久运行的 `claude` CLI 进程**，通过文件总线交换发现，队长监控全局并给出建议。

像使用 Claude 一样自然地描述题目，系统会自动协调 4 个 Agent 协作解题。

## 架构

```
python coordinator.py
         │
   交互式 REPL — 输入题目描述
         │
         ├── bus/ (文件消息总线)
         │   ├── challenge.json       ← 题目信息
         │   ├── meta.json            ← 运行状态
         │   ├── flag.txt             ← flag 出现即胜利
         │   ├── member1/findings.txt ← 成员发现
         │   ├── member2/findings.txt
         │   ├── member3/findings.txt
         │   └── leader/
         │       ├── advice.txt       ← 队长建议
         │       └── writeup.md       ← 最终 writeup
         │
         ├── member1  ← claude (持久 Agent)
         ├── member2  ← claude (持久 Agent)
         ├── member3  ← claude (持久 Agent)
         └── leader   ← claude (持久 Agent，战略指挥)
```

每个成员是**原生的 Claude Code**——用自带的 WebFetch、Bash、Read、Write 工具解题，
不需要自定义工具代码。

## 快速开始

```bash
# 启动交互式解题
python coordinator.py

# 或使用 tmux 自动启动 4 个 Agent
python coordinator.py --tmux
```

进入交互模式后，像和 Claude 对话一样描述题目：

```
> https://target.com 文件上传题
> /path/to/challenge.elf 逆向分析
> 帮我解这个 pwn，文件在 ~/challenges/pwn1
```

系统会自动识别 URL、文件路径、目录路径等，然后协调 4 个 Agent 并行解题。

## Agent 配置

编辑 `config.yaml`，每个 Agent 可独立配置不同的 LLM 后端：

```yaml
members:
  - id: member1
    base_url: "https://api.deepseek.com/anthropic"
    api_key: "sk-xxx"
    model: "deepseek-chat"

  - id: member2
    base_url: "https://api.anthropic.com"
    api_key: "sk-ant-xxx"
    model: "claude-sonnet-4-6"

  - id: member3
    base_url: "http://localhost:11434/v1"
    api_key: "any"
    model: "qwen2.5-coder:7b"

leader:
  id: leader
  base_url: "https://api.deepseek.com/anthropic"
  api_key: "sk-xxx"
  model: "deepseek-chat"
```

## 协作机制

1. **成员** — 初始提示词告诉它们：使用工具解题、定期写 findings.txt、检查队友发现和队长建议
2. **队长** — 初始提示词告诉它：监控成员进度、卡住时写 advice.txt 给建议、找到 flag 后生成 writeup
3. **协调器** — 交互式 REPL：接受用户描述 → 设置总线 → 生成提示词 → 指导/自动启动 Agent → 监控 flag

所有智能来自 `claude` CLI 原生能力——不需要 SDK，不需要自定义工具代码。

## 命令

| 命令 | 说明 |
|------|------|
| `python coordinator.py` | 交互模式，手动启动 Agent |
| `python coordinator.py --tmux` | 交互模式，tmux 自动启动 Agent |
| `help` | 查看帮助 |
| `status` | 查看当前解题状态 |
| `stop` | 停止当前解题 |
| `exit/quit` | 退出 |
