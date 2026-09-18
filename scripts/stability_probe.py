# -*- coding: utf-8 -*-
# [自研工具] stability_probe
# 用途：多划分种子扫描 —— 只换数据划分种子（模型初始化固定），统计判据决策翻转率与增益跨种子离散度
# 适用场景：随机划分的 i.i.d. 数据集；需分辨「判据真的稳」与「只是这一次抽样运气好」
# 不适用场景：时序数据集 —— 固定尾切使划分与种子无关，扫描会空转（守卫会检出并以退出码 2 拒绝出汇总）
# 作者：数模工作区自研（K折验证增益-2026-09-17）
# 仓库：https://github.com/Garvin666/ai-workflow-tools/blob/main/scripts/stability_probe.py
"""稳定性探针：只换**数据划分种子**，看三种判据的结论会不会翻。

为什么必须有这一步
------------------
三臂对照里 ``debiased`` 把 ``demo_linear_mlp`` 判成了回退（−1.11%），看起来"去偏就修好了"。
但同一组的 K 折逐折增益是 ``[+10.3%, +15.9%, +16.2%, +14.8%, −32.9%]`` ——
**单折离群值就能把均值从 +14% 拽到 +4.9%**。这说明 demo 上"判据增益"这个量本身的
方差极大，那么 debiased 那一次的 −1.11% 完全可能只是一次幸运抽样。

只报一个点估计就宣布"升级有效"，等于用一次抽样下结论。所以这里把
**数据划分种子**扫一遍（模型初始化种子固定不动，否则分不清翻的是划分还是初始化），
统计每个判据的**决策翻转率**与判据增益的跨种子离散度。

★ 扫描维度有效性守卫（2026-09-18 新增）
---------------------------------------
本探针的判据**只在"换划分种子真的会改变划分"时才成立**。时序数据集用固定尾切，
划分与种子**无关** —— 于是每个种子的 `judge_gain` 与划分规模完全一样，
汇总里的"翻转率 0%"不是"判据稳定"，而是**扫描在空转**：一个恒说"是"的判据
翻转率也是 0%。这两件事在数字上长得一模一样，光看输出无法区分。

守卫的做法：跑完之后逐 (组合, 判据) 检查各划分种子的
``(judge_gain, n_fit, n_val)`` 是否**恒等**；恒等 ⇒ 该组被判定为"扫描空转"，
从聚合中剔除并单列一节说明，而不是让它以 0% 翻转率混进结论。

⚠️ "恒等"用**容差**而不是逐位相等：RF 底座的 ``predict`` 走 joblib 并行归约，
同一进程内连调两次浮点末位都可能不同（见 frozen_adapter 的"已知边界 2"）。
要求逐位相等的话，时序 + RF 那一组会**漏检** —— 判据过窄会把空转当成稳定。
划分规模（``n_fit``/``n_val``）则要求**严格相等**，它是整数，没有理由容忍漂移。

``--include-series`` 是**阴性对照用**的开关：强制把这些空转组纳入聚合。
一旦真的检出空转，脚本会**拒绝产出汇总并以退出码 2 结束** ——
因为那份汇总里的每个聚合数字都是无意义的（"无法判定" ≠ "通过"）。

用法::

    python tasks/K折验证增益-2026-09-17/stability_probe.py [--seeds 10] [--folds 5]
    python tasks/K折验证增益-2026-09-17/stability_probe.py --datasets demo,nonlinear
    python tasks/K折验证增益-2026-09-17/stability_probe.py --include-series   # 阴性对照
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(r"D:\数模")
FIN = ROOT / "competitions" / "2026-底座适配层" / "finetune"
TASK = ROOT / "tasks" / "K折验证增益-2026-09-17"
EVID = TASK / "证据"

sys.path.insert(0, str(FIN))
from run_frozen_adapter import MATRIX, run_one  # noqa: E402

MODES = ["holdout", "debiased", "kfold"]
# 划分种子用一组与 project.seed(42) 无关的数，避免"恰好命中默认划分"造成假稳定
SEED_BASE = 1000


def detect_vacuous(d: pd.DataFrame, tol: float) -> tuple[bool, str]:
    """判定一组 (combo, mode) 的扫描是否**空转**。

    判据：各划分种子的 ``n_fit``/``n_val`` 严格相等，且 ``judge_gain`` 的极差 ≤ tol。

    三态语义（不许把"判不了"混成"没问题"）：
      * ``len < 2``  ⇒ 返回 ``(False, 说明)``，理由写「样本不足，无法判定」——
        只有 1 个种子时根本不能断言"换种子没影响"。
      * 恒等        ⇒ 空转。
      * 非恒等      ⇒ 扫描有效。
    """
    d = d.dropna(subset=["judge_gain"])
    if len(d) < 2:
        return False, f"仅 {len(d)} 个有效种子，**无法判定**（不当作「扫描有效」）"
    if d["n_fit"].nunique() != 1 or d["n_val"].nunique() != 1:
        return False, (f"划分规模随种子变化（n_fit {sorted(d['n_fit'].unique())}），"
                       f"扫描有效")
    spread = float(d["judge_gain"].max() - d["judge_gain"].min())
    if spread <= tol:
        return True, (f"n_fit={int(d['n_fit'].iloc[0])} 恒定、n_val={int(d['n_val'].iloc[0])} 恒定，"
                      f"增益极差 {spread:.3e} ≤ {tol:.1e} ⇒ **扫描空转**"
                      f"（划分与种子无关，0% 翻转率是恒等式而不是稳定性）")
    return False, f"划分规模恒定但增益极差 {spread:.3e} > {tol:.1e} ⇒ 扫描有效"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=10)
    ap.add_argument("--folds", type=int, default=5)
    ap.add_argument("--epochs", type=int, default=300)
    ap.add_argument("--datasets", default=None,
                    help="逗号分隔的数据集白名单（默认全跑）。例：demo,nonlinear")
    ap.add_argument("--vacuity-tol", type=float, default=1e-6,
                    help="判定「扫描空转」时 judge_gain 允许的极差（划分规模须严格相等）")
    ap.add_argument("--include-series", action="store_true",
                    help="★ 阴性对照用：把空转组强行纳入聚合。检出空转即拒绝出汇总并退出码 2")
    args = ap.parse_args()

    matrix = MATRIX
    if args.datasets:
        want = {x.strip() for x in args.datasets.split(",") if x.strip()}
        matrix = [m for m in MATRIX if m[0] in want]
        if not matrix:
            print(f"[!!] --datasets={args.datasets} 未匹配到任何组合；可选 "
                  f"{sorted({m[0] for m in MATRIX})}")
            return 2

    rows: list[dict] = []
    t_all = time.time()
    for ds_key, base_name, adapter_kind in matrix:
        combo = f"{ds_key}_{base_name}_{adapter_kind}"
        kw = ({"hidden": 16, "n_layers": 1, "use_base_feature": False}
              if adapter_kind == "mlp" else {"rank": 2})
        for mode in MODES:
            for i in range(args.seeds):
                s = SEED_BASE + i
                t0 = time.time()
                try:
                    r = run_one(ds_key, base_name, adapter_kind, judge=mode,
                                folds=args.folds, epochs=args.epochs,
                                adapter_kw=kw, split_seed=s, write_artifacts=False)
                    m = r["metrics"]
                    rows.append({
                        "combo": combo, "dataset": ds_key, "base": base_name,
                        "adapter": adapter_kind, "mode": mode, "split_seed": s,
                        "judge_gain": m["val_rel_gain"],
                        "kfold_std": (m["kfold_std_gain"]
                                      if m["kfold_std_gain"] is not None else np.nan),
                        "accepted": m["adapter_accepted"],
                        "delta_r2": m["delta_r2"],
                        "policy_loss": max(0.0, -m["delta_r2"]),
                        "n_fit": m["n_fit"], "n_val": m["n_val"],
                        "elapsed": round(time.time() - t0, 3),
                    })
                except Exception as e:                      # 单次失败不该毁掉整轮
                    print(f"[!!] {combo} {mode} seed={s}: {type(e).__name__}: {e}")
                    rows.append({"combo": combo, "dataset": ds_key, "base": base_name,
                                 "adapter": adapter_kind, "mode": mode, "split_seed": s,
                                 "judge_gain": np.nan, "kfold_std": np.nan,
                                 "accepted": None, "delta_r2": np.nan,
                                 "policy_loss": np.nan, "n_fit": np.nan,
                                 "n_val": np.nan, "elapsed": round(time.time() - t0, 3)})
            print(f"  [done] {combo:<28} {mode:<9} "
                  f"{time.time()-t_all:6.1f}s")

    df = pd.DataFrame(rows)
    EVID.mkdir(parents=True, exist_ok=True)
    df.to_csv(EVID / "07_稳定性明细.csv", index=False, encoding="utf-8-sig")

    # ---------------- 扫描维度有效性守卫（先于任何聚合） ----------------
    vac: dict[tuple[str, str], tuple[bool, str]] = {}
    for combo in df["combo"].unique():
        for mode in MODES:
            d = df[(df["combo"] == combo) & (df["mode"] == mode)]
            if d.empty:
                continue
            vac[(combo, mode)] = detect_vacuous(d, args.vacuity_tol)
    vac_combos = sorted({c for (c, _m), (v, _r) in vac.items() if v})
    eff_combos = [c for c in df["combo"].unique() if c not in set(vac_combos)]
    dfe = df[df["combo"].isin(eff_combos)]

    guard: list[str] = []
    guard.append("=" * 92)
    guard.append("  扫描维度有效性守卫 · 逐 (组合, 判据) 明细")
    guard.append("=" * 92)
    guard.append(f"  判定容差 --vacuity-tol={args.vacuity_tol:.1e}（划分规模须严格相等）")
    guard.append("")
    for c, m in sorted(vac):
        v, r = vac[(c, m)]
        guard.append(f"  [{'空转' if v else '有效'}] {c:<28} {m:<9} {r}")
    guard.append("")
    if vac_combos:
        guard.append(f"  检出 {len(vac_combos)} 个组合的扫描为空转（划分与种子无关）：")
        for c in vac_combos:
            guard.append(f"    - {c}")
        guard.append("  含义：这些组「翻转率 0%」是**恒等式**，不是「判据稳定」。")
        guard.append("  处置：默认从聚合中剔除并单列一节；若用 --include-series 强行纳入，")
        guard.append("        脚本拒绝出汇总并以退出码 2 结束（那份汇总每个数都无意义）。")
    else:
        guard.append("  未检出空转组 ⇒ 所有组合的扫描维度均有效。")
    guard_txt = "\n".join(guard) + "\n"
    (EVID / "07_稳定性_空转守卫.txt").write_text(guard_txt, encoding="utf-8")

    if vac_combos and args.include_series:
        print("=" * 92)
        print("  ✘ 无法判定：扫描维度无效，拒绝产出汇总")
        print("=" * 92)
        print(f"  检出 {len(vac_combos)} 个组合的划分与种子无关（扫描空转）：")
        for c in vac_combos:
            print(f"    - {c}")
        print("  这些组的聚合数字（接受率 / 翻转率 / 平均增益标准差）在数学上无意义：")
        print("  划分根本没变，所谓『跨种子的离散度』衡量的是别的东西。")
        print("  处置：去掉 --include-series（守卫会自动剔除并单列），")
        print("        或改用对时序数据有效的扫描维度（滚动起点 / 扩展窗口）。")
        print("  ⚠️ 本次**未写** 07_稳定性汇总.txt 与 07_稳定性聚合.json。")
        print("     盘上若已存在这两个文件，它们是**上一轮**的运行结果，不要当作本次结论。")
        print("=" * 92)
        print(guard_txt)
        return 2

    # ---------------- 汇总 ----------------
    lines: list[str] = []
    lines.append("=" * 92)
    lines.append("  判据稳定性：只换数据划分种子（模型初始化种子固定），看结论会不会翻")
    lines.append("=" * 92)
    lines.append(f"  每组 {args.seeds} 个划分种子；共 {len(df)} 次运行；"
                 f"总耗时 {time.time()-t_all:.0f}s")
    lines.append(f"  扫描维度守卫：有效组 {len(eff_combos)} 个"
                 + (f"，**空转组 {len(vac_combos)} 个已剔除**" if vac_combos else "，未检出空转")
                 + "；容差 "
                 + f"--vacuity-tol={args.vacuity_tol:.1e}")
    lines.append("")
    lines.append(f"  【逐组：判据增益的跨划分离散度 + 决策翻转率】"
                 + (f"（已剔除 {len(vac_combos)} 个扫描空转组，见 07_稳定性_空转守卫.txt）"
                    if vac_combos else ""))
    lines.append(f"  {'组合':<28} {'判据':<9} {'增益均值':>10} {'增益标准差':>11} "
                 f"{'接受率':>7} {'翻转率':>7} {'策略损失':>10}")

    summary: list[dict] = []
    for combo in eff_combos:
        for mode in MODES:
            d = df[(df["combo"] == combo) & (df["mode"] == mode)].dropna(subset=["judge_gain"])
            if d.empty:
                continue
            rate = float(d["accepted"].mean())
            flip = min(rate, 1.0 - rate)
            loss = float(d["policy_loss"].mean())
            lines.append(f"  {combo:<28} {mode:<9} {d['judge_gain'].mean():>+9.4%} "
                         f"{d['judge_gain'].std(ddof=1):>10.4%} {rate:>6.0%} "
                         f"{flip:>6.0%} {loss:>10.6f}")
            summary.append({"combo": combo, "mode": mode,
                            "gain_mean": float(d["judge_gain"].mean()),
                            "gain_std": float(d["judge_gain"].std(ddof=1)),
                            "accept_rate": rate, "flip_rate": flip,
                            "policy_loss_mean": loss,
                            "delta_r2_mean": float(d["delta_r2"].mean()),
                            "delta_r2_std": float(d["delta_r2"].std(ddof=1)),
                            "n": int(len(d))})

    lines.append("")
    lines.append(f"  【按判据聚合（跨 {len(eff_combos)} 个**有效**组"
                 + (f"，已剔除 {len(vac_combos)} 个空转组）" if vac_combos else "）"))
    lines.append(f"  {'判据':<10} {'平均翻转率':>10} {'平均增益标准差':>14} "
                 f"{'累计策略损失':>12} {'平均单次耗时':>12}")
    agg: dict[str, dict] = {}
    for mode in MODES:
        d = dfe[dfe["mode"] == mode]
        sd = pd.DataFrame([x for x in summary if x["mode"] == mode])
        agg[mode] = {
            "mean_flip": float(sd["flip_rate"].mean()),
            "mean_gain_std": float(sd["gain_std"].mean()),
            "total_policy_loss": float(d["policy_loss"].mean() * d["combo"].nunique()),
            "mean_elapsed": float(d["elapsed"].mean()),
            "mean_accept_rate": float(sd["accept_rate"].mean()),
            "n_effective_combos": int(d["combo"].nunique()),
            "n_vacuous_combos_excluded": int(len(vac_combos)),
        }
        a = agg[mode]
        lines.append(f"  {mode:<10} {a['mean_flip']:>9.0%} {a['mean_gain_std']:>13.4%} "
                     f"{a['total_policy_loss']:>12.6f} {a['mean_elapsed']:>11.3f}s")

    base = agg["holdout"]
    lines.append("")
    lines.append("  【相对 holdout（升级前）的变化】")
    for mode in ("debiased", "kfold"):
        a = agg[mode]
        dflip = a["mean_flip"] - base["mean_flip"]
        dloss = a["total_policy_loss"] - base["total_policy_loss"]
        cost = a["mean_elapsed"] / base["mean_elapsed"] if base["mean_elapsed"] else float("nan")
        lines.append(f"  {mode:<10} 翻转率 {base['mean_flip']:.0%} → {a['mean_flip']:.0%} "
                     f"({dflip:+.0%})；累计策略损失 {base['total_policy_loss']:.6f} → "
                     f"{a['total_policy_loss']:.6f} ({dloss:+.6f})；"
                     f"单次耗时 {cost:.2f}×")
    lines.append("")
    if vac_combos:
        lines.append("  【扫描空转组（**已从上表与聚合中剔除**，列在此处以免信息丢失）】")
        lines.append(f"  {'组合':<28} {'判据':<9} {'增益（恒定）':>12} {'n_fit':>7} "
                     f"{'n_val':>7} {'接受率':>7}  说明")
        for c in vac_combos:
            for mode in MODES:
                d = df[(df["combo"] == c) & (df["mode"] == mode)].dropna(subset=["judge_gain"])
                if d.empty:
                    continue
                lines.append(f"  {c:<28} {mode:<9} {d['judge_gain'].mean():>+11.4%} "
                             f"{int(d['n_fit'].iloc[0]):>7} {int(d['n_val'].iloc[0]):>7} "
                             f"{float(d['accepted'].mean()):>6.0%}  "
                             f"翻转率恒 0%，但换种子划分不变 ⇒ **不构成稳定性证据**")
        lines.append("")
    lines.append("=" * 92)
    lines.append("  ⚠️ 翻转率 = min(接受率, 1−接受率)，即「与多数结论不一致」的概率。")
    lines.append("     判据增益标准差是**跨数据划分**的离散度 —— 它就是「判据能不能当结论用」的度量。")
    if vac_combos:
        lines.append(f"  ⚠️ 本次检出 {len(vac_combos)} 个扫描空转组并已剔除：不是判据稳定，")
        lines.append( "     而是「换划分种子根本不改变划分」。明细见 07_稳定性_空转守卫.txt。")
    lines.append("=" * 92)

    txt = "\n".join(lines)
    (EVID / "07_稳定性汇总.txt").write_text(txt + "\n", encoding="utf-8")
    (EVID / "07_稳定性聚合.json").write_text(
        json.dumps({"agg": agg, "per_combo": summary}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    print()
    print(txt)
    return 0


if __name__ == "__main__":
    sys.exit(main())
