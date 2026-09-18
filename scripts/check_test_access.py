# -*- coding: utf-8 -*-
# [自研工具] check_test_access.py
# 用途：校验留出测试集访问台账的两条不变量（I1 单次运行内每数据集测试集只读一次 / I2 读取调用点须落在打分路径白名单内），三态退出码，不许把「没测到」混进「通过」
# 适用场景：任何「留出测试集防泄漏」的审计；与生成台账的 `_test_access.py` 成对使用
# 作者：数模工作区自研（底座泛化优化-2026-09-17，R3）
# 仓库：https://github.com/Garvin666/ai-workflow-tools/blob/main/scripts/check_test_access.py
"""校验留出测试集访问台账（I1 / I2 两条不变量）。

自研脚本（本任务自行编写）：check_test_access.py
判据（由 _test_access.py 的口径澄清给出）：
  I1 单次运行内，每个数据集的测试集**只被读取一次**；
  I2 读取动作的调用点必须全部落在白名单内（打分路径），
     不允许出现在会反过来影响模型选择的路径上。

退出码（三态独立，不允许"没测到"混进"通过"）：
  0 = I1 与 I2 均成立
  1 = 有违反（查了，而且不符）
  2 = 无法判定（台账不存在 = 从未记录 ⇒ **不是通过**）

⚠️ 注意退出码 2 的含义：台账文件不存在**不能**当作"没问题"。它要么意味着
   测试集从未被读（那评估根本没做），要么意味着守卫没被挂上 —— 两种都要人来看。

用法::

    python check_test_access.py
    python check_test_access.py --ledger outputs/logs/test_access.jsonl
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

_HERE = Path(__file__).resolve().parent
COMP_DIR = _HERE.parent
sys.path.insert(0, str(_HERE))


def _write_summary(rel: str, payload: dict) -> None:
    """把判定结果落成 JSON。报告要引用这些数字，必须可被独立复算。

    ⚠️ 无法判定（退出码 2）时 i1_ok/i2_ok 写 **null**，不能写 false ——
    false 表示"查了，不成立"，null 表示"没查到"，两者性质不同。
    报告里若把 null 当 false 展示，就是把"没测到"说成了"出问题"。
    """
    try:
        p = COMP_DIR / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:                                            # noqa: BLE001
        pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ledger", default="outputs/logs/test_access.jsonl")
    ap.add_argument("--max-per-dataset", type=int, default=1,
                    help="单次运行内每个数据集允许的读取次数上限（默认 1）")
    ap.add_argument("--json-out", default="outputs/logs/test_access_summary.json",
                    help="把判定结果落成 JSON 供报告引用（报告数字须可复算）")
    args = ap.parse_args()

    try:
        from _test_access import ALLOWED_CALLERS
    except Exception as e:                                       # noqa: BLE001
        print(f"[无法判定] 无法导入访问台账模块: {e}")
        _write_summary(args.json_out, {"exit_code": 2, "reason": "import_failed",
                                      "i1_ok": None, "i2_ok": None})
        return 2

    p = COMP_DIR / args.ledger
    if not p.is_file():
        print(f"[无法判定] 台账不存在: {p}")
        print("  → 退出码 2。**不是通过**：可能测试集从未被读（评估没做），"
              "也可能守卫未挂上。两种情况都需要人确认。")
        _write_summary(args.json_out, {"exit_code": 2, "reason": "ledger_missing",
                                      "ledger": args.ledger,
                                      "i1_ok": None, "i2_ok": None})
        return 2

    recs = []
    bad_lines = 0
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            recs.append(json.loads(line))
        except json.JSONDecodeError:
            bad_lines += 1
    if not recs:
        print(f"[无法判定] 台账存在但无有效记录（损坏行 {bad_lines} 条）")
        _write_summary(args.json_out, {"exit_code": 2, "reason": "empty_ledger",
                                      "ledger": args.ledger, "bad_lines": bad_lines,
                                      "i1_ok": None, "i2_ok": None})
        return 2

    # ---- I1：按 (pid, dataset) 计数 ----
    cnt = Counter((r.get("pid"), r.get("dataset")) for r in recs)
    v1 = [{"pid": k[0], "dataset": k[1], "count": v}
          for k, v in sorted(cnt.items(), key=lambda kv: str(kv[0])) if v > args.max_per_dataset]

    # ---- I2：调用点白名单 ----
    callers = defaultdict(int)
    for r in recs:
        callers[r.get("caller") or "<unknown>"] += 1
    v2 = []
    for c in callers:
        fn = c.split(":")[-1]
        if fn not in ALLOWED_CALLERS:
            v2.append(c)

    print("=" * 88)
    print("留出测试集访问台账校验")
    print("=" * 88)
    print(f"台账: {p}")
    print(f"记录 {len(recs)} 条（损坏 {bad_lines} 行）；"
          f"不同进程 {len({r.get('pid') for r in recs})} 个；"
          f"数据集 {len({r.get('dataset') for r in recs})} 个")
    print(f"\n白名单（允许的调用点函数名）: {sorted(ALLOWED_CALLERS)}")
    print("实际调用点分布:")
    for c, n in sorted(callers.items(), key=lambda kv: -kv[1]):
        mark = "✔" if c.split(":")[-1] in ALLOWED_CALLERS else "✘"
        print(f"  {mark} {c}  ×{n}")

    print(f"\nI1 每次运行每数据集只读一次（上限 {args.max_per_dataset}）: "
          f"{'成立 ✔' if not v1 else '违反 ✘'}")
    for v in v1:
        print(f"    pid={v['pid']} dataset={v['dataset']} 读了 {v['count']} 次")
    print(f"I2 调用点全在白名单内: {'成立 ✔' if not v2 else '违反 ✘'}")
    for c in v2:
        print(f"    越界调用点: {c}")

    ok = not v1 and not v2
    print(f"\n结论: {'通过（退出码 0）' if ok else '不通过（退出码 1）'}")
    if bad_lines:
        print(f"⚠️ 另有 {bad_lines} 行无法解析 —— 台账本身可能损坏，请人工查看。")
    _write_summary(args.json_out, {
        "exit_code": 0 if ok else 1,
        "ledger": args.ledger,
        "n_records": len(recs),
        "bad_lines": bad_lines,
        "n_pids": len({r.get("pid") for r in recs}),
        "n_datasets": len({r.get("dataset") for r in recs}),
        "n_callers": len(callers),
        "callers": dict(sorted(callers.items(), key=lambda kv: -kv[1])),
        "allowed_callers": sorted(ALLOWED_CALLERS),
        "max_per_dataset": args.max_per_dataset,
        "i1_violations": v1,
        "i2_violations": v2,
        "i1_ok": not v1,
        "i2_ok": not v2,
    })
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
