# CTF Swarm — 多 Agent 协作解题系统

> 4 个持久运行的 Claude 实例通过文件消息总线协作解题：**1 个指挥 + 3 个执行**。
> 探索多 Agent 协作的编排机制——如何让多个 LLM Agent 像战队一样分工，而不是各自为战。

---

## 为什么做这个

单个 LLM Agent 的上下文与注意力有限。自然的想法是"多开几个 Agent 分工"，但一旦真的并行起来，会立刻撞上两个编排难题：

1. **指挥官如何掌握全局而不抢占执行？**
   指挥官一旦开始自己分析，就停止监控全局，并行度归零。
2. **Agent 自报的进展可信吗？**
   成员写的 `findings.txt` 会滞后、会失真——它可能写着"正在分析"，实际已卡死五分钟；也可能反复试同一条错路却以为自己有进展。

这个项目的所有设计决策，都是为了回答这两个问题。

---

## 实际成果

系统自主解出真实 CTF Web 题目（`escapeshellcmd` + 40 余条命令黑名单 + 符号黑名单场景，PHP 7.3 + Nginx + MySQL）：

- 4 个 Agent 协作发现**两条黑名单绕过路径**：
  - `pack(C, ascii)` + `join(array(...))` 按字节构造字符串，**完全规避 `.` 拼接过滤**
  - `base_convert(28,10,36)` 等**运行时动态构造函数名**，绕过关键字过滤
- 标准文件位置全部无果后，**转查 MySQL 库发现隐藏表** `F1ag_Se3Re7`（Flag_Secret 的 leet 变体）提取 flag
- 自动产出完整 writeup

> 完整解题报告（含黑名单绕过思路、失败路径、经验总结）见 [`docs/writeup-example.md`](docs/writeup-example.md)

---

## 架构

```
python coordinator.py
         │
   交互式 REPL —— 输入题目描述
         │
         ├── bus/ （文件消息总线 = 共享状态）
         │   ├── challenge.json       ← 题目信息
         │   ├── meta.json            ← 运行状态 / flag
         │   ├── flag.txt             ← flag 出现即胜利
         │   ├── member{1,2,3}/
         │   │   ├── findings.txt     ← 最新发现（覆盖写）
         │   │   └── log.txt          ← 历史记录（追加）
         │   └── leader/
         │       ├── advice.txt       ← 全局策略广播
         │       ├── advice_memberX.txt ← 定向纠偏
         │       └── writeup.md       ← 最终 writeup
         │
         ├── member1  ← claude（持久 Agent）
         ├── member2  ← claude（持久 Agent）
         ├── member3  ← claude（持久 Agent）
         └── leader   ← claude（持久 Agent，战略指挥）
```

每个成员是**原生的 Claude Code CLI 进程**——直接使用其自带的 WebFetch / Bash / Read / Write 工具解题，**不需要自定义任何工具代码**。

---

## 核心设计

### 1. 调度器只做事件分发，不做判断

`_run_scheduler()` 是一个中心化轮询循环。它的注释写明了职责边界：

> *"永久调度循环：监视文件 mtime 变化 → 唤醒对应 agent。**不做判断，不做转述**。"*

它盯着 `findings.txt` / `advice.txt` / `advice_memberX.txt` 的变更，决定唤醒谁；**所有判断权归指挥 Agent**。这样调度层不会因为"自作聪明"而扭曲 Agent 的决策。

### 2. 观测缓冲：让指挥官的上下文不被淹没

如果每次文件变化都唤醒指挥官，它的上下文会被琐碎活动冲爆。`ObservationBuffer` 用四档策略控制推送时机：

| 策略 | 值 | 目的 |
|---|---|---|
| 启动安静期 | 90s | 给指挥官初始思考时间，只积累不推送 |
| 最小推送间隔 | 90s | 避免频繁打断 |
| 批量阈值 | 20 条 或 6 轮 | 攒够再推，减少琐碎干扰 |
| 强制打断 | 40 条 或 15 轮 | 防止指挥官长时间忙导致消息积压丢失 |
| 内容去重 | body 哈希 | 内容与上次相同则跳过 |

### 3. 看终端，不看自报 ⭐

**指挥官的判断依据是成员终端的实时输出**（命令、报错、结果、思考状态），而**不是**成员自己写的 `findings.txt`。

原因：Agent 自报的进展**会滞后、会失真**——它可能刚写了"正在分析"实际已卡死，也可能重复走错路却自以为有进展。**终端输出才是真相**。

对应实现：`_capture_observations()` 抓取 tmux pane 内容 → `_is_noise()` 过滤噪音 → 喂入 `ObservationBuffer`。

### 4. 双通道干预

| 通道 | 触发 | 作用 |
|---|---|---|
| **定向纠偏** | `advice_memberX.txt` 变化 | 只打断指定成员 |
| **全局广播** | `advice.txt` 变化 | 通知所有成员（**限一次**） |

### 5. 指挥行为准则

写进指挥 Agent 的 `CLAUDE.md`：

- **禁止指挥者自己动手解题**（逆向、反汇编、跑脚本都不许）——**这是队员的工作**，指挥官动手等于放弃监控
- **同方向连续失败 3 次以上 → 强制换向**
- **只打断方向错误的成员；正确推进的不干扰**（干预过度比不干预更糟）
- 建议必须**具体可执行**（如"用 objdump 反汇编 `get_rc4_key` 地址 0x62dd0，跟随数据流"），而不是泛泛鼓励

### 6. 鲁棒性设计（长时间无人值守的前提）

| 机制 | 实现 |
|---|---|
| 空闲检测 | `_is_agent_idle()` 判断 Agent 是否卡住 |
| 探活心跳 | 每 180s 向指挥官同步成员状态 |
| 卡死预警 | 300s 内容哈希无变化 → 预警（**用内容哈希而非时间戳**，避免假变化） |
| API 错误自愈 | `_has_api_error()` 检测 → `_recover_agent()` 恢复 + 重试上限 |
| 残留清理 | `_kill_stray_claude()` 清理上一次运行的僵尸进程 |
| 多后端 | 每个 Agent 可独立配置云端 API 或本地模型（混合部署） |

---

## 快速开始

```bash
# 1. 准备配置
cp config.example.yaml config.yaml
#    编辑 config.yaml，填入你的 API 密钥

# 2. 确认依赖
#    - python3 + pyyaml
#    - tmux
#    - claude CLI（Claude Code）

# 3. 启动交互式解题
python coordinator.py

# 或使用 tmux 自动启动 4 个 Agent
python coordinator.py --tmux
```

进入交互模式后，像和 Claude 对话一样描述题目：

```
> https://target.example.com 文件上传题
> /path/to/challenge.elf 逆向分析
> 帮我解这个 pwn，文件在 ~/challenges/pwn1
```

系统会自动识别 URL / 文件路径 / 目录路径，然后协调 4 个 Agent 并行解题。

### 命令

| 命令 | 说明 |
|---|---|
| `python coordinator.py` | 交互模式，手动启动 Agent |
| `python coordinator.py --tmux` | 交互模式，tmux 自动启动 Agent |
| `help` | 查看帮助 |
| `status` | 查看当前解题状态 |
| `stop` | 停止当前解题 |
| `exit` / `quit` | 退出 |

---

## 迭代历史

```
修复：适配 Claude CLI 新参数、改进 agent API 错误恢复逻辑
稳定版本：调度架构重构完成
fix: ObservationBuffer 等待 advice.txt 后再推送
保存当前版本：ObservationBuffer + 观测流程修复
项目初始化：CTF Swarm 多 Claude 协作解题系统
保存为演示版本
init: CTF Swarm 项目初始版本
```

关键迭代：**总线机制 → ObservationBuffer 观测流程 → 调度架构重构 → CLI 参数适配**。

---

## 已知限制（诚实说明）

1. **依赖 tmux 做终端 I/O** —— 用 `send-keys` / `capture-pane` 驱动 Agent，是当时条件下最直接的方式；换成原生 SDK 调用会更干净
2. **共享状态是文件系统** —— 简单可靠，但不如显式 State schema；进程崩溃后**只有状态标记（`meta.json` 的 status），没有可恢复的执行检查点**
3. **路由是隐式的** —— "谁变了唤醒谁"写在调度循环的 `if` 里，不是显式声明的条件边
4. **Agent 需要较高权限运行** —— 成员要能自由执行命令（这是 CTF 场景的要求）；**请在隔离环境中运行**
5. **只在单机验证** —— 没有做分布式或并发规模测试

> 这些限制后来让我理解了 LangGraph 那类编排框架替我封装了什么：显式 State、条件边、可恢复 Checkpointer。**我手写实现了消息节流、超时监控、故障自愈，但没做到可恢复的检查点。**

---

## 安全提示

- `config.yaml`（含 API 密钥）已被 `.gitignore` 排除，**请勿提交**
- 运行产物（`bus/`、`.prompt_*.md`、各类 probe 脚本）同样被排除
- 本项目用于**授权范围内的 CTF 竞赛与安全研究**，请勿用于未授权目标

---

## 目录结构

```
coordinator.py      调度器 + 交互式 REPL（核心）
bus.py              文件消息总线封装
proxy.py            API 代理
reconnect.py        Agent 重连工具
monitor.sh          监控脚本
config.example.yaml 配置示例
prompts/
  ├── leader.md     指挥 Agent 提示词
  └── member.md     执行 Agent 提示词
.claude/CLAUDE.md   项目约定（指挥/成员职责划分）
docs/
  └── writeup-example.md  实战解题报告
```
