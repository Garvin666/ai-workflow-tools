#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# [自研工具] laya_record.py
# 用途：Laya 影子期的**采样落盘**——把每轮判定的「A 侧取值 + B 侧真实返回」配成一条样本记录，
#       原子追加进样本文件；另提供 stats（进度汇总）与 export（导出可喂 agree 的 pairs 文件）。
# 适用场景：影子期出口条件（「N ≥ 50 条 A/B 一致率」）的样本累积与进度报告。
# 作者：ai-workflow 自研（用上Laya-2026-09-28，2026-09-28）
# 仓库：https://github.com/Garvin666/ai-workflow-tools
"""laya_record.py —— Laya 影子期**采样器**（样本生产者）

【定位：务必按此理解，勿误用】
  本脚本**不产判定**、**不接管路由**、**不参与任何放行决策**。它只做一件事：
  把「A 侧（脑内协议 / thinking_model / homework_model）给出的取值」与「B 侧（Laya）**真实返回**」
  配成一条记录，落盘存档。

【为什么需要它（这是本脚本存在的全部理由）】
  `thinking_model.py agree --pairs` 与 `homework_model.py agree --pairs` 都已具备"算一致率"的能力，
  但**没有任何东西在生产 pairs 文件** —— 于是影子期的出口条件「N ≥ 50 条一致率」结构上恒为 0：
  工具解决了"能不能测"，没解决"有没有得测"。本脚本补的就是**样本供给**这一环。

【三条硬纪律（每条都有 selftest 阴性对照）】
  1. **不编造 B 值**：`b` 只能来自 B 侧真实返回（`--b-file` 为首选）。B 侧 `degraded` / 缺席 ⇒
     `b = null` 且 `laya_status ∈ {degraded, absent}` —— **绝不**用 A 值顶替（否则一致率是自己跟自己比）。
  2. **不冒充人工标**：`provenance` 缺省 `agent_seed`（自标）。D12/D13 的分母纪律要求
     `agreement_rate` 的分母**只含** `{user_spot_checked, human_verified}`；缺省值**不得**落在其中。
  3. **不落原文**：`--state` 默认只存 **sha1 指纹**（样本文件含用户请求原文=隐私面）。
     需要原文时显式 `--keep-state`。

【thinking-judge 的特例（v4.11.0 契约）】
  B 侧**刻意不产 `verdict`**（只产 `verdict_probe` + `aux_probes` 旁证）。故该类样本记
  `b_field = "verdict_probe"`、`comparable = false` —— **只留痕，不进一致率**（C16/D15）。

用法：
  # ⭐ 常规路径：一条命令完成「探活 → 跑 B 侧 → 配对 → 落盘」（推荐）
  python scripts/laya_record.py run --file <samples.json> --judge self-judge \
         --a "code" --state "<请求原文>" [--ensure]
  # 低层路径：B 侧已经拿到手（离线复算/取证）时用 append
  python scripts/laya_record.py append --file <samples.json> --judge self-judge \
         --a "code" --b-file <B侧JSON> [--state "<请求原文>"] [--provenance agent_seed]
  python scripts/laya_record.py stats  --file <samples.json>
  python scripts/laya_record.py review --file <samples.json> --id 3 --verdict user_spot_checked \
         --note "<抽查依据>"          # 人工抽查回填：分母的唯一合法升格路径（只改 provenance）
  python scripts/laya_record.py export --file <samples.json> --out <pairs.json> [--judge self-judge]
  python scripts/laya_record.py selftest
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime

# --- 口径单一源：judge → (B 侧承载取值的主字段, 是否可进一致率) -----------------
#     thinking-judge 的 B 侧不产 verdict ⇒ 不可进一致率（契约 C16/D15）。
JUDGE_B_FIELD = {
    "self-judge": ("category", True),
    "method-judge": ("gap_class", True),
    "retrieval-judge": ("source_class", True),
    "homework-judge": ("mode", True),
    "thinking-judge": ("verdict_probe", False),
}
# --- ★ 非 judge 来源（D2，2026-10-08）-------------------------------------------
#     数模决策层（tools/mmdecide.py）的**同源影子采样**：同一份 state、同一批原子问题，
#     分别问「代码基线」与「Laya」，产出 A/B 配对。它不是 ai-workflow 的某份 judge，
#     而是**另一个消费方**在用同一个瓶颈（温度未校准 + 缺人工标），所以复用同一份
#     样本文件与同一套分母纪律，但**必须用独立来源名**，避免与 judge 语义混淆：
#       * `b_field` = 原子问题名（run_needs_review / model_selection / run_stability）
#       * `comparable` = True（A/B 判的是同一份 state，可算一致率）
SHADOW_SOURCES = {
    "mmdecide-shadow": ("<按问题名逐条填>", True),
}
# 合法来源 = judge ∪ 非 judge 影子来源（下游一律读这个并集，不各自硬编码）
ALL_SOURCES = {**JUDGE_B_FIELD, **SHADOW_SOURCES}
# --- 分母纪律（同 thinking_model.agreement 的 D12/D13） -----------------------
QUALIFIED_PROVENANCE = ("user_spot_checked", "human_verified")
ALL_PROVENANCE = QUALIFIED_PROVENANCE + ("agent_seed", "disputed")
DEFAULT_PROVENANCE = "agent_seed"
MULTILINGUAL = "multilingual"
NOTE = "Laya 影子期真机样本（由 scripts/laya_record.py 生产）；仅供 agree 计算**一致率**，非准确率。"


class RecordError(Exception):
    pass


# ---------------------------------------------------------------------------
# 文件读写（原子写）
# ---------------------------------------------------------------------------
def _empty():
    return {"_note": NOTE, "records": []}


def load_samples(path):
    if not os.path.isfile(path):
        return _empty()
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):                      # 兼容纯数组形态
        data = {"_note": NOTE, "records": data}
    if not isinstance(data, dict) or not isinstance(data.get("records"), list):
        raise RecordError("样本文件结构非法：应为 {_note, records:[...]}：%s" % path)
    data.setdefault("_note", NOTE)
    return data


def save_samples(path, data):
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".laya_rec_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, path)                        # 原子替换，避免半写
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _state_sha1(text):
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# B 侧返回 → (b 值, laya_status, model_key, latency_ms, reason)
# ---------------------------------------------------------------------------
def parse_b(judge, raw):
    """raw: B 侧 JSON 文本（`laya_client.py` 的 stdout）。返回取证元组。

    ⚠️ **不编造**：degraded / 取不到字段 ⇒ b 为 None。
    """
    if raw is None:
        return None, "absent", None, None, "未提供 B 侧返回"
    try:
        d = json.loads(raw)
    except Exception as ex:
        raise RecordError("B 侧返回不是合法 JSON：%s" % ex)
    if not isinstance(d, dict):
        raise RecordError("B 侧返回应为 JSON 对象")
    if d.get("degraded") is True:
        return None, "degraded", None, None, str(d.get("reason") or "")[:120]
    laya = d.get("_laya") or {}
    field, _ = JUDGE_B_FIELD[judge]
    val = d.get(field)
    if val is None and isinstance(d.get("laya"), dict):
        val = d["laya"].get(field)
    if val is None:
        return None, "absent", laya.get("model_key"), laya.get("latency_ms"), "B 侧无 `%s` 字段" % field
    return (str(val).strip(), "ok", laya.get("model_key"),
            laya.get("latency_ms"), "")


def build_shadow_record(source, question, a, b, state=None, rec_id=None,
                        prob_a=None, prob_b=None, comp=None, run=None,
                        file=None, task=None, keep_state=False, ts=None):
    """构造一条**同源影子**样本（D2：数模决策层的 A/B 配对）。

    与 ``build_record`` 的关键差异，以及**为什么必须分开**：

    * ``build_record`` 的 B 值来自 **B 侧进程的 stdout**（``parse_b`` 解析 JSON）。
      影子的 B 值来自**调用方内存里的 Answer 对象** —— 没有 JSON 可解，也没有
      ``laya_status`` 可推。硬塞进 ``build_record`` 只会得到一个 ``b=None`` 的残废记录。
    * 但**纪律必须一样**：``provenance`` 仍硬编码 ``agent_seed``（**本函数不接
      provenance 参数** ⇒ 结构上没有冒充人工标的入口），``state`` 仍只落 sha1。

    ``prob_a``/``prob_b`` 另存：一致率只看**取值**是否一致，但"两个都给了 0.9 却
    内部分布完全不同"是真实存在的信息，丢掉就再也查不回来了。
    """
    if source not in SHADOW_SOURCES:
        raise RecordError("未知影子来源：%s（可选 %s）"
                          % (source, "/".join(SHADOW_SOURCES)))
    if a is None or b is None:
        # 半边缺席 ⇒ 不产配对。用 0.0 之类的占位会把"没测到"变成"测得一致"。
        raise RecordError("影子配对要求 a/b 都在场（缺一边就没有对照可言）")
    rec = {
        "id": rec_id,
        "judge": source,
        "a": a,
        "b": b,
        "b_field": question,
        "comparable": True,
        "laya_status": "ok",
        "model_key": MULTILINGUAL,          # 数模决策层已钉死 multilingual
        "d16_violation": False,
        "latency_ms": None,
        "provenance": DEFAULT_PROVENANCE,   # ★ 硬编码：本函数没有 provenance 开关
        "ts": ts or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "reason": None,
        "state_sha1": _state_sha1(state) if state else None,
        "facts": {
            "source": source,
            "comp": comp,
            "run": run,
            "file": file,
            "task": task,
            "prob_a": prob_a,
            "prob_b": prob_b,
            "a_backend": "heuristic",
            "b_backend": "laya",
        },
    }
    if keep_state and state:
        rec["state"] = state
    return rec


def build_record(judge, a, b_raw, state=None, provenance=None, rec_id=None,
                 keep_state=False, ts=None):
    if judge not in JUDGE_B_FIELD:
        raise RecordError("未知 judge：%s（可选 %s）" % (judge, "/".join(JUDGE_B_FIELD)))
    prov = provenance or DEFAULT_PROVENANCE
    if prov not in ALL_PROVENANCE:
        raise RecordError("provenance 非法：%s（可选 %s）" % (prov, "/".join(ALL_PROVENANCE)))
    b, status, model_key, latency, reason = parse_b(judge, b_raw)
    field, comparable_ok = JUDGE_B_FIELD[judge]
    rec = {
        "id": rec_id,
        "judge": judge,
        "a": a,
        "b": b,
        "b_field": field,
        # 可进一致率 = B 侧真的跑通(ok) ∧ a/b 都在 ∧ 该 judge 的 B 侧字段就是主分类
        "comparable": bool(comparable_ok and status == "ok" and a is not None and b is not None),
        "laya_status": status,
        "model_key": model_key,
        # D16：影子跑错模型 ⇒ 对照无效，故即使 status=ok 也须打标
        "d16_violation": bool(status == "ok" and model_key not in (None, MULTILINGUAL)),
        "latency_ms": latency,
        "provenance": prov,
        "ts": ts or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "reason": reason or None,
    }
    if keep_state:
        rec["state"] = state
    elif state:
        rec["state_sha1"] = _state_sha1(state)      # 默认只留指纹（不落原文）
    return rec


# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------
def cmd_append(args):
    data = load_samples(args.file)
    raw = None
    if args.b_file:
        with open(args.b_file, "r", encoding="utf-8") as f:
            raw = f.read()
    elif args.b:
        raw = args.b
    rec_id = args.id if args.id is not None else (len(data["records"]) + 1)
    rec = build_record(args.judge, args.a, raw, state=args.state,
                       provenance=args.provenance, rec_id=rec_id,
                       keep_state=args.keep_state)
    data["records"].append(rec)
    save_samples(args.file, data)
    print(json.dumps(rec, ensure_ascii=False, indent=2))
    print("[append] 已写入 %s；累计 %d 条（可进一致率 %d 条）"
          % (args.file, len(data["records"]),
             sum(1 for r in data["records"] if r.get("comparable"))))
    return 0


def _stats(data):
    recs = data.get("records") or []
    by_judge, by_status, by_prov, by_model = {}, {}, {}, {}
    for r in recs:
        by_judge[r.get("judge")] = by_judge.get(r.get("judge"), 0) + 1
        by_status[r.get("laya_status")] = by_status.get(r.get("laya_status"), 0) + 1
        by_prov[r.get("provenance")] = by_prov.get(r.get("provenance"), 0) + 1
        by_model[str(r.get("model_key"))] = by_model.get(str(r.get("model_key")), 0) + 1
    qualified = [r for r in recs
                 if r.get("laya_status") == "ok" and r.get("comparable")
                 and r.get("a") is not None and r.get("b") is not None
                 and r.get("provenance") in QUALIFIED_PROVENANCE]
    return {
        "n_total": len(recs),
        "n_comparable": sum(1 for r in recs if r.get("comparable")),
        "n_qualified": len(qualified),        # agreement_rate 的真实分母
        "by_judge": by_judge,
        "by_laya_status": by_status,
        "by_provenance": by_prov,
        "by_model_key": by_model,
        "d16_violations": sum(1 for r in recs if r.get("d16_violation")),
        "a_missing": sum(1 for r in recs if r.get("a") is None),
        "gap_to_50": max(0, 50 - len(qualified)),
    }


def cmd_stats(args):
    data = load_samples(args.file)
    st = _stats(data)
    print(json.dumps(st, ensure_ascii=False, indent=2))
    print("[进度] 出口条件为「N ≥ 50 条 A/B 一致率」，且**分母只含人工标**（D12/D13）。")
    print("       当前真实分母 n_qualified = %d，距 50 还差 %d 条；"
          "`agent_seed` 样本（%d 条）**不进分母**，需人工抽查后改 provenance。"
          % (st["n_qualified"], st["gap_to_50"], st["by_provenance"].get("agent_seed", 0)))
    # 把「缺人」这个事实接到**可执行动作**上 —— 否则告警指向的动作无工具承载（出口条件恒不可达）
    seeds = st["by_provenance"].get("agent_seed", 0)
    if st["n_qualified"] == 0 and seeds > 0:
        print("       ⇒ 出口条件当前**不可达**：分母为 0，且没有任何机制会自动产生人工标（缺的不是样本量，是人）。")
        print("         下一步（人工抽查后回填，逐条给 id）：")
        print("           python scripts/laya_record.py review --file \"%s\" --id <样本id> "
              "--verdict user_spot_checked --note \"<抽查依据>\"" % args.file)
    return 0


def cmd_export(args):
    data = load_samples(args.file)
    recs = data.get("records") or []
    if args.judge:
        recs = [r for r in recs if r.get("judge") == args.judge]
    if args.comparable_only:
        recs = [r for r in recs if r.get("comparable")]
    out = {"_note": NOTE, "records": [
        {"id": r.get("id"), "a": r.get("a"), "b": r.get("b"),
         "laya_status": r.get("laya_status"), "provenance": r.get("provenance")}
        for r in recs]}
    save_samples(args.out, out)
    print("[export] %d 条 → %s（可直接喂 `agree --pairs`）" % (len(out["records"]), args.out))
    return 0


def cmd_review(args):
    """人工抽查回填：把 `agent_seed` 升为人工标 —— D12/D13 分母的**唯一**合法升格路径。

    ⚠️ 只改 `provenance` 与抽查元数据；**绝不触碰 a / b / laya_status**（碰了就是编造）。
    ⚠️ **刻意不提供「全部标合格」**：一键全标 = 伪造人工抽查，会让分母彻底失去意义。
    """
    data = load_samples(args.file)
    recs = data.get("records") or []
    ids = []
    if args.id is not None:
        ids.append(args.id)
    if args.ids:
        ids.extend(int(x) for x in args.ids.split(",") if x.strip())
    if not ids:
        raise RecordError("review 需要 --id 或 --ids（**不提供「全部标合格」**：那等于伪造人工抽查）")
    verdict = args.verdict
    if verdict not in ALL_PROVENANCE:
        raise RecordError("verdict 非法：%s（可选 %s）" % (verdict, "/".join(ALL_PROVENANCE)))
    by_id = {r.get("id"): r for r in recs}
    missing = [i for i in ids if i not in by_id]
    if missing:
        raise RecordError("以下 id 不存在（fail-closed，不做部分成功）：%s" % missing)

    changed, refused, notes = [], [], []
    for i in ids:
        r = by_id[i]
        if verdict in QUALIFIED_PROVENANCE:
            # 不可比对的样本不得计入分母（否则一致率被稀释/虚高）
            if not (r.get("laya_status") == "ok" and r.get("a") is not None and r.get("b") is not None):
                refused.append({"id": i, "why": "laya_status=%r / a=%r / b=%r —— 不可比对样本不得计入分母"
                                               % (r.get("laya_status"), r.get("a"), r.get("b"))})
                continue
            if not r.get("comparable"):
                notes.append("id=%s comparable=false（如 thinking-judge）：已标 %s，但**仍不进一致率**（契约 C16/D15）"
                             % (i, verdict))
        r["provenance"] = verdict
        r["reviewed_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if args.note:
            r["review_note"] = args.note
        changed.append(i)
    if changed:
        save_samples(args.file, data)
    st = _stats(load_samples(args.file))
    print(json.dumps({"changed": changed, "refused": refused, "notes": notes,
                      "n_qualified": st["n_qualified"], "gap_to_50": st["gap_to_50"]},
                     ensure_ascii=False, indent=2))
    print("[review] 改 %d 条 / 拒 %d 条；现真实分母 n_qualified = %d（距 50 差 %d）"
          % (len(changed), len(refused), st["n_qualified"], st["gap_to_50"]))
    return 0 if changed else 2


# ---------------------------------------------------------------------------
# worksheet：把「待人工抽查」的样本摊成一份人能照着做的作业单
# ---------------------------------------------------------------------------
def cmd_worksheet(args):
    """生成**人工抽查作业单**（D12/D13 出口条件的唯一人工入口的前置步骤）。

    ⚠️ 本命令**只读**，不写任何 provenance —— 它把"该抽查什么"摆到人面前，
    标不标由人决定。**刻意不提供 `--auto-approve`**：一键全标 = 伪造人工抽查。

    为什么要它：`stats` 已经会把"缺的是人"接到 `review` 命令上，但从**看到命令**
    到**能做出判断**之间还差一步 —— 人需要知道：这条样本问的是什么问题、A 说了什么、
    B 说了什么、当时的输入指纹是什么、判"一致"到底该怎么判。没有这一步，
    `review` 就成了"盲签"，而盲签出来的分母比 0 更糟（它看起来像有依据）。
    """
    data = load_samples(args.file)
    recs = data.get("records") or []
    pending = [r for r in recs
               if r.get("provenance") == DEFAULT_PROVENANCE and r.get("comparable")]

    lines = []
    lines.append("# Laya 影子样本 · 人工抽查作业单")
    lines.append("")
    lines.append("> 本文件由 `laya_record.py worksheet` 生成（只读快照，不会自动更新）。")
    lines.append("> **口径**：这里标的是「A 与 B 的取值是否一致」，**不是**「Laya 判得对不对」——")
    lines.append("> 一致率 **不是准确率**（两者都不是，但混用会把结论说错）。")
    lines.append("")
    lines.append("| 项 | 值 |")
    lines.append("| --- | --- |")
    lines.append("| 样本文件 | `%s` |" % args.file)
    lines.append("| 总记录 | %d |" % len(recs))
    lines.append("| 可比对（comparable） | %d |" % sum(1 for r in recs if r.get("comparable")))
    lines.append("| **待人工抽查**（本单工作面） | **%d** |" % len(pending))
    lines.append("| 当前真实分母 n_qualified | %d |" % _stats(data)["n_qualified"])
    lines.append("")
    if not pending:
        lines.append("## 没有待抽查的样本")
        lines.append("")
        lines.append("要么还没采到可比对样本（跑 `mmdecide --shadow` 或 `laya_record run`），")
        lines.append("要么都已标过。**注意**：已标 ≠ 分母够 —— 出口条件还要 N ≥ 50。")
    else:
        lines.append("## 待抽查样本（逐条独立判断）")
        lines.append("")
        lines.append("**怎么判**：看 `a` 与 `b` 是否为同一取值。")
        lines.append("取值一致 ⇒ 一致；不一致 ⇒ 不一致。**判的是这个，不是对错。**")
        lines.append("")
        for r in pending:
            f = r.get("facts") if isinstance(r.get("facts"), dict) else {}
            lines.append("### id=%s　`%s`%s" % (
                r.get("id"), r.get("judge"),
                ("　·　run=`%s`" % f.get("run")) if f.get("run") else ""))
            lines.append("")
            lines.append("- **问的问题**：`%s`" % (r.get("b_field") or "—"))
            lines.append("- **A（代码基线）说**：`%r`%s" % (
                r.get("a"),
                ("　（概率 %.4f）" % f["prob_a"]) if isinstance(f.get("prob_a"), (int, float)) else ""))
            lines.append("- **B（Laya）说**：`%r`%s" % (
                r.get("b"),
                ("　（概率 %.4f）" % f["prob_b"]) if isinstance(f.get("prob_b"), (int, float)) else ""))
            lines.append("- **一致？**　%s" % ("**取值相同**" if r.get("a") == r.get("b") else "**取值不同**"))
            lines.append("- **B 侧状态**：`%s`（model_key=`%s`，D16 违规=%s）" % (
                r.get("laya_status"), r.get("model_key"), r.get("d16_violation")))
            if f.get("file") or f.get("task") or f.get("comp"):
                lines.append("- **输入指纹**：comp=`%s` task=`%s` file=`%s`" % (
                    f.get("comp"), f.get("task"), f.get("file")))
            if r.get("state_sha1"):
                lines.append("- **state sha1**：`%s`（原文未落盘，这是可复算的锚）" % r["state_sha1"])
            if r.get("ts"):
                lines.append("- **采样时间**：%s" % r["ts"])
            lines.append("")
        lines.append("## 回填（每条单独给 id，无「全部标合格」）")
        lines.append("")
        lines.append("```bash")
        lines.append("# 逐条：确认过 a/b 与输入指纹，且判断为「可信样本」才标")
        lines.append("python scripts/laya_record.py review --file \"%s\" \\" % args.file)
        lines.append("    --id <上表的 id> --verdict user_spot_checked --note \"<你的抽查依据>\"")
        lines.append("```")
        lines.append("")
        lines.append("⚠️ 抽查依据请**具体**（例如「比对了 state_sha1 对应的 run 产物，a/b 取值确如记录」）——")
        lines.append("写「已检查」等于没写。")

    text = "\n".join(lines) + "\n"
    if args.out:
        with open(args.out, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        print("[worksheet] %d 条待抽查 → %s" % (len(pending), args.out))
    else:
        print(text)
    return 0


# ---------------------------------------------------------------------------
# run：一条命令完成「探活 → 跑 B 侧 → 配对 → 落盘」
# ---------------------------------------------------------------------------
LC_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "laya_client.py")


def _load_laya_ensure():
    """同源复用 laya_ensure 的探活/拉起实现 —— **绝不自造第二套就绪判据**。"""
    d = os.path.dirname(os.path.abspath(__file__))
    if d not in sys.path:
        sys.path.insert(0, d)
    import laya_ensure
    return laya_ensure


def _call_b(judge, state, base, timeout=600):
    """子进程调 laya_client 取 B 侧原始输出。返回 (raw_or_None, rc_or_None, err)。"""
    cmd = [sys.executable, LC_PATH, "--judge", judge]
    if state:
        cmd += ["--state", state]
    if base:
        cmd += ["--base", base]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except Exception as e:
        return None, None, "%s: %s" % (type(e).__name__, e)
    out = (r.stdout or b"").decode("utf-8", "replace").strip()
    err = (r.stderr or b"").decode("utf-8", "replace").strip()
    return (out or None), r.returncode, err


def _split_a_list(values):
    """把 `--a` 的取值列表解析成 `[(judge|None, 取值), ...]`（v4.14.0 多 judge 支持）。

    两种形式：**裸取值**（单 judge，靠 `--judge` 补键）与 **`judge=取值`**（自带键，可重复）。
    ⚠️ **不允许混用** —— 混用时「哪条 --a 属于哪个 judge」全靠猜，而猜错会写出一条
    judge 与 A 值错配的样本，**污染一致率分母且事后不可辨**（比报错坏得多）。
    故一律 RecordError 交给调用方 fail-closed。
    """
    flags = [("=" in v) for v in values]
    if any(flags) and not all(flags):
        raise RecordError("--a 不得混用：要么全写 `judge=取值`，要么全写裸取值")
    out = []
    for v in values:
        if "=" in v:
            key, _, val = v.partition("=")
            key = key.strip()
            if key not in JUDGE_B_FIELD:
                raise RecordError("--a 的键不是合法 judge：%s（可选 %s）"
                                  % (key, "/".join(sorted(JUDGE_B_FIELD))))
            if not val:
                raise RecordError("--a %s= 的取值为空" % key)
            out.append((key, val))
        else:
            if not v:
                raise RecordError("--a 取值为空")
            out.append((None, v))
    return out


def cmd_run(args):
    """一条命令完成采样：探活（同源）→ 跑 B 侧 → 与 `--a` 配对 → append。

    ⚠️ 存在的理由：把「用上 Laya」从 5 步降到 1 步。此前 append 只接受**已经拿到手**的
    B 侧文件 ⇒ 调用方要先手工探活、手工跑判定、手工转存 —— 摩擦即漏采（实测：真机样本
    长期停在 0 条）。本命令把这条链路合并，且**三条纪律在代码层锁死**（不靠自觉）：

      ① **不编造 B 值**：`b` 只可能来自 B 侧 stdout；不可达 / 非法 JSON ⇒ `b=None`
         （`laya_status` 落 absent/degraded）。**本命令没有把 `a` 写进 `b` 的任何代码路径。**
      ② **不冒充人工标**：`provenance` **硬编码 agent_seed 且不提供开关** ——
         要进一致率分母只能走 `review --verdict user_spot_checked`（人工抽查）。
      ③ **不落原文**：`state` 只在内存里传给 B 侧，落盘只为 `state_sha1` 指纹
         （`--keep-state` 显式打开才落原文，默认关）。
      ④ **可一次跑多个 judge**（v4.14.0）：`--a judge=取值` 可重复，各 judge **共用同一份
         `--state`/`--state-file`** —— 判的是同一份输入，只是 A 侧取值语义各不同。
         若各 judge 的输入本就不同，**分次调用**，不要硬塞进一次（那会把 state 与 judge 错配）。
    """
    le = _load_laya_ensure()
    base = args.base or le.lc.DEFAULT_BASE

    # --- v4.14.0：先解析 --a 并判参数，**再做探活**（参数错就不该拉起服务）---
    def _argfail(msg):
        print(json.dumps({"ok": False, "stage": "args", "detail": msg},
                         ensure_ascii=False, indent=2), file=sys.stderr)
        return le.EXIT_MISCONFIG

    try:
        jobs = _split_a_list(list(args.a or []))
    except RecordError as ex:
        return _argfail(str(ex))
    keyed = [j for j, _ in jobs if j]
    if keyed and args.judge:
        return _argfail("带键 --a 与 --judge 不得并用（前者已含 judge）")
    if keyed:
        pairs = jobs
    else:
        if not args.judge:
            return _argfail("裸 --a 必须配 --judge；多 judge 请改用 `--a judge=取值`")
        if len(jobs) != 1:
            return _argfail("裸 --a 只允许给一次；多 judge 请改用带键形式")
        pairs = [(args.judge, jobs[0][1])]
    if args.id is not None and len(pairs) > 1:
        return _argfail("多 judge 时 --id 不可手动指定（会撞 id；自动自增）")
    state = None
    if args.state_file:
        with open(args.state_file, "r", encoding="utf-8") as f:
            state = f.read().strip()
    elif args.state:
        state = args.state

    # --- 探活（判据同源，不自造）---
    _, ready, detail = le.probe(base)
    launched = False
    proc = None
    if not ready and args.ensure:
        miss = le.preconditions()
        if miss:
            print(json.dumps({"ok": False, "stage": "preconditions", "missing": miss},
                             ensure_ascii=False, indent=2), file=sys.stderr)
            return le.EXIT_MISCONFIG
        off = le.log_offset()
        proc = le.launch(detach=False)      # 持句柄 ⇒ 本进程存活期间服务一定存活
        launched = True
        ready, detail, elapsed = le.wait_ready(base, args.timeout)
        if not ready:
            le._kill(proc)
            print(json.dumps({"ok": False, "stage": "wait_ready", "detail": detail,
                              "server_log_tail": le._server_log_tail(from_offset=off)},
                             ensure_ascii=False, indent=2), file=sys.stderr)
            return le.EXIT_UNAVAILABLE
        print("[run] 服务不可达 → 已拉起并就绪（%ss）：%s" % (elapsed, detail), file=sys.stderr)

    try:
        n_jobs = len(pairs)
        for idx, (judge, a_val) in enumerate(pairs, 1):
            raw, rc, err = _call_b(judge, state, base)
            data = load_samples(args.file)
            rec_id = args.id if args.id is not None else (len(data["records"]) + 1)
            try:
                rec = build_record(judge, a_val, raw, state=state,
                                   provenance="agent_seed",   # 纪律②：不允许外部指定
                                   rec_id=rec_id, keep_state=args.keep_state)
            except RecordError:
                # B 侧 stdout 非法（客户端崩溃/超时）—— 记 absent，**绝不用 a 顶替**
                rec = build_record(judge, a_val, None, state=state,
                                   provenance="agent_seed",
                                   rec_id=rec_id, keep_state=args.keep_state)
                rec["reason"] = ("B 侧输出非 JSON（rc=%s）：%s" % (rc, (err or "")[:120])).strip()
            rec["runner"] = {"ensure": bool(args.ensure), "launched": launched,
                             "rc": rc, "probe": detail,
                             "batch": ([idx, n_jobs] if n_jobs > 1 else None)}
            data["records"].append(rec)
            save_samples(args.file, data)
            if args.b_out and raw:
                # 多 judge 时按 judge 加后缀，防后一条静默覆盖前一条（丢证据无声无息）
                bpath = args.b_out
                if n_jobs > 1:
                    stem, ext = os.path.splitext(args.b_out)
                    bpath = "%s.%s%s" % (stem, judge, ext or ".json")
                with open(bpath, "w", encoding="utf-8", newline="\n") as f:
                    f.write(raw)
            print(json.dumps(rec, ensure_ascii=False, indent=2))
            print("[run] %sjudge=%s laya_status=%s b=%r（b 恒来自 B 侧，未用 a 顶替）；累计 %d 条"
                  % (("(%d/%d) " % (idx, n_jobs)) if n_jobs > 1 else "",
                     judge, rec["laya_status"], rec["b"], len(data["records"])))
            if rec["laya_status"] != "ok":
                print("      ⚠️ 本次未取到有效 B 值（%s）—— 已如实记为 %s、**不进一致率**；"
                      "不得据此推测 Laya 的判断。"
                      % (rec.get("reason") or "见 laya_status", rec["laya_status"]))
        return 0
    finally:
        if launched:
            le._kill(proc)


# ---------------------------------------------------------------------------
# selftest（含阴性对照）
# ---------------------------------------------------------------------------
def selftest():
    import shutil
    tmpdir = tempfile.mkdtemp(prefix="laya_rec_st_")
    fails = []
    counter = {"n": 0}

    def _check(cond, name, detail=""):
        counter["n"] += 1
        print(("  [OK]   " if cond else "  [FAIL] ") + name + (("  " + detail) if detail and not cond else ""))
        if not cond:
            fails.append(name)

    def _f(name):
        return os.path.join(tmpdir, name)

    good_b = json.dumps({"category": "code", "confidence": 0.69,
                         "_laya": {"model_key": "multilingual", "latency_ms": 788.6}}, ensure_ascii=False)
    deg_b = json.dumps({"degraded": True, "reason": "服务未就绪"}, ensure_ascii=False)
    eng_b = json.dumps({"category": "code", "_laya": {"model_key": "english", "latency_ms": 1.0}})
    think_b = json.dumps({"laya": {"status": "ok", "model_key": "multilingual"},
                          "verdict_probe": "修正",
                          "_laya": {"model_key": "multilingual", "latency_ms": 2807.5}}, ensure_ascii=False)

    print("① append 建文件 + 字段齐全")
    f1 = _f("s1.json")
    r = build_record("self-judge", "code", good_b, state="帮我把三份 CSV 合并", rec_id=1)
    data = _empty(); data["records"].append(r); save_samples(f1, data)
    got = load_samples(f1)
    _check(len(got["records"]) == 1, "记录数 = 1")
    _check(got["records"][0]["b"] == "code" and got["records"][0]["laya_status"] == "ok",
           "b 与 laya_status 取自 B 侧返回")
    _check(got["records"][0]["comparable"] is True, "self-judge 且双值齐 ⇒ comparable")

    print("② 阴性对照：degraded 不得被当成 ok，且不得用 a 顶替 b")
    r2 = build_record("self-judge", "code", deg_b, rec_id=2)
    _check(r2["laya_status"] == "degraded", "status = degraded")
    _check(r2["b"] is None, "b = None（**不编造**）")
    _check(r2["comparable"] is False, "comparable = False")
    r2b = build_record("self-judge", "code", None, rec_id=3)
    _check(r2b["laya_status"] == "absent" and r2b["b"] is None, "无 B 返回 ⇒ absent / b=None")

    print("③ 阴性对照：provenance 缺省必须是 agent_seed，不得落在分母白名单里")
    r3 = build_record("self-judge", "code", good_b, rec_id=4)
    _check(r3["provenance"] == "agent_seed", "缺省 = agent_seed")
    _check(r3["provenance"] not in QUALIFIED_PROVENANCE, "缺省 ∉ 分母白名单（不冒充人工标）")
    try:
        build_record("self-judge", "code", good_b, provenance="human")
        _check(False, "非法 provenance 应被拒绝")
    except RecordError:
        _check(True, "非法 provenance 被拒绝（fail-closed）")

    print("④ thinking-judge：B 侧不产 verdict ⇒ 只留痕、不进一致率")
    r4 = build_record("thinking-judge", "修正", think_b, rec_id=5)
    _check(r4["b"] == "修正" and r4["b_field"] == "verdict_probe", "b 取 verdict_probe")
    _check(r4["comparable"] is False, "comparable = False（不进一致率）")

    print("⑤ D16：model_key=english 且 status=ok ⇒ 打标 d16_violation")
    r5 = build_record("self-judge", "code", eng_b, rec_id=6)
    _check(r5["d16_violation"] is True, "english ⇒ d16_violation=True")
    _check(r3["d16_violation"] is False, "multilingual ⇒ d16_violation=False（阴性对照）")

    print("⑥ 隐私：默认只存 state 指纹，**不得**出现原文")
    r6 = build_record("self-judge", "code", good_b, state="我的手机号是 13800000000", rec_id=7)
    _check("state" not in r6 and "state_sha1" in r6, "默认存指纹不存原文")
    f6 = _f("s6.json")
    d6 = _empty(); d6["records"].append(r6); save_samples(f6, d6)
    raw6 = open(f6, "r", encoding="utf-8").read()
    _check("13800000000" not in raw6, "样本文件内**不含**原文（阴性对照）")
    _check("13800000000" in json.dumps(build_record("self-judge", "code", good_b,
                                                    state="我的手机号是 13800000000",
                                                    keep_state=True), ensure_ascii=False),
           "--keep-state 时才显式保留原文")

    print("⑦ 原子写：写后文件合法、无 .tmp 残留")
    _check(isinstance(load_samples(f1), dict), "写后可 json.load")
    _check(not [n for n in os.listdir(tmpdir) if n.endswith(".tmp")], "无 .tmp 残留")

    print("⑧ stats / export，且 export 结果可被 thinking_model.agreement 消费（跨模块同源）")
    f8 = _f("s8.json")
    d8 = _empty()
    d8["records"] = [build_record("self-judge", "code", good_b, rec_id=1,
                                  provenance="user_spot_checked"),
                     build_record("self-judge", "content", good_b, rec_id=2,
                                  provenance="user_spot_checked"),
                     build_record("self-judge", "code", deg_b, rec_id=3)]
    save_samples(f8, d8)
    st = _stats(load_samples(f8))
    _check(st["n_total"] == 3 and st["n_comparable"] == 2, "stats 计数正确")
    _check(st["n_qualified"] == 2, "人工标 ∧ ok ⇒ n_qualified=2")
    _check(st["gap_to_50"] == 48, "gap_to_50 派生正确（不硬编码）")
    pairs = _f("p.json")
    ns = argparse.Namespace(file=f8, out=pairs, judge=None, comparable_only=False)
    cmd_export(ns)
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import thinking_model as tm
    rep = tm.agreement(json.load(open(pairs, encoding="utf-8"))["records"])
    _check(rep["n_total"] == 3 and rep["n_qualified"] == 2, "被 agreement 消费：n_total=3 / n_qualified=2")
    _check(rep["agreement_rate"] == 0.5, "一致率 = 0.5（1 对 1 错）")
    _check(rep["laya_absent"] == 1, "degraded 样本被排除在分母外（laya_absent=1）")

    print("⑨ review：只改 provenance（绝不碰 a/b）；不可比对样本拒绝升格；无 id 即 fail-closed")
    import io as _io
    from contextlib import redirect_stdout as _rso

    def _quiet(fn):
        with _rso(_io.StringIO()):
            return fn()

    f9 = _f("s9.json")
    d9 = _empty()
    d9["records"] = [build_record("self-judge", "code", good_b, rec_id=1),   # agent_seed
                     build_record("self-judge", "code", deg_b, rec_id=2)]    # degraded
    save_samples(f9, d9)
    _check(_stats(load_samples(f9))["n_qualified"] == 0, "初始分母 = 0（agent_seed + degraded）")
    r_before = load_samples(f9)["records"][0]
    a_before, b_before = r_before["a"], r_before["b"]
    rc = _quiet(lambda: cmd_review(argparse.Namespace(file=f9, id=1, ids=None,
                                                     verdict="user_spot_checked", note="人工核对 B 侧返回")))
    r_after = load_samples(f9)["records"][0]
    _check(rc == 0, "升格成功返回 0")
    _check(r_after["provenance"] == "user_spot_checked", "provenance 已升格")
    _check(r_after["a"] == a_before and r_after["b"] == b_before, "**a/b 一字未动**（只改 provenance）")
    _check(r_after.get("review_note") == "人工核对 B 侧返回" and bool(r_after.get("reviewed_at")),
           "留痕 reviewed_at / review_note")
    _check(_stats(load_samples(f9))["n_qualified"] == 1, "分母 +1")

    rc2 = _quiet(lambda: cmd_review(argparse.Namespace(file=f9, id=2, ids=None,
                                                      verdict="user_spot_checked", note="x")))
    _check(_stats(load_samples(f9))["n_qualified"] == 1, "不可比对的 degraded 样本被拒 ⇒ 分母不变")
    _check(rc2 == 2, "全部被拒 ⇒ 返回 2（fail-closed）", "got %r" % rc2)
    _check(load_samples(f9)["records"][1]["provenance"] == "agent_seed", "被拒记录 provenance 未变")

    try:
        _quiet(lambda: cmd_review(argparse.Namespace(file=f9, id=999, ids=None,
                                                    verdict="user_spot_checked", note="x")))
        _check(False, "不存在的 id 应抛 RecordError")
    except RecordError:
        _check(True, "不存在的 id 被拒（且不做部分成功）")

    try:
        _quiet(lambda: cmd_review(argparse.Namespace(file=f9, id=None, ids=None,
                                                    verdict="user_spot_checked", note="x")))
        _check(False, "无 id 应抛 RecordError")
    except RecordError:
        _check(True, "无 id 被拒（**无「一键全标」后门**）")

    f9b = _f("s9b.json")
    d9b = _empty()
    d9b["records"] = [build_record("thinking-judge", "接受", think_b, rec_id=1)]
    save_samples(f9b, d9b)
    _quiet(lambda: cmd_review(argparse.Namespace(file=f9b, id=1, ids=None,
                                                verdict="user_spot_checked", note="x")))
    _check(_stats(load_samples(f9b))["n_qualified"] == 0, "thinking-judge 标合格仍不进分母（C16/D15）")

    # ⑩ run 的三条纪律 —— **全程注入，零副作用**（绝不真起服务、绝不真发网络请求）
    #    注入点选在 `_call_b`（B 侧唯一入口）与 `laya_ensure.probe`（探活唯一入口）：
    #    若 run 绕过它们去自造判据，下面的断言就会失手 —— 这正是本组对照的效力来源。
    print("⑩ run 三纪律（注入式，零副作用）")
    le_mod = _load_laya_ensure()
    g, old_call, old_probe = globals(), globals()["_call_b"], le_mod.probe
    f10 = _f("s10.json")
    f10b = _f("s10b.json")

    def _mk(a_val="code"):
        # ⚠️ v4.14.0 起 run 的 --a 是 **append 列表**（为支持带键多 judge）——夹具必须跟着边界改，
        #    否则 `list("code")` 会被拆成 4 个裸 job ⇒ 参数校验直接拒绝、一条都不落
        #    （**夹具出偏差时先怀疑夹具**：改谓词/基线，不得改期望值迁就它）
        return argparse.Namespace(file=f10, judge="self-judge", a=[a_val], state="请求原文示例",
                                  state_file=None, base="http://127.0.0.1:1", ensure=False,
                                  timeout=1.0, id=None, keep_state=False, b_out=f10b)

    try:
        le_mod.probe = lambda base=None: (True, True, "injected-ready")
        # (a) 正常路径：b 来自 B 侧，provenance 恒为 agent_seed
        g["_call_b"] = lambda judge, state, base, timeout=600: (good_b, 0, "")
        _quiet(lambda: cmd_run(_mk()))
        r10 = load_samples(f10)["records"][0]
        _check(r10["b"] == "code" and r10["laya_status"] == "ok", "run 正常路径取到 B 值")
        _check(r10["provenance"] == "agent_seed", "run 的 provenance 恒为 agent_seed（纪律②）")
        _check("state" not in r10 and r10.get("state_sha1"), "run 默认只落 state 指纹、不落原文（纪律③）")
        _check(os.path.isfile(f10b), "run --b-out 落盘 B 侧原始输出（取证）")

        # (b) ★ 关键阴性对照：B 侧完全取不到 ⇒ b 必须为 None，**绝不许把 a 顶替进 b**
        g["_call_b"] = lambda judge, state, base, timeout=600: (None, 3, "")
        _quiet(lambda: cmd_run(_mk("code")))
        r10b = load_samples(f10)["records"][1]
        _check(r10b["b"] is None and r10b["laya_status"] in ("absent", "degraded"),
               "B 侧取不到 ⇒ b=None（纪律①：不编造、不用 a 顶替）", "got b=%r" % r10b["b"])
        _check(r10b["a"] == "code", "同一记录里 a 仍原样保留（a/b 不串）")

        # (c) degraded JSON ⇒ 记为 degraded 且不进一致率
        g["_call_b"] = lambda judge, state, base, timeout=600: (deg_b, 3, "")
        _quiet(lambda: cmd_run(_mk()))
        r10c = load_samples(f10)["records"][2]
        _check(r10c["laya_status"] == "degraded" and r10c["b"] is None and not r10c["comparable"],
               "degraded ⇒ b=None 且 comparable=false")

        # (d) 非法 JSON ⇒ 兜底为 absent 并写明 reason（不得抛崩）
        g["_call_b"] = lambda judge, state, base, timeout=600: ("<html>502</html>", 1, "boom")
        _quiet(lambda: cmd_run(_mk()))
        r10d = load_samples(f10)["records"][3]
        _check(r10d["b"] is None and r10d["laya_status"] == "absent" and r10d.get("reason"),
               "B 侧输出非 JSON ⇒ 兜底 absent + reason（不崩）")

        # (e) run 的命令行**结构上不存在** --provenance：人工标只能走 review
        import contextlib
        import io
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                main(["run", "--file", f10, "--judge", "self-judge", "--a", "code",
                      "--provenance", "user_spot_checked"])
            rejected = False
        except SystemExit as ex:
            rejected = (ex.code == 2)          # argparse 对未知参数固定 exit 2
        _check(rejected, "run 拒绝 --provenance（纪律②：无任何冒充人工标的入口）")
        _check(_stats(load_samples(f10))["n_qualified"] == 0,
               "run 写入的样本一律不进一致率分母（n_qualified 仍为 0）")

        # (f) v4.14.0 多 judge 端到端（注入式）：一次调用写两条、保序、A 值不串、纪律②不被批量绕过
        g["_call_b"] = lambda judge, state, base, timeout=600: (
            good_b if judge == "self-judge" else think_b, 0, "")
        ns_f = _mk()
        ns_f.a = ["self-judge=code", "thinking-judge=接受"]
        ns_f.judge = None
        n_before = len(load_samples(f10)["records"])
        _quiet(lambda: cmd_run(ns_f))
        rs = load_samples(f10)["records"]
        _check(len(rs) == n_before + 2 and rs[-2]["judge"] == "self-judge"
               and rs[-1]["judge"] == "thinking-judge",
               "带键 --a 一次写两条、按给定顺序", "got %r" % [(r["judge"], r["a"]) for r in rs[-2:]])
        _check(rs[-2]["a"] == "code" and rs[-1]["a"] == "接受", "两条各自的 A 值未串位")
        _check(all(r["laya_status"] == "ok" for r in rs[-2:]), "两条都取到有效 B 值")
        _check(all(r["provenance"] == "agent_seed" for r in rs[-2:]),
               "多 judge 批量同样恒为 agent_seed（纪律②不被批量绕过）")
        _check(rs[-2]["id"] != rs[-1]["id"], "两条 id 自增、不撞")
    finally:
        g["_call_b"], le_mod.probe = old_call, old_probe

    # ⑪ run 多 judge（v4.14.0）：解析正确 + 三种非法形态必须 fail-closed
    print("⑪ run 多 judge：带键 --a 解析正确；混用/非法键/空取值一律报错")
    _check(_split_a_list(["self-judge=code", "thinking-judge=接受"])
           == [("self-judge", "code"), ("thinking-judge", "接受")],
           "带键 --a 解析为 (judge, 取值) 对，且保序")
    _check(_split_a_list(["code"]) == [(None, "code")],
           "裸 --a 仍解析为 (None, 取值) —— 向后兼容不变")
    for bad, why in [(["self-judge=code", "code"], "混用带键与裸 --a"),
                     (["no-such-judge=x"], "非法 judge 键"),
                     (["self-judge="], "空取值")]:
        try:
            _split_a_list(bad)
            _check(False, "%s 必须报错（阴性对照）" % why)
        except RecordError:
            _check(True, "%s 必须报错（阴性对照）" % why)

    # ⑫ worksheet（D2/A）：作业单必须**真列出**待抽查项，且**不含**任何自动标路径
    print("⑫ worksheet：只读作业单，列出待抽查项；结构上无「一键全标」")
    import io as _io
    f12 = _f("s12.json")
    d12 = _empty()
    # ⚠️ 必须用**有效的 B 侧 JSON** 造数据：b_raw=None 会让 laya_status=absent、
    #    comparable=False ⇒ 样本压根不进待抽查区，测试就会假绿（首版踩过）。
    d12["records"].append(build_record("self-judge", "code", good_b, rec_id=101))
    d12["records"].append(build_record("thinking-judge", "修正", think_b, rec_id=102))
    d12["records"].append(build_record("self-judge", "code", good_b, rec_id=103,
                                       provenance="user_spot_checked"))
    save_samples(f12, d12)
    _buf, _old = _io.StringIO(), sys.stdout
    sys.stdout = _buf
    try:
        _rc_ws = cmd_worksheet(argparse.Namespace(file=f12, out=None))
    finally:
        sys.stdout = _old
    _txt = _buf.getvalue()
    _check(_rc_ws == 0, "worksheet 正常返回 0", "got %r" % _rc_ws)
    # 前提校验：数据里确实有可比对样本（否则下面几条是空跑，会假绿）
    _n_comp = sum(1 for r in load_samples(f12)["records"]
                  if r.get("comparable") and r.get("provenance") == DEFAULT_PROVENANCE)
    _check(_n_comp >= 1, "前提：测试数据含可比对且未标的样本", "n=%d" % _n_comp)
    _check("## 待抽查样本" in _txt and "id=101" in _txt,
           "作业单列出待抽查项（含 id）", _txt[:160].replace("\n", "|"))
    _check("id=103" not in _txt.split("## 回填")[0],
           "已标样本（user_spot_checked）不出现在待抽查区（阴性对照）")
    _check("**取值相同**" in _txt or "**取值不同**" in _txt,
           "逐条给出「一致？」的**机器预判**（人只需复核，不从零判）")
    _check("--verdict user_spot_checked" in _txt, "作业单给出具体回填命令")
    _check("auto-approve" not in _txt and "全部标" not in _txt.replace("无「全部标合格」", ""),
           "作业单**不含**任何一键全标入口（结构上堵死）")
    # 子命令分派：未知子命令不得落回 selftest（同 laya_ensure ⑨ 的缺陷形态）。
    # ⚠️ argparse 会先 raise SystemExit(2)（比我们的分支更早）⇒ 断言「非 0」而非钉死 2。
    _buf2, _old2, _old2e = _io.StringIO(), sys.stdout, sys.stderr
    sys.stdout, sys.stderr = _buf2, _buf2
    try:
        try:
            _rc_unknown = main(["__nope__"])
        except SystemExit as _se:
            _rc_unknown = _se.code
    finally:
        sys.stdout, sys.stderr = _old2, _old2e
    _check(_rc_unknown not in (0, None),
           "未知子命令 ⇒ 非 0 退出（不得落回 selftest）", "got %r" % _rc_unknown)
    _check("项检查" not in _buf2.getvalue(),
           "未知子命令 ⇒ **不**打 selftest 输出（阴性对照）")

    shutil.rmtree(tmpdir, ignore_errors=True)
    # 汇总语由计数器生成，**不硬编码项数**（硬编码会在增删检查项时静默失真）
    print("\n[%s] laya_record selftest：%d 项检查，%d 失败"
          % ("PASS" if not fails else "FAIL", counter["n"], len(fails)))
    return 1 if fails else 0


# ---------------------------------------------------------------------------
def main(argv=None):
    p = argparse.ArgumentParser(description="Laya 影子期采样器（样本生产者）")
    sub = p.add_subparsers(dest="cmd", required=True)

    ap = sub.add_parser("append", help="追加一条 A/B 样本")
    ap.add_argument("--file", required=True, help="样本文件（**不设默认**：默认路径会掩盖写错位置）")
    ap.add_argument("--judge", required=True, choices=sorted(JUDGE_B_FIELD))
    ap.add_argument("--a", default=None, help="A 侧取值（脑内协议 / *_model 的产出）")
    ap.add_argument("--b", default=None, help="B 侧 JSON 文本")
    ap.add_argument("--b-file", default=None, help="B 侧 JSON 文件（优先于 --b）")
    ap.add_argument("--state", default=None, help="请求原文（默认只存 sha1 指纹）")
    ap.add_argument("--keep-state", action="store_true", help="显式保留原文（隐私面，慎用）")
    ap.add_argument("--provenance", default=None,
                    help="标注来源；缺省 %s（**不进一致率分母**）" % DEFAULT_PROVENANCE)
    ap.add_argument("--id", type=int, default=None)

    sp = sub.add_parser("stats", help="进度汇总（不判阈值：出口阈值未定值）")
    sp.add_argument("--file", required=True)

    ep = sub.add_parser("export", help="导出可喂 agree 的 pairs 文件")
    ep.add_argument("--file", required=True)
    ep.add_argument("--out", required=True)
    ep.add_argument("--judge", default=None, choices=sorted(JUDGE_B_FIELD))
    ep.add_argument("--comparable-only", action="store_true")

    rv = sub.add_parser("review", help="人工抽查回填 provenance（分母的唯一合法升格路径）")
    rv.add_argument("--file", required=True)
    rv.add_argument("--id", type=int, default=None)
    rv.add_argument("--ids", default=None, help="逗号分隔，显式枚举（无「全部标合格」选项）")
    rv.add_argument("--verdict", default="user_spot_checked", choices=sorted(ALL_PROVENANCE))
    rv.add_argument("--note", default=None, help="抽查依据（建议填写，落进 review_note）")

    ws = sub.add_parser("worksheet", help="生成人工抽查作业单（只读；把「该抽查什么」摊给人看）")
    ws.add_argument("--file", required=True)
    ws.add_argument("--out", default=None, help="写到文件；缺省打 stdout")
    # ⚠️ 刻意**不提供** --auto-approve / --all：一键全标 = 伪造人工抽查

    rn = sub.add_parser("run", help="一条命令完成采样：探活 → 跑 B 侧 → 与 --a 配对 → 落盘")
    rn.add_argument("--file", required=True, help="样本文件（**不设默认**：默认路径会掩盖写错位置）")
    rn.add_argument("--judge", default=None, choices=sorted(JUDGE_B_FIELD),
                    help="**单 judge 模式**下与裸 --a 配对。⚠️ 与带键 --a 并用即报错（含义不明，宁可报错不猜）")
    rn.add_argument("--a", required=True, action="append",
                    help="A 侧取值 —— **由调用者给出**（本命令不代判）。两种形式：① 裸取值（单 judge，配 --judge）"
                         "② `judge=取值`（v4.14.0 新增，**可重复** → 一次跑多 judge，各 judge 共用同一份 --state）"
                         "　**不得混用**：要么全裸、要么全带键")
    rn.add_argument("--state", default=None, help="请求原文（送给 B 侧当输入；默认不落盘）")
    rn.add_argument("--state-file", default=None, help="从文件读请求原文（长文本/含引号时用）")
    rn.add_argument("--base", default=None, help="Laya 基址，缺省同 laya_client")
    rn.add_argument("--ensure", action="store_true", help="不可达时先拉起服务并等就绪（冷启动约 40s）")
    rn.add_argument("--timeout", type=float, default=180.0, help="--ensure 的等待就绪上限（秒）")
    rn.add_argument("--id", type=int, default=None)
    rn.add_argument("--keep-state", action="store_true",
                    help="⚠️ 把请求原文落盘（默认只存 sha1 指纹；非必要不要开）")
    rn.add_argument("--b-out", default=None, help="把 B 侧原始 JSON 另存一份（取证用）")
    # ⚠️ 刻意**不提供** --provenance：run 是自动采样，人工标只能走 review（纪律②）

    sub.add_parser("selftest", help="离线自证（含阴性对照）")

    args = p.parse_args(argv)
    if args.cmd == "append":
        return cmd_append(args)
    if args.cmd == "stats":
        return cmd_stats(args)
    if args.cmd == "export":
        return cmd_export(args)
    if args.cmd == "review":
        return cmd_review(args)
    if args.cmd == "worksheet":
        return cmd_worksheet(args)
    if args.cmd == "run":
        return cmd_run(args)
    if args.cmd == "selftest":
        return selftest()
    # 未知子命令：**不得**静默 fall through 到 selftest（同 laya_ensure 的 ⑨ 号缺陷）
    print("[laya_record] 未知子命令：%r —— 可用：append / stats / export / review / "
          "worksheet / run / selftest" % (args.cmd,), file=sys.stderr)
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RecordError as ex:
        print("[FAIL] %s" % ex, file=sys.stderr)
        sys.exit(2)
