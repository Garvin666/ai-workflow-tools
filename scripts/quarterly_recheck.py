# -*- coding: utf-8 -*-
# [自研工具] quarterly_recheck
# 用途：对知识库「开源项目精选清单」做季度时效复核 —— 拉取清单条目的当前指标，比对归档状态、
#      停更硬线、Star/Fork/许可证漂移，产出可直接照做的复核报告（不自动改清单）
#      另有 `--lint-only`：**离线**只查格式与内部一致性（不联网、不落文件），用于新增条目后自检
# 适用场景：开源项目收录清单的定期维护（季度或任意周期）；清单增删条目后想确认远端状态；
#          新增/编辑条目后跑 `--lint-only` 立刻知道格式有没有破
#          不适用：首次收录（那要走候选池 → 硬线筛选 → 入选名单那条链路）；单仓库随手查询
# 作者：ai-workflow 自研（开源项目收录-季度复核 2026-10-11）
# 仓库：https://github.com/Garvin666/ai-workflow-tools/blob/main/scripts/quarterly_recheck.py
"""开源项目精选清单 · 季度时效复核。

设计取舍（三点，都是刻意的）：

1. **只报告、不改清单** —— 脚本产出「建议动作」，改清单由人确认后执行。清单是有判断成分的
   产物（哪些项目值得留、备注怎么写），机器不该替人做这个决定；但「哪个库归档了、哪个停更了」
   是纯事实，机器判得比人快也比人准。
2. **复用 ai-workflow 的 http_fetch.py 取数** —— 不另写一套 GitHub 调用，指标口径与当初收录时
   完全一致（同一脚本、同一字段）；`--github-repo` 走 api.github.com，绕开本机 github.com 网页拦截。
3. **无变化也出报告** —— 「本次复核 0 项需处理」本身就是结论，落盘才可回溯（否则下次无从判断
   是「查过没问题」还是「压根没查」）。
4. **取数强制绕缓存** —— 调用 `http_fetch.py` 时带 `--no-cache`。复核的全部意义就是拿**当下**的
   事实；用一小时内缓存会让同一天连跑两次得到互相矛盾的结果（实测遇到过）。
5. **解析不过关就退出（码 2），不产出报告** —— 条目数、指标行数、名字解析三样对不上即报错。
   这是把「静默漏条」变成可证伪：少一条的报告比不出报告危险得多。
6. **`--lint-only` 与联网复核共用同一套解析** —— 新增条目时的「接口规范」由机器守，不靠人自觉。
   用同一个 `parse_checklist()` 保证「lint 过了但复核跑不通」这种分裂不会发生；离线、不落文件、
   不碰网络，所以可以在任何时刻随手跑。
"""
import argparse
import re
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

DEFAULT_FETCHER = Path.home() / ".workbuddy/skills/ai-workflow/scripts/http_fetch.py"
DEFAULT_STALE_DAYS = 183          # 近 6 个月（约 183 天）
BATCH = 20                        # 每批取多少个仓库（URL 长度与限流兼顾）

HEAD_RE = re.compile(r"^####\s+([A-Z]\d{2})\s+·\s+(.+?)\s*$")
SLUG_RE = re.compile(r"[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+")
LINK_RE = re.compile(r"https://github\.com/([A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+)")
METRIC_RE = re.compile(
    r"^-\s+\*\*实测指标\*\*：Star\s+([\d,]+)\s+·\s+Fork\s+([\d,]+)\s+·\s+许可证\s+(\S+)\s+·\s+"
    r"主语言\s+(.+?)\s+·\s+最近 push\s+(\d{4}-\d{2}-\d{2})\s+·\s+(未归档|已归档)\s*$"
)
REJECT_ROW_RE = re.compile(r"^\|\s*([A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+)\s*\|")
ID_RE = re.compile(r"^[A-Z]\d{2}$")
# 占位符：规范要求「无许可证 / 无法判定时填『未标注』」。若填成 `—`、`-`、`未知` 之类，
# 比对时会与实测侧的空值不相等 → **每轮都被报成漂移**（假阳性，实测踩过）。故统一归一。
PLACEHOLDER = {"", "—", "–", "-", "－", "n/a", "N/A", "na", "未知", "无", "none", "None"}


def norm_field(v) -> str:
    """把占位符统一成「未标注」，避免与实测值比对时产生永久假漂移。"""
    s = str(v or "").strip()
    return "未标注" if s in PLACEHOLDER else s
# §二 领域覆盖表的行：| **Q** | AI × 量化投资 | 11 | … |
GROUP_DECL_RE = re.compile(r"^\|\s*\*\*([A-Z])\*\*\s*\|[^|]*\|\s*(\d+)\s*\|")
# §四 分组标题：### 4.1 AI × 量化投资（Q 组 · 11 条）
GROUP_HEAD_RE = re.compile(r"^###\s+4\.\d+\s+.*?（([A-Z])\s*组\s*·\s*(\d+)\s*条）")


def parse_checklist(md: Path) -> tuple[list[dict], set[str], list[str], int]:
    """解析清单，返回（条目列表, 已剔除集合, 解析告警, 指标行总数）。

    两处刻意做得比「够用」更稳：

    * **按标题切块，块内无序查找** —— 先按 `#### Q04 · …` 把正文切成一条一块，再在块内
      分别找「实测指标」行与「链接」行。**不按行序**做状态机。这一条是被测试逼出来的：
      最初的写法用「读到最后一条指标就把当前条目收尾」，而 `链接` 行排在 `实测指标` **之后**，
      于是「标题只写简称时回退到链接行」的逻辑压根没机会执行。
    * **条目名的两级回退** —— 标题按 `owner/repo` 解析；若标题只写了项目简称（真实发生过：
      `#### Q04 · OpenBB`，其余 39 条都是全名），则从块内的 `链接` 行回捞。宁可多解析一次，
      也不静默丢条。
    * **指标行数对账** —— 返回值带上「正文里 `- **实测指标**` 出现次数」，由调用方与条目数
      核对。这是把「漏条」变成**可证伪**的关键：解析数少于指标行数即报错退出，而不是产出一份
      少一条的报告还看起来一切正常。
    """
    warnings: list[str] = []
    lines = md.read_text(encoding="utf-8").splitlines()

    # —— 定位 §五「已剔除」区段，并收集其中的仓库全名 ——
    marks = [i for i, l in enumerate(lines) if l.startswith("## ")]
    marks.append(len(lines))
    reject_ranges = [(marks[k], marks[k + 1])
                     for k in range(len(marks) - 1) if "已剔除" in lines[marks[k]]]
    rejected: set[str] = set()
    for a, b in reject_ranges:
        for line in lines[a:b]:
            m = REJECT_ROW_RE.match(line)
            if m:
                rejected.add(m.group(1))

    def in_reject(i: int) -> bool:
        return any(a <= i < b for a, b in reject_ranges)

    # —— 正文里的条目块 ——
    heads = [i for i, l in enumerate(lines) if HEAD_RE.match(l) and not in_reject(i)]
    entries = []
    for pos, i in enumerate(heads):
        nxt_head = heads[pos + 1] if pos + 1 < len(heads) else len(lines)
        nxt_sec = next((j for j in marks if j > i), len(lines))
        block = lines[i + 1:min(nxt_head, nxt_sec)]
        m = HEAD_RE.match(lines[i])
        raw = m.group(2).strip()
        slug = SLUG_RE.search(raw)
        e: dict = {"id": m.group(1), "title": raw, "line": i + 1,
                   "name": slug.group(0) if slug else None}
        for bl in block:
            mlink = LINK_RE.search(bl)
            if mlink and "link_name" not in e:
                # 链接行的 slug 一律记下来（不论标题是否已给出全名）——`--lint-only` 靠它查
                # 「标题与链接打架」，否则只会一直用标题那份、永远发现不了两者不一致。
                e["link_name"] = mlink.group(1)
                if e["name"] is None:
                    e["name"], e["name_from"] = mlink.group(1), "链接行"
            mm = METRIC_RE.match(bl)
            if mm and "stars" not in e:
                e.update({
                    "stars": int(mm.group(1).replace(",", "")),
                    "forks": int(mm.group(2).replace(",", "")),
                    "license": mm.group(3),
                    "language": mm.group(4).strip(),
                    "pushed_at": mm.group(5),
                    "archived": mm.group(6) == "已归档",
                })
        entries.append(e)

    # —— 原始计数（不看正则是否匹配得上），用于事后对账 ——
    n_metric_lines = sum(1 for i, l in enumerate(lines)
                         if l.lstrip().startswith("- **实测指标**") and not in_reject(i))
    for i, l in enumerate(lines):
        if l.lstrip().startswith("- **实测指标**") and not in_reject(i) \
                and not METRIC_RE.match(l.strip()):
            warnings.append(f"第 {i + 1} 行「实测指标」行格式与规范不符，未被解析")
    return entries, rejected, warnings, n_metric_lines


def lint_checklist(md: Path) -> tuple[list[dict], list[tuple[str, str]]]:
    """离线校验清单的格式与内部一致性。返回（条目列表, [(级别, 说明)…]）。

    能做而人眼容易漏的四类：

    * **结构** —— id 格式 / 唯一 / 组内是否跳号；条目数·指标行数·名字解析三样对账。
    * **名字** —— 能否确定 owner/repo；标题与链接行是否**打架**（标题写 A、链接指 B 这种，
      联网复核时会被当成 A 去查、拿回 A 的数据，看上去一切正常）。
    * **字段** —— 许可证 / 主语言非空、`pushed_at` 是合法日期。
    * **分组自洽** —— §二 领域覆盖表声明的条数、§四 分组标题里写的条数，与 §四 实际条数核对。

    刻意**不联网、不落文件**：它要能在新增条目后随手跑，快且无副作用。
    """
    lines = md.read_text(encoding="utf-8").splitlines()
    entries, _rejected, warnings, n_metric = parse_checklist(md)
    problems: list[tuple[str, str]] = [("WARN", w) for w in warnings]
    problems += [("FAIL", p) for p in check_parse_integrity(entries, n_metric)]

    # —— 每条归属哪个分组（由上方最近的 ### 4.x 标题决定）——
    gmap: dict[int, str] = {}
    cur_g = None
    for i, l in enumerate(lines):
        m = GROUP_HEAD_RE.match(l)
        if m:
            cur_g = m.group(1)
        gmap[i] = cur_g
    by_group: dict[str, list[dict]] = {}
    for e in entries:
        by_group.setdefault(gmap.get(e["line"] - 1) or "?", []).append(e)

    # —— id 格式 / 唯一 ——
    seen: dict[str, int] = {}
    for e in entries:
        if not ID_RE.match(e["id"]):
            problems.append(("FAIL", f"第 {e['line']} 行 id 格式不合规：{e['id']!r}"
                                     f"（应为「分组字母 + 两位数字」，如 Q01）"))
        if e["id"] in seen:
            problems.append(("FAIL", f"id 重复：{e['id']}"
                                     f"（第 {seen[e['id']]} 行与第 {e['line']} 行）"))
        seen[e["id"]] = e["line"]

    # —— 名字：能否确定 / 标题与链接是否一致 ——
    for e in entries:
        if not e.get("link_name"):
            problems.append(("FAIL", f"{e['id']} 缺少 GitHub 链接行（`- **链接**：<https://github.com/owner/repo>`）"))
        elif e.get("name_from") == "链接行":
            problems.append(("WARN", f"{e['id']} 标题只写了「{e['title']}」、没写 owner/repo，"
                                     f"已从链接行回退为 {e['name']} —— 能解析，但与其余条目格式不一致"))
        elif e["name"] != e["link_name"]:
            problems.append(("FAIL", f"{e['id']} 标题与链接打架：标题写 {e['name']}、"
                                     f"链接指向 {e['link_name']} —— 联网复核会按标题那份去查"))
        g = gmap.get(e["line"] - 1)
        if g and ID_RE.match(e["id"]) and e["id"][0] != g:
            problems.append(("FAIL", f"第 {e['line']} 行 {e['id']} 落在 {g} 组里，id 前缀应是 {g}"))

    # —— 字段 ——
    for e in entries:
        if "stars" not in e:
            continue                                  # 缺指标已由完整性守卫报过，不重复
        if norm_field(e.get("license")) != str(e.get("license", "")).strip():
            problems.append(("WARN", f"{e['id']} 许可证写成占位符「{e.get('license')}」—— "
                                     f"规范要求无许可证时填「未标注」"))
        if norm_field(e.get("language")) != str(e.get("language", "")).strip():
            problems.append(("WARN", f"{e['id']} 主语言写成占位符「{e.get('language')}」—— "
                                     f"规范要求填主语言，无法判定时填「未标注」"))
        try:
            date.fromisoformat(e.get("pushed_at", ""))
        except ValueError:
            problems.append(("FAIL", f"{e['id']} 最近 push 不是合法日期：{e.get('pushed_at')!r}"
                                     f"（应为 YYYY-MM-DD）"))

    # —— 分组：跳号 / 与声明条数对账 ——
    for g in sorted(by_group):
        es = by_group[g]
        nums = sorted(int(e["id"][1:]) for e in es if ID_RE.match(e["id"]) and e["id"][0] == g)
        if nums and nums != list(range(1, len(nums) + 1)):
            problems.append(("WARN", f"{g} 组 id 序号不连续：{nums} —— 规范要求 id 顺延不复用，"
                                     f"跳号通常意味着中间有条目被删（若是有意删除可忽略）"))
    for l in lines:
        m = GROUP_DECL_RE.match(l)
        if m:
            g, n = m.group(1), int(m.group(2))
            if len(by_group.get(g, [])) != n:
                problems.append(("FAIL", f"§二 声明 {g} 组 {n} 条，§四 实际 "
                                         f"{len(by_group.get(g, []))} 条"))
    for l in lines:
        m = GROUP_HEAD_RE.match(l)
        if m:
            g, n = m.group(1), int(m.group(2))
            if len(by_group.get(g, [])) != n:
                problems.append(("FAIL", f"§四 分组标题写 {g} 组 {n} 条，该组实际 "
                                         f"{len(by_group.get(g, []))} 条"))

    return entries, problems


def render_lint(checklist: Path, entries: list[dict], problems: list[tuple[str, str]]) -> str:
    fails = [p for p in problems if p[0] == "FAIL"]
    warns = [p for p in problems if p[0] == "WARN"]
    tally: dict[str, int] = {}
    for e in entries:
        tally[e["id"][0]] = tally.get(e["id"][0], 0) + 1
    counts = " / ".join(f"{g} {n}" for g, n in sorted(tally.items()))
    lines = [f"扫描对象: {checklist.name}",
             f"条目: {len(entries)} 条" + (f"（{counts}）" if counts else ""),
             f"  FAIL = {len(fails)}  |  WARN = {len(warns)}"]
    for level in ("FAIL", "WARN"):
        for lv, msg in problems:
            if lv == level:
                lines.append(f"  [{lv}] {msg}")
    lines.append("结果：" + ("格式合规，无问题" if not problems
                            else f"不通过（{len(fails)} 项必须修 / {len(warns)} 项建议修）"))
    return "\n".join(lines)


def check_parse_integrity(entries: list[dict], n_metric: int) -> list[str]:
    """返回问题清单；空列表 = 解析完整。

    三样必须同时对上：每条都得有 owner/repo、每条都得有指标、完整条目数得等于指标行数。
    少任何一样都说明「清单里有一条没被读到」—— 这种情况下出的报告是错的，宁可不出。
    """
    problems = []
    for e in entries:
        if not e.get("name"):
            problems.append(f"第 {e['line']} 行 {e['id']}（{e['title']}）确定不了 owner/repo"
                            f"—— 标题未含全名，且其后未找到 GitHub 链接行")
        if "stars" not in e:
            problems.append(f"{e['id']}（{e.get('name') or e['title']}）没有解析到「实测指标」行")
    complete = sum(1 for e in entries if e.get("name") and "stars" in e)
    if complete != n_metric:
        problems.append(f"指标行总数 {n_metric} ≠ 完整条目数 {complete}")
    return problems


def fetch(fetcher: Path, names: list[str]) -> dict[str, dict]:
    """调用 ai-workflow 的 http_fetch.py 批量取数，返回 {full_name: {...}}。"""
    out: dict[str, dict] = {}
    for i in range(0, len(names), BATCH):
        batch = names[i:i + BATCH]
        proc = subprocess.run(
            [sys.executable, str(fetcher), "--github-repo", ",".join(batch), "--no-cache"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        body = (proc.stdout or "") + (proc.stderr or "")
        for line in body.splitlines():
            if not line.strip() or line.startswith("[INFO]") or line.startswith("full_name\t"):
                continue
            parts = line.split("\t")
            if len(parts) < 7 or "/" not in parts[0]:
                continue
            name, stars, forks, lic, pushed, archived, err = parts[:7]
            try:
                stars_i, forks_i = int(stars), int(forks)
            except ValueError:
                continue
            out[name] = {
                "stars": stars_i, "forks": forks_i,
                "license": "" if lic in ("—", "-") else lic,
                "pushed_at": pushed[:10], "archived": archived == "是",
                "error": "" if err in ("—", "-") else err,
            }
    return out


def _is_noise(field: str, old, new: int, noise_abs: int, noise_pct: float) -> bool:
    """Star/Fork 的极小变化视为正常波动。

    判定要**同时**满足两个条件才算噪声：绝对差 ≤ noise_abs **且** 相对差 ≤ noise_pct。
    两个条件缺一不可，是为了避免把「小仓库的大变化」误吞掉 —— 100 星的库涨 5 个是 5%，
    落在噪声区间外，仍会被报出来。
    """
    try:
        old_i = int(str(old).replace(",", ""))
    except (TypeError, ValueError):
        return False
    delta = abs(new - old_i)
    if delta == 0 or delta > noise_abs:
        return False
    return not (old_i and delta / old_i > noise_pct / 100)


def classify(entries: list[dict], fetched: dict[str, dict], rejected: set[str],
             stale_before: str, noise_abs: int = 5, noise_pct: float = 0.5) -> dict:
    """四类互斥：建议移出 / 数字漂移 / 正常波动 / 无需处理，外加取数失败。

    分类互斥是刻意的 —— 一条只归一类，各级条数相加必等于条目总数，报告里的数字能自洽对账。
    """
    need_drop, drift, wave, ok, failed = [], [], [], [], []
    for e in entries:
        n = e["name"]
        f = fetched.get(n)
        if not f or f["error"]:
            failed.append({"id": e.get("id", ""), "name": n, "error": (f or {}).get("error", "未返回该仓库")})
            continue
        reasons = []
        if f["archived"] and not e.get("archived"):
            reasons.append("远端已归档（archived=true）")
        if f["pushed_at"] < stale_before:
            reasons.append(f"最近 push {f['pushed_at']} 早于硬线 {stale_before}")

        changes, noise = [], []
        for field, old_v, new_v in (("Star", e.get("stars"), f["stars"]),
                                    ("Fork", e.get("forks"), f["forks"])):
            if old_v != new_v:
                bucket = noise if _is_noise(field, old_v, new_v, noise_abs, noise_pct) else changes
                bucket.append((field, f"{old_v or 0:,}", f"{new_v:,}"))
        # 许可证与 push 时间**不设噪声门槛**：任何变动都是有意义的
        if norm_field(e.get("license")) != norm_field(f["license"]):
            changes.append(("许可证", norm_field(e.get("license")), norm_field(f["license"])))
        if e.get("pushed_at") != f["pushed_at"]:
            changes.append(("最近 push", e.get("pushed_at", "—"), f["pushed_at"]))

        row = {"id": e.get("id", ""), "name": n, "old": e, "new": f,
               "reasons": reasons, "changes": changes, "noise": noise}
        if reasons and n not in rejected:
            need_drop.append(row)
        elif changes:
            drift.append(row)
        elif noise:
            wave.append(row)
        else:
            ok.append(row)
    return {"need_drop": need_drop, "drift": drift, "wave": wave, "ok": ok, "failed": failed}


def render(checklist: Path, today: date, stale_before: str, res: dict, total: int,
           noise_abs: int = 5, noise_pct: float = 0.5) -> str:
    nd, dr, wv, ok, fa = (res["need_drop"], res["drift"], res["wave"],
                          res["ok"], res["failed"])
    L = [
        "---",
        f"tags: [开源项目, 复核报告]",
        f"created: {today.isoformat()}",
        f"source: GitHub API 实测（{today.isoformat()} 取数）",
        "---",
        "",
        f"# 开源项目清单 · 季度复核报告（{today.isoformat()}）",
        "",
        f"> 对象：`{checklist.name}` §四 入选清单（共 {total} 条）　｜　停更硬线：`pushed_at ≥ {stale_before}`",
        f"> 取数：GitHub API 实测、**绕过缓存**（经 ai-workflow `http_fetch.py --github-repo --no-cache`，走 api.github.com）",
        "> 范围：仅复核 §四 入选条目；**不含** §五「已剔除与不推荐」（那些条目本就已出局，其 Star 不刷新）",
        "> 本报告只给**建议动作**，不自动改清单 —— 改不改由人定。",
        "",
        "## 一、复核结论",
        "",
        "| 类别 | 条数 | 含义 |",
        "|---|---|---|",
        f"| **建议移出** | {len(nd)} | 远端已归档，或已超出停更硬线 |",
        f"| **需回写** | {len(dr)} | Star / Fork / 许可证 / push 时间有变化，须刷新清单 |",
        f"| **正常波动** | {len(wv)} | 仅 Star/Fork 有 ≤ {noise_abs} 且 ≤ {noise_pct}% 的变化，不回写、仅汇总 |",
        f"| **无需处理** | {len(ok)} | 四项均无变化 |",
        f"| **取数失败** | {len(fa)} | 未取到数据，须人工确认（**不等于**该仓库不存在） |",
        "",
        f"> 上表五类**互斥**，相加 = {len(nd) + len(dr) + len(wv) + len(ok) + len(fa)}，"
        f"应等于条目总数 {total}。",
        "",
    ]
    if nd:
        L += ["## 二、建议移出（改清单 §五「已剔除与不推荐」）", "",
              "| id | 项目 | 原 Star | 原 push | 当前状态 | 触发理由 |", "|---|---|---|---|---|---|"]
        for r in nd:
            st = "已归档" if r["new"]["archived"] else "未归档"
            L.append(f"| {r['id']} | `{r['name']}` | {r['old'].get('stars', 0):,} | "
                     f"{r['old'].get('pushed_at', '—')} | {st} / {r['new']['pushed_at']} | "
                     f"{'；'.join(r['reasons'])} |")
        L += ["", "> 移出时按模板补「剔除理由」列，理由用上表「触发理由」，勿改写为泛泛表述。", ""]
    else:
        L += ["## 二、建议移出", "", "无。本次全部条目均未归档、且都在停更硬线之内。", ""]

    L += ["## 三、需回写的指标变化（对应清单 §四条目）", ""]
    if dr:
        L += ["| id | 项目 | 字段 | 清单现值 | 实测值 |", "|---|---|---|---|---|"]
        for r in dr:
            for field, old, new in r["changes"]:
                L.append(f"| {r['id']} | `{r['name']}` | {field} | {old} | {new} |")
        L += ["", "> Star 与 Fork 会实时变动，逐项回写即可；若某条变化幅度异常大，"
                  "值得单独看一眼它近期发生了什么。**许可证或 push 时间有变化时**，"
                  "还要顺带确认该条目的备注是否仍成立。", ""]
    else:
        L += ["无。全部条目的四项指标与清单记录一致。", ""]

    if wv:
        detail = "；".join(
            r["id"] + "".join(f" {f[0]} {f[1]}→{f[2]}" for f in r["noise"]) for r in wv)
        L += [f"另有 **{len(wv)} 条**仅有 Star/Fork 的微小变化（≤ {noise_abs} 且 ≤ {noise_pct}%），"
              "视为正常波动，不列入回写；此处完整列出以备核对：", "",
              f"> {detail}", ""]

    if fa:
        L += ["## 四、取数失败（须人工确认）", "",
              "| 项目 | 错误 |", "|---|---|"]
        for r in fa:
            L.append(f"| `{r['name']}` | {r['error']} |")
        L += ["", "> 取数失败**不等价于**仓库不存在或已删除 —— 网络、限流、改名都可能造成。"
                  "须单独复核后再决定处理方式，**不要直接据此删条目**。", ""]
    else:
        L += ["## 四、取数失败", "", "无。全部条目均取到数据。", ""]

    L += [
        "## 五、建议动作清单（照做即可）",
        "",
    ]
    actions = []
    for r in nd:
        actions.append(f"1. 把 `{r['name']}`（{r['id']}）从 §四移到 §五，理由：{'；'.join(r['reasons'])}")
    for r in dr:
        fld = "、".join(f[0] for f in r["changes"])
        actions.append(f"1. 更新 `{r['name']}`（{r['id']}）的 {fld}")
    if actions:
        L += actions
        L += ["", f"1. 更新清单 frontmatter 的 `updated` 为 `{today.isoformat()}`",
              "1. 清单有变动时，同步更新 `00-索引\\索引.md` 的「🧰 开源项目收录」分区日期与一行维护日志",
              "1. 向工作区 `Ledger.md` 追加一行"]
    else:
        L += ["无待处理项 —— 本次复核四项指标全部一致，无需改动清单。"]
    L += ["", "---", "",
          f"> 本报告由自研工具 `quarterly_recheck.py` 生成："
          f"https://github.com/Garvin666/ai-workflow-tools/blob/main/scripts/quarterly_recheck.py", ""]
    return "\n".join(L)


def main() -> None:
    ap = argparse.ArgumentParser(description="开源项目精选清单 · 时效复核（只报告，不改清单）")
    ap.add_argument("--checklist", default="", help="清单 MD 路径；缺省取同目录最新的 开源项目精选清单-*.md")
    ap.add_argument("--out", default="", help="报告输出路径；缺省为清单同目录 复核报告-<日期>.md")
    ap.add_argument("--fetcher", default=str(DEFAULT_FETCHER), help="ai-workflow 的 http_fetch.py 路径")
    ap.add_argument("--stale-days", type=int, default=DEFAULT_STALE_DAYS, help="停更硬线天数（默认 183）")
    ap.add_argument("--noise-abs", type=int, default=5, help="Star/Fork 变化的绝对噪声门槛（默认 5）")
    ap.add_argument("--noise-pct", type=float, default=0.5, help="Star/Fork 变化的相对噪声门槛百分数（默认 0.5）")
    ap.add_argument("--date", default="", help="复核日期 YYYY-MM-DD（默认今天）")
    ap.add_argument("--lint-only", action="store_true",
                    help="只做离线格式与一致性校验（不联网、不落文件）；有问题即退出码 2")
    args = ap.parse_args()

    today = date.fromisoformat(args.date) if args.date else date.today()
    stale_before = (today - timedelta(days=args.stale_days)).isoformat()

    if args.checklist:
        checklist = Path(args.checklist)
    else:
        cands = sorted(Path(".").glob("开源项目精选清单-*.md"))
        if not cands:
            cands = sorted((Path(__file__).parent).glob("开源项目精选清单-*.md"))
        if not cands:
            print("[FAIL] 找不到清单文件（开源项目精选清单-*.md）", file=sys.stderr)
            sys.exit(2)
        checklist = cands[-1]

    # —— 离线自检模式：不联网、不落文件，新增条目后随手跑 ——
    if args.lint_only:
        l_entries, l_problems = lint_checklist(checklist)
        print(render_lint(checklist, l_entries, l_problems))
        sys.exit(2 if any(lv == "FAIL" for lv, _ in l_problems) else 0)

    entries, rejected, warnings, n_metric = parse_checklist(checklist)
    for w in warnings:
        print(f"[WARN] {w}", file=sys.stderr)

    # —— 解析完整性对账：宁可报错，也不产出一份「少了一条却看起来正常」的报告 ——
    if not entries:
        print(f"[FAIL] 未能从 {checklist} 解析出任何条目 —— 清单格式可能已变，请核对 HEAD_RE/METRIC_RE",
              file=sys.stderr)
        sys.exit(2)
    problems = check_parse_integrity(entries, n_metric)
    if problems:
        print("[FAIL] 解析不完整，拒绝产出报告（清单格式或人工编辑有问题）：", file=sys.stderr)
        for p in problems:
            print(f"  · {p}", file=sys.stderr)
        sys.exit(2)
    ok_entries = [e for e in entries if e.get("name") and "stars" in e]

    fetcher = Path(args.fetcher)
    if not fetcher.exists():
        print(f"[FAIL] 取数脚本不存在：{fetcher}", file=sys.stderr)
        sys.exit(2)

    fetched = fetch(fetcher, [e["name"] for e in ok_entries])
    if not fetched:
        print("[FAIL] 全部取数失败 —— 疑似网络问题（api.github.com 不可达），不产出报告", file=sys.stderr)
        sys.exit(2)

    res = classify(ok_entries, fetched, rejected, stale_before, args.noise_abs, args.noise_pct)
    report = render(checklist, today, stale_before, res, len(ok_entries), args.noise_abs, args.noise_pct)
    out = Path(args.out) if args.out else checklist.parent / f"复核报告-{today.isoformat()}.md"
    out.write_text(report, encoding="utf-8")

    print(f"清单：{checklist.name}｜解析 {len(ok_entries)} 条｜取到 {len(fetched)} 条")
    print(f"建议移出 {len(res['need_drop'])}｜需回写 {len(res['drift'])}｜"
          f"正常波动 {len(res['wave'])}｜无需处理 {len(res['ok'])}｜取数失败 {len(res['failed'])}")
    print(f"报告：{out}")


if __name__ == "__main__":
    main()
