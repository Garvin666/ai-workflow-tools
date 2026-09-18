# -*- coding: utf-8 -*-
# [自研工具] analyze_selection_bias.py
# 用途：把「CV − 留出测试」分解为「公共项」与「选型偏差」两部分，修正「直接当成 winner's curse」的口径错误；用**差分法**减掉事前固定参照模型后再谈偏差，并附反证数据
# 适用场景：任何「先 CV 选型、又用 CV 报数」的场景下量化选型偏差；不适用于没有留出测试集、无法做差分的场景
# 作者：数模工作区自研（底座泛化优化-2026-09-17，R3）
# 仓库：https://github.com/Garvin666/ai-workflow-tools/blob/main/scripts/analyze_selection_bias.py
"""把「CV − 留出测试」分解为「公共项」与「选型偏差」两部分。

自研脚本（本任务自行编写）：analyze_selection_bias.py
用途：修正在步骤 5 最初设计里的口径错误 —— 不能把 "CV 分数 − 留出测试分数"
      直接当成选型偏差（winner's curse）。实测反证：``pm25_small`` 在池大小 k=2
      （池里只有 linear / ridge，"挑选"几乎不存在）反而给出全表最大偏差 +0.115，
      且 5 个种子全部复现 —— 若这是 winner's curse，就该"池越大偏差越大"，与实测相反。

口径（本节起，全文以此为准）
----------------------------
对同一「数据集 × 种子 × 池大小 k」，设选中模型为 M，参照（事前固定）模型为 R：

    naive_bias(M) = CV(M)   − Test(M)          # 朴素口径，混了三个来源
    ref_gap(R)    = CV(R)   − Test(R)          # 公共项：CV 噪声 + 该数据集固有差异
    sel_gain_cv   = CV(M)   − CV(R)            # "挑"在选型指标上买到多少
    sel_gain_test = Test(M) − Test(R)          # "挑"在真实泛化上买到多少
    sel_bias      = sel_gain_cv − sel_gain_test
                  = naive_bias(M) − ref_gap(R)  # 等价形式：公共项被相减抵掉

`sel_bias` 才是"因为选了 CV 最高者而多报出来的那部分"。R 必须**事前固定**
（本任务取底座出厂默认模型 linear / logistic），绝不能也是"挑"出来的，否则差分不成立。

判据（事先固定）
----------------
· H1 若 naive_bias 随 k 单调上升 → 支持 winner's curse；
     若在 k=2 处最大 → **反证**，naive 口径测的不是选型偏差。
· H2 sel_bias(k) 应随 k 增大（候选越多、挑得越狠）；且在 k=2 处应接近 0
     （池里只有两个近似模型，"挑"的空间极小）。
· H3 sel_bias 在各数据集上的符号一致性 —— 只报平均值会掩盖分歧。

退出码：0 = 分析完成；2 = 输入缺失/无法判定（**不是通过**）。

用法::

    python analyze_selection_bias.py
    python analyze_selection_bias.py --baseline outputs/bias/selection_bias_baseline.json
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

_HERE = Path(__file__).resolve().parent
COMP_DIR = _HERE.parent


def load(p: Path) -> dict:
    if not p.is_file():
        raise FileNotFoundError(p)
    return json.loads(p.read_text(encoding="utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", default="outputs/bias/selection_bias_baseline.json")
    ap.add_argument("--reference", default="outputs/bias/default_model_reference.json")
    ap.add_argument("--out", default="outputs/bias/selection_bias_analysis.json")
    args = ap.parse_args()

    try:
        base = load(COMP_DIR / args.baseline)
        ref = load(COMP_DIR / args.reference)
    except FileNotFoundError as e:
        print(f"[无法判定] 缺少输入文件: {e}")
        print("  → 退出码 2（不是通过，也不是失败）")
        return 2

    ref_map = {(r["dataset"], r["seed"]): r for r in ref["records"]}
    rows = []
    missing = []
    for rec in base["records"]:
        key = (rec["dataset"], rec["seed"])
        if key not in ref_map:
            missing.append(key)
            continue
        R = ref_map[key]
        cv_m, te_m = float(rec["reported"]), float(rec["test"])
        cv_r, te_r = float(R["cv_mean"]), float(R["test"])
        rows.append({
            "dataset": rec["dataset"], "task": rec["task"], "seed": rec["seed"],
            "k": rec["k"], "arm": rec["arm"],
            "chosen": rec["chosen"], "reference": R["reference_model"],
            "cv_chosen": round(cv_m, 6), "test_chosen": round(te_m, 6),
            "cv_ref": round(cv_r, 6), "test_ref": round(te_r, 6),
            # ---- 口径分离 ----
            "naive_bias_chosen": round(cv_m - te_m, 6),
            "ref_gap": round(cv_r - te_r, 6),
            "sel_gain_cv": round(cv_m - cv_r, 6),
            "sel_gain_test": round(te_m - te_r, 6),
            "sel_bias": round((cv_m - cv_r) - (te_m - te_r), 6),
        })

    if missing:
        print(f"[warn] {len(missing)} 个 (数据集,种子) 在参照文件里缺失，例如 {missing[:3]}")

    def agg(rs, key):
        v = [r[key] for r in rs]
        return {"n": len(v), "mean": round(statistics.fmean(v), 6),
                "median": round(statistics.median(v), 6),
                "min": round(min(v), 6), "max": round(max(v), 6),
                "frac_positive": round(sum(1 for x in v if x > 0) / len(v), 4)}

    # ---- H1：naive_bias 随 k ----
    by_k_naive = defaultdict(list)
    by_k_sel = defaultdict(list)
    for r in rows:
        by_k_naive[r["k"]].append(r)
        by_k_sel[r["k"]].append(r)
    # ⚠️ 「选中模型 == 参照模型」的格是**结构性退化的**，必须单独统计。
    #    原因：view_topk 取的是「复杂度序前 k 个」，不是「CV 排名前 k 个」。
    #    k=2 的池只有 {linear, ridge}、k=4 只多 {lasso, elasticnet} —— 全是线性族，
    #    与参照模型（linear/logistic）几乎不可分。此时 sel_gain_cv == sel_gain_test == 0，
    #    sel_bias 恒等于 0 —— 这是**构造**，不是"测出无偏差"。
    #    若把 k=2/4 的 0.0000 与 k=6/8 的真值混在一个均值里报，就是"绿灯但无意义"。
    #    做法：每个 k 档同时给出「有效选型格」（chosen != reference）的比例与其中位/均值。
    def sel_stats(rs):
        diff = [r for r in rs if r["chosen"] != r["reference"]]
        out = {"n_effective": len(diff),
               "pct_effective": round(len(diff) / len(rs), 4) if rs else 0.0}
        if diff:
            v = [r["sel_bias"] for r in diff]
            out.update({
                "sel_bias_mean_eff": round(statistics.fmean(v), 6),
                "sel_bias_median_eff": round(statistics.median(v), 6),
                "frac_positive_eff": round(sum(1 for x in v if x > 0) / len(v), 4)})
        else:
            out.update({"sel_bias_mean_eff": None, "sel_bias_median_eff": None,
                        "frac_positive_eff": None})
        return out

    h1 = {k: agg(v, "naive_bias_chosen") for k, v in sorted(by_k_naive.items())}
    h2 = {k: {**agg(v, "sel_bias"), **sel_stats(v)} for k, v in sorted(by_k_sel.items())}

    # 只看 max_cv 臂（winner's curse 的本体）
    mcv = [r for r in rows if r["arm"] == "max_cv"]
    h1_mcv = {k: agg([r for r in mcv if r["k"] == k], "naive_bias_chosen")
              for k in sorted({r["k"] for r in mcv})}
    h2_mcv = {k: {**agg([r for r in mcv if r["k"] == k], "sel_bias"),
                  **sel_stats([r for r in mcv if r["k"] == k])}
              for k in sorted({r["k"] for r in mcv})}

    # ---- H3：逐数据集（max_cv 臂）----
    # ⚠️ 不能只取全局 kmax：池守卫会让个别数据集的可达 k 更小
    #    （coupon_sale 触发守卫后池从 8 降到 7，没有 k=8）。
    #    若按全局 kmax 过滤，那个数据集会**从逐数据集表里静默消失** ——
    #    读者会以为它没测，而实际上只是它的最大档不同。
    #    做法：以全局 kmax 为主口径（保证跨数据集可比），对缺失者回退到它自己的最大 k，
    #    并在每行记录实际用的 k 与"是否为全局 kmax"，由报告如实标注。
    kmax = max(r["k"] for r in mcv)
    per_ds_max = {d: max(r["k"] for r in mcv if r["dataset"] == d)
                  for d in {r["dataset"] for r in mcv}}
    per_ds = defaultdict(list)
    for r in mcv:
        if r["k"] == min(kmax, per_ds_max[r["dataset"]]):
            per_ds[r["dataset"]].append(r)
    h3 = {d: {"naive": agg(v, "naive_bias_chosen"), "sel": agg(v, "sel_bias"),
              "ref_gap": agg(v, "ref_gap"),
              **sel_stats(v),
              "k": v[0]["k"], "k_is_global": bool(v[0]["k"] == kmax),
              "k_max_of_ds": per_ds_max[d],
              "n_records": len(v),
              "chosen_mode": max(set(x["chosen"] for x in v),
                                 key=lambda c: [x["chosen"] for x in v].count(c)),
              "reference": v[0]["reference"], "task": v[0]["task"]}
          for d, v in sorted(per_ds.items())}

    # ---- 公共项占比（朴素偏差里有多少是"与挑无关"的部分）----
    share = []
    for r in rows:
        if abs(r["naive_bias_chosen"]) > 1e-9:
            share.append(r["ref_gap"] / r["naive_bias_chosen"])
    payload = {
        "definitions": {
            "naive_bias_chosen": "CV(选中) − 留出测试(选中)；**混了**选型偏差/CV噪声/固有CV-留出差",
            "ref_gap": "CV(参照) − 留出测试(参照)；参照=事前固定模型，不看任何 CV",
            "sel_gain_cv": "CV(选中) − CV(参照)",
            "sel_gain_test": "留出测试(选中) − 留出测试(参照)",
            "sel_bias": "sel_gain_cv − sel_gain_test；**这才是选型偏差**（winner's curse）",
        },
        "reference_model": ref["default_model"],
        "baseline_file": args.baseline, "reference_file": args.reference,
        "n_rows": len(rows), "n_missing": len(missing),
        "H1_naive_bias_by_k_all_arms": h1,
        "H2_sel_bias_by_k_all_arms": h2,
        "H1_naive_bias_by_k_maxcv": h1_mcv,
        "H2_sel_bias_by_k_maxcv": h2_mcv,
        "H3_per_dataset_at_kmax": h3,
        "kmax": kmax,
        "ref_gap_over_naive_mean": round(statistics.fmean(share), 4) if share else None,
        "rows": rows,
    }
    out = COMP_DIR / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    # ---------------- 打印 ----------------
    print("=" * 92)
    print("口径分离：naive_bias（混） vs sel_bias（纯选型偏差）")
    print("=" * 92)
    for nm, tab, key in (("H1 naive_bias（全部臂）", h1, "naive_bias_chosen"),
                         ("H2 sel_bias（全部臂）", h2, "sel_bias"),
                         ("H1 naive_bias（仅 max_cv）", h1_mcv, "naive_bias_chosen"),
                         ("H2 sel_bias（仅 max_cv）", h2_mcv, "sel_bias")):
        print(f"\n{nm}")
        show_eff = "sel_bias" in key and any(
            "pct_effective" in v for v in tab.values())
        hdr = f"  {'k':>3}{'n':>6}{'均值':>12}{'中位':>12}{'最小':>12}{'最大':>12}{'占比>0':>9}"
        if show_eff:
            hdr += f"{'有效格':>8}{'有效占比':>10}{'sel(有效)':>12}"
        print(hdr)
        for k, v in tab.items():
            line = (f"  {k:>3}{v['n']:>6}{v['mean']:>12.5f}{v['median']:>12.5f}"
                    f"{v['min']:>12.5f}{v['max']:>12.5f}{v['frac_positive']:>9.2%}")
            if show_eff:
                me = v.get("sel_bias_mean_eff")
                me_txt = f"{me:>12.5f}" if me is not None else f"{'n/a':>12}"
                line += (f"{v.get('n_effective', 0):>8}"
                         f"{v.get('pct_effective', 0):>10.1%}"
                         f"{me_txt}")
            print(line)
        if show_eff and any(v.get("pct_effective", 1) < 0.99 for v in tab.values()):
            print("  ⚠ 有效格 = 选中模型 ≠ 参照模型的格。占比低说明该档池内几乎全是线性族，"
                  "sel_bias≈0 是构造而非测量结果，不能当成「无偏差」的证据。")

    print(f"\nH3 逐数据集（主口径 k={kmax}, max_cv 臂）—— 不用平均值掩盖分歧")
    print(f"  {'数据集':<16}{'k':>3}{'任务':<15}{'参照':<9}{'选中':<18}"
          f"{'naive':>10}{'ref_gap':>10}{'sel_bias':>10}")
    for d, v in h3.items():
        kk = f"{v['k']}" if v["k_is_global"] else f"{v['k']}*"
        print(f"  {d:<16}{kk:>3}{v['task']:<15}{v['reference']:<9}{v['chosen_mode']:<18}"
              f"{v['naive']['mean']:>10.5f}{v['ref_gap']['mean']:>10.5f}"
              f"{v['sel']['mean']:>10.5f}")
    if any(not v["k_is_global"] for v in h3.values()):
        miss = [d for d, v in h3.items() if not v["k_is_global"]]
        print(f"  * 号者因池守卫导致最大档小于 {kmax}，已回退到其自身最大 k —— "
              f"它们**没有**从本表消失：{miss}")

    print(f"\n公共项(ref_gap) 占 naive_bias 的比例（逐行平均）: {payload['ref_gap_over_naive_mean']}")
    print(f"→ {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
