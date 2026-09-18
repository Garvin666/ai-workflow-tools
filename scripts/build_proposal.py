# -*- coding: utf-8 -*-
# [自研工具] build_proposal.py
# 用途：把拟定补丁施加到 `model_base` 的一份**拷贝**上（不动本体），产出 unified diff 与改动前后 sha256 清单；每个替换点必须**命中且仅命中一次**，否则直接报错退出
# 适用场景：「先原型、验证通过再落底座」的提案式改动；不适用于要直接改底座本体的场景
# 作者：数模工作区自研（底座泛化优化-2026-09-17，R3）
# 仓库：https://github.com/Garvin666/ai-workflow-tools/blob/main/scripts/build_proposal.py
"""生成「落底座改动提案」：把补丁施加到 model_base 的一份**拷贝**上，产出可复核的 diff。

自研脚本（本任务自行编写）：build_proposal.py
用途：本轮口径是「先原型、验证通过再落底座」，因此**不改 model_base 本体**。
      本脚本把拟定的改动施加到 `proposal/patched_base/`（model_base 的完整拷贝），
      并产出 unified diff + 改动前后 sha256 清单，供后续落底座时直接使用。

设计要点（都是为了"补丁本身可被审计"）：
  1. 每个补丁的**替换点必须命中且仅命中一次** —— 命中 0 次或 >1 次都直接报错退出。
     否则文本替换会在作者不知情的情况下"改了别处"或"什么都没改"，而脚本仍报成功。
  2. diff 由 `difflib` 从**实际文件内容**生成，不是手写的 —— 保证 diff 与文件同源。
  3. 拷贝时清掉只读位（底座是 88/88 只读的，拷贝会继承该属性）。
  4. 输出 before/after 的 sha256，任何人可复算校验。

注意：本脚本只读 `model_base/`，从不写它；全部产物落在本提案目录内。

用法::

    python build_proposal.py            # 重建 patched_base 并输出 diff
    python build_proposal.py --check    # 只校验：现有 patched_base 是否与补丁一致
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
COMP_DIR = _HERE.parent                       # competitions/2026-底座泛化优化
WORKSPACE = COMP_DIR.parent.parent
BASE_DIR = WORKSPACE / "model_base"
PATCHED = _HERE / "patched_base"
DIFFS = _HERE / "diffs"
# 回收站：本环境 rmtree/os.remove 有批量删除守卫，一律 move 到这里而不是删除。
TRASH_DIR = Path(os.environ.get("TEMP", ".")) / "mm_proposal_trash"
TRASH_DIR.mkdir(parents=True, exist_ok=True)

sys.dont_write_bytecode = True

# ===========================================================================
# 补丁定义：每项 = (说明, 相对路径, 原文, 新文)
# 原文必须是**逐字符精确**的，且在同一文件里唯一出现。
# ===========================================================================
PATCHES: list[dict] = [
    {
        "id": "P1",
        "title": "修复 LogisticRegressionModel.summary() 在二分类下的 IndexError",
        "file": "mmbase/models/classification.py",
        "why": (
            "sklearn 在二分类时 coef_ 的 shape 是 (1, n_features)（ndim==2、行数只有 1），"
            "那一行对应 classes_[1]（正类），classes_[0] 的系数是它的相反数。"
            "原实现 `row = coefs[i] if coefs.ndim > 1 else coefs` 却按 classes_ 的个数循环 → "
            "走到 i==1 时 coefs[1] 越界（shape[0]==1）。实测只有 logistic 在二分类目标上触发。"
        ),
        "old": '''        classes = list(getattr(self.model_, "classes_", range(coefs.shape[0])))
        for i, cls in enumerate(classes):
            row = coefs[i] if coefs.ndim > 1 else coefs
            top = sorted(zip(names, np.asarray(row).ravel()), key=lambda kv: -abs(kv[1]))[:8]
            lines.append(f"  类别 {cls}: " +
                         ", ".join(f"{n}={v:+.3f}" for n, v in top))
        return "\\n".join(lines)''',
        "new": '''        # ⚠️ sklearn 的 coef_ 形状不对称，必须分情况展开，不能按 classes_ 数直接索引：
        #   · 多分类：coef_ 有 n_classes 行，第 i 行对应 classes_[i]；
        #   · 二分类：coef_ 只有 **1 行**（ndim==2 但 shape[0]==1），它对应 classes_[1]（正类），
        #            classes_[0] 的系数是它的**相反数**。
        #   （原实现写成 `coefs[i] if coefs.ndim > 1 else coefs`，二分类时 i==1 → 索引越界。）
        #   ⚠️ 这里**不能**写 `getattr(..., None) or []` —— classes_ 是 ndarray，
        #      `arr or []` 会触发 "truth value of an array is ambiguous" 的 ValueError。
        _cls_attr = getattr(self.model_, "classes_", None)
        classes = list(_cls_attr) if _cls_attr is not None else []
        if coefs.ndim == 2 and coefs.shape[0] == 1 and len(classes) == 2:
            rows = [(-coefs[0], classes[0]), (coefs[0], classes[1])]
        elif coefs.ndim == 1:
            rows = [(coefs, classes[0] if classes else "唯一类别")]
        else:
            labels = classes or [f"类别{i}" for i in range(coefs.shape[0])]
            rows = list(zip(coefs, labels))
        for row, cls in rows:
            top = sorted(zip(names, np.asarray(row).ravel()), key=lambda kv: -abs(kv[1]))[:8]
            lines.append(f"  类别 {cls}: " +
                         ", ".join(f"{n}={v:+.3f}" for n, v in top))
        return "\\n".join(lines)''',
    },
    {
        "id": "P2",
        "title": "评估路径上的 model.summary() 不得让整条链路失败（且不得静默吞错）",
        "file": "mmbase/evaluation/validator.py",
        "why": (
            "summary() 只产出**文本展示**，不参与任何指标计算。它一旦抛错，"
            "原实现会把已经算好的 train/test 全部指标一起丢掉 —— 一个纯展示故障"
            "升级成了评估失败。修复要同时满足两点：① 指标不受影响；② 错误必须**可见**"
            "（记入 summary_error 字段），不能静默吞掉。"
        ),
        "old": '''    out["summary_text"] = str(model.summary()) if hasattr(model, "summary") else ""
    return out''',
        "new": '''    # summary 是**纯展示**产物，不应让指标计算成果陪葬；但也不能静默吞错 ——
    # 失败信息记进 out["summary_error"]，调用方可据此判断"这次少了一份诊断"。
    out["summary_text"], out["summary_error"] = "", None
    if hasattr(model, "summary"):
        try:
            out["summary_text"] = str(model.summary())
        except Exception as e:                                  # noqa: BLE001
            out["summary_error"] = f"{type(e).__name__}: {e}"
            out["summary_text"] = (
                f"[summary 不可用] {type(model).__name__}.summary() 抛出 "
                f"{type(e).__name__}: {e}；本次评估各项指标不受影响。")
    return out''',
    },
    {
        "id": "P3",
        "title": "plot_roc_curve 支持非数值（中文/字符串）二分类标签",
        "file": "mmbase/viz/plots.py",
        "why": (
            "sklearn 的 roc_curve 在 y_true 不取 {0,1} 时**强制**要求 pos_label；"
            "比赛数据里二分类标签常是中文（实测基准集里 invoice_out / invoice_in 的 "
            "`发票状态` 就是字符串），因此底座的 ROC 诊断图会在主流程步骤 7 直接崩。"
            "注意 plot_confusion_matrix 与 classification_report_text 对字符串标签是**正常**的 —— "
            "本补丁只针对 ROC 这一个入口，不做无依据的扩大化改写。"
        ),
        "old": '''    classes = np.unique(y_true)
    if y_proba.ndim == 1 or y_proba.shape[1] == 2:
        score = y_proba if y_proba.ndim == 1 else y_proba[:, 1]
        fpr, tpr, _ = roc_curve(y_true, score)
        ax.plot(fpr, tpr, linewidth=2, color=pal[0],
                label=f"AUC = {auc(fpr, tpr):.4f}")''',
        "new": '''    classes = np.unique(y_true)
    if y_proba.ndim == 1 or y_proba.shape[1] == 2:
        score = y_proba if y_proba.ndim == 1 else y_proba[:, 1]
        # ⚠️ sklearn 的 roc_curve 在 y_true 不取 {0,1} 时**强制**要求显式 pos_label
        #    —— 中文/字符串标签（如 "有效/作废"、"是/否"）就会在这里报
        #    "pos_label is not specified"。正类取 classes[1]：与 predict_proba 的第 1 列
        #    同源于 model.classes_（sklearn 两者都用 np.unique 排序，顺序一致）。
        _kw = {}
        if len(classes) == 2 and not np.array_equal(np.asarray(classes), np.array([0, 1])):
            _kw["pos_label"] = classes[1]
        fpr, tpr, _ = roc_curve(y_true, score, **_kw)
        _pos = f"（正类={_kw['pos_label']}）" if "pos_label" in _kw else ""
        ax.plot(fpr, tpr, linewidth=2, color=pal[0],
                label=f"AUC = {auc(fpr, tpr):.4f}{_pos}")''',
    },
    {
        "id": "P4a",
        "title": "主流程打印模型摘要的调用不得让 run() 崩掉（且不得静默吞错）",
        "file": "mmbase/pipeline.py",
        "why": (
            "`log.info(f\"\\n{model.summary()}\")` 是**无条件**调用。summary 只是文本展示，"
            "但它在 P1 类缺陷下会直接把整个 run() 掀翻在步骤 5 —— 这正是实测 C3 用例的失败点。"
            "与 P2 同理：不陪葬、也不静默，失败要写进日志里看得见。"
        ),
        "old": '''    eval_res = evaluate_on_split(model, X_fit, y_fit, X_test, y_test, task=task)
    log.info(f"\\n{model.summary()}")''',
        "new": '''    eval_res = evaluate_on_split(model, X_fit, y_fit, X_test, y_test, task=task)
    # ⚠️ summary() 是纯展示，但它一旦抛错会把整个 run() 掀翻在这里。
    #    兜住并在日志里**说清楚**，而不是静默跳过（静默跳过会让人以为一切正常）。
    _summary_err = None
    try:
        log.info(f"\\n{model.summary()}")
    except Exception as e:                                      # noqa: BLE001
        _summary_err = f"{type(e).__name__}: {e}"
        log.warning(f"模型摘要打印失败（不影响建模与评估）: {_summary_err}")''',
    },
    {
        "id": "P4b",
        "title": "把「模型初筛」这件事写进结果（选型留痕）",
        "file": "mmbase/pipeline.py",
        "why": (
            "现状只留下 comparison_df，看不出「冠军的优势是否显著」。"
            "这在比赛里是实打实的风险：若冠军与次优差距不足 1 个标准误，"
            "那这个「最优」就是噪声，换更简单的模型往往更稳。"
            "本补丁**不改选择规则**，只把事实（谁在 1 个标准误内）说出来，"
            "使后续判断有依据；是否改用 1-SE 规则由实测偏差数据决定。"
        ),
        "old": '''            plot_model_comparison(comparison_df, **fig_kw)
            result["figure_paths"].append(str(dirs["figures"] / "06_model_comparison.png"))
            result["comparison"] = comparison_df''',
        "new": '''            plot_model_comparison(comparison_df, **fig_kw)
            result["figure_paths"].append(str(dirs["figures"] / "06_model_comparison.png"))
            result["comparison"] = comparison_df

            # ★ 选型留痕：记录"选了谁、依据什么、差距多大"。
            #   ⚠️ 这里**不改**选择规则（是否改用 1-SE 由实测偏差数据决定），
            #   改的是"把选型这件事说出来"，使下游能对 cv_mean 标注正确口径。
            _prim = {"regression": "r2", "classification": "accuracy"}.get(task, "r2")
            _top = comparison_df.iloc[0]
            _std = float(_top.get(f"{_prim}_std", 0.0) or 0.0)
            _se = _std / max(1.0, float(_top.get("n_splits", 1)) ** 0.5) \\
                if _std == _std else 0.0
            _best_val = float(_top[_prim])
            result["selection"] = {
                "rule": "max_cv",
                "primary_metric": _prim,
                "candidates": list(candidates),
                "best": str(_top["model"]),
                "best_value": _best_val,
                "best_std": _std,
                "best_se": _se,
                # 落在 1 个标准误内的候选（含冠军自身）：多于一个即说明冠军优势不显著
                "within_1se": [str(r["model"]) for _, r in comparison_df.iterrows()
                               if float(r[_prim]) >= _best_val - _se],
                "cv_used_for_selection": True,
            }
            if len(result["selection"]["within_1se"]) > 1:
                log.info(
                    f"提示：{result['selection']['within_1se']} 与冠军 '{_top['model']}' 的差距"
                    f"都在 1 个标准误（{_se:.4f}）内 —— 冠军优势**不显著**，"
                    f"报告结论时不宜表述为「该模型更优」。")''',
    },
    {
        "id": "P4c",
        "title": "结果文件里给 cv_mean 标注口径（它是选型所用口径，不是无偏泛化估计）",
        "file": "mmbase/pipeline.py",
        "why": (
            "结果文件同时写了 `test_metrics`（留出集真实表现）与 `cv_mean`"
            "（在训练数据上算的 K 折均值）。开了 auto_select 时 cv_mean 已被用于挑模型，"
            "**同一个数既当运动员又当裁判**。现状没有任何标注，写论文时极易误用，"
            "把乐观的选型分数当成泛化能力报出去。"
        ),
        "old": '''        "cv_mean": result["cv"]["mean"],
        "cv_std": result["cv"]["std"],
        "cv_folds": result["cv"]["n_splits"],
        "feature_names": list(X_train.columns),
    }''',
        "new": '''        "cv_mean": result["cv"]["mean"],
        "cv_std": result["cv"]["std"],
        "cv_folds": result["cv"]["n_splits"],
        "feature_names": list(X_train.columns),
        # ★ 口径标注：cv_mean 与 test_metrics 不是同一性质的两个数，必须说清
        "cv_note": ("cv_mean 是**模型初筛所用口径**（训练数据上的 K 折均值）。"
                    "当 model.auto_select=true 时它已被用于挑选模型，"
                    "因此**不是**无偏的泛化估计 —— 报出泛化能力请以 test_metrics 为准。"),
        "cv_is_selection_metric": bool(result.get("selection")),
        "selection": result.get("selection"),
    }''',
    },
]


def sha256_of(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def copy_clear_ro(src: Path, dst: Path) -> int:
    """拷贝目录并清掉只读位（底座是只读锁定的，拷贝会继承该属性）。"""
    if dst.exists():
        # ⚠️ 本环境的 rmtree 有批量删除守卫会打断脚本 → 用 move 挪走。
        #    目标名必须**唯一**：固定名在重复运行时必然撞名（实测第二次运行即失败）。
        shutil.move(str(dst), str(TRASH_DIR / f"{dst.name}_{int(time.time()*1000)}"))
    shutil.copytree(src, dst)
    n = 0
    for root, dirs, files in os.walk(dst):
        for f in files:
            p = Path(root) / f
            try:
                os.chmod(p, 0o644)
                n += 1
            except OSError:
                pass
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="只校验现有 patched_base 与补丁是否一致，不重建")
    args = ap.parse_args()

    print("=" * 96)
    print("落底座改动提案 —— 生成器")
    print("=" * 96)
    print(f"底座(只读)  : {BASE_DIR}")
    print(f"提案(副本)  : {PATCHED}")
    print(f"补丁数      : {len(PATCHES)}")
    sys.stdout.flush()

    if not args.check:
        n = copy_clear_ro(BASE_DIR, PATCHED)
        print(f"\n[1/4] 已拷贝底座 -> patched_base（清除只读位 {n} 个文件）")
        sys.stdout.flush()
    else:
        print("\n[1/4] --check 模式：跳过拷贝")

    manifests: list[dict] = []
    DIFFS.mkdir(parents=True, exist_ok=True)

    print("\n[2/4] 施加补丁（每个替换点必须命中且仅命中一次）")
    for pt in PATCHES:
        f = PATCHED / pt["file"]
        base_f = BASE_DIR / pt["file"]
        # ⚠️ 必须**按字节读、按字节写**。Path.read_text/write_text 走文本模式：
        #    读入时 universal-newline 把 CRLF 抹成 LF，写出时又把 LF 翻成 os.linesep(CRLF)。
        #    后果是**一次 3 行的修改会静默改掉整份文件每一行的字节**，而 diff 用文本模式
        #    比对时视差被抹平、看起来只有那 3 行 —— 于是 manifest 里**字节级** sha256
        #    与文档里**文本级** diff 声称的"改了什么"根本不是同一个量，读者无法对账。
        #    实测后果：底座原文是 LF，落补丁后 4 个文件整体变成 CRLF（每一行都变）。
        raw = f.read_bytes()
        text = raw.decode("utf-8")
        # 作用对象：--check 时比对已改后的文件；否则从拷贝（=底座原文）出发
        before_sha = sha256_of(base_f)

        if args.check:
            hit_old = text.count(pt["old"])
            hit_new = text.count(pt["new"])
            ok = (hit_old == 0 and hit_new == 1)
            print(f"  {pt['id']} {pt['file']}: 原文命中 {hit_old} 次 / 新文命中 {hit_new} 次 "
                  f"→ {'已施加 ✔' if ok else '不一致 ✘'}")
            if not ok:
                return 2
            manifests.append({"id": pt["id"], "title": pt["title"], "file": pt["file"],
                              "why": pt["why"], "base_sha256": before_sha,
                              "patched_sha256": sha256_of(f), "applied": True})
            continue

        c = text.count(pt["old"])
        if c != 1:
            print(f"  ✘ {pt['id']} {pt['file']}: 原文命中 {c} 次（要求恰好 1 次）—— 已中止")
            print(f"     这意味着补丁锚点失效：要么底座已变，要么锚点写错。"
                  f"绝不继续，否则会静默改错位置或什么都没改。")
            return 1
        f.write_bytes(text.replace(pt["old"], pt["new"], 1).encode("utf-8"))
        print(f"  ✔ {pt['id']} {pt['file']}: 命中 1 次，已替换")
        manifests.append({"id": pt["id"], "title": pt["title"], "file": pt["file"],
                          "why": pt["why"], "base_sha256": before_sha,
                          "patched_sha256": sha256_of(f), "applied": True})
        sys.stdout.flush()

    diff_index: list[dict] = []
    if args.check:
        print("\n[3/4] 跳过 diff 生成（--check）")
    else:
        print("\n[3/4] 生成 unified diff（**按文件**一份，由文件内容比对生成，非手写）")
        # ⚠️ 必须按文件分组：同一文件上的多个补丁（如 P4a/P4b/P4c 都在 pipeline.py）
        #    若各自出一份 diff，三份内容会完全一样（都是从底座到补丁后的全量 diff），
        #    文件名却不同 —— 看的人会以为改了三个地方。按文件出一份才是真实情况。
        by_file: dict[str, list[dict]] = {}
        for pt in PATCHES:
            by_file.setdefault(pt["file"], []).append(pt)
        expected = {f"{Path(rel).stem}.diff" for rel in by_file}
        # ⚠️ 先清陈旧 diff：上一版生成器按「补丁」出文件（p1_/p4a_…），改了策略后旧文件
        #    留在盘上 —— 它们与新文件**逐字节相同**却不同名，会让读者以为有 10 处改动。
        #    必须显式清理，且用 move 到回收站而不是删除（本环境有批量删除守卫）。
        for stale in sorted(DIFFS.glob("*.diff")):
            if stale.name in expected:
                continue
            trash = TRASH_DIR / f"{stale.name}_{int(time.time() * 1000)}"
            shutil.move(str(stale), str(trash))
            print(f"  [清陈旧] {stale.name} -> {trash}")
        for rel, pts in by_file.items():
            b = (BASE_DIR / rel).read_text(encoding="utf-8").splitlines(keepends=True)
            a = (PATCHED / rel).read_text(encoding="utf-8").splitlines(keepends=True)
            ids = "+".join(p["id"] for p in pts)
            stem = Path(rel).stem
            d = difflib.unified_diff(b, a, fromfile=f"a/{rel}", tofile=f"b/{rel}", n=3)
            out = DIFFS / f"{stem}.diff"
            # 同样按字节写：diff 文件自身也必须是 LF，否则它的 sha256 会随平台漂移
            out.write_bytes("".join(d).encode("utf-8"))
            nlines = len(out.read_bytes().decode("utf-8").splitlines())
            diff_index.append({"diff": out.name, "file": rel, "patch_ids": ids,
                               "sha256": sha256_of(out), "n_lines": nlines})
            print(f"  {rel}  [{ids}] -> {out.name}  ({nlines} 行)")
        # 自证：盘上的 diff 集合必须与索引完全一致（防"写了却漏登记"或"残留未清"）
        on_disk = {p.name for p in DIFFS.glob("*.diff")}
        assert on_disk == expected, f"diff 目录与索引不一致: 盘上={sorted(on_disk)} 期望={sorted(expected)}"

    man = {
        "generated_by": "build_proposal.py",
        "base_dir": str(BASE_DIR),
        "patched_dir": str(PATCHED),
        "note": "本目录只产生**提案**（生成时未改动 model_base）；是否落底座由后续流程决定，"
                "落与未落都以 mmguard 基线指纹为准、另行留痕。",
        # ⚠️ 显式声明哈希语义。缺了这段，读者极可能把 patched_sha256 误读成
        #    「把**只有这个**补丁的改动施加到**底座原文**」—— 那是**另一个量**，
        #    而且会得出"6 个补丁全部对不上、哈希是编的"这种**错误结论**（实测踩过）。
        "sha_semantics": {
            "base_sha256": "该文件在**底座原文**中的 sha256（不含任何补丁；"
                           "同一文件上的多个补丁共享同一个值）",
            "patched_sha256": "**累积态**：按本清单顺序把同一文件的补丁逐个施加到**同一份副本**后，"
                              "截至本补丁的状态的 sha256。同文件 N 个补丁的该值构成一条链 "
                              "base→h1→…→hN；只有链尾等于盘上 patched_base 成品。",
            "diff": "**文件级**一份，由 base 与 patched_base 两态现场比对生成（非手写）；"
                    "同一文件的多个补丁共用这一份，不按补丁重复出。",
            "line_endings": "全程按**字节**读写以保行尾：底座源码约定为 LF。"
                            "若用文本模式读写，一次小改动会静默把整份文件翻成 CRLF，"
                            "字节级哈希随之全变，而文本级 diff 看不出来。",
        },
        "patches": manifests,
        "diffs": diff_index,
    }
    ( _HERE / "proposal_manifest.json").write_text(
        json.dumps(man, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n[4/4] 已写出 proposal_manifest.json")

    # ---- 自证：底座本体必须仍未被改动 ----
    print("\n[自证] 底座本体 sha256 与补丁前一致？")
    all_ok = True
    for pt in PATCHES:
        now = sha256_of(BASE_DIR / pt["file"])
        exp = next(m for m in manifests if m["id"] == pt["id"])["base_sha256"]
        ok = now == exp
        all_ok &= ok
        print(f"  {pt['id']} {pt['file']}: {'未改动 ✔' if ok else '已被改动 ✘'}  {now[:16]}…")
    print(f"\n结论: {'底座本体完好，提案只作用于副本' if all_ok else '⚠️ 底座被改动，需立即排查'}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
