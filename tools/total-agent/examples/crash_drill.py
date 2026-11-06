"""真实崩溃演练：把总代理跑到一半强杀，重启续跑，验证「已完成的不重跑」。

这是「可恢复」唯一有说服力的验收方式 —— 逻辑层回放证明不了持久化时序，
必须让进程真的死在半路上。

两个在本次实现中实测踩到的坑，写在这里省下一次重踩：
1. Windows 上 `proc.kill()` 只杀父进程，`shell=True` 起的 cmd.exe → python 孙进程会活下来继续写文件。
   必须杀进程树（taskkill /T /F），否则演练结果不可复现。
2. 不能预设「第几秒被杀的是哪一步」—— 解释器启动耗时会让它漂移。
   改为崩溃后读 state.json 找出 running 的那一步，以它为界做动态断言。

用法：
    python examples/crash_drill.py

退出码：0 = RESUME_OK；1 = 断言未通过（逐条打印不符项）
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parent.parent
STEP_SCRIPT = "_drill_step.py"
STEP_SOURCE = '''import sys, time, pathlib, os

sid = sys.argv[1]
delay = float(sys.argv[2]) if len(sys.argv) > 2 else 0.5
time.sleep(delay)
p = pathlib.Path("trace.log")
with p.open("a", encoding="utf-8") as f:
    f.write(sid + "\\n")
    f.flush()
    os.fsync(f.fileno())
'''

STEPS = ["a", "b", "c", "d", "e"]
DELAY = 0.8
KILL_AFTER = 2.2  # 落在第二、三步之间，保证既有「已完成」也有「被打断」


def build_spec() -> dict:
    steps = []
    for i, sid in enumerate(STEPS):
        steps.append(
            {
                "id": sid,
                "action": f"演练步骤 {sid}",
                "executor": "shell",
                "command": f'"{sys.executable}" {STEP_SCRIPT} {sid} {DELAY}',
                "depends_on": [STEPS[i - 1]] if i else [],
                "timeout_s": 60,
                "max_attempts": 1,
            }
        )
    return {
        "name": "crash-drill",
        "goal": "验证进程被强杀后能从 checkpoint 续跑且不重放已完成步骤",
        "steps": steps,
        "boundaries": {"max_wall_seconds": 180, "max_steps": 20},
    }


def env_with_pkg() -> dict:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(PKG_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def run_cli(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "total_agent", *args],
        cwd=str(PKG_ROOT),
        env=env_with_pkg(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )


def kill_tree(pid: int) -> None:
    """连子孙一起杀。taskkill /T 会把 cmd.exe 与它起的 python 一并终止。"""
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True,
            text=True,
            errors="replace",
        )
    else:
        import signal

        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except ProcessLookupError:
            pass


def count_lines(path: Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    if not path.exists():
        return counts
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            counts[line] = counts.get(line, 0) + 1
    return counts


def journal_stats(run_dir: Path) -> dict:
    started: dict[str, int] = {}
    finished: dict[str, int] = {}
    path = run_dir / "journal.jsonl"
    if not path.exists():
        return {"started": started, "finished": finished}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        sid = e.get("step_id")
        if not sid:
            continue
        if e.get("event") == "step_started":
            started[sid] = started.get(sid, 0) + 1
        elif e.get("event") == "step_finished" and e.get("ok"):
            finished[sid] = finished.get(sid, 0) + 1
    return {"started": started, "finished": finished}


def main() -> int:
    failures: list[str] = []

    tmp = Path(tempfile.mkdtemp(prefix="total-agent-drill-"))
    work = tmp / "ws"
    run_dir = tmp / "run"
    work.mkdir(parents=True)

    (work / STEP_SCRIPT).write_text(STEP_SOURCE, encoding="utf-8")
    spec_path = tmp / "drill.json"
    spec_path.write_text(json.dumps(build_spec(), ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[1/4] 启动运行（工作区 {work}）")
    kwargs: dict = {}
    if os.name != "nt":
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(
        [sys.executable, "-m", "total_agent", "run", str(spec_path),
         "--workspace", str(work), "--run-dir", str(run_dir), "--quiet"],
        cwd=str(PKG_ROOT),
        env=env_with_pkg(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        **kwargs,
    )

    time.sleep(KILL_AFTER)
    killed = proc.poll() is None
    if killed:
        kill_tree(proc.pid)
    proc.wait(timeout=60)
    time.sleep(1.5)  # 等可能残留的子进程收尾，否则计数会漂
    print(f"[2/4] {'已强杀进程树' if killed else '进程提前退出（未杀到）'}")

    if not killed:
        failures.append("进程在预设时间之前就结束了，演练没有命中执行中间态")

    state_path = run_dir / "state.json"
    pre: dict = {}
    interrupted: str | None = None
    if not state_path.exists():
        failures.append("强杀后 state.json 缺失 —— 状态没有落盘")
    else:
        pre = json.loads(state_path.read_text(encoding="utf-8"))
        print(f"      崩溃时 status = {pre.get('status')}")
        for sid, st in pre.get("steps", {}).items():
            print(f"      {sid}: {st['status']} attempts={st['attempts']}")
        for sid in STEPS:
            if pre.get("steps", {}).get(sid, {}).get("status") == "running":
                interrupted = sid
        print(f"      被中断的步骤 = {interrupted or '（无，崩溃发生在步骤之间）'}")

    print("[3/4] resume 续跑")
    resumed = run_cli(["resume", str(run_dir), "--workspace", str(work), "--quiet"])
    print(f"      退出码 {resumed.returncode}｜{resumed.stdout.strip()}")
    if resumed.returncode != 0:
        failures.append(f"resume 退出码 {resumed.returncode}（期望 0）：{resumed.stderr[-400:]}")

    print("[4/4] 校验")
    report_path = run_dir / "report.json"
    report = {}
    if not report_path.exists():
        failures.append("report.json 未生成")
    else:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    if report.get("status") != "succeeded":
        failures.append(f"最终状态 {report.get('status')}，期望 succeeded")

    counts = count_lines(work / "trace.log")
    stats = journal_stats(run_dir)
    print(f"      trace.log 每步执行次数 = {counts}")
    print(f"      journal step_started   = {stats['started']}")
    print(f"      journal step_finished  = {stats['finished']}")

    # 动态断言：被中断的那一步允许跑 1–2 次（副作用窗口），其余必须恰好 1 次
    for sid in STEPS:
        n = counts.get(sid, 0)
        if sid == interrupted:
            if n not in (1, 2):
                failures.append(
                    f"被中断的步骤 {sid} 执行 {n} 次（允许 1–2 次：已写盘但未确认收尾）"
                )
            if stats["started"].get(sid, 0) not in (1, 2):
                failures.append(f"步骤 {sid} 的 step_started 记录异常：{stats['started'].get(sid, 0)}")
        else:
            if n != 1:
                failures.append(f"步骤 {sid} 执行 {n} 次，期望 1 次")
            if stats["started"].get(sid, 0) != 1:
                failures.append(
                    f"步骤 {sid} 的 step_started 记录 {stats['started'].get(sid, 0)} 次，期望 1 次"
                )
        if stats["finished"].get(sid, 0) != 1:
            failures.append(
                f"步骤 {sid} 的 step_finished(ok) 记录 {stats['finished'].get(sid, 0)} 次，期望 1 次（重复完成 = 重放）"
            )

    if not failures:
        shutil.rmtree(tmp, ignore_errors=True)
        print("\nRESUME_OK")
        print(f"说明：崩溃前已完成的步骤没有被重跑；{interrupted or '（无中断步骤）'} 处于")
        print("      「可能已产生副作用」的窗口，执行 1–2 次属预期 —— 这正是 side_effects 标记的意义。")
        return 0

    print("\n[DRILL_FAILED]")
    for f in failures:
        print("  - " + f)
    print(f"（现场保留在 {tmp}，便于追查）")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
