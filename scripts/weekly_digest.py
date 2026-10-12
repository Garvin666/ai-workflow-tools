# -*- coding: utf-8 -*-
# [自研工具] weekly_digest
# 用途：把「每日资讯简报」的素材蒸馏成「周报」——汇总上周 7 天 items.json、跨天去重、
#      出候选清单 → 由模型精选 → 渲染周报 MD/HTML/PDF，并沉淀领域笔记素材
# 适用场景：知识库资讯链路的周更环节（日链路负责采集出日报，本工具负责每周蒸馏纳库）
#          不适用：单日简报（用 daily_brief）；非 RSS/items.json 来源的资讯聚合
# 作者：ai-workflow 自研（资讯日报自动化 2026-10-08）
# 仓库：https://github.com/Garvin666/ai-workflow-tools/blob/main/scripts/weekly_digest.py
"""weekly_digest —— 把「每日简报」的素材蒸馏成「周报」

定位：日链路（daily_brief.py）负责**采集 + 出日报**；本脚本负责**每周蒸馏 + 纳库**。
数据源不是 RSS，而是已经落盘的每日 items.json —— 这样日链路不动，且天然不丢数据。

两段式用法（脚本不做语义判断，"最有效"由模型判定）：
    1) prepare : 汇总上一周（周一~周日）各日 items.json → 跨天去重 → 出「候选清单.json」
                 同时出「领域笔记素材.json」（按类分组的精选候选 + 落点提示）
    2) [模型]  : 读候选清单 → 挑精选、写中文标题/要点/本周综述 → 写「精选.json」
    3) render  : 读「精选.json」→ 渲染周报 MD/HTML/PDF

    不带参数 = prepare；若「精选.json」已存在则继续 render。

命令行：
    python weekly_digest.py                # prepare（有精选则一并 render）
    python weekly_digest.py prepare        # 只汇总，出候选清单
    python weekly_digest.py render         # 只渲染（需精选.json 已存在）
    python weekly_digest.py --week 2026-W41   # 指定 ISO 周（默认：上一个完整周）

设计约束（延续日链路的纪律）：
- **全绝对路径**：自动化触发时 CWD 不保证是库根
- **失败可见**：该周一天素材都没有 → 退出码 2 且不产出周报，不伪造内容
- **缺天可见**：报头明列"覆盖 X/7 天"，缺哪天写清楚，避免把"没抓到"当成"没有"
- 零第三方依赖（RSS/JSON 用标准库；PDF 走 Chrome headless）
"""

import html
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------- 路径常量
# 工作区根：优先环境变量 KB_ROOT；缺省由脚本位置推导（本脚本位于 <库根>/tasks/<任务>/）
KB = os.environ.get("KB_ROOT") or os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
DAILY_ROOT = os.path.join(KB, "30-产出", "资讯日报")      # 日产物（素材源）
WEEKLY_ROOT = os.path.join(KB, "30-产出", "资讯周报")     # 周报落点
DOMAIN_ROOT = os.path.join(KB, "10-领域")                 # 领域笔记落点
TASK_DIR = os.path.join(KB, "tasks", "资讯日报自动化-2026-10-08")
LOG_DIR = os.path.join(TASK_DIR, "logs")

CST = timezone(timedelta(hours=8))
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36"

# 分类顺序（与日链路一致）
CATEGORY_ORDER = ["量化分析", "AI与大模型", "数学建模", "数据科学", "政策与监管动态"]

# 每类默认精选条数（模型可少选，不建议多选——周报要"蒸馏"而非堆砌）
PICK_PER_CATEGORY = 5
WEEKLY_PER_SOURCE_LIMIT = 10    # 候选清单：每源每周最多条数（保多源覆盖）
WEEKLY_PER_CATEGORY_LIMIT = 60  # 候选清单：每类每周最多条数（控总量，防模型读不完）
ZH_BRIEF_MAX = 260          # 中文要点兜底上限（与日链路同口径）
PICK_BRIEF_ZH_MAX = 220     # 周报精选要点上限（周报更紧凑）

# 分类 → 领域笔记落点（仅对有明确对应目录的类目沉淀；未映射的类目跳过并记录）
DOMAIN_MAP = {
    "量化分析": os.path.join(DOMAIN_ROOT, "量化分析"),
    "AI与大模型": os.path.join(DOMAIN_ROOT, "AI与计算机科学"),
    "数学建模": os.path.join(DOMAIN_ROOT, "数学建模"),
}


# ---------------------------------------------------------------- 工具函数
def log(msg):
    line = f"[{datetime.now(CST).strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(os.path.join(LOG_DIR, "weekly_digest.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:  # noqa: BLE001
        pass


def _norm_link(link):
    """链接规范化：去锚点、去尾斜杠、统一小写（与日链路口径一致）。"""
    return (link or "").split("#")[0].rstrip("/").lower()


def _cut_zh(s, limit):
    """中文兜底裁剪：优先按句末收尾，其次次级标点，最后硬切。"""
    s = (s or "").strip()
    if len(s) <= limit:
        return s
    head = s[:limit]
    for ch in ("。", "！", "？", "；"):
        i = head.rfind(ch)
        if i >= limit * 0.5:
            return head[:i + 1]
    for ch in ("，", "、", "：", "）", ")"):
        i = head.rfind(ch)
        if i >= limit * 0.5:
            return head[:i] + "…"
    return head.rstrip() + "…"


# ---------------------------------------------------------------- 周区间
def week_range(week_str=None, ref=None):
    """返回 (monday, sunday, week_label)。

    默认取**上一个完整周**（以 ref 所在 ISO 周往前推一周），保证素材已齐。
    """
    ref = ref or datetime.now(CST)
    if week_str:
        m = re.match(r"^(\d{4})-?W(\d{1,2})$", week_str.strip())
        if not m:
            raise ValueError(f"--week 格式应为 2026-W41，收到：{week_str}")
        y, w = int(m.group(1)), int(m.group(2))
        monday = datetime.fromisocalendar(y, w, 1)
    else:
        iso = ref.isocalendar()
        this_monday = datetime.fromisocalendar(iso[0], iso[1], 1)
        monday = this_monday - timedelta(days=7)
    sunday = monday + timedelta(days=6)
    iso_y, iso_w, _ = monday.isocalendar()
    label = f"{iso_y}-W{iso_w:02d}"
    return monday.date(), sunday.date(), label


# ---------------------------------------------------------------- 汇总素材
def collect_week(monday, sunday):
    """读取该周各日的 items.json。返回 (items, 有数据的天, 缺数据的天)。"""
    items, have, miss = [], [], []
    d = monday
    while d <= sunday:
        ds = d.strftime("%Y-%m-%d")
        p = os.path.join(DAILY_ROOT, ds, "items.json")
        if os.path.isfile(p):
            try:
                with open(p, encoding="utf-8") as f:
                    day_items = json.load(f)
                for it in day_items:
                    it["_date"] = ds
                items.extend(day_items)
                have.append(ds)
                log(f"  {ds}: {len(day_items)} 条")
            except Exception as e:  # noqa: BLE001
                miss.append(ds)
                log(f"  {ds}: items.json 读取失败 {type(e).__name__}")
        else:
            miss.append(ds)
            log(f"  {ds}: 无日报（跳过）")
        d += timedelta(days=1)
    return items, have, miss


def dedupe(items):
    """跨天去重：同一链接只保留最早出现的一次（cross-list 会致多日重复）。"""
    seen, out = set(), []
    for it in items:
        k = _norm_link(it.get("link", ""))
        if not k or k in seen:
            continue
        seen.add(k)
        out.append(it)
    return out, len(items) - len(out)


def build_candidates(items):
    """按分类分组，构造候选清单（供模型蒸馏）。

    ⚠️ 必须限额（2026-10-09 加）：日报改造为「多源 + 粗筛」后，每日 items.json
    已有 250–300 条候选，一周累计近 2000 条 —— 不限额会把候选清单撑到模型读不完。
    限额策略：每源每周最多 WEEKLY_PER_SOURCE_LIMIT 条（保多源覆盖），
              再按日期倒序每类截断到 WEEKLY_PER_CATEGORY_LIMIT 条（控总量）。
    """
    by_cat = {}
    for it in items:
        by_cat.setdefault(it.get("category", "未分类"), []).append(it)

    def pack(lst):
        per_src = {}
        for it in lst:                      # 先按源限额，避免单一高产源（arXiv）独占
            s = it.get("source", "")
            bucket = per_src.setdefault(s, [])
            if len(bucket) < WEEKLY_PER_SOURCE_LIMIT:
                bucket.append(it)
        flat = [x for v in per_src.values() for x in v]
        flat.sort(key=lambda x: x.get("_date", ""), reverse=True)
        return flat[:WEEKLY_PER_CATEGORY_LIMIT]

    cand = []
    for cat in CATEGORY_ORDER:
        for it in pack(by_cat.get(cat, [])):
            cand.append({
                "link": it.get("link", ""),
                "category": cat,
                "title": it.get("title", ""),
                "summary": it.get("summary", ""),
                "source": it.get("source", ""),
                "published": it.get("published", ""),
                "date": it.get("_date", ""),
            })
    for cat, lst in by_cat.items():          # 防御：未在 CATEGORY_ORDER 中的类目
        if cat not in CATEGORY_ORDER:
            for it in pack(lst):
                cand.append({
                    "link": it.get("link", ""), "category": cat,
                    "title": it.get("title", ""), "summary": it.get("summary", ""),
                    "source": it.get("source", ""), "published": it.get("published", ""),
                    "date": it.get("_date", ""),
                })
    return cand


# ---------------------------------------------------------------- 渲染
CSS = """
  * { box-sizing: border-box; }
  body { font-family: "Microsoft YaHei","PingFang SC","Segoe UI",Arial,sans-serif;
         margin: 0; padding: 15mm 14mm 12mm 14mm; color: #1f2a35; font-size: 9pt; line-height: 1.5; }
  .masthead { border-bottom: 2.5px solid #0b2a52; padding-bottom: 6px; margin-bottom: 3px; }
  .masthead h1 { font-size: 19pt; margin: 0; color: #0b2a52; letter-spacing: 1px; }
  .masthead .date { font-size: 9.5pt; color: #5a6570; }
  .stats { display: flex; gap: 14px; font-size: 8.4pt; color: #5a6570;
           padding-bottom: 7px; border-bottom: 1px solid #e3e7eb; margin-bottom: 11px; flex-wrap: wrap; }
  .stats b { color: #0b2a52; font-size: 9.6pt; }
  .warn { color: #b3261e; }
  .sec-h { font-size: 12pt; color: #0b2a52; margin: 14px 0 7px 0; padding: 4px 0 4px 9px;
           border-left: 3.5px solid #c0392b; background: #fdf5f4; letter-spacing: .5px; }
  .cat-h { font-size: 10.5pt; margin: 11px 0 6px 0; padding: 3px 0 3px 8px;
           border-left: 3.5px solid #0b5cad; background: #f4f7fb; color: #0b2a52; }
  .cat-h .cnt { font-size: 8.2pt; color: #7a848e; font-weight: 400; }
  .review { background: #f8fafc; border: 1px solid #e3e7eb; border-left: 3px solid #0b5cad;
            padding: 7px 10px; margin: 0 0 9px 0; color: #3f4a55; font-size: 8.8pt; line-height: 1.55; }
  .row { margin: 0 0 6px 0; padding: 0 0 5px 0; border-bottom: 1px solid #eef1f4;
         page-break-inside: avoid; }
  .row:last-child { border-bottom: none; }
  .row .t { font-weight: 600; color: #12325c; font-size: 9.6pt; line-height: 1.35; }
  .row .t2 { color: #8b949e; font-size: 7.6pt; line-height: 1.3; margin-top: 0.5px; }
  .row .b { color: #3f4a55; font-size: 8.7pt; line-height: 1.45; margin-top: 1px; }
  .row .m { color: #8b949e; font-size: 7.3pt; margin-top: 1.5px; word-break: break-all; }
  .row .m a { color: #0b5cad; text-decoration: none; }
  .note { background: #f7f9fb; border: 1px solid #e3e7eb; border-left: 3px solid #7a848e;
          padding: 7px 10px; font-size: 8pt; color: #5a6570; margin-top: 14px; line-height: 1.55; }
  table { width: 100%; border-collapse: collapse; font-size: 8pt; margin-top: 5px; }
  th,td { border: 1px solid #e3e7eb; padding: 3px 5px; text-align: left; }
  th { background: #f4f7fb; font-weight: 600; color: #0b2a52; }
  .ok { color: #1a7f37; }
  footer { margin-top: 14px; padding-top: 6px; border-top: 1px solid #e3e7eb;
           font-size: 7.6pt; color: #98a1a9; }
  .pb { page-break-before: always; }
"""

HTML_TMPL = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>{title}</title><style>{css}</style></head><body>

<div class="masthead">
  <h1>每周资讯蒸馏周报</h1>
  <span class="date">{week_label}（{monday} ~ {sunday}）</span>
</div>

<div class="stats">
  <span>本周采集 <b>{n_raw}</b></span>
  <span>跨天去重 <b>{n_drop}</b></span>
  <span>候选 <b>{n_cand}</b></span>
  <span>精选 <b>{n_pick}</b></span>
  <span>覆盖 <b class="{cover_cls}">{n_have}/7 天</b></span>
  <span>生成 {generated}</span>
</div>
{missing_note}

<div class="sec-h">本周综述</div>
{reviews}

<div class="sec-h pb">每周精选</div>
{picks}

<div class="note">
  <b>蒸馏口径</b>：周报不是条目的堆砌，而是从本周全部候选（{n_cand} 条）中按
  「与研究方向相关度 × 信息量/新颖度 × 跨源印证」蒸馏出的 <b>{n_pick} 条</b>。
  未入选条目仍完整保留在对应日期的日报中（<code>30-产出\\资讯日报\\&lt;日期&gt;\\</code>）。
  <br>
  <b>口径纪律</b>：本报告只聚合每日简报所覆盖的源；<b>"周报中未出现" ≠ "该领域本周无动态"</b>。
  <br>
  <b>素材来源</b>：{src_days}
</div>

<footer>由自研工具 weekly_digest 生成 ｜ 仅供个人知识归档使用 ｜ 条目版权归原作者所有</footer>
</body></html>
"""


def render_html(meta, cand, picks, reviews, week_label, monday, sunday, have, miss):
    by_cat = {}
    for it in picks:
        by_cat.setdefault(it.get("category", "未分类"), []).append(it)

    rv = []
    for cat in CATEGORY_ORDER:
        if cat in reviews and (reviews[cat] or "").strip():
            rv.append(f'<div class="review"><b>{html.escape(cat)}</b>　'
                      f'{html.escape(reviews[cat].strip())}</div>')
    if not rv:
        rv.append('<div class="review">（本期未附综述——精选.json 中未提供「综述」字段）</div>')

    pk = []
    for cat in CATEGORY_ORDER:
        lst = by_cat.get(cat, [])
        if not lst:
            continue
        pk.append(f'<div class="cat-h">{html.escape(cat)} <span class="cnt">（{len(lst)} 条）</span></div>')
        for it in lst:
            t_zh = (it.get("title_zh") or "").strip()
            b_zh = (it.get("brief_zh") or "").strip()
            src_t = (it.get("title") or "").strip()
            link = it.get("link", "")
            pk.append(
                '<div class="row">'
                f'<div class="t">{html.escape(t_zh or src_t)}</div>'
                + (f'<div class="t2">{html.escape(src_t)}</div>' if t_zh else '')
                + (f'<div class="b">{html.escape(_cut_zh(b_zh, PICK_BRIEF_ZH_MAX))}</div>' if b_zh else '')
                + f'<div class="m">{html.escape(it.get("source",""))}'
                + (f' ｜ {html.escape(it.get("date",""))}' if it.get("date") else '')
                + f' ｜ <a href="{html.escape(link)}">{html.escape(link)}</a>'
                '</div></div>'
            )
    if not pk:
        pk.append('<div class="review">（本期无精选条目）</div>')

    n_have = len(have)
    miss_note = ""
    if miss:
        miss_note = (f'<div class="note" style="margin:0 0 10px 0">'
                     f'<b class="warn">本期素材不完整</b>：缺 {len(miss)} 天日报（{html.escape("、".join(miss))}）。'
                     f'这些天未产出日报，故其条目未纳入本周蒸馏——'
                     f'<b>属"没抓到"，不等于"这些天没有动态"</b>。</div>')

    return HTML_TMPL.format(
        title=f"每周资讯蒸馏周报 · {week_label}",
        css=CSS,
        week_label=week_label, monday=monday, sunday=sunday,
        generated=datetime.now(CST).strftime("%Y-%m-%d %H:%M"),
        n_raw=meta["n_raw"], n_drop=meta["n_drop"], n_cand=len(cand), n_pick=len(picks),
        n_have=n_have, cover_cls="ok" if n_have == 7 else "warn",
        missing_note=miss_note,
        reviews="\n".join(rv), picks="\n".join(pk),
        src_days=html.escape("、".join(have) if have else "（无）"),
    )


def render_markdown(meta, cand, picks, reviews, week_label, monday, sunday, have, miss):
    by_cat = {}
    for it in picks:
        by_cat.setdefault(it.get("category", "未分类"), []).append(it)

    L = [f"# 每周资讯蒸馏周报 · {week_label}", "",
         f"> 周区间 {monday} ~ {sunday}　｜　采集 {meta['n_raw']} 条　｜　跨天去重 {meta['n_drop']} 条　｜　"
         f"候选 {len(cand)} 条　｜　精选 {len(picks)} 条　｜　覆盖 {len(have)}/7 天　｜　"
         f"生成 {datetime.now(CST).strftime('%Y-%m-%d %H:%M')}", ""]
    if miss:
        L += [f"> ⚠️ **本期素材不完整**：缺 {len(miss)} 天日报（{'、'.join(miss)}）。"
              f"缺天条目未纳入蒸馏——**属\"没抓到\"，不等于\"这些天没有动态\"**。", ""]

    L += ["## 本周综述", ""]
    any_rv = False
    for cat in CATEGORY_ORDER:
        if cat in reviews and (reviews[cat] or "").strip():
            L += [f"**{cat}**　{reviews[cat].strip()}", ""]
            any_rv = True
    if not any_rv:
        L += ["（本期未附综述）", ""]

    L += ["## 每周精选", ""]
    for cat in CATEGORY_ORDER:
        lst = by_cat.get(cat, [])
        if not lst:
            continue
        L += [f"### {cat}（{len(lst)} 条）", ""]
        for it in lst:
            t_zh = (it.get("title_zh") or "").strip()
            b_zh = (it.get("brief_zh") or "").strip()
            L.append(f"- **{t_zh or it.get('title','')}**")
            if t_zh and it.get("title"):
                L.append(f"  - 原文：{it['title']}")
            if b_zh:
                L.append(f"  - {_cut_zh(b_zh, PICK_BRIEF_ZH_MAX)}")
            L.append(f"  - `{it.get('source','')}`"
                     + (f" ｜ {it.get('date','')}" if it.get("date") else "")
                     + f" ｜ {it.get('link','')}")
        L.append("")

    L += ["> **蒸馏口径**：周报从本周全部候选中按「与研究方向相关度 × 信息量/新颖度 × 跨源印证」蒸馏，"
           "未入选条目仍完整保留在对应日期的日报中。",
          "> **口径纪律**：只聚合每日简报所覆盖的源；\"周报中未出现\" ≠ \"该领域本周无动态\"。",
          "", "---", "", "*由自研工具 weekly_digest 生成，仅供个人知识归档使用。*"]
    return "\n".join(L)


# ---------------------------------------------------------------- PDF
def find_browser():
    """定位 Chromium 内核浏览器 —— 候选路径经环境变量拼装，源码内不写死本机绝对路径。"""
    cands = []
    if os.environ.get("CHROME_PATH"):
        cands.append(os.environ["CHROME_PATH"])
    roots = []
    for env in ("ProgramFiles", "ProgramW6432"):
        if os.environ.get(env):
            roots.append(os.environ[env])
    drive = os.environ.get("SystemDrive")
    if drive:
        base = drive if drive.endswith(os.sep) else drive + os.sep
        roots += [os.path.join(base, "Program Files"), os.path.join(base, "Program Files (x86)")]
    for base in roots:
        cands.append(os.path.join(base, "Google", "Chrome", "Application", "chrome.exe"))
        cands.append(os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe"))
    local = os.environ.get("LOCALAPPDATA")
    if local:
        cands.append(os.path.join(local, "Google", "Chrome", "Application", "chrome.exe"))
        cands.append(os.path.join(local, "Microsoft", "Edge", "Application", "msedge.exe"))
    for c in cands:
        if os.path.isfile(c):
            return c
    return None


def html_to_pdf(html_path, pdf_path):
    browser = find_browser()
    if not browser:
        log("  未找到 Chrome/Edge，无法生成 PDF")
        return False
    url = "file:///" + os.path.abspath(html_path).replace("\\", "/")
    cmd = [browser, "--headless=new", "--disable-gpu", "--no-sandbox",
           "--no-pdf-header-footer", f"--print-to-pdf={os.path.abspath(pdf_path)}", url]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=120)
        if os.path.isfile(pdf_path) and os.path.getsize(pdf_path) > 1000:
            return True
        log(f"  PDF 生成异常 rc={r.returncode} err={r.stderr[:200]!r}")
        return False
    except Exception as e:  # noqa: BLE001
        log(f"  PDF 生成异常 {type(e).__name__}: {e}")
        return False


# ---------------------------------------------------------------- 领域笔记
def _note_section_body(review, picks):
    L = []
    if (review or "").strip():
        L += [review.strip(), ""]
    if not picks:
        L += ["（本周无精选条目）", ""]
    else:
        for it in picks:
            t = (it.get("title_zh") or it.get("title") or "").strip()
            b = _cut_zh((it.get("brief_zh") or "").strip(), PICK_BRIEF_ZH_MAX)
            bits = []
            if it.get("source"):
                bits.append(f"`{it['source']}`")
            if it.get("date"):
                bits.append(it["date"])
            meta = ("　" + " · ".join(bits)) if bits else ""
            L.append(f"- **{t}**　{b}　（[原文]({it.get('link','')})）{meta}")
        L.append("")
    return "\n".join(L)


def _upsert_section(txt, heading, body):
    """把 heading 节整体替换/追加（同周重跑幂等，不产生重复节）。"""
    lines = txt.split("\n")
    idx = next((i for i, l in enumerate(lines) if l.strip() == heading.strip()), None)
    if idx is None:
        return txt.rstrip("\n") + "\n\n" + heading + "\n\n" + body.rstrip("\n") + "\n"
    end = len(lines)
    for j in range(idx + 1, len(lines)):
        if lines[j].startswith("## "):
            end = j
            break
    return "\n".join(lines[:idx] + [heading, ""] + body.rstrip("\n").split("\n") + [""] + lines[end:])


def _upsert_moc_link(moc_path, stem="前沿追踪", desc="每周资讯蒸馏（自动维护）"):
    if not os.path.isfile(moc_path):
        return False
    txt = open(moc_path, encoding="utf-8").read()
    if f"[[{stem}]]" in txt:
        return False
    lines, out, done = txt.split("\n"), [], False
    for ln in lines:
        out.append(ln)
        if not done and ln.strip().startswith("## 笔记清单"):
            out += ["", f"- [[{stem}]]　—— {desc}"]
            done = True
    if not done:
        out += ["", f"- [[{stem}]]　—— {desc}"]
    with open(moc_path, "w", encoding="utf-8") as f:
        f.write("\n".join(out))
    return True


def write_domain_notes(sel, label, monday, sunday, picks):
    """把该周精选沉淀成 10-领域 下的「前沿追踪」笔记，并在 MOC 挂链。

    `picks` 须为已经过 `resolve_picks` 对齐（含 source/date）的条目。
    未映射的类目（DOMAIN_MAP 未覆盖）**跳过并记录** —— 不擅自新建库的顶层分区。
    """
    reviews = sel.get("综述", {}) if isinstance(sel, dict) else {}
    written, skipped = [], []

    for cat in CATEGORY_ORDER:
        d = DOMAIN_MAP.get(cat)
        lst = [p for p in picks if p.get("category") == cat]
        review = reviews.get(cat, "")
        if not d:
            if lst or (review or "").strip():
                skipped.append(cat)
            continue
        if not lst and not (review or "").strip():
            continue

        os.makedirs(d, exist_ok=True)
        note = os.path.join(d, "前沿追踪.md")
        heading = f"## {label}（{monday} ~ {sunday}）"
        body = _note_section_body(review, lst)

        if os.path.isfile(note):
            txt = _upsert_section(open(note, encoding="utf-8").read(), heading, body)
            action = "更新"
        else:
            head = (f"---\ntags: [{cat}, 前沿追踪]\ncreated: {datetime.now(CST).strftime('%Y-%m-%d')}\n---\n\n"
                    f"# {cat} · 前沿追踪\n\n"
                    f"> 由「每周资讯蒸馏」自动维护（每周一 09:00）。每节对应一个 ISO 周，"
                    f"记录该周从每日资讯中蒸馏出的精选条目与主线归纳。\n"
                    f"> 上游：`30-产出\\资讯周报\\<年-周>\\`　｜　素材：`30-产出\\资讯日报\\<日期>\\`\n\n")
            txt = head + heading + "\n\n" + body.rstrip("\n") + "\n"
            action = "创建"

        with open(note, "w", encoding="utf-8") as f:
            f.write(txt)
        written.append((action, note, len(lst)))
        log(f"  领域笔记{action}：{note}（{len(lst)} 条）")

        moc = os.path.join(d, os.path.basename(d) + ".md")
        if _upsert_moc_link(moc):
            log(f"    MOC 已挂链：{moc}")

    if skipped:
        log(f"  未映射类目（跳过领域笔记）：{'、'.join(skipped)} —— 无对应 10-领域 目录，不擅自新建")
    return written


def resolve_picks(sel, cand):
    """把模型填的精选条目与本时候选对齐。

    - 只保留确实出现在本周候选里的链接（防止手打/记忆错配 —— 日链路踩过这个坑）
    - 用候选数据补 source / date / 原文标题，模型只需提供 link + title_zh + brief_zh
    """
    picks = sel.get("精选", []) if isinstance(sel, dict) else (sel or [])
    cand_keys = {_norm_link(x["link"]): x for x in cand}
    ok, bad = [], []
    for p in picks:
        k = _norm_link(p.get("link", ""))
        if k in cand_keys:
            merged = dict(cand_keys[k])
            merged.update({kk: vv for kk, vv in p.items() if kk in ("title_zh", "brief_zh")})
            ok.append(merged)
        else:
            bad.append(p.get("link", ""))
    return ok, bad


def sort_picks(picks):
    """按分类顺序排列（周报与笔记的呈现顺序一致）。"""
    order = {c: i for i, c in enumerate(CATEGORY_ORDER)}
    return sorted(picks, key=lambda x: order.get(x.get("category", ""), 99))


# ---------------------------------------------------------------- 主流程
def load_json(p, default=None):
    if not os.path.isfile(p):
        return default
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:  # noqa: BLE001
        log(f"  读取失败 {os.path.basename(p)}：{type(e).__name__}: {e}")
        return default


def main(argv):
    week_str = None
    if "--week" in argv:
        i = argv.index("--week")
        week_str = argv[i + 1] if i + 1 < len(argv) else None
    do = "auto"
    for a in argv[1:]:
        if a in ("prepare", "render", "notes"):
            do = a

    monday, sunday, label = week_range(week_str)
    out_dir = os.path.join(WEEKLY_ROOT, label)
    os.makedirs(out_dir, exist_ok=True)

    log(f"=== weekly_digest 启动 {datetime.now(CST).strftime('%Y-%m-%d %H:%M:%S')} ===")
    log(f"目标周：{label}（{monday} ~ {sunday}）")
    log("汇总该周日报素材：")

    items, have, miss = collect_week(monday, sunday)

    if not items:
        log("致命：该周无任何日报素材，不产出周报（失败可见性设计）")
        with open(os.path.join(out_dir, "FAILED.txt"), "w", encoding="utf-8") as f:
            f.write(f"周 {label}（{monday} ~ {sunday}）无任何日日报素材 @ {datetime.now(CST).isoformat()}\n")
            f.write(f"缺失日期：{'、'.join(miss)}\n")
        return 2

    uniq, n_drop = dedupe(items)
    cand = build_candidates(uniq)
    meta = {"n_raw": len(items), "n_drop": n_drop}
    log(f"采集 {len(items)} 条 → 跨天去重 {n_drop} 条 → 候选 {len(cand)} 条（覆盖 {len(have)}/7 天）")

    cand_path = os.path.join(out_dir, "候选清单.json")
    with open(cand_path, "w", encoding="utf-8") as f:
        json.dump(cand, f, ensure_ascii=False, indent=2)
    log(f"候选清单已导出：{len(cand)} 条 -> 候选清单.json")

    # 领域笔记素材：按类分组 + 落点提示（供模型写领域笔记）
    dom = []
    for cat in CATEGORY_ORDER:
        d = DOMAIN_MAP.get(cat)
        dom.append({
            "category": cat,
            "domain_dir": d or "",
            "note": ("该分类无对应领域目录，本期跳过领域笔记沉淀"
                     if not d else "追加到该目录下的「<分类>前沿追踪.md」"),
            "candidates": [x for x in cand if x["category"] == cat][:PICK_PER_CATEGORY],
        })
    with open(os.path.join(out_dir, "领域笔记素材.json"), "w", encoding="utf-8") as f:
        json.dump(dom, f, ensure_ascii=False, indent=2)
    log("领域笔记素材已导出 -> 领域笔记素材.json")

    # ---- 精选（模型产出）
    sel_path = os.path.join(out_dir, "精选.json")
    sel = load_json(sel_path)

    if do == "notes":
        if sel is None:
            log("未发现 精选.json —— 无法写领域笔记")
            return 2
        ok_picks, bad = resolve_picks(sel, cand)
        if bad:
            log(f"警告：{len(bad)} 条精选不在本周候选中（已丢弃）：{bad[:5]}")
        if not ok_picks:
            log("致命：精选条目无一匹配本周候选")
            return 2
        ok_picks = sort_picks(ok_picks)
        log("写领域笔记：")
        write_domain_notes(sel, label, monday, sunday, ok_picks)
        return 0

    if do == "prepare" or (do == "auto" and sel is None):
        if sel is None:
            log("未发现 精选.json —— 已完成 prepare，等待模型蒸馏后再跑 render")
        return 0

    reviews = sel.get("综述", {}) if isinstance(sel, dict) else {}
    ok_picks, bad = resolve_picks(sel, cand)
    if bad:
        log(f"警告：{len(bad)} 条精选不在本周候选中（已丢弃）：{bad[:5]}")
    if not ok_picks:
        log("致命：精选条目无一匹配本周候选，不渲染周报")
        return 2
    ok_picks = sort_picks(ok_picks)

    html_text = render_html(meta, cand, ok_picks, reviews, label, monday, sunday, have, miss)
    md_text = render_markdown(meta, cand, ok_picks, reviews, label, monday, sunday, have, miss)

    html_path = os.path.join(out_dir, f"周报-{label}.html")
    md_path = os.path.join(out_dir, f"周报-{label}.md")
    pdf_path = os.path.join(out_dir, f"周报-{label}.pdf")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html_text)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md_text)

    ok = html_to_pdf(html_path, pdf_path)
    log(f"PDF: {'成功' if ok else '失败'} -> {pdf_path}")

    # 周报渲染成功即沉淀领域笔记（用户选定「周报 + 领域笔记」）
    log("写领域笔记：")
    write_domain_notes(sel, label, monday, sunday, ok_picks)

    log(f"完成：精选 {len(ok_picks)} 条 / 输出 {out_dir}")
    print()
    print("输出目录:", out_dir)
    print("  MD  :", md_path)
    print("  HTML:", html_path)
    print("  PDF :", pdf_path, "(OK)" if ok else "(FAILED)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
