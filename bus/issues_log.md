# 系统问题观察记录

> 时间：2026-04-29
> 题目：simple_php（MySQL 数据库存储 flag）
> 最终解题用时：约 2 小时

## 2026-04-29 运行观察

### 问题 1：DeepSeek API 生成极慢
- **表现**：单次 thinking 循环长达 15-28 分钟，token 生成速度约 0.7-1k tokens/min
- **影响**：整体解题进度的核心瓶颈，member1/member2 在最后阶段分别进入 26+ 分钟的单一 thinking 循环
- **根因**：DeepSeek 通过 anthropic 兼容接口调用时响应极慢，尤其是上下文较大（50k+ tokens）时

### 问题 2：Findings 冻结导致 Leader 误判
- **表现**：agent 进入长思考时不更新 findings.txt（只有完成一轮 Writing 操作后才写），Leader 误以为成员卡住
- **影响**：leader 向 member2/member3 发送了不必要的紧急纠偏（advice_member2.txt, advice_member3.txt），打断了有效工作
- **根因**：调度器只靠 findings.txt mtime 判断成员状态，无法区分"在思考"和"卡住"

### 问题 3：Agent 上下文持续膨胀
- **表现**：member1 在运行 25 分钟后上下文达 65k tokens，RSS 内存 400MB+；所有 agent RSS 在 350-440MB 范围
- **影响**：越大越慢，形成恶性循环；Claude 自动触发了一次上下文压缩（Conversation compacted）
- **根因**：没有跨 thinking 循环的上下文压缩机制，每次循环追加结果而非总结

### 问题 4：API 错误无自动恢复机制
- **表现**：leader 遇到 `The socket connection was closed unexpectedly` API 错误后，卡在错误状态不再响应
- **影响**：leader 失联约 5 分钟，手动发送 Enter 才重新激活
- **根因**：调度器检测不到 API 级错误（tmux 层面 agent 仍在运行），不会自动重试

### 问题 5：成员之间无直接通信机制
- **表现**：member2 和 member3 都在做重复的 `du -a /` 全盘扫描，浪费 API 配额和时间
- **影响**：重复劳动，解题效率低
- **根因**：架构设计上所有信息必须经 leader 中转，但 leader 只在 findings 更新时才被唤醒

### 问题 6：Leader 自身不输出 Findings
- **表现**：leader 在整个运行过程中从未写过 findings.txt
- **影响**：调度器无法判断 leader 状态（是空闲、在工作、还是卡住）；成员也看不到 leader 的分析进展
- **根因**：prompt 没有明确要求 leader 写 findings.txt（leader.md 只要求写 advice.txt 和 writeup.md）

### 问题 7：WAF 黑名单字符串匹配过于宽泛
- **表现**：`ch` 子串导致 `echo`、`chr` 等命令被拦截；`sh` 子串导致 `mysqlshow` 被拦截；`dir` 子串导致 `scandir` 被拦截
- **影响**：成员需不断试错发现哪些函数可用，浪费多个 thinking 循环
- **备注**：这是题目设计，不是系统 bug，但 agent 缺乏"黑名单试错"的全局策略导致反复踩坑

### 问题 8：Monitor 事件过于频繁
- **表现**：监控脚本每 10 秒报告一次无变化的状态，产生大量无意义的通知
- **影响**：干扰注意力，重要变化容易被淹没
- **根因**：应只在检测到 findings/advice/flag 变化时报告，而非周期性轮询

### 问题 9：Agent 对不同方向的探索缺乏全局协调
- **表现**：member1 扫文件系统、member2 研究 PHP 绕过、member3 搜索 writeup，三线并行但进度不一
- **正面案例**：最终 member1 的 MySQL 路径成功了，但 member2 的 PHP chr 绕过路径因 `ch` 被 WAF 拦截而卡住，member3 的 preg_match 回溯绕过路径未完成
- **根因**：虽然分工明确，但缺乏"Plan B 切换"的决策机制

## 观察总结

本次运行中 swarm 系统成功解出题目，flag 存储在 MySQL 数据库 `PHP_CMS.F1ag_Se3Re7` 表中。
最终找到 flag 的是 member1（文件枚举→环境提取→MySQL 查询路径）。

核心成功因素：member1 发现 `mysqldump --all-databases` 可绕过 WAF 读取 MySQL。
核心失败模式：DeepSeek API 速度导致 80% 的时间花在等待上。
