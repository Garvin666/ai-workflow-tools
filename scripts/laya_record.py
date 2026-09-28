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
    """
    le = _load_laya_ensure()
    base = args.base or le.lc.DEFAULT_BASE
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
        raw, rc, err = _call_b(args.judge, state, base)
        data = load_samples(args.file)
        rec_id = args.id if args.id is not None else (len(data["records"]) + 1)
        try:
            rec = build_record(args.judge, args.a, raw, state=state,
                               provenance="agent_seed",      # 纪律②：不允许外部指定
                               rec_id=rec_id, keep_state=args.keep_state)
        except RecordError:
            # B 侧 stdout 非法（客户端崩溃/超时）—— 记 absent，**绝不用 a 顶替**
            rec = build_record(args.judge, args.a, None, state=state,
                               provenance="agent_seed",
                               rec_id=rec_id, keep_state=args.keep_state)
            rec["reason"] = ("B 侧输出非 JSON（rc=%s）：%s" % (rc, (err or "")[:120])).strip()
        rec["runner"] = {"ensure": bool(args.ensure), "launched": launched,
                         "rc": rc, "probe": detail}
        data["records"].append(rec)
        save_samples(args.file, data)
        if args.b_out and raw:
            with open(args.b_out, "w", encoding="utf-8", newline="\n") as f:
                f.write(raw)
        print(json.dumps(rec, ensure_ascii=False, indent=2))
        print("[run] judge=%s laya_status=%s b=%r（b 恒来自 B 侧，未用 a 顶替）；累计 %d 条"
              % (args.judge, rec["laya_status"], rec["b"], len(data["records"])))
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
        return argparse.Namespace(file=f10, judge="self-judge", a=a_val, state="请求原文示例",
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
    finally:
        g["_call_b"], le_mod.probe = old_call, old_probe

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

    rn = sub.add_parser("run", help="一条命令完成采样：探活 → 跑 B 侧 → 与 --a 配对 → 落盘")
    rn.add_argument("--file", required=True, help="样本文件（**不设默认**：默认路径会掩盖写错位置）")
    rn.add_argument("--judge", required=True, choices=sorted(JUDGE_B_FIELD),
                    help="一次只跑一个 judge（多样本请多次调用：不同 judge 的 A 侧取值语义不同，"
                         "合并在一个 --a 里必错）")
    rn.add_argument("--a", required=True, help="A 侧（Realization A）本次判定取值 —— **由调用者给出**，本命令不代判")
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
    if args.cmd == "run":
        return cmd_run(args)
    return selftest()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RecordError as ex:
        print("[FAIL] %s" % ex, file=sys.stderr)
        sys.exit(2)
