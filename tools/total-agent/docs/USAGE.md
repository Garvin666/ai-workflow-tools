# 使用手册

## 1. 计划文件（spec.json）

顶层字段：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `goal` | ✔ | 本次运行要达成的目标，进报告与 journal |
| `steps` | ✔ | 步骤列表，至少一个 |
| `inputs` | | 输入说明（路径、参数），只作记录，框架不读 |
| `constraints` | | 约束清单，只作记录 |
| `acceptance` | | 验收标准清单，只作记录 |
| `boundaries` | | 行为边界覆盖，见 §3 |
| `name` / `run_id` | | 便于识别；不填自动生成 |

**注意**：`inputs` / `constraints` / `acceptance` 是**给人看和给报告用**的，框架不会据此做任何判定。想让某个条件真正生效，写成 `verify` 命令或 `boundaries` 字段。

### 步骤字段

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `id` | 必填 | 唯一标识，也用于审批文件名 |
| `action` | `""` | 一句话描述，进报告 |
| `executor` | `shell` | `shell` / `python` / `llm` / `none` |
| `command` | `""` | `executor=shell` 或 `llm` 时使用 |
| `target` | `""` | `executor=python` 时的注册名 |
| `args` | `{}` | 传给 python 执行器的参数 |
| `depends_on` | `[]` | 依赖的步骤 id 列表 |
| `priority` | `0` | 就绪集内越大越先执行 |
| `timeout_s` | `120` | 单步超时，超时按可重试失败处理 |
| `max_attempts` | `1` | 最大尝试次数（含首次） |
| `verify` | `""` | 执行成功后跑的校验命令，非 0 视为 `verify_failed` |
| `requires_approval` | `false` | 执行前需人工批准 |
| `side_effects` | `false` | 标记该步会改外部状态，影响重放策略（§5） |
| `artifacts` | `[]` | 预期产出的路径（只记录 + 越界检查，不校验存在性） |
| `idempotency_key` | `step:<id>` | 幂等键，恢复时按它比对 journal |
| `model_tier` | `default` | 仅作记录（`strong`/`mid`/`cheap`/`default`） |
| `question` | `""` | 审批时展示给人看的问题 |

### 依赖语义

- 依赖必须**全部成功**（`succeeded`）才就绪。
- 上游 `failed` 后，其所有传递下游会被标 `skipped`，原因 `dependency_failed`。
- `skipped` 是终态，不会自动复活；上游修好后 `resume` 只会重跑上游链路，被跳过的步骤需要新一轮计划。

> 这是刻意的：让「跳过」不可逆，避免「修了一半就自动往下冲」。

## 2. 执行器

| executor | 行为 | 需要 |
| --- | --- | --- |
| `shell` | 交给系统 shell（支持管道/重定向） | `command` |
| `python` | 调注册表里的 `fn(step, ctx)`，返回值经 `coalesce_result` 归一 | `target` + 注册 |
| `llm` | 子类实现 `complete(prompt, step)` | `LLMExecutor` 实例 |
| `none` | 什么都不做 | — |

python 目标的返回值可以是：`ExecResult` / `None` / `bool` / `str` / `dict`（支持 `ok`、`exit_code`、`stdout`、`stderr`、`error_kind`、`needs_info`）。

返回 `{"needs_info": "..."}` 会把步骤转成 `blocked` 并生成审批请求 —— 这是「信息不足时请求人工」的入口。

## 3. 行为边界（`boundaries`）

| 字段 | 默认 | 违反时 |
| --- | --- | --- |
| `max_steps` | 1000 | 运行 `aborted` |
| `max_wall_seconds` | 3600 | 运行 `aborted` |
| `max_total_attempts` | 200 | 运行 `aborted` |
| `allowed_commands` | `[]`（不限制） | 该步 `failed`，`policy_violation`，**不重试** |
| `denied_commands` | 内置危险表 | 同上（优先级高于白名单） |
| `require_approval` | `false` | 每步都走审批门 |
| `dry_run` | `false` | 不真执行 |
| `extra_allowed_paths` | `[]` | 额外允许写入的根（谨慎使用） |

`workspace_root` 由 CLI 的 `--workspace` 给出（默认当前目录），是**所有写入路径的唯一放行根**。

**命令检查粒度**：按 `&&` `||` `|` `;` `&` 切段，逐段取首个 token 校验。所以 `echo ok && rm -rf .` 会被拦。

## 4. 运行状态与判定

| 运行状态 | 什么时候进入 | 怎么出去 |
| --- | --- | --- |
| `running` | 启动 / resume | — |
| `paused` | 有步骤 `blocked` 且无其他可推进步骤 | `approve`/`reject` 后 `resume` |
| `succeeded` | 全部终态且无 failed，**且至少一步成功** | 终态 |
| `failed` | 有步骤 failed；或全部被取消/跳过 | 终态 |
| `aborted` | 预算超限 / 死锁 | 终态 |

**「至少一步成功」是刻意加的**：全部取消却报成功会让报告失真。

## 5. 恢复语义（重要）

恢复不靠状态文件，靠 **journal**：

| 崩溃现场 | journal 显示 | 恢复动作 |
| --- | --- | --- |
| `state` 说 `running` | 有 `step_finished(ok)` | 标 `succeeded`，**不重跑** |
| `state` 说 `running` | 无 `step_finished` | 标 `interrupted` → `ready`，重跑 |
| `state` 说 `succeeded` | — | 不动 |

**副作用防重放**：`side_effects: true` 的步骤，如果 journal 里 `step_started` 次数多于 `step_finished`，恢复时**不自动重跑**，而是转 `blocked` 并生成一条审批请求，问题写明「该步可能已产生副作用，请确认是否重放」。

这条是有代价的：它把「自动恢复」降级成「人工确认」。但重复执行一个 `git push` 或一次数据库写入的代价更高。

判断标准：**这一步重做一遍，外部世界会不会变成另一个样子？** 会，就标 `side_effects: true`。

## 6. 人工确认流程

```bash
# 运行卡住了
python -m total_agent status /path/to/run
#   [‖] 03-baseline-eval  blocked      即将跑满 95 min GPU...
#   待审批：
#     - 03-baseline-eval：即将跑满 95 min GPU 并写入 outputs/eval/b0_*。确认开始？

# 批准（或驳回）
python -m total_agent approve /path/to/run 03-baseline-eval --note "确认开跑"
python -m total_agent reject  /path/to/run 03-baseline-eval --note "先等 H2 结果"

# 继续
python -m total_agent resume /path/to/run
```

- **批准**：该步回到 `ready`，继续执行。
- **驳回**：该步 `cancelled`，其下游全部 `skipped`，运行最终为 `failed`（因为没产出）。
- **不处理直接 resume**：仍停在 `paused`，不会消耗预算。

审批是文件协议（`approvals/<step>.json`），所以也可以直接改文件，或由别的系统写入。

## 7. 结构化结果（`report.json`）

| 字段 | 内容 |
| --- | --- |
| `final_outputs` | 每个成功步骤的产物路径、摘要、验证方式 |
| `completed_steps` | 步骤 id、动作、尝试次数、耗时、验证类型、退出码 |
| `pending_steps` | 未完成的步骤 + 状态 + 原因 |
| `anomalies` | 错误码、消息、尝试次数、是否可重试 |
| `next_actions` | 由状态推导的后续动作（不是套话，每条指向具体操作） |
| `pending_approvals` | 尚未处理的审批请求 |
| `boundaries_hit` | 命中过哪些边界 |

`next_actions` 的推导规则在 `report.py:derive_next_actions`，错误码到建议的映射在 `ADVICE` 表。加新错误码时记得同步这两处 —— 否则会落到「查看 stderr 后自行判断」的兜底文案。

## 8. 常见问题

**Q：为什么不做并发？**
副作用顺序会变得不可复现，而可复现是恢复语义的前提。要并发，请把并行部分做成一个步骤内部的批处理。

**Q：`verify` 和 `artifacts` 有什么区别？**
`artifacts` 是声明「应该产出什么」（进报告、做越界检查）；`verify` 是判定「确实产出了、且符合预期」（非 0 就算失败）。只有 `artifacts` 没有 `verify` 的步骤，报告里会标 `verification: "none"` —— 那不是「已验证」，是「没验证」。

**Q：能在步骤里改边界吗？**
不能。边界在运行开始时固定，中途改会让「为什么这一步被放行」无法追溯。

**Q：怎么接真实模型？**
继承 `LLMExecutor` 实现 `complete()`，构造 `Runtime(..., llm=YourExecutor())`。凭据从环境变量读，不要写进 spec 或代码。

**Q：跑了一半想改计划？**
不要改 `spec.json`。它首跑时快照，`resume` 只读它 —— 改了会造成「状态按旧计划、执行按新计划」。正确做法：让当前运行收口（或 `reject` 掉剩余步骤），用新计划起一轮。
