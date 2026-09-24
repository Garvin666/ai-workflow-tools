#!/usr/bin/env python
# -*- coding: utf-8 -*-
# [自研工具] perf_baseline.py
# 名称：perf_baseline.py（ai-workflow 本地可测耗时基线采集器）
# 用途：把「本地可测耗时」固化成一条可重跑命令，产出可对比的 before/after JSON —— 让"性能
#       改动静默变快了"变成有原始数字支撑的结论，并内置**环境一致性前置判据**（本机 python
#       启动受 PYTHONPATH 注入的 shim 影响可差 4 倍，不同模式下的绝对值不可比）
# 适用场景：ai-workflow 本体的性能类改动前后对照（改 checks.py / http_fetch.py / 流程文档后）。
#           **不适用**：LLM 推理耗时（不在本机产生，本脚本不测也不估算）、token 精确计量
#           （本机无离线 tokenizer，需要占比请用分析文档的粗估口径）
# 作者：ai-workflow 自研（技能增强-ai-workflow-v3.4.0-2026-09-16，2026-09-16）
# 仓库：https://github.com/Garvin666/ai-workflow-tools/blob/main/scripts/perf_baseline.py
"""ai-workflow 本地可测耗时基线（v3.4.0：把 `.workbuddy/tmp/perf_probe*.py` 的测量固化为可重跑脚本）。

为什么需要它：性能类改动若没有**改造前**的原始数字，"变快了"就只是一句感觉。本脚本把
上一轮分析里每一项可测耗时固化为一条命令，产出可对比的 before/after。
（原始设计来自 `性能瓶颈分析与提速方案-ai-workflow-2026-09-16.md` §六 批次 0；
本流程自己的质量门禁要求性能类改动"耗时下降 **且** 输出与旧版逐字节一致"，故本脚本
只负责前半句，后半句由 `checks.py skill` 的新旧输出 diff 负责。）

用法：
    python perf_baseline_aiworkflow.py --label before
    python perf_baseline_aiworkflow.py --label after
    python perf_baseline_aiworkflow.py --label after --compare before.json
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# 解释器：显式环境变量优先，回退当前解释器（⚠️ 不得硬编码本机用户路径——出站清单第 4 项，v4.7.0 整理时修正）
PY = os.environ.get("AIWF_PY") or sys.executable
SKILL = Path(__file__).resolve().parent.parent
CHECKS = SKILL / "scripts" / "checks.py"
HTTP_FETCH = SKILL / "scripts" / "http_fetch.py"


def _find_sample_plan() -> Path:
    # 取一个真实存在、体量小的 plan.yaml 作 mark 基准（踩过的坑：伪造的 plan 会掩盖 BOM/缩进类缺陷）。
    # ⚠️ 勿硬编码任务目录名——archive_tasks.py 会把目录移进 tasks/_archive/，硬编码路径会静默失效
    #（v4.7.0 仓库整理实测：v3.2.1 目录被归档后 SAMPLE_PLAN.exists() 恒 False，mark 测点静默跳过）。
    tasks = SKILL / "tasks"
    hits = sorted(tasks.glob("*/plan.yaml")) or sorted(tasks.glob("_archive/*/*/plan.yaml"))
    if not hits:
        return tasks / "_占位不存在" / "plan.yaml"   # 让 .exists() 判 False，走既有降级分支
    return min(hits, key=lambda q: q.stat().st_size)


SAMPLE_PLAN = _find_sample_plan()
# v3.3.0 的 checks.py 备份：用于「同一棵树」的受控对照（见 m_checks_skill_old 的说明）
BACKUP_CHECKS = SKILL / "_archive" / "backups" / "_backup-v3.3.0" / "scripts" / "checks.v3.3.0.py"

RUNS = 3


def best(fn, runs: int = RUNS) -> float:
    """跑 runs 次取最小值（ms）。"""
    vals = []
    for _ in range(runs):
        t0 = time.perf_counter()
        fn()
        vals.append((time.perf_counter() - t0) * 1000)
    return round(min(vals), 1)


def run(*args, **kw) -> subprocess.CompletedProcess:
    return subprocess.run([PY, *args], capture_output=True, **kw)


def m_python_start() -> float:
    return best(lambda: run("-c", "pass"))


def m_gh_token() -> float | None:
    try:
        t0 = time.perf_counter()
        r = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=10)
        el = (time.perf_counter() - t0) * 1000
        return round(el, 1) if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def m_checks_skill() -> float:
    return best(lambda: run(str(CHECKS), "skill"))


def m_checks_skill_old() -> float | None:
    """同树对照：用 v3.3.0 的 checks.py 跑**同一棵** scripts/ 树。

    ⚠️ 为什么必须有这一项：before/after 两次基线之间**新增了脚本文件**（gen_skill_index.py、
    perf_baseline.py 自己），而 `checks.py skill` 的工作量与被测脚本数成正比 → 直接比两次
    基线的绝对耗时**不是同一被测对象**（after 天然吃亏）。本项固定「同一棵树 + 只换实现」，
    才是 P1（不产生 __pycache__）的干净判据 —— **这也是"性能类改动须有对照"的落地方式**，
    而不是把两次不同输入的快照摆在一起宣称变快了。
    """
    if not BACKUP_CHECKS.exists():
        return None
    return best(lambda: run(str(BACKUP_CHECKS), "skill"))


def m_checks_plan() -> float | None:
    if not SAMPLE_PLAN.exists():
        return None
    return best(lambda: run(str(CHECKS), "plan", str(SAMPLE_PLAN), "--base", str(SKILL)))


def m_checks_status() -> float:
    return best(lambda: run(str(CHECKS), "status", "--workspace", str(SKILL)))


def _plan_ids(plan: Path) -> list[str]:
    ids = []
    for line in plan.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s.startswith("- id:"):
            ids.append(s.split(":", 1)[1].strip())
    return ids


def m_mark_single(tmpdir: Path) -> tuple[float | None, float | None, int]:
    """逐条 mark（现状路径）与 --batch（已实现但未引导）的对比。返回 (单条总耗时, 批量耗时, 步数)。"""
    if not SAMPLE_PLAN.exists():
        return None, None, 0
    ids = _plan_ids(SAMPLE_PLAN)
    if not ids:
        return None, None, 0
    work = tmpdir / "plan.yaml"
    shutil.copyfile(SAMPLE_PLAN, work)
    t0 = time.perf_counter()
    for i in ids:
        run(str(CHECKS), "mark", str(work), i, "完成")
    t_one = (time.perf_counter() - t0) * 1000
    shutil.copyfile(SAMPLE_PLAN, work)
    bf = tmpdir / "batch.txt"
    bf.write_text("\n".join(f"{i} 完成" for i in ids), encoding="utf-8")
    t0 = time.perf_counter()
    run(str(CHECKS), "mark", str(work), "--batch", str(bf))
    t_bat = (time.perf_counter() - t0) * 1000
    return round(t_one, 1), round(t_bat, 1), len(ids)


def m_pycompile(tmpdir: Path) -> dict:
    """py_compile 的真实成本，以及"是否产生 __pycache__"。

    两路都测，因为 P1 的收益**依环境而定**：本机 `PYTHONPATH` 注入的 shim 会把非临时目录的
    `rmtree` 改走回收站（受控实验：同一删除按父目录相差 43.9 倍）。所以"省了多少"必须在
    **本机的当前模式**下实测，不能引用别处的数字。
      · legacy  = 现状路径：默认 cfile 写出 scripts/__pycache__ → 再 rmtree 清掉
      · tmpdir  = 建议路径：cfile 指向系统临时目录，根本不产生 __pycache__
    """
    files = sorted((SKILL / "scripts").glob("*.py"))
    cache = SKILL / "scripts" / "__pycache__"
    out = {"n_scripts": len(files)}
    import py_compile

    # legacy
    if cache.exists():
        shutil.rmtree(cache, ignore_errors=True)
    t0 = time.perf_counter()
    for f in files:
        py_compile.compile(str(f), doraise=True)
    out["legacy_produce_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    out["legacy_cache_files"] = len(list(cache.iterdir())) if cache.exists() else 0
    t0 = time.perf_counter()
    shutil.rmtree(cache, ignore_errors=True)
    out["legacy_cleanup_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    out["legacy_total_ms"] = round(out["legacy_produce_ms"] + out["legacy_cleanup_ms"], 1)
    if cache.exists():
        shutil.rmtree(cache, ignore_errors=True)

    # tmpdir
    dest = tmpdir / "pyc"
    dest.mkdir(exist_ok=True)
    t0 = time.perf_counter()
    for f in files:
        py_compile.compile(str(f), cfile=str(dest / (f.stem + ".pyc")), doraise=True)
    out["to_tmpdir_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    out["cache_left"] = cache.exists()
    return out


def m_github_cached(tmpdir: Path) -> dict:
    """`--github-repo` 缓存命中耗时（先冷取一次建缓存）。网络不可用时返回 {'skipped': ...}。

    ⚠️ 必须用 `--github-repo` 而不是裸 URL：裸 URL 分支本来就不解析凭据，测它等于测不到
    P3 要治的那段（主代理在发请求**之前**就 eager 解析 token，命中缓存也照付一张 gh 子进程）。
    """
    slug = "psf/requests"
    cold = run(str(HTTP_FETCH), "--github-repo", slug)
    if cold.returncode != 0:
        return {"skipped": f"冷取失败 rc={cold.returncode}（网络不可用）"}
    dest = tmpdir / "gh.json"

    def hit():
        with open(dest, "wb") as f:
            subprocess.run([PY, str(HTTP_FETCH), "--github-repo", slug, "--json"],
                           stdout=f, stderr=subprocess.DEVNULL)
    return {"cached_hit_ms": best(hit)}


def m_search_dryrun() -> float:
    """`--github-search --dry-run`：只打印 URL 不发请求，因此**不需要凭据**。"""
    return best(lambda: run(str(HTTP_FETCH), "--github-search", "topic:cli stars:>100", "--dry-run"))


def measure(label: str) -> dict:
    tmpdir = Path(tempfile.mkdtemp(prefix="aiwf_base_"))
    res: dict = {"label": label, "runs": RUNS, "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")}
    try:
        res["python_start_ms"] = m_python_start()
        res["gh_auth_token_ms"] = m_gh_token()
        res["checks_skill_ms"] = m_checks_skill()
        res["checks_skill_old_ms"] = m_checks_skill_old()
        res["checks_plan_ms"] = m_checks_plan()
        res["checks_status_ms"] = m_checks_status()
        one, bat, n = m_mark_single(tmpdir)
        res["mark_single_ms"], res["mark_batch_ms"], res["plan_steps"] = one, bat, n
        res["pycompile"] = m_pycompile(tmpdir)
        res["http_fetch"] = m_github_cached(tmpdir)
        res["search_dryrun_ms"] = m_search_dryrun()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return res


def show(res: dict) -> None:
    print(f"=== 基线：{res['label']}  （{res['timestamp']}，n={res['runs']} 取最小） ===")
    print(f"  python -c pass          {res['python_start_ms']:>9} ms   （解释器启动地板）")
    print(f"  gh auth token           {str(res['gh_auth_token_ms']):>9} ms")
    print(f"  checks.py skill         {res['checks_skill_ms']:>9} ms")
    print(f"  checks.py plan          {str(res['checks_plan_ms']):>9} ms")
    print(f"  checks.py status        {res['checks_status_ms']:>9} ms")
    n = res.get("plan_steps") or 0
    print(f"  mark 逐条 x{n:<3}         {str(res['mark_single_ms']):>9} ms")
    print(f"  mark --batch（1 次）    {str(res['mark_batch_ms']):>9} ms")
    if res["mark_single_ms"] and res["mark_batch_ms"]:
        d = res["mark_single_ms"] - res["mark_batch_ms"]
        print(f"    → 省 {d / 1000:.2f}s（{(1 - res['mark_batch_ms'] / res['mark_single_ms']) * 100:.0f}%）")
    pc = res["pycompile"]
    print(f"  py_compile {pc['n_scripts']} 脚本")
    print(f"    legacy（产生 __pycache__ 再删）{pc['legacy_total_ms']:>9} ms"
          f"   = 产生 {pc['legacy_produce_ms']} + 清理 {pc['legacy_cleanup_ms']}（{pc['legacy_cache_files']} 文件）")
    print(f"    cfile 指向临时目录            {pc['to_tmpdir_ms']:>9} ms   __pycache__ 残留={pc['cache_left']}")
    hf = res["http_fetch"]
    print(f"  --github-repo 缓存命中  {str(hf.get('cached_hit_ms', hf.get('skipped'))):>9}")
    print(f"  search --dry-run        {str(res.get('search_dryrun_ms')):>9} ms   （不发请求，不需凭据）")
    print(f"  __pycache__ 现存        {pool_check()}")


def pool_check() -> str:
    c = SKILL / "scripts" / "__pycache__"
    return f"{c} 存在={c.exists()}"


def compare(old: dict, new: dict) -> None:
    print()
    print(f"=== 对比：{old['label']} → {new['label']} ===")
    # ⭐ 环境一致性前置判据：本机 python 启动受 PYTHONPATH 注入的 shim 影响可差 4 倍以上，
    #    若两次基线不在同一模式下，绝对耗时**不可直接比**（会得出假的加速/倒退）。
    a0, b0 = old.get("python_start_ms"), new.get("python_start_ms")
    if a0 and b0:
        ratio = max(a0, b0) / max(min(a0, b0), 1e-6)
        tag = "✅ 同模式" if ratio < 1.5 else "❌ 环境不同（不可直接比绝对值）"
        print(f"  环境一致性：python 启动 {a0} ms vs {b0} ms（比值 {ratio:.2f}×）→ {tag}")
        if ratio >= 1.5:
            print("             → 请在**同一环境模式**下重跑两次基线，否则下表是假的。")
    print(f"  {'项目':<24}{'before':>10}{'after':>10}{'变化':>12}")
    rows = [
        ("python 启动", "python_start_ms"),
        ("gh auth token", "gh_auth_token_ms"),
        ("checks.py skill", "checks_skill_ms"),
        ("  └ 同树旧实现(v3.3.0)", "checks_skill_old_ms"),
        ("checks.py plan", "checks_plan_ms"),
        ("checks.py status", "checks_status_ms"),
        ("mark 逐条", "mark_single_ms"),
        ("mark --batch", "mark_batch_ms"),
        ("search --dry-run", "search_dryrun_ms"),
    ]
    for name, key in rows:
        a, b = old.get(key), new.get(key)
        if a is None or b is None:
            print(f"  {name:<24}{str(a):>10}{str(b):>10}{'—':>12}")
            continue
        pct = (b - a) / a * 100 if a else 0.0
        print(f"  {name:<24}{a:>10}{b:>10}{f'{pct:+.1f}%':>12}")
    a = old.get("pycompile", {}).get("legacy_total_ms")
    b = new.get("pycompile", {}).get("legacy_total_ms")
    if a and b:
        print(f"  {'py_compile legacy 合计':<22}{a:>10}{b:>10}{f'{(b - a) / a * 100:+.1f}%':>12}")
    a = old.get("pycompile", {}).get("legacy_cleanup_ms")
    b = new.get("pycompile", {}).get("legacy_cleanup_ms")
    if a is not None and b is not None:
        print(f"  {'  其中：清理 __pycache__':<20}{a:>10}{b:>10}{'（P1 目标=0）':>12}")
    a = old.get("http_fetch", {}).get("cached_hit_ms")
    b = new.get("http_fetch", {}).get("cached_hit_ms")
    if a and b:
        print(f"  {'http_fetch 缓存命中':<22}{a:>10}{b:>10}{f'{(b - a) / a * 100:+.1f}%':>12}")
    print()
    print("  ⚠️ 上表只含本机可测的确定性部分（脚本/子进程/IO）；LLM 推理耗时不在本机产生，未计。")


def main() -> None:
    ap = argparse.ArgumentParser(description="ai-workflow 本地可测耗时基线")
    ap.add_argument("--label", required=True, help="本次标签，如 before / after")
    ap.add_argument("--out", default=".", help="JSON 落盘目录")
    ap.add_argument("--compare", help="与之对比的既有 JSON 路径")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    res = measure(args.label)
    show(res)
    out = Path(args.out) / f"perf_baseline_{args.label}.json"
    out.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[ OK ] 已写入 {out}")
    if args.compare:
        old = json.loads(Path(args.compare).read_text(encoding="utf-8"))
        compare(old, res)


if __name__ == "__main__":
    main()
