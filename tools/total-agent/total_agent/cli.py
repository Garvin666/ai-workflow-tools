"""[自研工具] total-agent · 命令行入口

自研标识：Garvin666 自研工具 total-agent / 用途：run/resume/status/approve/reject/report/validate
适用场景：人机共同推进长时任务。仓库链接：待推送。

退出码按「结论是否可得」划分（与 ai-workflow 的接口契约同口径）：
  0 成功收口 / 2 暂停待人工（不是错误）/ 3 失败 / 4 中止 / 1 用法或输入错误
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from . import planner
from .errors import PlanError, StateError, TotalAgentError
from .executors import LLMExecutor
from .human import ApprovalQueue
from .model import RunStatus, TaskSpec
from .policy import Boundaries, Policy
from .report import build_report, render_markdown
from .runtime import Runtime
from .store import RunStore

EXIT_BY_STATUS = {
    RunStatus.SUCCEEDED.value: 0,
    RunStatus.PAUSED.value: 2,
    RunStatus.FAILED.value: 3,
    RunStatus.ABORTED.value: 4,
    RunStatus.RUNNING.value: 4,
}


def _stdout_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - 老环境无 reconfigure，忽略
            pass


def load_spec(path: str | Path) -> TaskSpec:
    p = Path(path)
    if not p.exists():
        raise PlanError(f"计划文件不存在：{p}")
    try:
        with open(p, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except json.JSONDecodeError as exc:
        raise PlanError(f"计划文件不是合法 JSON：{exc}") from exc
    spec = TaskSpec.from_dict(raw)
    planner.validate(spec.steps)
    return spec


def default_run_dir(workspace: Path, spec: TaskSpec) -> Path:
    return workspace / ".total-agent" / "runs" / spec.run_id


# --------------------------------------------------------------------------- #
# 子命令
# --------------------------------------------------------------------------- #


def cmd_run(args: argparse.Namespace) -> int:
    spec = load_spec(args.spec)
    workspace = Path(args.workspace).resolve() if args.workspace else Path.cwd().resolve()
    policy = Policy(Boundaries.from_dict(spec.boundaries, workspace))
    if args.max_steps is not None:
        policy.b.max_steps = args.max_steps
    if args.max_wall is not None:
        policy.b.max_wall_seconds = args.max_wall
    if args.dry_run:
        policy.b.dry_run = True
    if args.allow:
        policy.b.allowed_commands = tuple(args.allow)

    run_dir = Path(args.run_dir).resolve() if args.run_dir else default_run_dir(workspace, spec)
    store = RunStore(run_dir)
    if store.exists():
        raise StateError(f"{run_dir} 已有运行记录；续跑请用 `resume`，重跑请换 --run-dir")

    runtime = Runtime(spec, store, policy, dry_run=policy.b.dry_run)
    state = runtime.run()
    return _finish(state, spec, store, args)


def cmd_resume(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir).resolve()
    store = RunStore(run_dir)
    spec = store.load_spec()
    workspace = Path(args.workspace).resolve() if args.workspace else Path.cwd().resolve()
    policy = Policy(Boundaries.from_dict(spec.boundaries, workspace))
    if args.dry_run:
        policy.b.dry_run = True

    runtime = Runtime(spec, store, policy, dry_run=policy.b.dry_run)
    state = runtime.resume()
    return _finish(state, spec, store, args)


def _finish(state: Any, spec: TaskSpec, store: RunStore, args: argparse.Namespace) -> int:
    report = build_report(state, spec, store)
    store.write_report(report)
    if not args.quiet:
        print(render_markdown(report))
    else:
        print(f"[{state.status.value}] run_id={state.run_id} dir={store.dir}")
    return EXIT_BY_STATUS.get(state.status.value, 4)


def cmd_status(args: argparse.Namespace) -> int:
    store = RunStore(Path(args.run_dir).resolve())
    if not store.exists():
        raise StateError(f"{store.dir} 不是一次已有运行")
    state = store.load_state()
    spec = store.load_spec()
    if args.json:
        print(json.dumps({"state": state.to_dict(), "goal": spec.goal}, ensure_ascii=False, indent=2))
        return 0
    print(f"run_id : {state.run_id}")
    print(f"目标   : {state.goal}")
    print(f"状态   : {state.status.value}")
    print(f"计数   : {state.counters}")
    print("步骤：")
    for step in spec.steps:
        st = state.steps.get(step.id)
        if st is None:
            continue
        mark = {
            "succeeded": "✓",
            "failed": "✗",
            "blocked": "‖",
            "skipped": "-",
            "cancelled": "x",
            "running": ">",
            "ready": "·",
            "pending": " ",
            "interrupted": "!",
        }.get(st.status.value, "?")
        print(f"  [{mark}] {step.id:<16} {st.status.value:<12} {step.action[:40]}")
    pending = ApprovalQueue(store.approvals_dir).pending()
    if pending:
        print("待审批：")
        for ap in pending:
            print(f"  - {ap.step_id}：{ap.question}")
    return 0


def cmd_decide(args: argparse.Namespace) -> int:
    store = RunStore(Path(args.run_dir).resolve())
    if not store.exists():
        raise StateError(f"{store.dir} 不是一次已有运行")
    queue = ApprovalQueue(store.approvals_dir)
    ap = queue.decide(args.step_id, approved=args.command == "approve", note=args.note or "")
    print(f"{ap.step_id}: {ap.status}" + (f"（{ap.note}）" if ap.note else ""))
    print("下一步：total-agent resume " + str(store.dir))
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    store = RunStore(Path(args.run_dir).resolve())
    if not store.exists():
        raise StateError(f"{store.dir} 不是一次已有运行")
    state = store.load_state()
    spec = store.load_spec()
    report = build_report(state, spec, store)
    if args.format == "json":
        text = json.dumps(report, ensure_ascii=False, indent=2)
    else:
        text = render_markdown(report)
    if args.write:
        path = store.write_report(report)
        print(f"已写入 {path}")
    print(text)
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    spec = load_spec(args.spec)
    order = planner.topo_order(spec.steps)
    print(f"OK：{len(spec.steps)} 个步骤，拓扑序 = {' → '.join(order)}")
    for step in spec.steps:
        deps = ",".join(step.depends_on) or "-"
        print(
            f"  {step.id:<16} executor={step.executor:<7} deps={deps:<20} "
            f"attempts={step.max_attempts} approval={step.requires_approval}"
        )
    return 0


# --------------------------------------------------------------------------- #
# 参数
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="total-agent",
        description="零依赖的多步骤任务总代理：编排 / 边界 / 恢复 / 结构化结果",
    )
    sub = p.add_subparsers(dest="command", required=True)

    def add_common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--workspace", help="工作区根（默认当前目录）")
        sp.add_argument("--quiet", action="store_true", help="只打印一行结论")
        sp.add_argument("--dry-run", action="store_true", help="不真执行，只走流程")

    sp = sub.add_parser("run", help="按计划启动一次运行")
    sp.add_argument("spec", help="计划 JSON 路径")
    sp.add_argument("--run-dir", help="运行目录（默认 <workspace>/.total-agent/runs/<run_id>）")
    sp.add_argument("--max-steps", type=int, help="覆盖步数上限")
    sp.add_argument("--max-wall", type=float, help="覆盖墙钟上限（秒）")
    sp.add_argument("--allow", action="append", help="命令白名单（可重复；不传=不限制）")
    add_common(sp)
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("resume", help="从上次中断处续跑")
    sp.add_argument("run_dir", help="运行目录")
    add_common(sp)
    sp.set_defaults(func=cmd_resume)

    sp = sub.add_parser("status", help="查看运行状态")
    sp.add_argument("run_dir")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_status)

    for name in ("approve", "reject"):
        sp = sub.add_parser(name, help=f"{name} 一个人工确认请求")
        sp.add_argument("run_dir")
        sp.add_argument("step_id")
        sp.add_argument("--note", help="结论备注")
        sp.set_defaults(func=cmd_decide)

    sp = sub.add_parser("report", help="输出结构化结果")
    sp.add_argument("run_dir")
    sp.add_argument("--format", choices=("markdown", "json"), default="markdown")
    sp.add_argument("--write", action="store_true", help="同时写入 <run_dir>/report.json")
    sp.set_defaults(func=cmd_report)

    sp = sub.add_parser("validate", help="只校验计划，不执行")
    sp.add_argument("spec")
    sp.set_defaults(func=cmd_validate)

    return p


def main(argv: Sequence[str] | None = None) -> int:
    _stdout_utf8()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except TotalAgentError as exc:
        print(f"[{getattr(exc, 'code', 'error')}] {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("[interrupted] 已中止；用 resume 续跑", file=sys.stderr)
        return 130
