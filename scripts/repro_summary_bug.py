# -*- coding: utf-8 -*-
# [自研工具] repro_summary_bug.py
# 用途：二分类 `summary()` 越界缺陷的复现/修复验证：三个用例（单元层 / 评估链路 / 主流程）对**任意一份底座**跑同一套代码，只换被测底座即可对照「缺陷存在」与「补丁有效」
# 适用场景：缺陷复现 + 补丁有效性验证的「双跑」套路；不适用于没有补丁对照的单次验证
# 作者：数模工作区自研（底座泛化优化-2026-09-17，R3）
# 仓库：https://github.com/Garvin666/ai-workflow-tools/blob/main/scripts/repro_summary_bug.py
"""二分类 summary() 越界缺陷的复现 / 修复验证（对任意一份底座跑同一套用例）。

自研脚本（本任务自行编写）：repro_summary_bug.py
用途：给出**可执行证据**，证明三件事 ——
      ① 缺陷真实存在（对 `model_base` 本体跑 → 用例失败）；
      ② 缺陷的作用面（单元 / 评估链路 / 主流程三层，各失败到什么程度）；
      ③ 提案补丁确实修好了它（对 `proposal/patched_base` 跑 → 同样用例通过）。

三个用例（同一份代码、同一份数据，只换被测底座）：
  C1 单元层：`LogisticRegressionModel.summary()` 在二分类拟合后是否抛错
  C2 评估层：`evaluate_on_split(...)` 是否因 summary 抛错而丢掉全部指标
  C3 主流程：`mmbase.pipeline.run(cfg)` 端到端跑一个二分类任务是否崩在步骤 5

⚠️ 退出码约定（三态独立，不允许"没测到"混进"通过"）：
    0 = 全部用例通过
    1 = 有用例失败（查了，而且不符）
    2 = 无法判定（底座目录不存在 / 导入失败等，**不是通过也不是失败**）

用法::

    python repro_summary_bug.py --base D:/数模/model_base
    python repro_summary_bug.py --base ./patched_base
    python repro_summary_bug.py --compare          # 依次跑两份并给对照结论
"""
from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path

sys.dont_write_bytecode = True

_HERE = Path(__file__).resolve().parent
COMP_DIR = _HERE.parent
REPRO = _HERE / "repro"
DEFAULT_BASE = COMP_DIR.parent.parent / "model_base"


# --------------------------------------------------------------------------- 环境
def use_base(base: Path) -> None:
    """把指定底座放到 import 路径最前，并清掉可能已加载的 mmbase 模块。"""
    for m in [k for k in list(sys.modules) if k == "mmbase" or k.startswith("mmbase.")]:
        del sys.modules[m]
    p = str(base)
    while p in sys.path:
        sys.path.remove(p)
    sys.path.insert(0, p)
    sys.dont_write_bytecode = True


def make_dataset() -> Path:
    """极简二分类数据（两列特征即可触发缺陷；缺陷与特征数无关，只与类别数有关）。"""
    import numpy as np
    import pandas as pd

    dst = REPRO / "data" / "raw"
    dst.mkdir(parents=True, exist_ok=True)
    csv = dst / "demo_binary.csv"
    if not csv.is_file():
        rng = np.random.default_rng(42)
        n = 600
        x1 = rng.normal(0, 1, n)
        x2 = rng.normal(0, 1, n)
        # 让两类有可分的信号（保证模型能训出非退化结果）
        y = (x1 + 0.8 * x2 + rng.normal(0, 0.6, n) > 0).astype(int)
        pd.DataFrame({"x1": x1.round(4), "x2": x2.round(4),
                      "label": np.where(y == 1, "正常", "异常")}).to_csv(
            csv, index=False, encoding="utf-8")
    return csv


def make_cfg():
    """按比赛配置构造，但把 root 改道到本 repro 目录（不写任何东西到底座）。"""
    from mmbase import Config

    cfg = Config.load(str(COMP_DIR / "config" / "config.yaml"))
    cfg.root = REPRO
    csv = make_dataset()
    cfg.set("data.source.path", str(csv.relative_to(REPRO)).replace("\\", "/"))
    cfg.set("data.columns.target", "label")
    cfg.set("data.columns.time", None)
    cfg.set("data.columns.id_like", [])
    cfg.set("model.task", "classification")
    cfg.set("model.name", "logistic")
    cfg.set("model.auto_select", False)
    cfg.set("model.save_model", False)
    cfg.set("project.verbose", False)
    cfg.set("project.save_intermediate", False)
    cfg.set("project.theme", "paper")
    cfg.set("visualization.save", False)
    cfg.set("visualization.show", False)
    cfg.set("evaluation.confusion_matrix", True)
    cfg.set("evaluation.classification_report", True)
    cfg.set("output.append_summary", False)
    return cfg


# --------------------------------------------------------------------------- 用例
def case1_summary() -> dict:
    """C1：直接调 summary()，并检查**内容语义**（不只是"没抛错"）。

    ⚠️ 只断言「不抛异常」是弱判据：一个"只打印一个类别就返回"的错误实现同样不抛异常。
    二分类的正确摘要必须同时满足：
      · 两个类别名都出现在文本里；
      · 两类的系数互为**相反数**（sklearn 的二分类约定）。
    """
    import numpy as np
    import pandas as pd

    from mmbase.data.preprocess import Preprocessor
    from mmbase.models import build_model

    cfg = make_cfg()
    df = pd.read_csv(REPRO / "data" / "raw" / "demo_binary.csv")
    prep = Preprocessor(cfg, apply_outlier=False)
    X = prep.fit_transform(df)
    y = prep.get_target(df)
    m = build_model("logistic")
    m.fit(X, y)

    uniq = [str(v) for v in pd.unique(pd.Series(np.asarray(y).ravel()))]
    coef = np.asarray(m.model_.coef_)
    try:
        s = m.summary()
    except Exception as e:                                       # noqa: BLE001
        return {"case": "C1_summary_unit", "passed": False,
                "detail": f"{type(e).__name__}: {e}",
                "where": f"{type(e).__module__}.{type(e).__name__}",
                "traceback_tail": traceback.format_exc().strip().splitlines()[-3:],
                "coef_shape": list(coef.shape), "n_classes": len(uniq)}

    # --- 语义检查 ---
    missing = [c for c in uniq if c not in s]
    checks = {"both_labels_present": not missing}
    # 从摘要里把"类别 X: a=.., b=.."解析回来，验证相反数关系
    sign_ok = None
    parsed: dict[str, dict[str, float]] = {}
    for cls in uniq:
        for line in s.splitlines():
            if line.strip().startswith(f"类别 {cls}:"):
                d = {}
                for tok in line.split(":", 1)[1].split(","):
                    if "=" in tok:
                        k, v = tok.strip().split("=", 1)
                        try:
                            d[k] = float(v)
                        except ValueError:
                            pass
                parsed[cls] = d
    if len(parsed) == 2 and coef.shape[0] == 1:
        a, b = uniq[0], uniq[1]
        common = set(parsed.get(a, {})) & set(parsed.get(b, {}))
        if common:
            sign_ok = all(abs(parsed[a][k] + parsed[b][k]) < 1e-9 for k in common)
            checks["opposite_coefficients"] = sign_ok
    passed = all(checks.values())
    return {"case": "C1_summary_unit", "passed": passed,
            "detail": (f"摘要 {len(s)} 字符；类别={uniq}；coef_.shape={list(coef.shape)}；"
                       f"检查={checks}"
                       + (f"；缺失类别={missing}" if missing else "")),
            "summary_head": s.splitlines()[-2:],
            "checks": checks}


def case2_evaluate() -> dict:
    """C2：走评估链路 evaluate_on_split —— 指标算完后是否陪葬。"""
    import pandas as pd
    from mmbase.data.preprocess import Preprocessor
    from mmbase.evaluation.validator import evaluate_on_split
    from mmbase.models import build_model

    cfg = make_cfg()
    df = pd.read_csv(REPRO / "data" / "raw" / "demo_binary.csv")
    prep = Preprocessor(cfg, apply_outlier=False)
    X = prep.fit_transform(df)
    y = prep.get_target(df)
    cut = int(len(X) * 0.8)
    m = build_model("logistic")
    try:
        r = evaluate_on_split(m, X.iloc[:cut], y.iloc[:cut], X.iloc[cut:], y.iloc[cut:],
                              task="classification", verbose=False)
        return {"case": "C2_evaluate_path", "passed": True,
                "detail": f"指标正常返回；accuracy={r.get('metrics', {}).get('accuracy')}；"
                          f"summary_error={r.get('summary_error')!r}"}
    except Exception as e:                                       # noqa: BLE001
        return {"case": "C2_evaluate_path", "passed": False,
                "detail": f"{type(e).__name__}: {e}",
                "traceback_tail": traceback.format_exc().strip().splitlines()[-4:]}


def case3_pipeline() -> dict:
    """C3：主流程 run() 端到端。"""
    from mmbase import pipeline

    cfg = make_cfg()
    try:
        res = pipeline.run(cfg, run_name="repro_binary", task="classification")
        return {"case": "C3_pipeline_e2e", "passed": True,
                "detail": f"主流程跑通；模型={res.get('model').name if res.get('model') else '?'}；"
                          f"test={res.get('metrics')}"}
    except Exception as e:                                       # noqa: BLE001
        tb = traceback.format_exc()
        # 定位崩在 pipeline 的哪一行，便于对照源码
        here = [l.strip() for l in tb.splitlines() if "pipeline.py" in l]
        return {"case": "C3_pipeline_e2e", "passed": False,
                "detail": f"{type(e).__name__}: {e}",
                "pipeline_frames": here,
                "traceback_tail": tb.strip().splitlines()[-4:]}


def case4_roc_labels() -> dict:
    """C4：ROC 诊断图对**非数值**二分类标签的行为。

    单点定位用：C3 是端到端，崩了只能说明"主流程挂了"；
    C4 直接打 `plot_roc_curve`，把责任精确落到这一个函数上。
    判据：字符串标签与 0/1 标签**都必须**能出图，且图上标出的 AUC 应一致
          （同一份概率、同一组样本，换标签表示不该改变 AUC）。
    """
    import numpy as np
    import pandas as pd

    from mmbase.data.preprocess import Preprocessor
    from mmbase.models import build_model
    from mmbase.viz.plots import plot_roc_curve

    cfg = make_cfg()
    raw = pd.read_csv(REPRO / "data" / "raw" / "demo_binary.csv")
    aucs, errs = {}, {}
    for tag, convert in (("string", None), ("int01", "01")):
        d = raw.copy()
        if convert == "01":
            d["label"] = (d["label"] == "正常").astype(int)
        prep = Preprocessor(cfg, apply_outlier=False)
        X = prep.fit_transform(d)
        y = np.asarray(prep.get_target(d)).ravel()
        m = build_model("logistic")
        m.fit(X, y)
        try:
            fig, ax = plot_roc_curve(y, m.predict_proba(X), save=False, show=False)
            leg = ax.get_legend()
            txt = [t.get_text() for t in leg.get_texts()] if leg else []
            hit = [t for t in txt if t.startswith("AUC")]
            aucs[tag] = hit[0] if hit else None
        except Exception as e:                                   # noqa: BLE001
            errs[tag] = f"{type(e).__name__}: {e}"

    def _num(s):
        """从图例文本里抠出 AUC 数值。

        ⚠️ 不能直接比较图例字符串：本补丁**有意**给非数值标签的图例加了
        "（正类=…）"后缀。按字符串比会把"后缀不同"误判成"AUC 不一致"——
        判据必须盯住被测量的量（AUC），而不是它的展示形式。
        """
        if not s:
            return None
        import re
        m = re.search(r"([0-9]*\.?[0-9]+)", s)
        return float(m.group(1)) if m else None

    n_str, n_int = _num(aucs.get("string")), _num(aucs.get("int01"))
    # ⚠️ 有意不做链式比较 `a == b is not None`：a 为 None 时会短路成 False，读代码者易误判判据。
    passed = bool(not errs and n_str is not None and n_str == n_int)
    return {"case": "C4_roc_non_numeric_labels", "passed": passed,
            "detail": (f"字符串标签 AUC={aucs.get('string')!r}（解析={n_str}）；"
                       f"0/1 标签 AUC={aucs.get('int01')!r}（解析={n_int}）；"
                       f"错误={errs or '无'}"),
            "auc_string_raw": aucs.get("string"), "auc_int_raw": aucs.get("int01"),
            "auc_string": n_str, "auc_int": n_int, "errors": errs}


def case5_autoselect_disclosure() -> dict:
    """C5：开启 auto_select 时，选型留痕与 cv_mean 口径标注是否真的落到了结果里。

    ⚠️ 为什么必须有这个用例：C3 用的是 `auto_select=False`，
    因此 P4b（选型留痕）与 P4c（口径标注）的代码**一行都没被执行**。
    "补丁替换点命中"只说明文本改上了，不说明改完能跑 —— 这正是"修好了"与
    "看起来修好了"的分界，必须用一个真正走到该分支的用例来区分。
    """
    from mmbase import pipeline

    cfg = make_cfg()
    cfg.set("model.auto_select", True)
    # ⚠️ 候选必须与任务同类型。底座对"任务/候选池不匹配"**不做校验**：
    #    给 classification 传回归模型名不会立刻报"配置错误"，而是等到 CV 里
    #    才抛 `could not convert string to float: '异常'` 这种不知所云的错。
    #    这是真实的配置陷阱（默认 config 的 candidates 就是纯回归模型），
    #    已作为"低severity 可用性发现"写入报告，不在本用例里当缺陷断言。
    cfg.set("model.candidates", ["logistic", "naive_bayes", "random_forest_clf"])
    try:
        res = pipeline.run(cfg, run_name="repro_autoselect", task="classification")
    except Exception as e:                                       # noqa: BLE001
        tb = traceback.format_exc()
        return {"case": "C5_autoselect_disclosure", "passed": False,
                "detail": f"{type(e).__name__}: {e}",
                "pipeline_frames": [l.strip() for l in tb.splitlines() if "pipeline.py" in l],
                "traceback_tail": tb.strip().splitlines()[-4:]}

    sel = res.get("selection")
    if sel is None:
        return {"case": "C5_autoselect_disclosure", "passed": False,
                "detail": "pipeline 跑通了，但结果里没有 selection 字段 —— "
                          "选型留痕（P4b）没有生效"}

    need = {"rule", "primary_metric", "candidates", "best", "best_value",
            "best_se", "within_1se", "cv_used_for_selection"}
    miss = sorted(need - set(sel))
    # within_1se 必须是列表且**包含冠军自身**（口径：差距在 1 SE 内的所有候选）
    bad = []
    if not isinstance(sel.get("within_1se"), list):
        bad.append("within_1se 不是 list")
    elif sel["best"] not in sel["within_1se"]:
        bad.append("within_1se 未包含冠军自身")
    ok = not miss and not bad
    return {"case": "C5_autoselect_disclosure", "passed": ok,
            "detail": (f"selection 字段齐全={not miss}"
                       + (f"（缺 {miss}）" if miss else "")
                       + f"；rule={sel['rule']} 冠军={sel['best']}"
                       f"({sel['best_value']:.4f}±{sel['best_se']:.4f})"
                       f" within_1se={sel['within_1se']}"
                       + (f"；问题={bad}" if bad else "")),
            "selection": sel}


CASES = [case1_summary, case2_evaluate, case3_pipeline, case4_roc_labels,
         case5_autoselect_disclosure]


def run_all(base: Path) -> dict:
    use_base(base)
    results = []
    for fn in CASES:
        try:
            results.append(fn())
        except Exception as e:                                   # noqa: BLE001
            results.append({"case": fn.__name__, "passed": False,
                            "detail": f"用例自身异常（非被测对象）: {type(e).__name__}: {e}",
                            "traceback_tail": traceback.format_exc().strip().splitlines()[-3:]})
        sys.stdout.flush()
    n_pass = sum(1 for r in results if r["passed"])
    return {"base": str(base), "n_pass": n_pass, "n_total": len(results), "results": results}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="")
    ap.add_argument("--compare", action="store_true",
                    help="依次跑 底座本体 与 patched_base 并给对照结论")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    REPRO.mkdir(parents=True, exist_ok=True)

    if args.compare:
        targets = [("base", DEFAULT_BASE), ("patched", _HERE / "patched_base")]
    else:
        targets = [("base", Path(args.base) if args.base else DEFAULT_BASE)]

    for name, p in targets:
        if not (p / "mmbase" / "__init__.py").is_file():
            print(f"[无法判定] 底座目录不存在或不是底座: {p}")
            print("  → 退出码 2（**不是通过，也不是失败**）")
            return 2

    all_payload, codes = {}, []
    for name, p in targets:
        print("=" * 96)
        print(f"被测底座 [{name}]: {p}")
        print("=" * 96)
        payload = run_all(p)
        all_payload[name] = payload
        for r in payload["results"]:
            mark = "PASS ✔" if r["passed"] else "FAIL ✘"
            print(f"  {mark}  {r['case']:<18} {r['detail']}")
            if not r["passed"] and r.get("pipeline_frames"):
                for fr in r["pipeline_frames"]:
                    print(f"        pipeline 帧: {fr}")
        print(f"  → {payload['n_pass']}/{payload['n_total']} 通过\n")
        sys.stdout.flush()
        codes.append(0 if payload["n_pass"] == payload["n_total"] else 1)

    if args.compare:
        print("=" * 96)
        print("对照结论")
        print("=" * 96)
        b, q = all_payload["base"], all_payload["patched"]
        per = {r["case"]: r["passed"] for r in b["results"]}
        pat = {r["case"]: r["passed"] for r in q["results"]}
        for c in [r["case"] for r in b["results"]]:
            print(f"  {c:<18} 底座={'PASS' if per[c] else 'FAIL'}  "
                  f"补丁后={'PASS' if pat[c] else 'FAIL'}")
        fixed = [c for c in per if not per[c] and pat[c]]
        regressed = [c for c in per if per[c] and not pat[c]]
        print(f"\n  由补丁修复的用例: {fixed or '（无）'}")
        print(f"  由补丁引入的回归: {regressed or '（无）'}")
        if not fixed and not regressed:
            print("  ⚠️ 补丁未改变任何用例结果 —— 此时不能声称'补丁有效'，"
                  "要回头检查用例是否真的命中缺陷路径。")

    if args.out:
        (COMP_DIR / args.out).write_text(
            json.dumps(all_payload, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n证据已写出 -> {COMP_DIR / args.out}")

    return max(codes) if codes else 2


if __name__ == "__main__":
    raise SystemExit(main())
