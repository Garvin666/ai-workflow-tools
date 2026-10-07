# -*- coding: utf-8 -*-
"""[自研工具] knowledge-cram-report / cram_index.py

- 名称：knowledge-cram-report（知识点恶补 PDF 报告机制）
- 用途：恶补索引台账的读写 —— 字面级去重（全新/有增量/纯复习）、FSRS 形状的复习调度、报表派生、归档路径派生
- 适用场景：生成恶补报告前后维护 `恶补\\00-索引.md`；查询「今天该复习哪几条」；派生归档落点
- 仓库链接：https://github.com/Garvin666/ai-workflow-tools

⭐ 本文件是**复习间隔公式的唯一实现**。报告正文、索引、配图、设计文档任何一处出现间隔数字，
都必须由 `interval_days()` 产出 —— 上一轮正是「四处各写各的」导致同一份报告里
正文说 10 天、答案说 4 天（见 修补说明-判据与口径缺陷.md D5）。

⭐ 本文件也是**归档路径的唯一实现**（`place()`）。归档按**项目名**分层：
    <root>/<项目>/<年-月>/<日期>_<任务短名>_恶补报告.{md,pdf}
项目名**必须显式给出**（不给就报错，不猜默认）—— 猜错会静默归档到错误的分区，
而「文件在不在」这种判据查不出「放没放对」。

调度形状来自 open-spaced-repetition/fsrs4anki（MIT）的公开算法，此处**只取形状**、不引入依赖：
    R(t) = (1 + t / (9S))^-1        —— 可提取概率随天数衰减
    interval(S) = round(9 · S · (1/D − 1))，D = 目标留存率
⚠️ 参数（1.9 / 9 / D=0.9）是**未拟合先验**，只保证「间隔随熟练度增长」，不声称最优。
"""
from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

# =====================================================================
# 唯一口径：调度
# =====================================================================
TARGET_RECALL = 0.9          # D：目标可提取率
STABILITY_GROWTH = 1.9       # 每次成功复习的稳定度倍率
FACTOR = 9.0                 # R(t) = (1 + t/(FACTOR*S))^-1 里的时间尺度


def interval_days(stability: float, target_recall: float = TARGET_RECALL) -> int:
    """⭐ 复习间隔的唯一实现。任何地方要间隔数字，都调这里。"""
    if stability <= 0:
        raise ValueError("稳定度必须为正")
    if not (0 < target_recall < 1):
        raise ValueError("目标留存必须在 (0,1) 开区间")
    raw = FACTOR * stability * (1.0 / target_recall - 1.0)
    return int(round(raw))


def advance_stability(stability: float) -> float:
    """一次成功复习后的稳定度。"""
    return round(stability * STABILITY_GROWTH, 4)


def next_review(stability: float, on: date, target_recall: float = TARGET_RECALL) -> date:
    return on + timedelta(days=interval_days(stability, target_recall))


def retention_at(t: float, stability: float) -> float:
    """R(t)：复习后第 t 天的可提取概率（配图用，与 interval_days 同源）。"""
    return (1.0 + t / (FACTOR * stability)) ** -1


def schedule_table(max_n: int = 7, target_recall: float = TARGET_RECALL) -> list[dict]:
    """第 n 次复习的建议间隔序列 —— 供报告 §六 与配图共用，避免各写各的。"""
    rows, s = [], 1.0
    for n in range(1, max_n + 1):
        rows.append({"n": n, "S": round(s, 4), "间隔": interval_days(s, target_recall)})
        s = advance_stability(s)
    return rows


# =====================================================================
# 唯一口径：归档路径
# =====================================================================
NAME_SUFFIX = "_恶补报告"
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
PROJECT_RE = re.compile(r"^[^\\/:*?\"<>|]{1,40}$")


def sanitize(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "_", (name or "").strip())


def place(root: str | Path, project: str, on: str, task: str) -> dict:
    """⭐ 归档落点的唯一实现。项目名必填 —— 不给就报错，不猜默认。"""
    if not project or not str(project).strip():
        raise ValueError("项目名为空。归档按项目名分层，**必须显式给出**（例如 --project ai-workflow）")
    if not DATE_RE.match(on):
        raise ValueError(f"日期须为 YYYY-MM-DD：{on!r}")
    if not PROJECT_RE.match(str(project)):
        raise ValueError(f"项目名含非法字符：{project!r}")
    proj, month = sanitize(str(project)), on[:7]
    short = sanitize(task)
    d = Path(root) / proj / month
    stem = f"{on}_{short}{NAME_SUFFIX}"
    return {"项目": proj, "目录": d, "md": d / f"{stem}.md", "pdf": d / f"{stem}.pdf",
            "figure_prefix": f"{on}_{short}"}


# =====================================================================
# 索引 I/O
# =====================================================================
LEDGER_COLS = ["项目", "日期", "任务", "知识点标题", "关键词", "分级",
               "出现次数", "S", "下次复习", "最近报告"]
REPORT_COLS = ["项目", "日期", "报告", "知识点数", "全新", "有增量", "纯复习"]
NUM_COLS = {"出现次数", "知识点数", "全新", "有增量", "纯复习"}

NOTE_BLOCK = """# 恶补索引

> 本文件是「知识点恶补 PDF 报告机制」的**唯一去重事实源**与**复习调度台账**。
> ⚠️ **由 `cram_index.py` 维护，请勿手改**——手改会被下次写入覆盖，且会绕过数值单源。
>
> **归档结构**（按项目名分层，项目名取该任务的工作区名或显式 `--project`）：
> ```
> 恶补\\<项目名>\\<年-月>\\<日期>_<任务短名>_恶补报告.md / .pdf
> 恶补\\_assets\\                     # 全项目共用的模板与配图根
> ```
> **三态处置**：全新（新建行）／有增量（正文追加「补充」小节，出现次数 +1）／纯复习（不重写正文，仅推进复习日）。
> **调度**：`S ← S × 1.9`，`间隔 = round(9 · S · (1/0.9 − 1)) = round(S)`，首次 `S = 1`。
> ⚠️ 去重是**字面级**（同义改写会漏），本版不做语义级。
> ⚠️ 参数（1.9 / 9 / 0.9）是**未拟合先验**，只保证间隔随熟练度增长，不声称最优。
> ⚠️ 数值单源：任何间隔数字都出自 `cram_index.interval_days`，判据 V9 会实跑核对。"""


def _norm_key(s: str) -> str:
    """去重用的归一化：去空白与常见标点，转小写。"""
    s = re.sub(r"[\s　]+", "", s or "")
    s = re.sub(r"[，。、；：！？,.;:!?「」『』（）()\[\]【】—\-_/／]", "", s)
    return s.lower()


def _header_cells(ln: str) -> list[str]:
    return [c.strip() for c in ln.strip().strip("|").split("|")]


def _is_sep(cells: list[str]) -> bool:
    return bool(cells) and all(set(c) <= set("-: ") for c in cells)


def _parse_sections(lines: list[str], cols: list[str]) -> list[dict]:
    """按表头列名解析（而非按固定下标）——这样列顺序或新增列都不会错位。"""
    out = []
    for i, ln in enumerate(lines):
        if not ln.strip().startswith("|"):
            continue
        head = _header_cells(ln)
        if head[:len(cols)] != cols:
            continue
        j = i + 1
        if j < len(lines) and _is_sep(_header_cells(lines[j])):
            j += 1
        while j < len(lines) and lines[j].strip().startswith("|"):
            cells = _header_cells(lines[j])
            if _is_sep(cells):
                j += 1
                continue
            rec = {}
            for k, col in enumerate(cols):
                rec[col] = cells[k] if k < len(cells) else ""
            out.append(rec)
            j += 1
        break
    return out


def parse_index(path: Path) -> dict:
    """解析索引。**表头驱动**，因此兼容旧版（无「项目」列）的 8/9 列台账。"""
    if not path.exists():
        return {"items": [], "reports": []}
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    raw_items = _parse_sections(lines, LEDGER_COLS)
    raw_reports = _parse_sections(lines, REPORT_COLS)

    items = []
    for r in raw_items:
        n = r.get("出现次数", "")
        s = r.get("S", "")
        items.append({
            "项目": r.get("项目", "") or "（未分类）",
            "日期": r.get("日期", ""), "任务": r.get("任务", ""),
            "标题": r.get("知识点标题", ""), "关键词": r.get("关键词", ""),
            "分级": r.get("分级", "C"),
            "出现次数": int(n) if n.isdigit() else 1,
            "S": float(s) if re.match(r"^[\d.]+$", s or "") else 1.0,
            "下次复习": r.get("下次复习", ""), "最近报告": r.get("最近报告", ""),
        })
    reports = []
    for r in raw_reports:
        rec = {"项目": r.get("项目", "") or "（未分类）", "日期": r.get("日期", ""),
               "报告": r.get("报告", "")}
        for c in ("知识点数", "全新", "有增量", "纯复习"):
            v = r.get(c, "")
            rec[c] = int(v) if v.isdigit() else 0
        reports.append(rec)

    seen, uniq = set(), []
    for it in items:
        k = _norm_key(it["标题"])
        if k in seen:
            continue
        seen.add(k)
        uniq.append(it)
    return {"items": uniq, "reports": reports}


def _render(payload: dict, note_block: str = NOTE_BLOCK) -> str:
    items, reports = payload["items"], payload["reports"]
    out = [note_block, "", "## 知识点台账", "",
           "| " + " | ".join(LEDGER_COLS) + " |",
           "| --- | --- | --- | --- | --- | --- | ---: | ---: | --- | --- |"]
    for it in sorted(items, key=lambda x: (x["项目"], x["日期"], x["标题"])):
        out.append("| " + " | ".join([
            it["项目"], it["日期"], it["任务"], it["标题"], it["关键词"], it["分级"],
            str(it["出现次数"]), f"{it['S']:.1f}", it["下次复习"], it.get("最近报告", ""),
        ]) + " |")

    grade = {g: sum(1 for it in items if it["分级"] == g) for g in ("A", "B", "C")}
    due_dates = sorted(it["下次复习"] for it in items if it["下次复习"])
    latest = reports[-1] if reports else {}

    out += ["", "## 报表", "", "| 项 | 值 |", "| --- | --- |",
            f"| 报告总数 | {len(reports)} |",
            f"| 知识点总数 | {len(items)} |",
            f"| 项目数 | {len({it['项目'] for it in items})} |",
            f"| 最近一轮 全新 / 有增量 / 纯复习 | {latest.get('全新', 0)} / {latest.get('有增量', 0)} / {latest.get('纯复习', 0)} |",
            f"| 分级分布 | A {grade['A']} ｜ B {grade['B']} ｜ C {grade['C']} |",
            f"| 最早到期日 | {due_dates[0] if due_dates else '—'} |",
            f"| 台账最后更新 | {datetime.now().strftime('%Y-%m-%d %H:%M')} |"]

    out += ["", "### 按项目", "", "| 项目 | 报告数 | 知识点数 | 最早到期日 |",
            "| --- | ---: | ---: | --- |"]
    for proj in sorted({i["项目"] for i in items} | {r["项目"] for r in reports}):
        pi = [i for i in items if i["项目"] == proj]
        pr = [r for r in reports if r["项目"] == proj]
        dd = sorted(i["下次复习"] for i in pi if i["下次复习"])
        out.append(f"| {proj} | {len(pr)} | {len(pi)} | {dd[0] if dd else '—'} |")

    out += ["", "## 报告目录", "", "| " + " | ".join(REPORT_COLS) + " |",
            "| --- | --- | --- | ---: | ---: | ---: | ---: |"]
    for r in reports:
        out.append("| " + " | ".join([
            r["项目"], r["日期"], r["报告"], str(r.get("知识点数", 0)),
            str(r.get("全新", 0)), str(r.get("有增量", 0)), str(r.get("纯复习", 0)),
        ]) + " |")
    return "\n".join(out) + "\n"


def write_index(path: Path, payload: dict, note_block: str = NOTE_BLOCK) -> None:
    """原子写：先写 .tmp 再替换（防半截文件）。"""
    text = _render(payload, note_block)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    tmp.replace(path)


# =====================================================================
# 三态去重
# =====================================================================
def classify(items: list[dict], existing: list[dict], report_id: str, on: date,
             project: str | None = None) -> dict:
    """把候选知识点分成 全新 / 有增量 / 纯复习，并算出新的台账行。

    ⭐ 幂等哨兵：若某行的「最近报告」已等于本次 report_id，则判为**重跑同一份报告**，
    原地更新、**不推进出现次数与 S** —— 否则一次半成功重跑就会重复计数。

    ⚠️ `existing` 一律 **深拷贝**后再改：初版直接 `list(existing)` 只复制了外层，
    行字典仍是同一对象 ⇒ 连续对本函数返回值再判一次时，上一轮写入的计数会污染下一轮判定
    （自测 NC4/NC5 就是这样报出来的）。函数不改写调用方的输入，这是硬约束。
    """
    existing = copy.deepcopy(existing)
    by_key = {_norm_key(it["标题"]): it for it in existing}
    by_kw = {}
    for it in existing:
        for k in (it["关键词"] or "").split("/"):
            k = _norm_key(k)
            if k:
                by_kw.setdefault(k, it)

    new_items = list(existing)
    result = {"全新": [], "有增量": [], "纯复习": [], "幂等命中": [], "匹配到的既有标题": {}}

    for cand in items:
        tk = _norm_key(cand["标题"])
        hit = by_key.get(tk)
        if hit is None:
            for k in (cand.get("关键词") or "").split("/"):
                k = _norm_key(k)
                if k and k in by_kw:
                    hit = by_kw[k]
                    break
        is_increment = bool(cand.get("有增量"))
        proj = sanitize(cand.get("项目") or project or "")

        if hit is None:
            row = {"项目": proj or "（未分类）", "日期": on.isoformat(), "任务": cand["任务"],
                   "标题": cand["标题"], "关键词": cand.get("关键词", ""),
                   "分级": cand.get("分级", "C"), "出现次数": 1, "S": 1.0,
                   "下次复习": next_review(1.0, on).isoformat(), "最近报告": report_id}
            new_items.append(row)
            result["全新"].append(cand["标题"])
            continue

        result["匹配到的既有标题"][cand["标题"]] = hit["标题"]
        if hit.get("最近报告") == report_id:
            # 幂等：同一份报告重跑（report_id 由 <日期>_<任务> 构成，本身即唯一）
            hit["关键词"] = cand.get("关键词") or hit["关键词"]
            hit["分级"] = cand.get("分级") or hit["分级"]
            result["幂等命中"].append(cand["标题"])
            continue

        # 命中 => 一次复习：出现次数 +1、稳定度推进（不论是否带增量）
        hit["出现次数"] += 1
        hit["S"] = advance_stability(hit["S"])
        hit["下次复习"] = next_review(hit["S"], on).isoformat()
        hit["日期"] = on.isoformat()
        hit["任务"] = cand["任务"]
        # ⭐ 归属**固定在首次捕获的项目线**（origin wins）：跨线复现只推进复习，
        #    不搬动「项目」。若改成「最近一次出现者胜」，一条知识会随复盘在项目间漂移，
        #    原项目的台账会凭空少行 —— 而「按项目名分类」要的正是稳定的归属。
        #    「最近报告」列仍如实记录最近一次出现的位置。
        hit["分级"] = cand.get("分级") or hit["分级"]
        hit["关键词"] = cand.get("关键词") or hit["关键词"]
        hit["最近报告"] = report_id
        if is_increment:
            result["有增量"].append(cand["标题"])
        else:
            result["纯复习"].append(cand["标题"])

    return {"items": new_items, "三态": result}


def due_items(items: list[dict], on: date) -> list[dict]:
    out = []
    for it in items:
        try:
            d = date.fromisoformat(it["下次复习"])
        except (ValueError, TypeError):
            continue
        if d <= on:
            out.append({**it, "逾期天数": (on - d).days})
    return sorted(out, key=lambda x: x["下次复习"])


def verify_index(items: list[dict], reports: list[dict] | None = None) -> dict:
    """台账自洽性校验（V11 的血缘）。两条不变量：

    ① 每行的 `S` 必须能由**它自己的**「出现次数」递推得到：`S == 1.9^(出现次数-1)`。
    ② 每行的 `下次复习` 必须由**它自己的** `S` 与 `日期` 派生：`日期 + interval_days(S)`。

    ⭐ 专抓的失效形态＝**行间串扰**：把 S 当成在表里逐行推进的游标。迁移脚本第一版
    就是这样手搓台账的——7 条**全新**条目拿到了 `1.0/1.9/3.6/6.9/13.0/24.8/47.0` 的
    递进 S，而它们每条的「出现次数」都是 1。**这类错不会抛异常、也不会让判据变红**
    （S 与「下次复习」各自内部还是自洽的），只会让复习调度整体偏晚——所以必须显式校验。

    ⚠️ 容差 0.06：台账把 S 渲染成 1 位小数（`_render` 用 `:.1f`），
    `1.9^n` 的截断误差最大约 0.05（如 6.859→6.9、47.05→47.0）。
    """
    issues: list[dict] = []
    if not items:
        return {"判据": "台账自洽（S 由出现次数递推、下次复习由 S 派生）",
                "结论": "SKIP", "证据": "台账无数据行（未检测 ≠ 通过）",
                "行数": 0, "问题数": 0, "问题": []}
    for it in items:
        n = max(1, int(it.get("出现次数") or 1))
        s_expect = 1.0
        for _ in range(n - 1):
            s_expect = advance_stability(s_expect)
        s_actual = float(it.get("S") or 1.0)
        if abs(s_actual - s_expect) > 0.06:
            issues.append({
                "行": it.get("标题"), "字段": "S", "实际": it.get("S"),
                "应然": round(s_expect, 4),
                "依据": f"出现次数={n} ⇒ S 应为 1.9^{n - 1}={s_expect:.4f}"
                        f"（若按行序递进会得到别的值 ⇒ 行间串扰）"})
        nr = it.get("下次复习")
        if nr:
            try:
                base = date.fromisoformat(it["日期"])
                want = next_review(s_actual, base).isoformat()
            except (ValueError, KeyError, TypeError):
                issues.append({"行": it.get("标题"), "字段": "下次复习", "实际": nr,
                               "应然": "—", "依据": "「日期」或「S」不可解析"})
            else:
                if nr != want:
                    issues.append({"行": it.get("标题"), "字段": "下次复习",
                                   "实际": nr, "应然": want,
                                   "依据": f"日期 {it.get('日期')} + interval_days({it.get('S')})"})
    return {"判据": "台账自洽（S 由出现次数递推、下次复习由 S 派生）",
            "结论": "PASS" if not issues else "FAIL",
            "证据": f"台账 {len(items)} 行；问题 {len(issues)} 处",
            "行数": len(items), "问题数": len(issues), "问题": issues}


# =====================================================================
# 自测（阴性对照优先：判据必须能报红）
# =====================================================================
def selftest() -> int:
    import tempfile

    fails = []

    def expect(cond: bool, name: str, detail: str = "") -> None:
        print(f"[{'PASS' if cond else 'FAIL'}] {name} {detail}")
        if not cond:
            fails.append(name)

    # --- 单源与调度形状 ---
    expect(interval_days(1.0) == 1, "ST1 间隔 S=1 → 1 天", f"实际={interval_days(1.0)}")
    expect(interval_days(1.9) == 2, "ST2 间隔 S=1.9 → 2 天（旧设计文档写 4 天，是错的）",
           f"实际={interval_days(1.9)}")
    expect(interval_days(3.61) == 4, "ST3 间隔 S=3.61 → 4 天", f"实际={interval_days(3.61)}")
    expect(advance_stability(1.0) == 1.9, "ST4 稳定度推进 ×1.9")
    tab = schedule_table(7)
    expect([r["间隔"] for r in tab] == [1, 2, 4, 7, 13, 25, 47],
           "ST5 三次间隔序列与配图口径一致", str([r["间隔"] for r in tab]))
    expect(len({(r["S"], r["间隔"]) for r in tab}) == 7, "ST6 序列无重复点")
    try:
        interval_days(0)
        expect(False, "ST7 稳定度 0 应抛错")
    except ValueError:
        expect(True, "ST7 稳定度 0 应抛错")

    # --- 归档路径（项目分层）---
    p = place(r"C:\root", "量化分析平台", "2026-10-07", "设计-知识点恶补PDF报告机制")
    expect(str(p["目录"]) == r"C:\root\量化分析平台\2026-10", "ST9 归档目录 = 根/项目/年-月",
           str(p["目录"]))
    expect(p["pdf"].name == "2026-10-07_设计-知识点恶补PDF报告机制_恶补报告.pdf",
           "ST10 文件名规范", p["pdf"].name)
    try:
        place(r"C:\root", "", "2026-10-07", "t")
        expect(False, "NC12 空项目名应报错（不猜默认）")
    except ValueError:
        expect(True, "NC12 空项目名应报错（不猜默认）")
    try:
        place(r"C:\root", "a/b", "2026-10-07", "t")
        expect(False, "NC13 项目名含非法字符应报错")
    except ValueError:
        expect(True, "NC13 项目名含非法字符应报错")
    p3 = place(r"C:\root", "工作流", "2026-10-07", "a: b? c")
    expect(":" not in p3["pdf"].name and "?" not in p3["pdf"].name, "ST11 任务短名中的非法字符被替换",
           p3["pdf"].name)

    # --- 三态 ---
    on = date(2026, 10, 7)
    ex = [{"项目": "工作流", "日期": "2026-09-30", "任务": "T1", "标题": "依赖不在版本控制里",
           "关键词": "环境可复现/前置自检", "分级": "C", "出现次数": 1, "S": 1.0,
           "下次复习": "2026-10-08", "最近报告": "2026-09-30_T1"}]
    r1 = classify([{"标题": "全新的一条", "关键词": "甲/乙", "分级": "C", "任务": "T2"}], ex,
                  "2026-10-07_T2", on, project="工作流")
    expect(r1["三态"]["全新"] == ["全新的一条"] and len(r1["items"]) == 2,
           "NC1 未命中标题与关键词 → 全新（新增一行）")

    r2 = classify([{"标题": "依赖不在版本控制里", "关键词": "环境可复现/前置自检",
                    "分级": "B", "任务": "T2", "有增量": True}], ex, "2026-10-07_T2", on,
                  project="工作流")
    hit = [i for i in r2["items"] if i["标题"] == "依赖不在版本控制里"][0]
    expect(r2["三态"]["有增量"] == ["依赖不在版本控制里"] and hit["出现次数"] == 2
           and abs(hit["S"] - 1.9) < 1e-6, "NC2 标题命中 + 有增量 → 次数+1、S×1.9")
    expect(hit["下次复习"] == "2026-10-09", "NC3 下次复习 = 当日 + round(1.9) = +2 天",
           hit["下次复习"])

    r3 = classify([{"标题": "依赖不在版本控制里", "关键词": "环境可复现", "分级": "B",
                    "任务": "T2"}], ex, "2026-10-07_T2", on, project="工作流")
    expect(r3["三态"]["纯复习"] == ["依赖不在版本控制里"] and len(r3["items"]) == 1,
           "NC4 标题命中且无增量 → 纯复习（不新增行）")
    expect(r3["items"][0]["出现次数"] == 2, "NC4b 纯复习同样算一次复习 ⇒ 次数 +1")

    r4 = classify([{"标题": "依赖不在版本控制里", "关键词": "环境可复现", "分级": "C",
                    "任务": "T1"}], ex, "2026-09-30_T1", on, project="工作流")
    expect(r4["三态"]["幂等命中"] == ["依赖不在版本控制里"]
           and r4["items"][0]["出现次数"] == 1,
           "NC5 同一 report_id 重跑 → 幂等命中，不推进计数")

    r5 = classify([{"标题": "前复现口径一致", "关键词": "只认步骤自己写下的验证方式",
                    "分级": "C", "任务": "T2"}], ex, "2026-10-07_T2", on, project="工作流")
    expect(r5["三态"]["全新"] == ["前复现口径一致"], "PC1 阳性对照：真新条目仍能进入")

    # --- 跨项目复现：归属固定在首次捕获的项目线（origin wins），只推进复习 ---
    ex2 = [{"项目": "Laya", "日期": "2026-09-28", "任务": "T0",
            "标题": "推送是不可逆的对外动作", "关键词": "对外发布/逐次授权", "分级": "C",
            "出现次数": 1, "S": 1.0, "下次复习": "2026-09-29", "最近报告": "2026-09-28_T0"}]
    r6 = classify([{"标题": "推送是不可逆的对外动作", "关键词": "对外发布", "分级": "C",
                    "任务": "T3"}], ex2, "2026-10-07_T3", date(2026, 10, 7), project="建模")
    hit6 = next(it for it in r6["items"] if it["标题"] == "推送是不可逆的对外动作")
    expect(hit6["项目"] == "Laya", "NC20 跨项目复现不搬动归属（origin wins）",
           f"项目={hit6['项目']}")
    expect(hit6["最近报告"] == "2026-10-07_T3", "NC20b 最近报告仍记录最近一次出现")
    expect(hit6["出现次数"] == 2, "NC20c 跨项目复现仍算一次复习（次数 +1）")

    # --- 非破坏性 ---
    expect(ex[0]["出现次数"] == 1 and ex[0]["S"] == 1.0,
           "ST12 classify 不改写调用方输入（深拷贝）",
           f"出现次数={ex[0]['出现次数']} S={ex[0]['S']}")

    # --- 幂等写盘 ---
    with tempfile.TemporaryDirectory() as td:
        idx = Path(td) / "00-索引.md"
        payload = {"items": r2["items"], "reports": [
            {"项目": "工作流", "日期": "2026-10-07", "报告": "2026-10-07_T2",
             "知识点数": 1, "全新": 0, "有增量": 1, "纯复习": 0}]}
        write_index(idx, payload)
        sha1 = __import__("hashlib").sha256(idx.read_bytes()).hexdigest()
        write_index(idx, payload)
        sha2 = __import__("hashlib").sha256(idx.read_bytes()).hexdigest()
        expect(sha1 == sha2, "ST8 同 payload 连写两次字节一致（幂等）")
        # 回读断言：解析回来的项目与计数必须与写入一致（防"写了但结构变了"）
        back = parse_index(idx)
        expect(back["items"][0]["项目"] == "工作流" and back["items"][0]["出现次数"] == 2,
               "ST13 回读断言：项目列与出现次数往返一致",
               json.dumps(back["items"][0], ensure_ascii=False))
        # ⭐ 报告目录**也**必须往返：`_render` 写的表头列数与 `REPORT_COLS` 必须一致，
        #    否则 parse 永远读不回来 ⇒ 「报告总数」恒为 1、每次 upsert 都丢掉上一份报告行。
        expect(len(back["reports"]) == 1 and back["reports"][0]["报告"] == "2026-10-07_T2",
               "ST14 报告目录往返：写 1 条 ⇒ 读回 1 条（列数须与 REPORT_COLS 一致）",
               f"读回 {len(back['reports'])} 条")

    # --- 台账自洽（V11 血缘）：专抓「行间串扰」——把 S 当成逐行推进的游标 ---
    base = {"项目": "工作流", "日期": "2026-10-07", "任务": "T", "标题": "甲",
            "关键词": "x", "分级": "C", "出现次数": 1, "S": 1.0,
            "下次复习": "2026-10-08", "最近报告": "R"}
    expect(verify_index([base])["结论"] == "PASS", "PC14 全新条目 S=1.0、下次复习=+1 天 ⇒ 自洽")
    # 真缺陷形态：7 条**全新**条目被写成逐行递进的 S（迁移脚本第一版就是这么错的）。
    # 每行「下次复习」都按它自己的 S 派生（内部自洽）——所以**只有** S 与出现次数的矛盾能抓出来。
    drift = []
    for i, s in enumerate([1.0, 1.9, 3.6, 6.9, 13.0, 24.8, 47.0]):
        drift.append(dict(base, 标题=f"第{i}条", S=s,
                          下次复习=next_review(s, date(2026, 10, 7)).isoformat()))
    expect(verify_index(drift)["结论"] == "FAIL", "NC18 行间串扰：出现次数=1 却带递进 S ⇒ 必须报红")
    expect(verify_index(drift)["问题数"] == 6, "NC18b 除首行外 6 条应被逐行点出")
    expect(verify_index([dict(base, 出现次数=2, S=1.9, 下次复习="2026-10-09")])["结论"] == "PASS",
           "PC14b 出现次数=2、S=1.9 ⇒ 自洽")
    expect(verify_index([dict(base, 下次复习="2026-10-20")])["结论"] == "FAIL",
           "NC19 下次复习与 S 不一致 ⇒ 报红")
    expect(verify_index([])["结论"] == "SKIP", "V11 空台账判 SKIP（未检测 ≠ 通过）")

    print("")
    print(f"=== 自测结果：FAIL={len(fails)} ===")
    return 1 if fails else 0


# =====================================================================
# CLI
# =====================================================================
def _load_items(spec: str) -> list[dict]:
    p = Path(spec)
    raw = p.read_text(encoding="utf-8") if p.exists() else spec
    data = json.loads(raw)
    if isinstance(data, dict):
        data = data.get("items", [])
    return data


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="恶补索引台账（去重 + FSRS 调度 + 归档路径 + 报表）")
    sub = ap.add_subparsers(dest="cmd")

    up = sub.add_parser("upsert", help="写入一轮报告的知识点（三态判定）")
    up.add_argument("--index", required=True)
    up.add_argument("--project", required=True, help="项目名（归档分区；必填，不猜默认）")
    up.add_argument("--date", default=date.today().isoformat())
    up.add_argument("--task", required=True)
    up.add_argument("--report-id", default=None, help="缺省为 <date>_<task>")
    up.add_argument("--items", required=True, help="JSON 文件路径或内联 JSON")
    up.add_argument("--dry-run", action="store_true")

    du = sub.add_parser("due", help="列出到期该复习的知识点")
    du.add_argument("--index", required=True)
    du.add_argument("--on", default=date.today().isoformat())
    du.add_argument("--project", default=None)

    sc = sub.add_parser("schedule", help="打印间隔序列（唯一口径源）")
    sc.add_argument("--max-n", type=int, default=7)

    vf = sub.add_parser("verify", help="台账自洽性校验（S 由出现次数递推、下次复习由 S 派生）")
    vf.add_argument("--index", required=True)

    pl = sub.add_parser("place", help="打印归档落点（唯一口径源）")
    pl.add_argument("--root", required=True)
    pl.add_argument("--project", required=True)
    pl.add_argument("--date", required=True)
    pl.add_argument("--task", required=True)

    sub.add_parser("selftest", help="阴性对照自测")
    ap.add_argument("--json", action="store_true", help="（顶层）输出 JSON")
    args = ap.parse_args(argv)

    if args.cmd == "selftest":
        return selftest()
    if args.cmd == "schedule":
        tab = schedule_table(args.max_n)
        if args.json:
            print(json.dumps(tab, ensure_ascii=False, indent=2))
        else:
            print("| 第几次 | S | 间隔（天） |")
            print("| ---: | ---: | ---: |")
            for r in tab:
                print(f"| {r['n']} | {r['S']:.1f} | {r['间隔']} |")
        return 0
    if args.cmd == "place":
        p = place(args.root, args.project, args.date, args.task)
        if args.json:
            print(json.dumps({k: str(v) for k, v in p.items()}, ensure_ascii=False, indent=2))
        else:
            print(f"目录 : {p['目录']}")
            print(f"Markdown: {p['md']}")
            print(f"PDF     : {p['pdf']}")
            print(f"配图前缀: {p['figure_prefix']}")
        return 0
    if args.cmd == "verify":
        payload = parse_index(Path(args.index))
        res = verify_index(payload["items"], payload["reports"])
        if args.json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
        else:
            print(f"[{res['结论']}] {res['判据']} — {res['证据']}")
            for p in res["问题"]:
                print(f"  · [{p['行']}] {p['字段']}：实际={p['实际']} 应然={p['应然']}  ← {p['依据']}")
        return 1 if res["结论"] == "FAIL" else 0
    if args.cmd == "due":
        payload = parse_index(Path(args.index))
        items = [i for i in payload["items"]
                 if args.project is None or i["项目"] == args.project]
        on = date.fromisoformat(args.on)
        rows = due_items(items, on)
        if args.json:
            print(json.dumps({"on": args.on, "project": args.project, "due": rows},
                             ensure_ascii=False, indent=2))
        else:
            print(f"=== {args.on} 该复习 {len(rows)} 条"
                  + (f"（项目：{args.project}）" if args.project else "") + " ===")
            for r in rows:
                od = f"（逾期 {r['逾期天数']} 天）" if r["逾期天数"] > 0 else ""
                print(f"  · [{r['项目']}] {r['标题']}  S={r['S']:.1f}  应复习日={r['下次复习']}{od}")
        return 0
    if args.cmd == "upsert":
        idx = Path(args.index)
        payload = parse_index(idx)
        on = date.fromisoformat(args.date)
        report_id = args.report_id or f"{args.date}_{args.task}"
        cands = _load_items(args.items)
        for c in cands:
            c.setdefault("任务", args.task)
            c.setdefault("项目", args.project)
        out = classify(cands, payload["items"], report_id, on, project=args.project)
        tri = out["三态"]
        payload["items"] = out["items"]
        row = {"项目": sanitize(args.project), "日期": args.date, "报告": report_id,
               "知识点数": len(cands), "全新": len(tri["全新"]),
               "有增量": len(tri["有增量"]), "纯复习": len(tri["纯复习"])}
        payload["reports"] = [r for r in payload["reports"] if r["报告"] != report_id] + [row]
        payload["reports"] = sorted({r["报告"]: r for r in payload["reports"]}.values(),
                                    key=lambda r: r["报告"])
        if not args.dry_run:
            idx.parent.mkdir(parents=True, exist_ok=True)
            write_index(idx, payload)
        print(json.dumps({
            "report_id": report_id, "项目": sanitize(args.project), "任务": args.task,
            "候选数": len(cands), "全新": tri["全新"], "有增量": tri["有增量"],
            "纯复习": tri["纯复习"], "幂等命中": tri["幂等命中"],
            "匹配到的既有标题": tri["匹配到的既有标题"],
            "dry_run": bool(args.dry_run), "index": str(idx), "已写盘": not args.dry_run,
        }, ensure_ascii=False, indent=2))
        return 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
