#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# [自研工具] homework_model.py
# 用途：模式选用聚合模型——homework-judge 的 Realization A 可执行半边，把 W1–W5 维度分按确定性公式
#       聚合成 {mode, distribution, confidence, needs_homework}，并产出可粘进 plan.yaml 的片段。
# 适用场景：作业判定出口条件的 A 侧测量（agree 一致率）、检查项 18 结构对照、meta.作业判定 生成。
# 作者：ai-workflow 自研（模式选用模型-ai-workflow-v4.7.0-2026-09-23，2026-09-23）
# 仓库：https://github.com/Garvin666/ai-workflow-tools
"""homework_model.py —— 模式选用**聚合模型**（homework-judge 的 Realization A 可执行半边）

【定位：务必按此理解，勿误用】
  本脚本**不是第 5 个 judge**、**不是新的判据源**、**不接管路由**、**不做识别**。
  它是 `references/homework-judge.md` §5.1「聚合规则」的**唯一机器实现** —— 把
  「W1–W5 维度分（＋可选模糊度）」按**确定性公式**聚合成
  `{mode, distribution, confidence, dimensions, ambiguity, route_hint, needs_homework}`，
  与检查项 18（`checks_judges._check_homework_verdict`）的结构要求逐字段同构。

【为什么要它（这是本脚本存在的全部理由）】
  Realization A（脑内协议）此前**只定义到"给维度打分"**；而「维度分 → 三态分布 → mode → 档位」
  这一段是**黑箱**（藏在 Laya 内部，或每次现场即兴）。其后果是：
    · A 侧判定**不可复现** —— 同一组 W 值在不同会话可能给出不同分布；
    · 于是 **A/B 一致率无法测量** ⇒「B 升为主判据」的**出口条件结构上无法闭环**
      （契约 §7.3 登记为"未闭环"，本脚本即补那半块）。
  把这一段落成代码，等价于：**Realization A = 语义维度评估（仍需模型/人）+ 确定性聚合（本脚本）**。

【诚实边界（与契约 §10 同一条纪律）】
  · 锚点 `ANCHORS` 与温度 `TAU` 都是**先验、未经数据拟合** ⇒ 由此得到的 `distribution` /
    `confidence` **只可用于「对照、回归、抽样指路」，不得作为准确率或阈值依据**
    （与 Laya 侧"温度未拟合"完全同款）。
  · ⭐ **「锚例 8/8 命中」已过留一法复核，不是循环自证**（2026-09-23 实证）：
    锚点值与锚例维度分出自同一次设计，存在「锚点 ⟵ 样例 ⟵ 锚点」的循环嫌疑；实测
    **留一法 8/8**（剔除该条、用其余同类样例均值重算锚点，仍判对）、
    **全量均值锚点 8/8**（零手调）、**随机锚点均值 3.0/8**（对照显著低）
    ⇒ 8/8 反映的是样例在维度空间中**可分**，而非拟合到自身。
    但**锚例的维度分仍是推定值、不是独立标注**，故 8/8 **仍不构成准确率证据**。
  · 本脚本**不判"答案对不对"**，也**不判"这是不是一道题"**（那仍是启发式识别，契约 §10.2）。
  · `ANCHORS` / `TAU` 是**可拟合参数**：拿到标注样本后可直接替换（`--anchors-file` / `--tau`），
    **本轮不做拟合**（无样本），故一律按"未拟合"标注、不声称任何比率。
  · ⚠️ `agree` 的判定阈值**刻意不给默认值** —— 契约 §7.3 的出口阈值"未定值"，属用户决策项；
    不给 `--min-agree` 时本脚本**只报告不判定**。

【取值单一源】
  `mode` 三态、`W1–W5` 键名、`combine_mode_delta`、`thresholds`、`route_hint` **全部**从
  `scripts/judges.json` 的 `homework_judge` 读取，**本文件不复制其数值**（避免"同一物理量写两处"）。
  锚点与温度**不在手册与 json 中复制**，唯一实现即本文件的 `ANCHORS` / `TAU`。

用法：
  python scripts/homework_model.py aggregate --dims "W1=0.9,W2=0.85,W3=0.6,W4=0.2,W5=0.85"
  python scripts/homework_model.py aggregate --dims "..." --noul 0.5 --plan-block
  python scripts/homework_model.py agree --pairs <pairs.json> [--min-samples 50 --min-agree 0.90]
  python scripts/homework_model.py selftest
"""

import argparse
import ast
import json
import math
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TEMPLATES_PATH = HERE / "judges.json"
KIND = "homework_judge"

# ---------------------------------------------------------------------------
# 模型参数（**先验，未拟合** —— 唯一实现地，手册与 judges.json 均不复制其数值）
# ---------------------------------------------------------------------------
# 三态原型锚点：每个模式在 W1–W5 上的"理想位置"（各维 ∈ [0,1]）。
# 依据 = references/homework-judge.md §3.1 的维度定义（高分指向）：
#   · 作业题：题干结构强、要答案与过程、学习语境中上、受评不一定、结构收益高
#   · 讲解题：无题干结构、要"听懂"而非"答案"、学习语境强、结构收益中（可借 H2/H4）
#   · 非作业：无题干结构、要交付物、语境弱、结构收益低
ANCHORS = {
    "作业题": {"W1": 0.95, "W2": 0.95, "W3": 0.70, "W4": 0.30, "W5": 0.90},
    "讲解题": {"W1": 0.15, "W2": 0.35, "W3": 0.80, "W4": 0.15, "W5": 0.55},
    "非作业": {"W1": 0.20, "W2": 0.15, "W3": 0.25, "W4": 0.05, "W5": 0.15},
}

# softmax 温度：先验值，控制分布的锐度（越小越自信）。**未拟合。**
TAU = 0.10

# 口径提示：本文件的模式键必须与 judges.json 的 homework_judge.mode.criteria 键一致；
# selftest 会做交叉断言（不同 → FAIL），避免两处漂移。
_MODEL_TAG = "homework-model"


# ---------------------------------------------------------------------------
# 取值单一源
# ---------------------------------------------------------------------------
def _load_templates():
    with open(TEMPLATES_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def spec():
    """homework_judge 的模板块（mode/W1–W5/阈值/route_hint 的唯一取值源）。"""
    return _load_templates()[KIND]


def dim_keys():
    return tuple(spec()["dimensions"].keys())


def mode_keys():
    return tuple(spec()["mode"]["criteria"].keys())


# ---------------------------------------------------------------------------
# 聚合内核
# ---------------------------------------------------------------------------
def _softmax(scores, tau):
    """数值稳定化的 softmax（减 max 不改变结果，只防溢出）。"""
    m = max(scores.values())
    exps = {k: math.exp((v - m) / tau) for k, v in scores.items()}
    total = sum(exps.values()) or 1.0
    return {k: v / total for k, v in exps.items()}


def _combine(probs, delta):
    """top2 差 < delta → 'A+B'（与 scripts/laya_client.py 的 _combine 同规则，契约 §1）。"""
    items = sorted(probs.items(), key=lambda kv: -kv[1])
    if len(items) >= 2 and (items[0][1] - items[1][1]) < delta:
        return "%s+%s" % (items[0][0], items[1][0])
    return items[0][0]


def normalised_distance(dims, anchor, keys):
    """归一化欧氏距离（除以 √k，落在 [0,1]）—— 模型的"离原型的远近"。"""
    return math.sqrt(sum((dims[k] - anchor[k]) ** 2 for k in keys)) / math.sqrt(len(keys))


def aggregate(dims, ambiguity_noul=None, tau=TAU, anchors=None):
    """W1–W5（+ 可选 noul）→ homework-judge 契约字段（结构同检查项 18 的要求）。

    `dims`：{W1..W5}，取自本模型的 `dim_keys()`（单一取值源）。
    `ambiguity_noul`：Noul 的 P(true) ∈ [0,1]；None 表示调用侧未给（则 ambiguity=False）。
    """
    # 先验参数校验（v4.7.0 补）：tau ≤ 0 **必须显式报错**，不得静默——
    #   · tau = 0.0 → `_softmax` 除零（ZeroDivisionError，报错形状与语义无关）；
    #   · tau < 0   → softmax(−d/τ) 变成"距离越大越可能" ⇒ **语义完全反转**
    #     却**不报任何错**，返回一个看起来正常的结果（"取错形状不报错"族）。
    if isinstance(tau, bool) or not isinstance(tau, (int, float)) or not tau > 0.0:
        raise ValueError("tau=%r 非法（须为 > 0 的数）：tau=0 会除零，tau<0 会把"
                         "「最不像」判成「最像」（语义反转），二者都不允许静默通过" % (tau,))

    sp = spec()
    keys = dim_keys()
    missing = [k for k in keys if k not in dims]
    if missing:
        raise ValueError("缺维度 %s（须齐 %s）" % ("/".join(missing), "/".join(keys)))

    w = {}
    for k in keys:
        v = float(dims[k])
        if not 0.0 <= v <= 1.0:
            raise ValueError("维度 %s=%r 越界（须在 0–1）" % (k, dims[k]))
        w[k] = v

    modes = mode_keys()
    A = anchors or ANCHORS
    for m in modes:
        if m not in A:
            raise ValueError("锚点表缺模式 %r（须齐 %s）" % (m, "/".join(modes)))

    dist = {m: normalised_distance(w, A[m], keys) for m in modes}
    probs = _softmax({m: -dist[m] for m in modes}, tau)

    th = sp["thresholds"]
    lo, hi = th["ambiguity_band"]
    if ambiguity_noul is None:
        noul = None
        ambiguity = False
    else:
        noul = float(ambiguity_noul)
        if not 0.0 <= noul <= 1.0:
            raise ValueError("noul=%r 越界（须在 0–1）" % ambiguity_noul)
        ambiguity = bool(lo <= noul <= hi)

    # ---- distribution **先定稿**（round 到 4 位 + 重归一化到和恰为 1）----
    # ⭐ 顺序是**硬要求**（v4.7.0 补）：所有**派生字段**（mode / confidence / route_hint /
    #   needs_homework / closest）必须在 distribution 定稿**之后**取值。
    #   反例（原实现的隐患）：confidence 取自未 round 的 `probs[top1]`，而 distribution 经过
    #   round(4)+归一化 ⇒ 二者可差约 2e-4。这个差是**纯数值噪声、零语义**，却会让检查项 18
    #   的「confidence == max(distribution)」**误报**（"取错形状不报错"族的近亲）：
    #   真漂移与量化噪声在这一判据下长得一模一样，判据因此失去分辨力。
    #   修法是**让派生字段真的从派生源取值**，而不是"与派生源近似相等"。
    out_dist = {k: round(v, 4) for k, v in probs.items()}
    total = sum(out_dist.values()) or 1.0
    out_dist = {k: round(v / total, 4) for k, v in out_dist.items()}
    if abs(sum(out_dist.values()) - 1.0) > 1e-6:
        # 极少数舍入情形：把差额补到最大项，保证 checks 侧"和 = 1"恒成立
        k = max(out_dist, key=lambda x: out_dist[x])
        out_dist[k] = round(out_dist[k] + (1.0 - sum(out_dist.values())), 4)

    ordered = sorted(out_dist.items(), key=lambda kv: -kv[1])
    top1, top2 = ordered[0][0], ordered[1][0]
    conf = out_dist[top1]           # ← 派生自定稿后的分布，与 max() **恒等**
    if ambiguity:
        band = "clarify"
    elif conf >= th["auto_accept"]:
        band = "auto"
    elif conf >= th["mid"]:
        band = "mid"
    else:
        band = "clarify"

    out = {
        "mode": _combine(out_dist, sp["combine_mode_delta"]),
        "distribution": out_dist,
        "confidence": round(conf, 4),
        "dimensions": {k: w[k] for k in keys},
        "ambiguity": ambiguity,
        "route_hint": sp["route_hint"][top1],
        "needs_homework": top1 == "作业题",   # 派生字段：非独立问句（契约 §4）
        "_model": {
            "kind": _MODEL_TAG,
            "tau": tau,
            "anchors_fitted": False,          # ⚠️ 恒 False 直到真做拟合
            "band": band,
            "noul": noul,
            "noul_given": noul is not None,
            "closest": top1,
            "runner_up": top2,
            "distances": {m: round(v, 4) for m, v in dist.items()},
            "thresholds": {"auto_accept": th["auto_accept"], "mid": th["mid"],
                           "ambiguity_band": [lo, hi]},
        },
    }
    return out


# ---------------------------------------------------------------------------
# 装配：契约 dict → plan.yaml 的 meta.作业判定 YAML 片段
# ---------------------------------------------------------------------------
def to_yaml_block(verdict, indent=2):
    """产出可直接粘进 plan.yaml 的 `作业判定:` 片段。

    YAML 标量一律用 JSON 双引号形式（JSON 字符串是 YAML 双引号标量的子集）——
    这样含中文/括号/反斜杠的 `route_hint` 不需要手写转义（该坑在 v4.7.0 落 plan 时踩过一次）。
    """
    pad = " " * indent
    j = lambda o: json.dumps(o, ensure_ascii=False)
    out = {k: v for k, v in verdict.items() if not k.startswith("_")}
    lines = [pad + "作业判定:"]
    lines.append(pad + "  mode: " + j(out["mode"]))
    lines.append(pad + "  distribution: " + j(out["distribution"]))
    lines.append(pad + "  confidence: " + j(out["confidence"]))
    lines.append(pad + "  dimensions: " + j(out["dimensions"]))
    lines.append(pad + "  ambiguity: " + ("true" if out["ambiguity"] else "false"))
    lines.append(pad + "  route_hint: " + j(out["route_hint"]))
    lines.append(pad + "  needs_homework: " + ("true" if out["needs_homework"] else "false"))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# A/B 一致率（闭环契约 §7.3 的出口条件）
# ---------------------------------------------------------------------------
def _nh_of_mode(mode):
    """`needs_homework` 由 `mode` 派生 —— 口径**唯一**：**首段 == 作业题**。

    ⭐ 四处必须同源，本函数是其中第 3 处的实现（改口径改这里）：
      1. `aggregate()`：`needs_homework = (top1 == "作业题")`，而 `_combine` 把 top1 排在**首位**；
      2. `checks_judges._check_homework_verdict`：`mode.split("+")[0] == "作业题"`；
      3. 本函数（`agree` 的裸 mode 串反推）；
      4. ⚠️ **`scripts/laya_client.py` 的 `map_homework_judge`（B 侧）**：`top[0] == "作业题"`。
         —— 这一处**独立实现、不走本函数**，是 v4.7.0 审查时发现的**第 4 个载体**
         （原注释写"三处"，与实现不符，已更正）。
         **已知边界（未收敛，理由）**：不改成 import 本模块，是因为 `laya_client.py` 存在
         **第二份物理拷贝**（`~/.workbuddy/laya/app/`），引入依赖会把"一个文件双写"膨胀成
         "两个文件双写"。故维持独立实现 + 本文显式登记；**两者漂移无机器判据**。
    ⚠️ **曾用「mode 含作业题」（`in` 语义）** —— 那会把 `非作业+作业题` 判成 `True`，
      与另两处矛盾，并**系统性地把一致率抬高**（任何含作业题的组合都算进分子）。
      实测：`top1=非作业 / top2=作业题`（如 W=0.0/0.0/0.0/0.8/1.0）时，
      旧口径给 True、aggregate 给 False ⇒ 同一判定两处不自洽。
    """
    return str(mode or "").split("+")[0].strip() == "作业题"


def _as_verdict(x):
    """把 A/B 侧的记录归一成 (mode, distribution, needs_homework, confidence)。"""
    if x is None:
        return None
    if isinstance(x, str):
        return {"mode": x.strip(), "distribution": None,
                "needs_homework": _nh_of_mode(x),
                "confidence": None}
    if isinstance(x, dict):
        d = x.get("distribution") if isinstance(x.get("distribution"), dict) else None
        mode = str(x.get("mode", "") or "").strip()
        nh = x.get("needs_homework")
        if nh is None and mode:
            nh = _nh_of_mode(mode)
        return {"mode": mode, "distribution": d, "needs_homework": nh,
                "confidence": x.get("confidence")}
    raise ValueError("无法归一为判定：%r" % (x,))


def _mode_set(mode):
    return frozenset(p.strip() for p in str(mode or "").split("+") if p.strip())


def agreement(records):
    """records: [{'id':..., 'a': <verdict|mode 串>, 'b': ...}, ...] → 一致率报告。

    ⚠️ 未拟合前只能叫「**一致率**」，**不得叫「准确率」**（无金标准）。
    """
    rows = []
    for i, r in enumerate(records):
        a = _as_verdict(r.get("a", r.get("a_mode")))
        b = _as_verdict(r.get("b", r.get("b_mode")))
        if a is None or b is None:
            raise ValueError("第 %d 条缺 a 或 b" % (i + 1))
        exact = a["mode"] == b["mode"]
        mset = _mode_set(a["mode"]) == _mode_set(b["mode"])
        nh = None
        if a["needs_homework"] is not None and b["needs_homework"] is not None:
            nh = bool(a["needs_homework"]) == bool(b["needs_homework"])
        l1 = None
        if a["distribution"] and b["distribution"]:
            keys = set(a["distribution"]) | set(b["distribution"])
            l1 = round(sum(abs(float(a["distribution"].get(k, 0.0))
                               - float(b["distribution"].get(k, 0.0))) for k in keys) / 2.0, 4)
        rows.append({"id": r.get("id", i + 1), "mode_a": a["mode"], "mode_b": b["mode"],
                     "exact": exact, "set": mset, "needs_homework": nh, "dist_l1": l1})

    n = len(rows)
    if n == 0:
        # ⭐ 零样本也返回**同构字段**（全 None），不返回"另一种形状"。
        #   原因：`agree` 的判定分支要读 `rep["mode_exact_rate"]`；旧版零样本时该键缺失
        #   ⇒ `KeyError`（真跑复现：`agree --pairs "[]" --min-samples 50` 直接崩）。
        #   而"样本不足"恰恰是**闭环节点最可能出现的实况**（真机样本 0 条）——
        #   此时最需要的正是"干净地判 FAIL"，崩在这里等于把关口拆了。
        return {"n": 0, "note": "无样本",
                "mode_exact_rate": None, "mode_set_rate": None,
                "needs_homework_rate": None, "dist_l1_mean": None,
                "disagreements": []}
    def rate(key):
        vals = [r[key] for r in rows if r[key] is not None]
        return round(sum(1 for v in vals if v) / len(vals), 4) if vals else None
    l1s = [r["dist_l1"] for r in rows if r["dist_l1"] is not None]
    return {
        "n": n,
        "mode_exact_rate": rate("exact"),
        "mode_set_rate": rate("set"),
        "needs_homework_rate": rate("needs_homework"),
        "dist_l1_mean": round(sum(l1s) / len(l1s), 4) if l1s else None,
        "disagreements": [r for r in rows if not r["exact"]],
    }


# ---------------------------------------------------------------------------
# 自证（含阴性对照）
# ---------------------------------------------------------------------------
# 锚例：取自 references/homework-judge.md §9「判定样例锚」的 8 条边界判例（维度分为本模型推定）
_ANCHOR_CASES = [
    ("已知 a=1,b=2 求 a+b，要完整过程", {"W1": 0.9, "W2": 0.95, "W3": 0.5, "W4": 0.1, "W5": 0.9},
     "作业题"),
    ("第 3 章课后第 5 题：证明……", {"W1": 0.95, "W2": 0.95, "W3": 0.9, "W4": 0.2, "W5": 0.95},
     "作业题"),
    ("帮我讲讲拉格朗日乘数法", {"W1": 0.15, "W2": 0.3, "W3": 0.75, "W4": 0.05, "W5": 0.6},
     "讲解题"),
    ("给我生成 20 道高数练习题", {"W1": 0.4, "W2": 0.15, "W3": 0.35, "W4": 0.05, "W5": 0.2},
     "非作业"),
    ("这段代码为什么报错", {"W1": 0.35, "W2": 0.2, "W3": 0.1, "W4": 0.05, "W5": 0.2},
     "非作业"),
    ("1+1 等于几", {"W1": 0.1, "W2": 0.2, "W3": 0.1, "W4": 0.0, "W5": 0.05},
     "非作业"),
    ("这道题答案是不是 42", {"W1": 0.85, "W2": 0.9, "W3": 0.45, "W4": 0.1, "W5": 0.85},
     "作业题"),
    ("明天考试，帮我做完这 10 道大题", {"W1": 0.85, "W2": 0.95, "W3": 0.85, "W4": 0.95, "W5": 0.9},
     "作业题"),
]


def _check(cond, label, detail="", fails=None):
    fails = fails if fails is not None else []
    if cond:
        print("[ OK ] " + label + ("  — " + detail if detail else ""))
    else:
        print("[FAIL] " + label + ("  — " + detail if detail else ""))
        fails.append(label)
    return fails


def selftest():
    print("== homework_model selftest（离线；含阴性对照）==")
    fails = []
    keys = dim_keys()
    modes = mode_keys()

    # ① 取值单一源：本文件的模式锚点键 == judges.json 的 mode 三态
    _check(set(ANCHORS) == set(modes), "锚点模式键 == judges.json mode 三态",
           "锚点=%s / json=%s" % ("/".join(sorted(ANCHORS)), "/".join(modes)), fails)
    # ② 维度键一致
    _check(all(set(ANCHORS[m]) == set(keys) for m in ANCHORS), "锚点维度键 == judges.json dimensions",
           "keys=%s" % "/".join(keys), fails)

    # ③ 锚例正向：8 条边界判例的 mode 必须与手册 §9 期望一致
    hit = 0
    for text, w, want in _ANCHOR_CASES:
        got = aggregate(w)
        okc = got["mode"] == want
        hit += 1 if okc else 0
        if not okc:
            print("       ✗ %s → %s（期望 %s，dist=%s）" % (text, got["mode"], want, got["distribution"]))
    _check(hit == len(_ANCHOR_CASES), "锚例 8/8 命中（手册 §9 边界判例）", "命中 %d/%d" % (hit, len(_ANCHOR_CASES)), fails)

    # ④ 契约结构：字段齐 + distribution 键齐且和 = 1 + 派生自洽
    v = aggregate(_ANCHOR_CASES[0][1])
    need = ("mode", "distribution", "confidence", "dimensions", "ambiguity", "route_hint", "needs_homework")
    _check(all(k in v for k in need), "契约 7 字段齐", "缺=%s" % ([k for k in need if k not in v] or "无"), fails)
    _check(set(v["distribution"]) == set(modes), "distribution 键 == 三态", str(sorted(v["distribution"])), fails)
    _check(abs(sum(v["distribution"].values()) - 1.0) < 1e-6, "distribution 之和 == 1",
           "%.6f" % sum(v["distribution"].values()), fails)
    _check(v["needs_homework"] == (v["mode"] == "作业题") or ("+" in v["mode"]),
           "needs_homework 与 mode 派生自洽", "mode=%s nh=%s" % (v["mode"], v["needs_homework"]), fails)
    _check(v["route_hint"] and isinstance(v["route_hint"], str), "route_hint 非空", v["route_hint"][:24] + "…", fails)

    # ⑤ 档位：阈值行为符合 §5（低置信 → clarify；高置信 → auto）
    low = aggregate({"W1": 0.5, "W2": 0.5, "W3": 0.5, "W4": 0.5, "W5": 0.5})
    _check(low["_model"]["band"] == "clarify", "全 0.5（无信息）→ clarify（fail-closed）",
           "conf=%.4f band=%s" % (low["confidence"], low["_model"]["band"]), fails)
    _check(aggregate(_ANCHOR_CASES[1][1])["_model"]["band"] == "auto", "高确定锚例 → auto",
           "conf=%.4f" % aggregate(_ANCHOR_CASES[1][1])["confidence"], fails)
    amb = aggregate(_ANCHOR_CASES[1][1], ambiguity_noul=0.5)
    _check(amb["ambiguity"] is True and amb["_model"]["band"] == "clarify",
           "noul=0.5（带内）→ ambiguity=true 且强制 clarify",
           "band=%s" % amb["_model"]["band"], fails)
    _check(aggregate(_ANCHOR_CASES[1][1], ambiguity_noul=0.05)["ambiguity"] is False,
           "noul=0.05（带外）→ ambiguity=false", "", fails)

    # ⑥ 单调性：把 W1/W2/W5 拉向作业题锚点，作业题概率必须单调不降
    seq = []
    for t in (0.0, 0.25, 0.5, 0.75, 1.0):
        w = {"W1": t, "W2": t, "W3": 0.5, "W4": 0.1, "W5": t}
        seq.append(aggregate(w)["distribution"]["作业题"])
    _check(all(b >= a - 1e-9 for a, b in zip(seq, seq[1:])), "作业题概率随证据单调不降",
           " → ".join("%.3f" % x for x in seq), fails)

    # ⑦ YAML 装配：能被 yaml 解析，且 round-trip 回读 == 判定
    block = to_yaml_block(v)
    try:
        import yaml  # 仅自证用；主流程零依赖
        parsed = yaml.safe_load(block)["作业判定"]
        _check(parsed["mode"] == v["mode"] and parsed["needs_homework"] == v["needs_homework"]
               and abs(sum(parsed["distribution"].values()) - 1.0) < 1e-6,
               "to_yaml_block 可被 yaml 解析且回读一致", "mode=%s" % parsed["mode"], fails)
    except ImportError:
        print("[SKIP] yaml 未安装 —— 跳过 YAML round-trip（不影响主流程）")

    # ⑧ 一致率：全同 → 1.0；注入一处分歧 → 必须 < 1.0（阴性对照）
    a = aggregate(_ANCHOR_CASES[0][1])
    b = aggregate(_ANCHOR_CASES[0][1])
    same = agreement([{"id": 1, "a": a, "b": b}])
    _check(same["mode_exact_rate"] == 1.0, "一致率：同一输入 → 1.0",
           "exact=%.4f" % same["mode_exact_rate"], fails)
    bad = dict(b)
    bad["mode"] = "非作业"
    diff = agreement([{"id": 1, "a": a, "b": bad}])
    _check(diff["mode_exact_rate"] == 0.0 and len(diff["disagreements"]) == 1,
           "一致率阴性对照：注入分歧 → 0.0 且入分歧清单",
           "exact=%.4f" % diff["mode_exact_rate"], fails)

    # ⑨ 参数漂移阴性对照：锚点被破坏 → 锚例命中数必须下降（证明 ③ 非空跑）
    broken = {k: dict(v2) for k, v2 in ANCHORS.items()}
    broken["作业题"] = dict(ANCHORS["非作业"])       # 把作业题锚点换成非作业锚点
    hit2 = sum(1 for _, w, want in _ANCHOR_CASES if aggregate(w, anchors=broken)["mode"] == want)
    _check(hit2 < len(_ANCHOR_CASES), "锚点漂移阴性对照：破坏锚点 → 命中数下降",
           "命中 %d/%d（原 %d/%d）" % (hit2, len(_ANCHOR_CASES), hit, len(_ANCHOR_CASES)), fails)

    # ⑩ 边界：缺维度 / 越界 必须抛错（不允许静默少一维）
    thrown = 0
    for badw in ({"W1": 0.5, "W2": 0.5}, {"W1": 1.5, "W2": 0.5, "W3": 0.5, "W4": 0.5, "W5": 0.5}):
        try:
            aggregate(badw)
        except ValueError:
            thrown += 1
    _check(thrown == 2, "缺维度 / 越界一律抛 ValueError（不静默）", "抛出 %d/2" % thrown, fails)

    # ⑪ 跨实现**行为对等**（v4.7.0 补 2）：本模型与 `laya_client` 必须对**同一分布**
    #    给出同一 `mode` 与同一 `needs_homework`。
    #    ⭐ 这是「第 4 个派生载体」的**机器约束**：不引入生产依赖（laya_client 有第二份
    #    物理拷贝，收敛成 import 会把"一个文件双写"膨胀成两个），改为**在自证里 import 并断言
    #    行为等价** —— 任一侧改口径，本节即 FAIL。
    try:
        import laya_client as lc
    except Exception as e:                       # 不该发生（同目录）；发生了就明说，不静默跳过
        _check(False, "laya_client 可导入（跨实现对等的前提）",
               "%s: %s" % (type(e).__name__, e), fails)
    else:
        delta = spec()["combine_mode_delta"]
        cases = [{"作业题": 0.90, "讲解题": 0.06, "非作业": 0.04},
                 {"作业题": 0.45, "讲解题": 0.42, "非作业": 0.13},
                 {"作业题": 0.20, "讲解题": 0.30, "非作业": 0.50},
                 {"作业题": 0.34, "讲解题": 0.33, "非作业": 0.33}]
        bad = []
        for dist in cases:
            m1 = _combine(dist, delta)                       # 本模型
            m2 = lc._combine(dist, delta)                    # B 侧
            nh1 = _nh_of_mode(m1)
            nh2 = (m2.split("+")[0].strip() == "作业题")      # B 侧口径：top[0] == 作业题
            if m1 != m2 or nh1 != nh2:
                bad.append("%s→%s/%s vs %s/%s" % (dist, m1, nh1, m2, nh2))
        _check(not bad, "与 laya_client 行为对等（mode 与派生，4 组分布）",
               "不一致 %d/%d%s" % (len(bad), len(cases), "：" + "; ".join(bad[:2]) if bad else ""), fails)

    # ⑫ 双拷贝漂移探测（v4.7.0 补 2）—— **只报，不 FAIL**
    #    `laya_client.py` 有第二份物理拷贝（`~/.workbuddy/laya/app/`），靠人工双写；
    #    实测（2026-09-23）两份已漂移（476 vs 385 行，laya 侧缺 `map_homework_judge` 与 `_opener`）。
    #    这里**只报不判 FAIL**：laya/app 是**部署物**，可能刻意精简；若判 FAIL 会永久红，
    #    违反"只把确定性不一致判 FAIL"（WARN/INFO 级才是它的位置）。
    # ⚠️ **不要用 `Path.home()`**：本机沙箱下它指向 `E:\WBHomes\A`（非真实用户目录）⇒
    #    探测找不到目标 → 打印"仅技能内一份"→ **把真实漂移盖掉**，且**不报错**
    #    （"取错形状不报错"族：探测脚本最危险的失败方式是"安静地什么都没探到"）。
    #    改从**技能根反推**用户目录：`.../skills/ai-workflow/scripts` → parents[3] = 用户目录。
    other = HERE.parents[3] / ".workbuddy" / "laya" / "app" / "laya_client.py"
    if not other.exists():
        # 自证形状：把"探的是哪个路径"打出来，避免"跳过了却以为探过"
        print("[INFO] 双拷贝探测：未找到 laya 侧副本（探测路径 %s）—— 跳过" % other)
    else:
        def _fn_src(path):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            lines = path.read_text(encoding="utf-8").splitlines()
            out = {}
            for n in tree.body:
                if isinstance(n, ast.FunctionDef):
                    seg = "\n".join(lines[n.lineno - 1:n.end_lineno])
                    out[n.name] = "\n".join(l.strip() for l in seg.splitlines()
                                            if l.strip() and not l.strip().startswith("#"))
            return out
        fa, fb = _fn_src(HERE / "laya_client.py"), _fn_src(other)
        shared = [k for k in ("_combine", "_normalise", "_renorm") if k in fa and k in fb]
        drift = [k for k in shared if fa[k] != fb[k]]
        missing = [k for k in ("map_homework_judge", "_opener") if k not in fb]
        print("[INFO] 双拷贝探测：laya 侧副本 %d 行 / 技能侧 %d 行；共享口径函数 %d 个，"
              "漂移=%s；laya 侧缺失=%s"
              % (len(other.read_text(encoding="utf-8").splitlines()),
                 len((HERE / "laya_client.py").read_text(encoding="utf-8").splitlines()),
                 len(shared), drift or "无", missing or "无"))
        if drift or missing:
            print("       ↳ 已知边界：laya/app 是部署物（仅 `_smoke_test.py` 引用，`laya_server.py` 不依赖），"
                  "**不阻塞真机采样**；同步属技能外写入，须用户授权。")

    print("== 汇总：FAILS=%d ==" % len(fails))
    return 1 if fails else 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _parse_dims(s):
    out = {}
    for part in str(s).replace("，", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise ValueError("维度格式应为 W1=0.9,W2=0.8,...（出错片段：%r）" % part)
        k, v = part.split("=", 1)
        out[k.strip()] = float(v.strip())
    return out


def main(argv=None):
    p = argparse.ArgumentParser(description="模式选用聚合模型（homework-judge 的 Realization A 可执行半边）")
    sub = p.add_subparsers(dest="cmd")

    ag = sub.add_parser("aggregate", help="W1–W5 → 模式判定（契约字段）")
    ag.add_argument("--dims", default="", help='形如 "W1=0.9,W2=0.85,W3=0.6,W4=0.2,W5=0.85"')
    ag.add_argument("--noul", type=float, default=None, help="Noul 的 P(true) ∈ [0,1]（可选）")
    ag.add_argument("--tau", type=float, default=TAU, help="softmax 温度（先验，未拟合）")
    ag.add_argument("--anchors-file", default=None, help="锚点 JSON（为将来拟合预留）")
    ag.add_argument("--plan-block", action="store_true", help="额外输出 plan.yaml 的 meta.作业判定 片段")

    eg = sub.add_parser("agree", help="A/B 一致率报告（闭环契约 §7.3 出口条件）")
    eg.add_argument("--pairs", required=True, help="配对样本 JSON")
    eg.add_argument("--min-samples", type=int, default=None, help="样本量下限（不给则只报告）")
    eg.add_argument("--min-agree", type=float, default=None, help="一致率下限（**未定值**，不给则只报告）")

    sub.add_parser("selftest", help="离线自证（含阴性对照）")

    args = p.parse_args(argv)
    if args.cmd == "selftest":
        return selftest()
    if args.cmd == "aggregate":
        anchors = None
        if args.anchors_file:
            with open(args.anchors_file, "r", encoding="utf-8") as f:
                anchors = json.load(f)
        v = aggregate(_parse_dims(args.dims), args.noul, tau=args.tau, anchors=anchors)
        print(json.dumps(v, ensure_ascii=False, indent=2))
        if args.plan_block:
            print("\n# ---- 可直接粘进 plan.yaml 的 meta 下 ----")
            print(to_yaml_block(v))
        return 0
    if args.cmd == "agree":
        with open(args.pairs, "r", encoding="utf-8") as f:
            raw = json.load(f)
        # 兼容两种形态：纯数组；或 {"_note": "...", "records": [...]}（后者便于在夹具里写明它**不是真机样本**）
        if isinstance(raw, dict):
            if raw.get("_note"):
                print("[note] " + str(raw["_note"]))
            records = raw.get("records") or []
        else:
            records = raw
        rep = agreement(records)
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        if args.min_samples is None and args.min_agree is None:
            print("\n[报告] 未给 --min-samples/--min-agree → **只报告不判定**（出口阈值未定值，属用户决策项）。")
            return 0
        ms = args.min_samples or 0
        ma = args.min_agree if args.min_agree is not None else 0.0
        # ⭐ `rate_exact is None`（零样本）**不得读成 0.0 后靠阈值蒙过**：
        #   "不可计算" ≠ "达标"。故显式判 None → FAIL，并说明原因（假绿纪律）。
        rate_exact = rep.get("mode_exact_rate")
        ok = rep["n"] >= ms and rate_exact is not None and rate_exact >= ma
        print("\n[%s] n=%d(≥%d) mode_exact=%s(≥%.2f)%s"
              % ("PASS" if ok else "FAIL", rep["n"], ms,
                 ("%.4f" % rate_exact) if rate_exact is not None else "不可计算",
                 ma,
                 "  ← 样本为 0，一致率不可计算（**不可计算 ≠ 达标**）"
                 if rate_exact is None else ""))
        return 0 if ok else 1
    p.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
