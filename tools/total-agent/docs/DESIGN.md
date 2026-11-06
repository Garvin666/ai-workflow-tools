# Total Agent 设计规格

> 本文是实现的唯一口径源。代码与本文件冲突时，以本文件为准并返修代码。
> 版本：1.0 ｜ 2026-11-07

## 1. 目标与非目标

**目标**：给「多步骤、长时、会失败」的任务提供一个可执行内核 —— 计划给定后，它负责调度、执行、边界、恢复、报告。

**非目标**：不做任务拆解（拆解由调用方给定计划）；不内置 LLM；不做分布式；不做 UI。

## 2. 模块结构

| 模块 | 职责 | 依赖 |
| --- | --- | --- |
| `model.py` | 状态枚举、规格对象、状态迁移守卫 | 无 |
| `errors.py` | 异常分类（决定可重试/需人工/需熔断） | 无 |
| `planner.py` | DAG 校验、拓扑就绪集、优先级排序、下游传播 | model |
| `policy.py` | 行为边界（路径/命令/预算/审批门） | errors |
| `store.py` | 原子 checkpoint、append-only journal、中断修复 | model |
| `executors.py` | shell / python / dry-run / LLM 适配器 | errors, policy |
| `human.py` | 审批请求队列、批准/驳回、待办查询 | store |
| `runtime.py` | 主循环：调度→执行→验证→重试→暂停/终止 | 以上全部 |
| `report.py` | 结构化结果（五项）+ JSON/Markdown 渲染 | model, store |
| `cli.py` | 六个子命令 | 以上全部 |

## 3. 状态机

### 3.1 步骤状态

| 状态 | 含义 | 可迁入 |
| --- | --- | --- |
| `pending` | 依赖未满足 | — |
| `ready` | 依赖已满足，等待调度 | pending, running, blocked, interrupted |
| `running` | 正在执行 | ready, interrupted |
| `succeeded` | 执行且验证通过 | running |
| `failed` | 重试耗尽或不可重试错误 | running, ready |
| `blocked` | 需人工确认（计划内暂停，非错误） | running, ready |
| `skipped` | 上游失败/条件不满足 | pending, ready |
| `cancelled` | 运行被终止 | pending, ready, running, blocked |
| `interrupted` | 崩溃时处于 running（恢复期临时态） | running |

终态：`succeeded` / `failed` / `skipped` / `cancelled`。

### 3.2 运行状态

| 状态 | 含义 |
| --- | --- |
| `running` | 正在推进 |
| `paused` | 停在人工门，可 `resume` |
| `succeeded` | 全部步骤到达终态且无 failed |
| `failed` | 至少一步 failed，且无任何可推进步骤 |
| `aborted` | 预算超限 / 死锁 / 主动终止 |

迁移：`running → paused → running`（可循环）；`running/paused → succeeded/failed/aborted`（终态，不可逆）。

## 4. 计划与调度

- **DAG 校验**：Kahn 拓扑排序检测环，有环即 `PlanError`；依赖指向不存在的步骤同样报错。
- **就绪判定**：步骤 `pending` 且全部 `depends_on` 均为 `succeeded` → `ready`。
- **调度顺序**：就绪集内按 `(priority 降序, id 升序)` 取一个执行。串行执行，不并发（这是刻意的：并发会让副作用顺序不可复现）。
- **下游传播**：某步 `failed` 后，所有传递依赖它的未完成步标 `skipped`，原因 `dependency_failed`。`skipped` 不计失败，但会出现在「未完成项」。

## 5. 行为边界

`Boundaries` 全部有默认值，可逐项覆盖（`spec.boundaries`）。

| 字段 | 默认 | 作用 | 违反后果 |
| --- | --- | --- | --- |
| `workspace_root` | 必填 | 所有写入路径的允许根 | `PolicyViolation`，步骤 failed 且不可重试 |
| `max_steps` | 1000 | 单次运行最多执行步数（防死循环） | 运行 `aborted` |
| `max_wall_seconds` | 3600 | 墙钟上限 | 运行 `aborted` |
| `max_total_attempts` | 200 | 全运行累计尝试上限 | 运行 `aborted` |
| `allowed_commands` | `[]`（空=不限制） | shell 首 token 白名单（支持 `*` 通配） | `PolicyViolation` |
| `denied_commands` | 内置危险表 | 显式拒绝（优先级高于白名单） | `PolicyViolation` |
| `require_approval` | `false` | 全局人工门 | 步骤进入 `blocked` |
| `dry_run` | `false` | 只走流程不真执行 | 执行器返回模拟成功 |

**边界检查时机**：每次执行前一次。不做「运行中改写」。

## 6. 执行与验证

执行器协议：`run(cmd, cwd, timeout) -> ExecResult(ok, exit_code, stdout, stderr, duration_s, error_kind)`。

| 执行器 | 触发 | 行为 |
| --- | --- | --- |
| `shell` | `executor: shell` | `subprocess.run(shell=False)`，按 whitespace 切分；超时 kill |
| `python` | `executor: python` | 调用注册表里的可调用对象（测试与嵌入用） |
| `dry-run` | 全局 `dry_run` | 不执行，返回 `ok=True, simulated=True` |
| `llm` | `executor: llm` | 抽象；`MockLLMExecutor` 返回确定性文本；真实实现由使用者注入 |

**验证**：步骤可声明 `verify`（一条 shell 命令或 python 断言名）。执行成功后跑验证，非 0 即 `verify_failed`（可重试）。无 `verify` 的步骤以执行退出码为准 —— 这一点在报告里显式标注「无验证信号」，不假装通过。

## 7. 持久化与恢复（核心）

### 7.1 文件布局

```
<run_dir>/
  spec.json          计划快照（首跑时写入，之后只读；resume 用它而不是外部文件）
  state.json         当前状态（原子写：tmp → os.replace）
  journal.jsonl      append-only 事件流（真相源）
  approvals/         人工门请求与结论
  artifacts/         步骤产出
  report.json        终态报告
```

### 7.2 journal 是真相源，state 是缓存

`state.json` 每步状态变更后原子落盘；`journal.jsonl` 每发生一件事追加一行（`step_started` / `step_finished` / `retry` / `blocked` / `resumed` / `aborted`）。

**恢复时以 journal 为准**：若某步 journal 里已有 `step_finished(ok)` 但 `state.json` 仍写着 `running`（崩溃发生在两次写之间），恢复期直接把它标 `succeeded`，**不重跑**。反过来，`running` 且 journal 无 `step_finished` → 标 `interrupted` 并回到 `ready` 重跑。

这条规则解决的是「状态文件与执行事实不同步」这个最常见的恢复 bug。

### 7.3 幂等键与副作用防重放

每个步骤有 `idempotency_key`（默认 = 步骤 id）。执行器启动前查 journal：该 key 是否已出现 `step_finished(ok)`。

- **纯计算步骤**：重跑安全，直接重跑。
- **有副作用步骤**（`side_effects: true`）：若 journal 显示 key 已被消费但状态未确认，**不自动重跑**，转 `blocked` 并生成人工确认请求，问题写明「该步骤可能已产生副作用，请确认是否重放」。

**只用状态位做恢复一定会重放副作用** —— 状态位回答「跑到哪了」，回答不了「外部世界被改了几次」。这是本设计的核心结论。

### 7.4 中断修复流程

`resume` 启动时：
1. 读 `spec.json`（不是外部文件，防止两次运行的计划不一致）
2. 读 `state.json`；读 `journal.jsonl` 并按 §7.2 对齐
3. 把残留的 `running` 标 `interrupted` → `ready`
4. 校验 `run.status` 非终态，否则拒绝 resume
5. 追加 `resumed` 事件，继续主循环

## 8. 人工介入

**阻塞分级**：

| 场景 | 处理 | 状态 |
| --- | --- | --- |
| 可重试错误（超时/非零退出/验证失败） | 按 `max_attempts` 重试，每次记反思 | 留 `ready` |
| 不可重试错误（边界违反/计划错误） | 直接失败 | `failed` |
| 显式人工门（`requires_approval`） | 写审批请求，暂停整轮 | `blocked` + 运行 `paused` |
| 信息不足（步骤返回 `needs_info`） | 写审批请求，问题附在请求里 | `blocked` + `paused` |
| 副作用可疑 | 见 §7.3 | `blocked` + `paused` |

审批是文件协议：`approvals/<step_id>.json` 含 `status: pending|approved|rejected`、`question`、`note`、`decided_at`。CLI 的 `approve` / `reject` 只改这个文件，`resume` 时读取结论。驳回 → 该步 `cancelled`，其下游 `skipped`。

## 9. 结构化结果

`report.json` 顶层字段（需求五项一一对应）：

| 字段 | 内容 |
| --- | --- |
| `final_outputs` | 每步的产出路径 + 关键结论（仅终态为 succeeded 的步骤） |
| `completed_steps` | 已完成步骤列表（含耗时、尝试次数、验证证据） |
| `pending_steps` | 未完成项（含当前状态与原因：blocked/failed/skipped/cancelled） |
| `anomalies` | 异常原因（错误类型、消息、发生在第几次尝试、是否可重试） |
| `next_actions` | 后续处理建议（由状态与异常码推导，不是套话） |

附加字段：`run_id`、`goal`、`status`、`started_at`、`ended_at`、`duration_s`、`counters`、`boundaries_hit`。

`next_actions` 的推导规则（写死，便于测试）：见 `report.py:derive_next_actions`。

## 10. 完成 / 暂停 / 终止条件

| 判定 | 条件 |
| --- | --- |
| **完成** | 所有步骤处于终态，且不存在 `failed`（`skipped` 允许） |
| **暂停** | 存在 `blocked` 且无其他可执行 ready 步骤 → `paused`，等 `approve`/`reject` |
| **失败终止** | 存在 `failed` 且无 ready、无 blocked → `failed` |
| **强制终止** | 步数/墙钟/累计尝试任一超限，或检测到死锁（无 ready 无 blocked 但仍有非终态）→ `aborted` |

暂停不是失败：`paused` 可无限期停留，`resume` 不消耗预算计数以外的东西。

## 11. 已知边界（如实登记）

- **串行执行**：不做并发。并发会引入副作用顺序不可复现，与本设计的恢复语义冲突。
- **无验证信号的步骤**：只能信退出码，报告里标 `verification: "none"`。这是弱点，不是特性。
- **`artifacts` 只做记录不校验存在性**：它是「这一步应该产出什么」的声明，用于报告与越界检查；「确实产出了」由 `verify` 负责。二者别混。
- **崩溃窗口**：若进程死在 `os.replace` 之前且 journal 也还没来得及 append，该步视为未执行（会重跑）。纯计算安全，有副作用步骤靠 `side_effects` 标记兜底。
- **命令走系统 shell**：`shell=True`，因此支持管道与重定向。代价是命令字符串具备 shell 权限 —— `Policy` 的逐段首 token 校验能挡住误操作，**挡不住蓄意绕过**。要强隔离请用容器或低权限账户。
- **Windows 上 `proc.kill()` 不杀孙进程**（本次实测）：`shell=True` 起的 `cmd.exe` → 子进程在父进程被杀后仍会继续执行。若你的恢复逻辑依赖「被杀即停止」，必须杀进程树（`taskkill /T /F`），否则会看到「已完成步骤被执行两次」的假象。`examples/crash_drill.py` 已按此实现。
- **跨机器恢复**：`state.json` 里存的是绝对路径，换机器 resume 需要路径一致。
