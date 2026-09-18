# -*- coding: utf-8 -*-
# [自研工具] kfold_benefit
# 用途：量化「准入判据升级」的收益与代价：三臂对照（误接受 / 策略损失 / 逐组差异）与受控成本 A/B
# 适用场景：任何把判定/评分/阈值逻辑换掉、且需要证明「换得值」的评估任务
# 不适用场景：判据本身没变、只调超参的任务；测试集与判定集不物理分离的流水线（对照会失真）
# 作者：数模工作区自研（K折验证增益-2026-09-17）
# 仓库：https://github.com/Garvin666/ai-workflow-tools/blob/main/scripts/kfold_benefit.py
"""K 折判据收益量化：三臂对照 + 受控成本 A/B。

子命令::

    python kfold_benefit.py compare   # 三臂对照：误接受 / 策略损失 / 逐组差异
    python kfold_benefit.py cost      # 受控成本 A/B：同进程内交替执行三模式取最小值

为什么成本要单独做受控 A/B
--------------------------
本机墙钟噪声很大（同代码矩阵总耗时能在 11.3~14.6s 之间跳）。拿三个独立进程的
"总耗时"相除，量到的主要是噪声而不是 K 折的开销。所以成本口径改为：
**同一个进程内交替执行三种模式、各取多次运行的最小值** —— 最小值对
"别的进程在抢 CPU"这类干扰最不敏感。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pandas as pd

ROOT = Path(r"D:\数模")
COMP = ROOT / "competitions" / "2026-底座适配层"
FIN = COMP / "finetune"
TASK = ROOT / "tasks" / "K折验证增益-2026-09-17"
EVID = TASK / "证据"

ARMS = {
    "holdout": COMP / "outputs_kfold" / "holdout" / "results" / "summary.csv",
    "debiased": COMP / "outputs_kfold" / "debiased" / "results" / "summary.csv",
    "kfold": COMP / "outputs_kfold" / "kfold" / "results" / "summary.csv",
}
OLD = COMP / "outputs" / "results" / "summary.csv"
ARM_LABEL = {"holdout": "单折·同集(升级前)", "debiased": "单折·分离(去偏)", "kfold": "K折(K=5)"}


def _load() -> dict[str, pd.DataFrame]:
    out = {}
    for k, p in ARMS.items():
        df = pd.read_csv(p, encoding="utf-8-sig")
        out[k] = df.set_index("run")
    return out


def compare() -> int:
    arms = _load()
    runs = sorted(arms["holdout"].index)
    lines: list[str] = []
    L = lines.append

    L("=" * 108)
    L("  K 折准入判据 · 三臂对照（唯一变量 = 接受/回退怎么决定）")
    L("=" * 108)
    L("")

    # ---- 逐组：判据增益 / 决策 / 测试集 ΔR² ----
    L("  【逐组明细】")
    hdr = f"  {'组合':<28}"
    for m in ARMS:
        hdr += f" {ARM_LABEL[m][:12]:>13}"
    L(hdr)
    L(f"  {'':<28}" + "".join(f" {'增益/决策/ΔR²':>13}" for _ in ARMS))
    detail = []
    for run in runs:
        row = f"  {run:<28}"
        rec = {"run": run}
        for m, df in arms.items():
            if run not in df.index:
                row += f" {'—':>13}"
                continue
            g = float(df.loc[run, "val_rel_gain"])
            a = bool(df.loc[run, "accepted"])
            d = float(df.loc[run, "delta_r2"])
            row += f" {g:>+6.2%}{'接' if a else '退':>1}{d:>+8.5f}"
            rec[m] = {"gain": g, "accepted": a, "delta_r2": d}
        L(row)
        detail.append(rec)

    # ---- 误接受 / 策略损失 ----
    L("")
    L("  【误接受与策略损失】")
    L("    误接受 = 该臂接受了适配层、而该臂下**测试集 ΔR² < 0**（真值可观测，不需要假设）")
    L("    策略损失 = Σ max(0, −ΔR²)，即被吃掉的 R² 总量")
    L("")
    L(f"  {'判据':<20} {'误接受':>8} {'策略损失':>12} {'接受率':>8} {'平均ΔR²':>10}")
    stats = {}
    for m, df in arms.items():
        bad = df[df["delta_r2"] < -1e-12]
        loss = float((-df["delta_r2"].clip(upper=0)).sum())
        stats[m] = {
            "n_false_accept": int(len(bad)),
            "false_accept_runs": sorted(bad.index.tolist()),
            "policy_loss": loss,
            "accept_rate": float(df["accepted"].mean()),
            "mean_delta_r2": float(df["delta_r2"].mean()),
            "sum_delta_r2": float(df["delta_r2"].sum()),
            "mean_elapsed": float(df["elapsed_sec"].mean()),
        }
        L(f"  {ARM_LABEL[m]:<20} {len(bad):>8} {loss:>12.6f} "
          f"{df['accepted'].mean():>7.0%} {df['delta_r2'].mean():>+10.5f}")
    L("")
    for m in ("debiased", "kfold"):
        b, a = stats["holdout"], stats[m]
        dl = a["policy_loss"] - b["policy_loss"]
        rel = (dl / b["policy_loss"]) if b["policy_loss"] else float("nan")
        L(f"  → {ARM_LABEL[m]}：误接受 {b['n_false_accept']} → {a['n_false_accept']} 次；"
          f"策略损失 {b['policy_loss']:.6f} → {a['policy_loss']:.6f}"
          f"（{rel:+.1%}）")

    # ---- 与旧树一致性（证明 holdout 臂 == 未升级之前）----
    L("")
    old = pd.read_csv(OLD, encoding="utf-8-sig").set_index("run")
    same = all(
        abs(float(old.loc[r, c]) - float(arms["holdout"].loc[r, c])) <= 5e-7
        for r in old.index for c in ("delta_r2", "val_rel_gain", "accepted")
    )
    L(f"  【基线一致性】holdout 臂与旧产物树 outputs/ 的 9 组 ΔR²/判据增益/决策"
      f"{'逐格一致 ✔' if same else '★ 不一致'} —— 所以 holdout 臂就是「未升级之前」")

    txt = "\n".join(lines)
    EVID.mkdir(parents=True, exist_ok=True)
    (EVID / "06_三臂对照.txt").write_text(txt + "\n", encoding="utf-8")
    (EVID / "06_三臂对照.json").write_text(
        json.dumps({"detail": detail, "stats": stats, "baseline_match": same},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    print(txt)
    return 0


def cost(rounds: int = 3) -> int:
    """受控成本 A/B：同进程内交替执行三模式，各取最小值。"""
    sys.path.insert(0, str(FIN))
    from run_frozen_adapter import run_one  # noqa: E402

    combos = [("demo", "linear", "lora", {"rank": 2}),
              ("nonlinear", "linear", "mlp",
               {"hidden": 16, "n_layers": 1, "use_base_feature": False}),
              ("market", "linear", "lora", {"rank": 2})]
    lines = ["=" * 96,
             "  受控成本 A/B：同进程内交替执行三模式，各取最小值",
             "=" * 96,
             "  为什么不用「三次独立进程的总耗时相除」：本机墙钟噪声大，",
             "  那样量到的主要是噪声。交替执行 + 取最小值才是可比口径。", ""]
    payload = {}
    for ds, base, ad, kw in combos:
        combo = f"{ds}_{base}_{ad}"
        best: dict[str, float] = {}
        for r in range(rounds):
            for mode in ("holdout", "debiased", "kfold"):
                t0 = time.perf_counter()
                run_one(ds, base, ad, judge=mode, folds=5, adapter_kw=kw,
                        split_seed=1000 + r, write_artifacts=False)
                dt = time.perf_counter() - t0
                best[mode] = min(best.get(mode, dt), dt)
        payload[combo] = best
        h = best["holdout"]
        lines.append(f"  {combo:<26} holdout {h:6.3f}s | debiased {best['debiased']:6.3f}s "
                     f"({best['debiased']/h:5.2f}×) | kfold {best['kfold']:6.3f}s "
                     f"({best['kfold']/h:5.2f}×)")
    mean_k = sum(v["kfold"] / v["holdout"] for v in payload.values()) / len(payload)
    mean_d = sum(v["debiased"] / v["holdout"] for v in payload.values()) / len(payload)
    lines += ["",
              f"  平均倍数：debiased {mean_d:.2f}× | kfold {mean_k:.2f}×",
              "  （kfold 的倍数 ≈ K 次底座拟合 + K 次适配层训练 + 1 次交付训练，"
              "故略低于 K+1）", "=" * 96]
    txt = "\n".join(lines)
    (EVID / "06_成本AB.txt").write_text(txt + "\n", encoding="utf-8")
    (EVID / "06_成本AB.json").write_text(
        json.dumps({"per_combo": payload, "mean_mult_debiased": mean_d,
                    "mean_mult_kfold": mean_k}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    print(txt)
    return 0


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "compare"
    if cmd == "compare":
        sys.exit(compare())
    if cmd == "cost":
        sys.exit(cost(int(sys.argv[2]) if len(sys.argv) > 2 else 3))
    print(f"未知子命令: {cmd}（可选 compare / cost）")
    sys.exit(2)
