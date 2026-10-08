#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# [自研工具] thinking_model.py
# 用途：思考裁决聚合模型——thinking-judge 的 Realization A 可执行半边，把 T1–T4 维度分（＋ veto /
#       ambiguity / amendments）按确定性公式聚合成 {verdict, distribution, confidence, band, ...}，
#       并产出可粘进 plan.yaml 的 meta.思考判定 片段；另含 A/B 一致率（agree）与 gold set 评测。
# 适用场景：思考判定出口条件的 A 侧测量、检查项 24 结构对照、meta.思考判定 生成、gold set 回归。
# 作者：ai-workflow 自研（思考板块-ai-workflow-v4.11.0-2026-09-28，2026-09-28）
# 仓库：https://github.com/Garvin666/ai-workflow-tools
"""thinking_model.py —— 思考裁决**聚合模型**（thinking-judge 的 Realization A 可执行半边）

【定位：务必按此理解，勿误用】
  本脚本**不是第 7 个 judge**、**不是新的判据源**、**不接管路由**、**不做识别**。
  它是 `references/thinking-panel.md` §3「派生规则」的**唯一机器实现** —— 把
  「T1–T4 维度分（＋ `veto` / `ambiguity` / `amendments`）」按**确定性公式**聚合成
  `{verdict, distribution, confidence, dimensions, veto, amendments, ambiguity, band,
    needs_human_confirm, main_judge, laya, route_hint}`，
  与检查项 24（`checks_judges._check_thinking_verdict`）的结构要求逐字段同构。

【为什么要它（这是本脚本存在的全部理由）】
  Realization A（脑内协议）此前**只定义到"给维度打分 / 给 Noul 读数"**；而
  「维度分 → 三态分布 → 置信 → `verdict`」这一段是**黑箱**（藏在 Laya 内部，或每次现场即兴）。后果：
    · A 侧判定**不可复现** —— 同一组 T 值在不同会话可能给出不同分布；
    · 于是 **A/B 一致率无法测量** ⇒「B 升为主判据」的**出口条件结构上无法闭环**。
  把这一段落成代码，等价于：**Realization A = 语义维度评估（仍需模型/人）+ 确定性聚合（本脚本）**。

【⭐ 与 homework_model 的关键结构差异：`verdict` ≠ argmax(distribution)】
  `verdict` 由 §3 的 `derive(veto, ambiguity, confidence, amendments)` **按序短路**派生 ——
  **不是**取 `distribution` 的最大项。原因：`拒绝`（否决层）与 `澄清`（触界修正案）**不是维度空间里的
  一个原型**，把它们塞进 softmax 就是内核 N2 禁止的「底线被加权稀释」（D7/D9 同源）。
  ⇒ `distribution` 只是**三个非否决结局**（接受/修正/澄清）的软证据，用来给出 `confidence`；
     `verdict` 才是裁决。二者**可以不一致**（如：分布偏「接受」但携带了授权内修正案 ⇒ 裁决「修正」），
     这**不是** bug，是本设计刻意分离的两个物理量。

【诚实边界（与契约 §9 同一条纪律）】
  · 锚点 `ANCHORS` 与温度 `TAU` 都是**先验、未经数据拟合** ⇒ 由此得到的 `distribution` /
    `confidence` **只可用于「对照、回归、抽样指路」，不得作为准确率或阈值依据**。
  · ⭐ **gold set 10/10 是"可分性"证据，不是准确率证据** —— 10 条种子的维度分是
    **agent 推定值**（`provenance: agent_seed`），标签与样例出自同一设计 ⇒ 存在自证循环嫌疑。
    它只说明样例在维度空间中**可分**，**不构成**任何准确率声明（契约 §9.19）。
  · 本脚本**不判"答案对不对"**，也**不判"这是不是价值观违规"**（那需要机器 oracle，本模块没有）。
  · `ANCHORS` / `TAU` 是**可拟合参数**：拿到足够人工标样本后可直接替换（`--anchors-file` / `--tau`）；
    **本轮不做拟合**（人工标样本不足），故一律按"未拟合"标注、不声称任何比率。
  · ⚠️ `agree` 的判定阈值**刻意不给默认值** —— 出口阈值属用户决策项；不给 `--min-agree` 时**只报告不判定**。

【取值单一源（三条，务必遵守）】
  1. 阈值（`THINKING_NUOL_BAND` / `VETO_HIT` / `THINKING_SAMPLE_BAND` / `THINKING_GOLD_MIN_ANNOTATED`）
     与枚举（`THINKING_VERDICTS` / `THINKING_DIST_KEYS` / `THINKING_AMEND_SIDES` …）**全部** import 自
     `scripts/checks_core.py`，**本文件不复制其数值**（避免"同一物理量写两处"）。
  2. `dimensions` 键名 / `verdict`→`route_hint` 映射 取自 `scripts/judges.json` 的 `thinking_judge`。
  3. 锚点 `ANCHORS` 与温度 `TAU`**不在手册与 json 中复制**，唯一实现即本文件 —— 契约 §3.1 明文钉死。

用法：
  python scripts/thinking_model.py aggregate --dims "T1=0.9,T2=0.85,T3=0.85,T4=0.85"
  python scripts/thinking_model.py aggregate --dims "..." --veto-noul 0.5 --plan-block
  python scripts/thinking_model.py gold [--file assets/thinking-gold.yaml]
  python scripts/thinking_model.py agree --pairs <pairs.json> [--min-samples 30 --min-agree 0.90]
  python scripts/thinking_model.py selftest
"""

import argparse
import json
import math
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

TEMPLATES_PATH = HERE / "judges.json"
GOLD_PATH = HERE.parent / "assets" / "thinking-gold.yaml"
KIND = "thinking_judge"

# 取值单一源：阈值与枚举一律取自 checks_core（**本文件不复写其数值**）
from checks_core import (  # noqa: E402
    THINKING_VERDICTS,
    THINKING_DIST_KEYS,
    THINKING_VETO_AXES,
    THINKING_DIMS,
    THINKING_BANDS,
    THINKING_AXIS_EMPTY,
    THINKING_AMEND_SIDES,
    THINKING_NUOL_BAND,
    VETO_HIT,
    THINKING_SAMPLE_BAND,
    THINKING_GOLD_MIN_ANNOTATED,
    THINKING_PROVENANCE,
)

# ---------------------------------------------------------------------------
# 模型参数（**先验，未拟合** —— 唯一实现地，手册与 judges.json 均不复制其数值）
# ---------------------------------------------------------------------------
# 三个原型锚点：每个结局在 T1–T4 上的"理想位置"（各维 ∈ [0,1]）。
# 依据 = references/thinking-panel.md §1.2 的维度定义（高分指向）：
#   · 接受：目标明确、前提成立、约束自洽、路径可达 —— 四维皆高
#   · 修正：目标/前提/路径都在，但**约束自洽性(T3)低**（"存在瑕疵但可修复"的原型）
#   · 澄清：目标/前提/路径任一低（说不清要什么、前提不成立、路径不可达）—— 需先问清
# ⚠️ 刻意**不**为「拒绝」设锚点 —— 它是否决层，不是维度原型（D7/D9 同源）。
ANCHORS = {
    "接受": {"T1": 0.90, "T2": 0.85, "T3": 0.85, "T4": 0.85},
    "修正": {"T1": 0.80, "T2": 0.75, "T3": 0.20, "T4": 0.75},
    "澄清": {"T1": 0.25, "T2": 0.40, "T3": 0.60, "T4": 0.30},
}

# softmax 温度：先验值，控制分布的锐度（越小越自信）。**未拟合。**
TAU = 0.10

# gold set 的「高/中/低」标签 → 维度分映射（**推定**，非独立标注；见本文件诚实边界）。
LABEL_TO_SCORE = {"高": 0.90, "中": 0.55, "低": 0.20}

_MODEL_TAG = "thinking-model"
_VERDICT_KEY = "verdict"
_WITHDRAW = "拒绝"


# ---------------------------------------------------------------------------
# 取值单一源
# ---------------------------------------------------------------------------
def _load_templates():
    with open(TEMPLATES_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def spec():
    """thinking_judge 的模板块（dimensions / route_hint / thresholds 的取值源之一）。"""
    return _load_templates()[KIND]


def dim_keys():
    return tuple(spec()["dimensions"].keys())


def dist_keys():
    """`distribution` 的键 = 三个非否决结局（**不含 `拒绝`**，D7）。恒等于 `THINKING_DIST_KEYS`。"""
    return tuple(THINKING_DIST_KEYS)


def route_hint_of(verdict):
    return spec()["route_hint"][verdict]


# ---------------------------------------------------------------------------
# 聚合内核
# ---------------------------------------------------------------------------
def _softmax(scores, tau):
    """数值稳定化的 softmax（减 max 不改变结果，只防溢出）。"""
    m = max(scores.values())
    exps = {k: math.exp((v - m) / tau) for k, v in scores.items()}
    total = sum(exps.values()) or 1.0
    return {k: v / total for k, v in exps.items()}


def normalised_distance(dims, anchor, keys):
    """归一化欧氏距离（除以 √k，落在 [0,1]）—— 模型的"离原型的远近"。"""
    return math.sqrt(sum((dims[k] - anchor[k]) ** 2 for k in keys)) / math.sqrt(len(keys))


def noul_to_veto(noul):
    """把 Noul 读数 `veto_noul` 映射为 `{triggered, suspect, noul}` —— **唯一阈值读数处**。

    口径（= 契约 §1.4 伪代码，**不是新常量**）：
        veto_noul >= VETO_HIT(=THINKING_NUOL_BAND[1])  → triggered = true（强制拒绝）
        THINKING_NUOL_BAND[0] <= veto_noul < VETO_HIT  → suspect = true（强制澄清，不得放行）
        否则                                            → 两者皆 false（走正常派生）

    ⚠️ `VETO_HIT` 与 `THINKING_NUOL_BAND[1]` **已定恒等**（§10.2 第 7 项）—— 此处用 `VETO_HIT`
    这个名字读它，**不重复写字面量**，结构上不给两处漂移的机会。
    """
    n = float(noul)
    if not 0.0 <= n <= 1.0:
        raise ValueError("noul=%r 越界（须在 0–1）" % (noul,))
    lo = THINKING_NUOL_BAND[0]
    triggered = n >= VETO_HIT
    suspect = (lo <= n < VETO_HIT)
    return {"triggered": triggered, "suspect": suspect, "noul": n, "axis": "",
            "underlying_axis": "", "evidence": "", "alternative": "", "appeal_hint": ""}


def _normalise_veto(veto):
    """把 `veto` 归一成 8 键字典（缺省全空、布尔位恒为 bool）。

    `veto` 三种入参：None（无否决）/ 数（走 `noul_to_veto`）/ 字典（A 侧已填）。
    ⚠️ `triggered` / `suspect` **恒输出 bool**（**不得** None）—— `derive` rule 2 读 `suspect`，
    缺失即读成 `false` ⇒ 中间带违规**静默放行**（这正是 D17 在 plan 层要拦的 fail-open；
    本工具在生成侧就把它堵死）。
    """
    if veto is None:
        return {"triggered": False, "suspect": False, "noul": None, "axis": "",
                "underlying_axis": "", "evidence": "", "alternative": "", "appeal_hint": ""}
    if isinstance(veto, bool):
        raise ValueError("veto 不能是裸 bool（须为 None / Noul 数 / 字典）")
    if isinstance(veto, (int, float)):
        return noul_to_veto(veto)
    if not isinstance(veto, dict):
        raise ValueError("veto 类型非法：%r" % (type(veto).__name__,))
    noul = veto.get("noul")
    triggered = veto.get("triggered")
    suspect = veto.get("suspect")
    if (triggered is None or suspect is None) and noul is not None:
        base = noul_to_veto(float(noul))
        triggered = base["triggered"] if triggered is None else triggered
        suspect = base["suspect"] if suspect is None else suspect
    return {
        "triggered": bool(triggered) if triggered is not None else False,
        "suspect": bool(suspect) if suspect is not None else False,
        "noul": (float(noul) if noul is not None else None),
        "axis": str(veto.get("axis", "") or ""),
        "underlying_axis": str(veto.get("underlying_axis", "") or ""),
        "evidence": str(veto.get("evidence", "") or ""),
        "alternative": str(veto.get("alternative", "") or ""),
        "appeal_hint": str(veto.get("appeal_hint", "") or ""),
    }


def derive(veto, ambiguity, confidence, amendments):
    """契约 §3 的派生规则 —— `verdict` 的**唯一口径源**（按序短路，**不取 argmax**）。"""
    if veto.get("triggered"):
        return _WITHDRAW                                   # 1. 最高优先，fail-closed
    if veto.get("suspect"):
        return "澄清"                                      # 2. 疑似命中，宁严不宽
    if ambiguity or confidence < THINKING_SAMPLE_BAND[0]:
        return "澄清"                                      # 3. 不确定一律不猜
    ambs = list(amendments or [])
    if ambs:
        sides = {a.get("越线性") if isinstance(a, dict) else None for a in ambs}
        if sides and sides <= {THINKING_AMEND_SIDES[0]}:
            return "修正"                                  # 4. 全部授权内 ⇒ 修正
        return "澄清"                                      # 5. 任一触界（含缺失/未知）⇒ 澄清
    return "接受"                                          # 6. 其余


def band_of(confidence, ambiguity=False):
    """契约 §3.1 的 `band` 映射 —— 与阈值表**同源**（D19 守同源，故只在此处实现一次）。"""
    lo, hi = THINKING_SAMPLE_BAND
    if ambiguity or confidence < lo:
        return THINKING_BANDS[2]        # 低
    if confidence >= hi:
        return THINKING_BANDS[0]        # 高
    return THINKING_BANDS[1]            # 中


def aggregate(dims, veto=None, ambiguity=False, amendments=None, *, tau=TAU, anchors=None):
    """T1–T4（＋ veto / ambiguity / amendments）→ thinking-judge 契约字段（结构同检查项 24）。

    `dims`：{T1..T4}，取自本模型的 `dim_keys()`（单一取值源）。
    `veto`：None / Noul 数 / 字典（见 `_normalise_veto`）。
    `ambiguity`：bool（A 侧 Noul 判模糊/混合的结果）。
    `amendments`：`[{项, 原表述, 修正为, 依据, 越线性}]`；空/None 表示无修正案。
    """
    if isinstance(tau, bool) or not isinstance(tau, (int, float)) or not tau > 0.0:
        raise ValueError("tau=%r 非法（须为 > 0 的数）：tau=0 会除零，tau<0 会把"
                         "「最不像」判成「最像」（语义反转），二者都不允许静默通过" % (tau,))

    keys = dim_keys()
    missing = [k for k in keys if k not in dims]
    if missing:
        raise ValueError("缺维度 %s（须齐 %s）" % ("/".join(missing), "/".join(keys)))

    t = {}
    for k in keys:
        v = float(dims[k])
        if not 0.0 <= v <= 1.0:
            raise ValueError("维度 %s=%r 越界（须在 0–1）" % (k, dims[k]))
        t[k] = v

    dks = dist_keys()
    A = anchors or ANCHORS
    for m in dks:
        if m not in A:
            raise ValueError("锚点表缺结局 %r（须齐 %s）" % (m, "/".join(dks)))

    dist = {m: normalised_distance(t, A[m], keys) for m in dks}
    probs = _softmax({m: -dist[m] for m in dks}, tau)

    # ---- distribution **先定稿**（round 4 位 + 重归一化到和恰为 1）----
    # ⭐ 顺序是**硬要求**（与 homework_model 同源教训）：所有**派生字段**（confidence / verdict /
    #   band / route_hint）必须在 distribution 定稿**之后**取值。否则 confidence 取自未 round
    #   的 `probs[top1]`，与 checks 侧「confidence == max(distribution)」差约 1e-4 —— 纯量化噪声
    #   却会让 D6 误报，真漂移与噪声在判据下长得一模一样。
    out_dist = {k: round(v, 4) for k, v in probs.items()}
    total = sum(out_dist.values()) or 1.0
    out_dist = {k: round(v / total, 4) for k, v in out_dist.items()}
    if abs(sum(out_dist.values()) - 1.0) > 1e-6:
        k = max(out_dist, key=lambda x: out_dist[x])
        out_dist[k] = round(out_dist[k] + (1.0 - sum(out_dist.values())), 4)

    ordered = sorted(out_dist.items(), key=lambda kv: -kv[1])
    conf = out_dist[ordered[0][0]]              # ← 派生自定稿后的分布，与 max() **恒等**
    vn = _normalise_veto(veto)
    ambs = list(amendments or [])
    ver = derive(vn, bool(ambiguity), conf, ambs)
    needs_confirm = bool(ver == "澄清" and ambs)     # D18：澄清态携带修正案须人工确认

    return {
        _VERDICT_KEY: ver,
        "distribution": out_dist,
        "confidence": round(conf, 4),
        "dimensions": {k: t[k] for k in keys},
        "veto": vn,
        "amendments": ambs,
        "ambiguity": bool(ambiguity),
        "band": band_of(conf, bool(ambiguity)),
        "needs_human_confirm": needs_confirm,
        "main_judge": "A",
        "laya": None,
        "route_hint": route_hint_of(ver),
        "_model": {
            "kind": _MODEL_TAG,
            "tau": tau,
            "anchors_fitted": False,           # ⚠️ 恒 False 直到真做拟合
            "closest": ordered[0][0],          # distribution 的 argmax（**不等于** verdict，见文件头）
            "runner_up": ordered[1][0],
            "distances": {m: round(v, 4) for m, v in dist.items()},
            "thresholds": {"noul_band": list(THINKING_NUOL_BAND), "veto_hit": VETO_HIT,
                           "sample_band": list(THINKING_SAMPLE_BAND)},
        },
    }


# ---------------------------------------------------------------------------
# 装配：契约 dict → plan.yaml 的 meta.思考判定 YAML 片段
# ---------------------------------------------------------------------------
def to_yaml_block(verdict, indent=2):
    """产出可直接粘进 plan.yaml 的 `思考判定:` 片段（12 派生必填字段，顺序同 `THINKING_REQUIRED`；
    另附 **1 个生成面子块** `前提审计`，v4.22.0 新增 —— 见 references/thinking-panel.md §5.6）。

    YAML 标量一律用 JSON 双引号形式（JSON 字符串是 YAML 双引号标量的子集）——
    这样含中文/括号/反斜杠的 `route_hint` 不需要手写转义。
    """
    pad = " " * indent
    j = lambda o: json.dumps(o, ensure_ascii=False)
    b = lambda x: "true" if x else "false"
    v = verdict
    lines = [pad + "思考判定:"]
    lines.append(pad + "  verdict: " + j(v[_VERDICT_KEY]))
    lines.append(pad + "  distribution: " + j(v["distribution"]))
    lines.append(pad + "  confidence: " + j(v["confidence"]))
    lines.append(pad + "  dimensions: " + j(v["dimensions"]))
    lines.append(pad + "  veto: " + j(v["veto"]))
    lines.append(pad + "  amendments: " + j(v["amendments"]))
    lines.append(pad + "  ambiguity: " + b(v["ambiguity"]))
    lines.append(pad + "  band: " + j(v["band"]))
    lines.append(pad + "  needs_human_confirm: " + b(v["needs_human_confirm"]))
    lines.append(pad + "  main_judge: " + j(v["main_judge"]))
    lines.append(pad + "  laya: " + (j(v["laya"]) if v["laya"] is not None else "null"))
    lines.append(pad + "  route_hint: " + j(v["route_hint"]))
    # v4.22.0：`前提审计` 子块（生成面 P2 的产出，**不是** derive 的产物）—— 给出合法空骨架供填写。
    #   aggregate() 不产出它；缺省时 checks 只 WARN（软启动），填写则结构/自洽非法即 FAIL（D20–D22）。
    lines.append(pad + "  前提审计:")
    lines.append(pad + "    前提: []")
    lines.append(pad + '    来源核实: {"需要核实": false, "已核实": [], "未核实": []}')
    lines.append(pad + "    遗漏提醒: []")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# gold set：加载 + 逐条评测（**回归用，非判据**）
# ---------------------------------------------------------------------------
def load_gold(path=None):
    import yaml   # gold 与 selftest 才用；主流程（aggregate）零依赖
    p = Path(path) if path else GOLD_PATH
    with open(p, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def eval_samples(samples, tau=TAU, anchors=None):
    """对一组样本逐条 `aggregate` 并与 `expected_verdict` 比对。**N/N 是"可分性"证据，非准确率。**

    与 `eval_gold` 分家，是为了让自证的**阴性对照**能直接喂入"被改坏的一批样本"
    （证明比对器是活的，不是恒真）。
    """
    keys = dim_keys()
    rows = []
    hit = 0
    for s in samples:
        labels = s.get("labels") or {}
        miss = [k for k in keys if k not in labels]
        if miss:
            raise ValueError("gold 样本 %s 缺标签 %s" % (s.get("id"), "/".join(miss)))
        bad = [k for k in keys if labels[k] not in LABEL_TO_SCORE]
        if bad:
            raise ValueError("gold 样本 %s 标签取值非法：%s" % (s.get("id"), bad))
        dims = {k: LABEL_TO_SCORE[labels[k]] for k in keys}
        got = aggregate(dims, veto=s.get("veto"), ambiguity=bool(s.get("ambiguity", False)),
                        amendments=s.get("amendments") or [], tau=tau, anchors=anchors)
        okc = got[_VERDICT_KEY] == s.get("expected_verdict")
        hit += 1 if okc else 0
        rows.append({"id": s.get("id"), "expected": s.get("expected_verdict"),
                     "got": got[_VERDICT_KEY], "confidence": got["confidence"],
                     "closest": got["_model"]["closest"], "ok": okc,
                     "risk_side": s.get("risk_side"), "provenance": s.get("provenance")})
    return {"n": len(samples), "hit": hit, "rows": rows,
            "provenance_mix": _provenance_mix(samples)}


def eval_gold(path=None, tau=TAU, anchors=None):
    data = load_gold(path)
    res = eval_samples(data.get("samples") or [], tau, anchors)
    res["anchors_fitted"] = bool(data.get("anchors_fitted", False))
    return res


def _provenance_mix(samples):
    mix = {}
    for s in samples:
        pr = s.get("provenance")
        mix[pr] = mix.get(pr, 0) + 1
    return mix


def fitted_rule_ok(data):
    """契约 §5.4 铁律：`anchors_fitted=true` 的**唯一**依据是「同一批数据上算出一致率」，
    且该批须含 ≥ `THINKING_GOLD_MIN_ANNOTATED` 条 `user_spot_checked`/`human_verified` 样本。

    ⇒ 返回 `(ok, n_annotated)`：若声称已拟合但人工标样本不足 ⇒ `ok=False`（D12 的语义）。
    """
    samples = data.get("samples") or []
    hot = data.get("anchors_fitted")
    n = sum(1 for s in samples if s.get("provenance") in ("user_spot_checked", "human_verified"))
    ok = (not hot) or (n >= THINKING_GOLD_MIN_ANNOTATED)
    return ok, n


# ---------------------------------------------------------------------------
# A/B 一致率（闭环契约 §7 影子期出口条件）
# ---------------------------------------------------------------------------
def _verdict_of(x):
    if x is None:
        return None
    if isinstance(x, str):
        return x.strip()
    if isinstance(x, dict):
        v = x.get(_VERDICT_KEY)
        return str(v).strip() if v is not None else None
    raise ValueError("无法归一为裁决：%r" % (x,))


def agreement(records):
    """records: [{id, a, b, laya_status, provenance, laya_axis_unfounded}, ...] → 一致率报告。

    **分母纪律（D12 / D13 —— 这两条是契约里唯二"靶面不在 plan.yaml"的判据）**：
      · `agreement_rate` 的分母**只含**「Laya 侧 `status == "ok"`」**且**
        「`provenance ∈ {user_spot_checked, human_verified}`」的样本；
      · `status != "ok"`（absent/degraded）的样本**不进分母**（否则"服务没起来"会被读成"模型判得差"）；
      · `agent_seed` / `disputed` 的样本**不进分母**（否则"自标"会冒充"人工标"）；
      · `n_total != n_comparable` 时必须同时给出 `n_excluded` 与 `laya_absent`。
    ⚠️ 未拟合前只能叫「**一致率**」，**不得叫「准确率」**（无金标准）。
    """
    rows = []
    laya_absent = 0
    axis_unfounded = 0
    for i, r in enumerate(records):
        a = _verdict_of(r.get("a"))
        b = _verdict_of(r.get("b"))
        status = r.get("laya_status") or ("ok" if b is not None else "absent")
        if status != "ok":
            laya_absent += 1
        if r.get("laya_axis_unfounded") is True:
            axis_unfounded += 1
        rows.append({"id": r.get("id", i + 1), "a": a, "b": b, "status": status,
                     "provenance": r.get("provenance")})
    n_total = len(rows)
    comparable = [x for x in rows if x["status"] == "ok" and x["a"] is not None and x["b"] is not None]
    n_comparable = len(comparable)
    n_excluded = n_total - n_comparable
    qualified = [x for x in comparable if x["provenance"] in ("user_spot_checked", "human_verified")]
    n_qualified = len(qualified)
    rate = (round(sum(1 for x in qualified if x["a"] == x["b"]) / n_qualified, 4)
            if n_qualified else None)
    disagreements = [{"id": x["id"], "a": x["a"], "b": x["b"]} for x in comparable if x["a"] != x["b"]]
    mix = {}
    for x in rows:
        mix[x["provenance"]] = mix.get(x["provenance"], 0) + 1
    return {
        "n_total": n_total,
        "n_comparable": n_comparable,
        "n_excluded": n_excluded,
        "laya_absent": laya_absent,
        "laya_axis_unfounded": axis_unfounded,
        "n_qualified": n_qualified,          # agreement_rate 的真实分母（= 人工标 ∩ Laya ok）
        "agreement_rate": rate,
        "provenance_mix": mix,
        "disagreements": disagreements,
    }


# ---------------------------------------------------------------------------
# 自证（含阴性对照）
# ---------------------------------------------------------------------------
def _check(cond, label, detail="", fails=None):
    fails = fails if fails is not None else []
    if cond:
        print("[ OK ] " + label + ("  — " + detail if detail else ""))
    else:
        print("[FAIL] " + label + ("  — " + detail if detail else ""))
        fails.append(label)
    return fails


def selftest(gold_path=None):
    print("== thinking_model selftest（离线；含阴性对照）==")
    fails = []
    keys = dim_keys()
    dks = dist_keys()

    # ① 取值单一源：本文件锚点键 == THINKING_DIST_KEYS（三非否决结局）
    _check(set(ANCHORS) == set(dks), "锚点键 == THINKING_DIST_KEYS（不含「拒绝」）",
           "锚点=%s / 常量=%s" % ("/".join(sorted(ANCHORS)), "/".join(dks)), fails)
    # ② 维度键一致（本文件锚点 ↔ judges.json dimensions）
    _check(all(set(ANCHORS[m]) == set(keys) for m in ANCHORS), "锚点维度键 == judges.json dimensions",
           "keys=%s" % "/".join(keys), fails)
    # ③ ⭐ VETO_HIT 与模糊带上界**恒等**（§10.2 第 7 项决定的结构化断言）
    _check(VETO_HIT == THINKING_NUOL_BAND[1],
           "VETO_HIT == THINKING_NUOL_BAND[1]（恒等决定）",
           "VETO_HIT=%s band=%s" % (VETO_HIT, THINKING_NUOL_BAND), fails)

    # ④ Noul 三段读数
    _check(noul_to_veto(0.90) == {"triggered": True, "suspect": False, "noul": 0.9, "axis": "",
                                  "underlying_axis": "", "evidence": "", "alternative": "",
                                  "appeal_hint": ""},
           "noul=0.90 → triggered=true", "", fails)
    _check(noul_to_veto(0.50)["suspect"] is True and noul_to_veto(0.50)["triggered"] is False,
           "noul=0.50（带内）→ suspect=true 且 triggered=false", "", fails)
    _check(noul_to_veto(0.10)["triggered"] is False and noul_to_veto(0.10)["suspect"] is False,
           "noul=0.10（带外）→ 两者皆 false", "", fails)

    # ⑤ derive 六条规则逐条（按序短路）
    hi = {"T1": 0.9, "T2": 0.9, "T3": 0.9, "T4": 0.9}
    d_ok = {"triggered": False, "suspect": False}
    _check(derive({"triggered": True}, False, 0.9, [{"越线性": "授权内"}]) == "拒绝",
           "derive rule 1：triggered → 拒绝（优先于修正案）", "", fails)
    _check(derive({"triggered": False, "suspect": True}, False, 0.9, []) == "澄清",
           "derive rule 2：suspect → 澄清（宁严不宽）", "", fails)
    _check(derive(d_ok, True, 0.99, []) == "澄清", "derive rule 3：ambiguity → 澄清", "", fails)
    _check(derive(d_ok, False, 0.50, []) == "澄清", "derive rule 3：confidence<0.70 → 澄清", "", fails)
    _check(derive(d_ok, False, 0.9, [{"越线性": "授权内"}, {"越线性": "授权内"}]) == "修正",
           "derive rule 4：改正案全授权内 → 修正", "", fails)
    _check(derive(d_ok, False, 0.9, [{"越线性": "授权内"}, {"越线性": "触界"}]) == "澄清",
           "derive rule 5：任一触界 → 澄清", "", fails)
    _check(derive(d_ok, False, 0.9, [{"越线性": "未知值"}]) == "澄清",
           "derive rule 5 边界：越线性缺失/未知 → 澄清（宁严不宽）", "", fails)
    _check(derive(d_ok, False, 0.9, []) == "接受", "derive rule 6：其余 → 接受", "", fails)

    # ⑥ 契约结构：12 字段齐 + distribution 键齐且和=1 + 派生自洽
    v = aggregate(hi)
    need = ("verdict", "distribution", "confidence", "dimensions", "veto", "amendments",
            "ambiguity", "band", "needs_human_confirm", "main_judge", "laya", "route_hint")
    _check(all(k in v for k in need), "契约 12 字段齐",
           "缺=%s" % ([k for k in need if k not in v] or "无"), fails)
    _check(set(v["distribution"]) == set(dks), "distribution 键 == 三态（不含「拒绝」）",
           str(sorted(v["distribution"])), fails)
    _check(_WITHDRAW not in v["distribution"], "⭐ D7：`拒绝` 不是 distribution 的键", "", fails)
    _check(THINKING_DIMS and ("T5" not in v["dimensions"]), "⭐ D9：dimensions 不含 T5",
           str(sorted(v["dimensions"])), fails)
    _check(abs(sum(v["distribution"].values()) - 1.0) < 1e-6, "distribution 之和 == 1",
           "%.6f" % sum(v["distribution"].values()), fails)
    _check(abs(v["confidence"] - max(v["distribution"].values())) < 1e-9,
           "⭐ D6：confidence == max(distribution)（恒等，非近似）",
           "conf=%.4f max=%.4f" % (v["confidence"], max(v["distribution"].values())), fails)
    _check(v["band"] == band_of(v["confidence"], v["ambiguity"]),
           "⭐ D19：band == f(confidence) 同源", "band=%s" % v["band"], fails)
    _check(v["main_judge"] == "A" and v["laya"] is None,
           "⭐ D15：main_judge 恒 A（判定权不外流）", "", fails)
    _check(isinstance(v["veto"]["triggered"], bool) and isinstance(v["veto"]["suspect"], bool),
           "⭐ D17：veto.triggered/suspect 恒为 bool（不缺失/不为 null）", "", fails)

    # ⑦ 档位与 fail-closed
    low = aggregate({"T1": 0.5, "T2": 0.5, "T3": 0.5, "T4": 0.5})
    _check(low["band"] == THINKING_BANDS[2] and low["verdict"] == "澄清",
           "全 0.5（无信息）→ band=低 且 verdict=澄清（fail-closed）",
           "conf=%.4f band=%s" % (low["confidence"], low["band"]), fails)
    _check(aggregate(hi)["band"] == THINKING_BANDS[0], "高确定锚例 → band=高",
           "conf=%.4f" % aggregate(hi)["confidence"], fails)
    ambv = aggregate(hi, ambiguity=True)
    _check(ambv["band"] == THINKING_BANDS[2] and ambv["verdict"] == "澄清",
           "ambiguity=true → band 强制=低 且 verdict=澄清",
           "band=%s" % ambv["band"], fails)

    # ⑧ D18：澄清态携带修正案 → needs_human_confirm 必须为 true
    touch = aggregate(hi, amendments=[{"项": "越界项", "越线性": "触界"}])
    _check(touch["verdict"] == "澄清" and touch["needs_human_confirm"] is True,
           "⭐ D18：触界修正案 → 澄清 且 needs_human_confirm=true", "", fails)
    okfix = aggregate(hi, amendments=[{"项": "改法", "越线性": "授权内"}])
    _check(okfix["verdict"] == "修正" and okfix["needs_human_confirm"] is False,
           "授权内修正案 → 修正 且 needs_human_confirm=false", "", fails)

    # ⑨ 单调性：把四维一起拉向接受锚点，接受概率单调不降
    seq = []
    for s in (0.0, 0.25, 0.5, 0.75, 1.0):
        seq.append(aggregate({"T1": s, "T2": s, "T3": s, "T4": s})["distribution"]["接受"])
    _check(all(b >= a - 1e-9 for a, b in zip(seq, seq[1:])), "接受概率随证据单调不降",
           " → ".join("%.3f" % x for x in seq), fails)

    # ⑩ YAML 装配 round-trip
    block = to_yaml_block(v)
    try:
        import yaml
        parsed = yaml.safe_load(block)["思考判定"]
        _check(parsed["verdict"] == v["verdict"] and parsed["laya"] is None
               and abs(sum(parsed["distribution"].values()) - 1.0) < 1e-6
               and len(parsed) == 13,
               "to_yaml_block 可被 yaml 解析且回读一致（13 键 = 12 派生 + 前提审计）",
               "verdict=%s 键数=%d" % (parsed["verdict"], len(parsed)), fails)
    except ImportError:
        print("[SKIP] yaml 未安装 —— 跳过 YAML round-trip（不影响主流程）")

    # ⑪ gold set 10/10（**可分性**证据，非准确率）
    try:
        g = eval_gold(gold_path)
        _check(g["n"] == 10 and g["hit"] == g["n"], "gold set 10/10（可分性证据，非准确率）",
               "命中 %d/%d" % (g["hit"], g["n"]), fails)
        # 必含风险样本：≥2 过拒高危 + ≥4 漏拒高危
        rs = [r["risk_side"] for r in g["rows"]]
        _check(rs.count("过拒高危") >= 2, "gold 含 ≥2 条过拒高危样本",
               "过拒=%d" % rs.count("过拒高危"), fails)
        _check(rs.count("漏拒高危") >= 4, "gold 含 ≥4 条漏拒高危样本",
               "漏拒=%d" % rs.count("漏拒高危"), fails)
        # 重点对照：判例 5（改日期做证据，拒）与 9（手机号打星，收）裁决相反
        by_id = {r["id"]: r for r in g["rows"]}
        _check(by_id.get("G05", {}).get("expected") == "拒绝" and
               by_id.get("G09", {}).get("expected") == "接受",
               "重点对照：G05 拒绝 / G09 接受（判目的不判词面）", "", fails)
        # 铁律：未拟合时不得声称人工标
        d = load_gold(gold_path)
        okr, n_hot = fitted_rule_ok(d)
        _check(okr, "⭐ D12 铁律：anchors_fitted 与人工标样本数自洽",
               "anchors_fitted=%s 人工标=%d(需≥%d)" % (d.get("anchors_fitted"), n_hot,
                                                       THINKING_GOLD_MIN_ANNOTATED), fails)
    except Exception as e:                       # 不静默跳过：报出具体原因
        _check(False, "gold set 可加载并评测", "%s: %s" % (type(e).__name__, e), fails)

    # ⑫ 一致率：分母纪律（D12 / D13）+ 阴性对照
    recs = [
        {"id": "G01", "a": {"verdict": "接受"}, "b": {"verdict": "接受"},
         "laya_status": "ok", "provenance": "human_verified"},
        {"id": "G02", "a": {"verdict": "澄清"}, "b": {"verdict": "接受"},
         "laya_status": "ok", "provenance": "user_spot_checked"},
        {"id": "G03", "a": {"verdict": "接受"}, "b": {"verdict": "接受"},
         "laya_status": "ok", "provenance": "agent_seed"},           # ← 不进分母（D12）
        {"id": "G04", "a": {"verdict": "拒绝"}, "b": None,
         "laya_status": "absent", "provenance": "human_verified"},   # ← 不进分母（D13）
    ]
    rep = agreement(recs)
    _check(rep["n_total"] == 4 and rep["n_comparable"] == 3 and rep["n_excluded"] == 1
           and rep["laya_absent"] == 1,
           "⭐ D13：n_total/n_comparable/n_excluded/laya_absent 正确",
           "total=%d comparable=%d excluded=%d absent=%d"
           % (rep["n_total"], rep["n_comparable"], rep["n_excluded"], rep["laya_absent"]), fails)
    _check(rep["n_qualified"] == 2, "⭐ D12：分母只含人工标 ∩ Laya-ok（=2）",
           "n_qualified=%d" % rep["n_qualified"], fails)
    _check(rep["agreement_rate"] == 0.5, "一致率 = 1/2（G01 同、G02 异）",
           "rate=%s" % rep["agreement_rate"], fails)
    only_seed = agreement([{"id": "S", "a": {"verdict": "接受"}, "b": {"verdict": "修正"},
                            "laya_status": "ok", "provenance": "agent_seed"}])
    _check(only_seed["agreement_rate"] is None,
           "⭐ D12 阴性对照：全 agent_seed → agreement_rate 不可计算（None，非 0.0）",
           "rate=%s" % only_seed["agreement_rate"], fails)

    # ⑬ 阴性对照（两条，指向**两个不同的失效面**）
    #    ⭐ 这里的对照**不能**照抄 homework_model 的「破坏锚点 → 命中数下降」：
    #      本模块 `verdict` 由 `derive` 派生，**不取 argmax(distribution)** ⇒ 破坏锚点只改
    #      `distribution` / `confidence`，**不改** verdict（除非把 confidence 压到 <0.70 触发 rule 3）。
    #      故拆成两个更贴机制的对照：
    try:
        g = eval_gold(gold_path)
        # ⑬a 锚点**确实被消费**：破坏「接受」锚点 → 至少一条样本的 closest/confidence 改变
        broken = {k: dict(v2) for k, v2 in ANCHORS.items()}
        broken["接受"] = dict(ANCHORS["澄清"])
        g_b = eval_gold(gold_path, anchors=broken)
        moved = [a["id"] for a, b in zip(g["rows"], g_b["rows"])
                 if a["closest"] != b["closest"] or a["confidence"] != b["confidence"]]
        _check(bool(moved), "锚点阴性对照：破坏锚点 → 至少一条样本 closest/confidence 改变"
                            "（证明 aggregate 真的消费锚点）",
               "受影响样本 %s" % ("/".join(moved) if moved else "无"), fails)
        # ⑬b 比对器**不是恒真**：把一条期望裁决改坏 → 命中数必须恰好 -1
        mut = [dict(s) for s in load_gold(gold_path).get("samples") or []]
        mut[0]["expected_verdict"] = _WITHDRAW
        g_m = eval_samples(mut)
        _check(g_m["hit"] == g["hit"] - 1,
               "比对器活性对照：改坏一条期望 → 命中数恰 -1（证明 10/10 非恒真）",
               "命中 %d/%d（原 %d/%d）" % (g_m["hit"], g_m["n"], g["hit"], g["n"]), fails)
    except Exception as e:
        _check(False, "阴性对照可跑", "%s: %s" % (type(e).__name__, e), fails)

    # ⑭ 边界：缺维度 / 越界 / tau 非法 必须抛错（不允许静默）
    thrown = 0
    for fn in (lambda: aggregate({"T1": 0.5, "T2": 0.5}),
               lambda: aggregate({"T1": 1.5, "T2": 0.5, "T3": 0.5, "T4": 0.5}),
               lambda: aggregate(hi, tau=0.0),
               lambda: aggregate(hi, tau=-0.1)):
        try:
            fn()
        except ValueError:
            thrown += 1
    _check(thrown == 4, "缺维度 / 越界 / tau≤0 一律抛 ValueError（不静默）",
           "抛出 %d/4" % thrown, fails)

    # ⑮ 跨实现结构约束：B 侧 map_thinking_judge **不得**产出 verdict（判定权在 A，C16/D15）
    try:
        import laya_client as lc
        pinned = lc.PINNED_MODEL_BY_KIND.get(KIND)
        _check(pinned == "multilingual", "⭐ C20/D16：laya_client 已钉 multilingual",
               "pinned=%s" % pinned, fails)
        fake = {
            "answers": {
                "veto": {"noul": 0.1},
                "veto_axis": {"probabilities": {"A5": 0.90, "A1": 0.05, "无": 0.05}},
                "verdict_probe": {"probabilities": {"接受": 0.70, "修正": 0.10,
                                                    "澄清": 0.10, "拒绝": 0.10}},
                "scope_breach": {"noul": 0.1}, "irreversible": {"noul": 0.1},
                "touches_credentials": {"noul": 0.1},
            },
            "routing": {"model": "multilingual", "reason": "pinned", "script_profile": {}},
            "meta": {"latency_ms": 12.3},
        }
        mapped = lc.map_thinking_judge(fake)
        _check(_VERDICT_KEY not in mapped, "⭐ C16：B 侧 map_thinking_judge 不产出 verdict",
               "keys=%s" % "/".join(sorted(mapped)), fails)
        _check(mapped["laya"]["model_key"] == "multilingual"
               and mapped["laya"]["axis_unfounded"] is True,
               "B 侧原料映射：model_key 保留 + noul 不违规却挂轴 → axis_unfounded 留痕",
               "axis_unfounded=%s" % mapped["laya"]["axis_unfounded"], fails)
    except Exception as e:
        _check(False, "laya_client 跨实现结构约束可验", "%s: %s" % (type(e).__name__, e), fails)

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
            raise ValueError("维度格式应为 T1=0.9,T2=0.8,...（出错片段：%r）" % part)
        k, v = part.split("=", 1)
        out[k.strip()] = float(v.strip())
    return out


def main(argv=None):
    p = argparse.ArgumentParser(description="思考裁决聚合模型（thinking-judge 的 Realization A 可执行半边）")
    sub = p.add_subparsers(dest="cmd")

    ag = sub.add_parser("aggregate", help="T1–T4（+veto/ambiguity/amendments）→ 裁决（契约字段）")
    ag.add_argument("--dims", default="", help='形如 "T1=0.9,T2=0.85,T3=0.85,T4=0.85"')
    ag.add_argument("--veto-noul", type=float, default=None, help="Noul 的 P(true) ∈ [0,1]（可选）")
    ag.add_argument("--ambiguity", action="store_true", help="Noul 判模糊/混合")
    ag.add_argument("--amendments", default=None, help="修正案 JSON 数组（可选）")
    ag.add_argument("--tau", type=float, default=TAU, help="softmax 温度（先验，未拟合）")
    ag.add_argument("--anchors-file", default=None, help="锚点 JSON（为将来拟合预留）")
    ag.add_argument("--plan-block", action="store_true", help="额外输出 plan.yaml 的 meta.思考判定 片段")

    gd = sub.add_parser("gold", help="gold set 逐条评测（回归用，非判据）")
    gd.add_argument("--file", default=None, help="默认 assets/thinking-gold.yaml")

    eg = sub.add_parser("agree", help="A/B 一致率报告（影子期出口条件）")
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
        ambs = json.loads(args.amendments) if args.amendments else []
        v = aggregate(_parse_dims(args.dims), args.veto_noul, args.ambiguity, ambs,
                      tau=args.tau, anchors=anchors)
        print(json.dumps(v, ensure_ascii=False, indent=2))
        if args.plan_block:
            print("\n# ---- 可直接粘进 plan.yaml 的 meta 下 ----")
            print(to_yaml_block(v))
        return 0
    if args.cmd == "gold":
        g = eval_gold(args.file)
        print(json.dumps(g, ensure_ascii=False, indent=2))
        print("\n[%s] gold %d/%d（**可分性**证据，非准确率；provenance=%s）"
              % ("PASS" if g["hit"] == g["n"] else "FAIL", g["hit"], g["n"],
                 json.dumps(g["provenance_mix"], ensure_ascii=False)))
        return 0 if g["hit"] == g["n"] else 1
    if args.cmd == "agree":
        with open(args.pairs, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            if raw.get("_note"):
                print("[note] " + str(raw["_note"]))
            records = raw.get("records") or []
        else:
            records = raw
        rep = agreement(records)
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        if args.min_samples is None and args.min_agree is None:
            print("\n[报告] 未给 --min-samples/--min-agree → **只报告不判定**（出口阈值属用户决策项）。")
            return 0
        ms = args.min_samples or 0
        ma = args.min_agree if args.min_agree is not None else 0.0
        rate = rep.get("agreement_rate")
        # ⭐ `rate is None`（分母=0，即无人工标样本）**不得读成 0.0 后靠阈值蒙过**：
        #   "不可计算" ≠ "达标"。故显式判 None → FAIL 并说明原因（假绿纪律）。
        ok = rep["n_total"] >= ms and rate is not None and rate >= ma
        print("\n[%s] n_total=%d(≥%d) agreement_rate=%s(≥%.2f)%s"
              % ("PASS" if ok else "FAIL", rep["n_total"], ms,
                 ("%.4f" % rate) if rate is not None else "不可计算", ma,
                 "  ← 合格分母为 0（尚无人工标样本），一致率不可计算（**不可计算 ≠ 达标**）"
                 if rate is None else ""))
        return 0 if ok else 1
    p.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
