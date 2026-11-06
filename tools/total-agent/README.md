# total-agent · 多步骤任务总代理

零依赖（纯标准库）的任务编排内核：给它一份计划，它负责调度、执行、边界、恢复和汇报。

```
计划(JSON) ──► 调度（DAG + 优先级）──► 执行（shell/python/llm）
                    │                        │
                    │                   验证 / 重试
                    ▼                        │
              行为边界（路径/命令/预算）      │
                    │                        ▼
              journal.jsonl ◄──── 状态落盘 + 崩溃恢复
                    │
                    ▼
         report.json（产出/已完成/未完成/异常/建议）
```

## 30 秒上手

```bash
cd tools/total-agent

# 1. 校验计划（不执行）
python -m total_agent validate examples/p07-pipeline.json

# 2. 只看流程，不产生副作用
python -m total_agent run examples/p07-pipeline.json \
    --workspace ../.. --run-dir /tmp/demo --dry-run

# 3. 卡在人工门时：批准后继续
python -m total_agent approve /tmp/demo 03-baseline-eval --note "确认开跑"
python -m total_agent resume /tmp/demo --workspace ../.. --dry-run

# 4. 生成结构化结果
python -m total_agent report /tmp/demo --format json
```

不加 `PYTHONPATH` 也能跑：在 `tools/total-agent/` 目录下执行即可。

## 它解决的五个问题

| 问题 | 做法 | 位置 |
| --- | --- | --- |
| 任务有依赖，顺序不能乱 | DAG 校验 + 拓扑就绪集 + 优先级 | `planner.py` |
| 跑飞了要找回来 | 路径白名单 / 命令黑白名单 / 步数·墙钟·尝试上限 | `policy.py` |
| 崩了要能续 | 原子 checkpoint + append-only journal，**journal 是真相源** | `store.py` |
| 关键步骤要人点头 | 文件协议审批队列，暂停不等于失败 | `human.py` |
| 交付要说清做了什么 | 五项结构化结果 + 可推导的后续建议 | `report.py` |

## 计划格式（JSON，示例见 `examples/`）

```json
{
  "goal": "这次要达成什么（必填）",
  "steps": [
    {
      "id": "01-check",
      "action": "人能看懂的一句话",
      "executor": "shell",
      "command": "bash scripts/check.sh",
      "depends_on": [],
      "priority": 9,
      "timeout_s": 600,
      "max_attempts": 2,
      "requires_approval": false,
      "side_effects": false,
      "verify": "test -f out/result.json",
      "artifacts": ["out/result.json"]
    }
  ],
  "boundaries": {
    "max_wall_seconds": 3600,
    "allowed_commands": ["bash", "python"]
  }
}
```

字段详解见 [`docs/USAGE.md`](docs/USAGE.md)，设计理由见 [`docs/DESIGN.md`](docs/DESIGN.md)。

## CLI

| 命令 | 作用 | 退出码 |
| --- | --- | --- |
| `validate <spec>` | 只校验，不执行 | 0 / 1 |
| `run <spec>` | 启动一次运行 | 0 成功 / 2 暂停 / 3 失败 / 4 中止 |
| `resume <run_dir>` | 从上次中断处续跑 | 同上 |
| `status <run_dir>` | 查看每步状态 | 0 |
| `approve` / `reject` | 处理人工确认请求 | 0 |
| `report <run_dir> --format json\|markdown` | 结构化结果 | 0 |

退出码按「结论是否可得」划分：**2 = 暂停等人工，不是错误**。

## 验收证据（本机实跑）

```
$ python -m unittest discover -s tests -t tests
Ran 136 tests in 9.5s
OK

$ python examples/crash_drill.py
[2/4] 已强杀进程树
      崩溃时 a: succeeded  b: succeeded  c: running  d: pending  e: pending
[4/4] trace.log 每步执行次数 = {'a': 1, 'b': 1, 'c': 2, 'd': 1, 'e': 1}
RESUME_OK
```

崩溃前完成的 a、b **没有被重跑**；被打断的 c 重跑一次（副作用窗口，符合预期）。

## 目录

```
total-agent/
├── total_agent/          内核（9 个模块，零第三方依赖）
├── tests/                136 个用例
├── examples/
│   ├── p07-pipeline.json 本项目真实流程（10 步、3 条支线、1 个人工门）
│   └── crash_drill.py    真实崩溃演练
└── docs/
    ├── DESIGN.md         设计规格（状态机 / 边界 / 恢复语义）
    └── USAGE.md          字段与流程详解
```

## 已知边界

- **串行执行**，不并发（并发会让副作用顺序不可复现，与恢复语义冲突）
- **不是安全沙箱**：命令走系统 shell，`Policy` 挡误操作，挡不住蓄意绕过
- **不内置 LLM**：`LLMExecutor` 是抽象接口 + mock，真接模型由使用方注入
- **artifacts 只声明不校验**：存在性由 `verify` 负责

完整清单见 `docs/DESIGN.md` §11。

---

[自研工具] 名称：total-agent ｜ 用途：多步骤任务编排与恢复 ｜ 适用场景：长时实验/构建流程的自主推进 ｜ 仓库：待推送
