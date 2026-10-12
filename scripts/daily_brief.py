#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
[自研工具] daily_brief — 每日精选资讯简报生成

用途：抓取多类型公开 RSS 源（预印本 / 期刊会议 / 行业博客 / 新闻媒体 / 技术社区 /
      政策机构），按分类整理，先做规则粗筛（时效 + 每源限额 + 去重）产出候选清单，
      再由模型从候选中精选，渲染成「首页速览 + 分栏简讯」形态的 PDF 简报，
      归档入个人知识库（Obsidian）。

适用场景：个人知识库的日常资讯聚合与归档；任何需要「多源 RSS 聚合 → 粗筛 → 精选 →
          PDF/HTML 简报」的定时任务。

仓库链接：https://github.com/Garvin666/ai-workflow-tools/blob/main/scripts/daily_brief.py

设计约束（重要）：
  * 本脚本被自动化定时任务调用，**运行时 CWD 不确定** —— 路径一律按绝对路径解析。
  * 工作区根取 `KB_ROOT` 环境变量；缺省由脚本位置推导（本脚本位于 <库根>/tasks/<任务>/）。
    只读写工作区根内，不越界。
  * 零第三方依赖（RSS 用标准库 xml.etree 解析；feedparser 本机未安装）。
  * **抓取源必须本机实测可达**才写入 SOURCES（见 tasks/.../源清单.md）。
  * PDF 走本机 Chrome/Edge headless 打印 —— WeasyPrint 在本机缺 GTK 依赖不可用。
  * **失败可见**：源全部失败时以非零退出码结束，不产出空报告。

两段式流程（2026-10-09 起）：
  ┌ 第 1 段（脚本）：全量抓取 → 时效过滤 + 每源限额 + 跨期/期内去重 → 候选清单.json
  └ 第 2 段（模型）：读候选清单，按「相关度 × 信息量/新颖度 × 跨源印证」精选 →
                    写 精选.json（中文标题 + 简要摘要）→ 重跑脚本套用并渲染
  无 精选.json 时**自动退化**为「每类按时效取前 N 条」，简报照常产出（不静默失败）。
"""

import os
import re
import sys
import json
import html
import gzip
import zlib
import time
import email.utils
import shutil
import subprocess
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from xml.etree import ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed

# ---------------------------------------------------------------- 路径常量
# 工作区根：优先环境变量 KB_ROOT；缺省由脚本位置推导（本脚本位于 <库根>/tasks/<任务>/）
KB_ROOT = os.environ.get("KB_ROOT") or os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
OUT_ROOT = os.path.join(KB_ROOT, "30-产出", "资讯日报")
TASK_DIR = os.path.join(KB_ROOT, "tasks", "资讯日报自动化-2026-10-08")
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")

CST = timezone(timedelta(hours=8))

# ---------------------------------------------------------------- 源配置
# ⚠️ 只有本机实测可达的源才在此列（实测记录见 tasks/.../源清单.md）。
#    格式：(显示名, URL, 内容分类, 来源类型)
#    来源类型：期刊会议 / 行业博客 / 新闻媒体 / 技术社区 / 政策机构 / 开源项目
#    ⚠️ 2026-10-10 用户口径：**不要再从 arXiv（预印本）找** —— 已移除全部 arXiv 源，
#       改为「新闻讯息 + GitHub 每日热门项目」。GitHub 走 api.github.com（可达），
#       github.com/trending 本机被墙不可用。
SOURCES = [
    # ---- GitHub 每日热门项目（api.github.com/search，{d5} 由脚本替换为「今天-5 天」）
    ("GitHub 热门项目",    "https://api.github.com/search/repositories?q=created:%3E{d5}&sort=stars&order=desc&per_page=20", "开源项目", "开源项目"),
    ("GitHub 量化项目",    "https://api.github.com/search/repositories?q=topic:quantitative-finance+created:%3E{d5}&sort=stars&order=desc&per_page=15", "开源项目", "开源项目"),
    ("GitHub 运筹优化项目", "https://api.github.com/search/repositories?q=topic:optimization+created:%3E{d5}&sort=stars&order=desc&per_page=15", "开源项目", "开源项目"),
    ("GitHub LLM 项目",    "https://api.github.com/search/repositories?q=topic:llm+created:%3E{d5}&sort=stars&order=desc&per_page=15", "开源项目", "开源项目"),
    # ---- 期刊 / 会议
    ("JMLR",              "https://www.jmlr.org/jmlr.xml",                       "数据科学",     "期刊会议"),
    ("Nature Mach. Intell.", "https://www.nature.com/natmachintell.rss",         "AI与大模型",   "期刊会议"),
    ("Science News",      "https://www.science.org/rss/news_current.xml",        "数学建模",     "期刊会议"),
    # ---- 数学 / 科学新闻与博客（补 数学建模 方向）
    ("Quanta Magazine",   "https://www.quantamagazine.org/feed/",                "数学建模",     "新闻媒体"),
    ("Simons Foundation", "https://www.simonsfoundation.org/feed/",              "数学建模",     "新闻媒体"),
    ("Terence Tao",       "https://terrytao.wordpress.com/feed/",                "数学建模",     "行业博客"),
    # ---- 人类最新科技动向（前沿科学/工程新闻，跨领域）
    ("Phys.org",          "https://phys.org/rss-feed/",                          "科技动向",     "新闻媒体"),
    ("ScienceDaily",      "https://www.sciencedaily.com/rss/top/science.xml",    "科技动向",     "新闻媒体"),
    ("IEEE Spectrum",     "https://spectrum.ieee.org/feeds/feed.rss",            "科技动向",     "新闻媒体"),
    ("New Scientist",     "https://www.newscientist.com/feed/home/",             "科技动向",     "新闻媒体"),
    # ---- 政策 / 监管 / 央行（Fed FEDS、ECB 见源清单的不可达/解析失败登记）
    ("NBER WP",           "https://www.nber.org/rss/new.xml",                    "政策与监管动态", "政策机构"),
    ("Fed FEDS",          "https://www.federalreserve.gov/feeds/feds.xml",       "政策与监管动态", "政策机构"),
    ("Bank of England",   "https://www.bankofengland.co.uk/rss/publications",    "政策与监管动态", "政策机构"),
    ("NY Fed Liberty St.", "https://libertystreeteconomics.newyorkfed.org/feed/", "量化分析",    "政策机构"),
    # ---- 量化 / 金融行业博客
    ("Alpha Architect",   "https://alphaarchitect.com/feed/",                    "量化分析",     "行业博客"),
    ("Quantpedia",        "https://quantpedia.com/feed/",                        "量化分析",     "行业博客"),
    ("Robot Wealth",      "https://www.robotwealth.com/feed/",                   "量化分析",     "行业博客"),
    ("QuantInsti",        "https://blog.quantinsti.com/feed/",                   "量化分析",     "行业博客"),
    # ---- 行业博客 / 实验室
    ("OpenAI",            "https://openai.com/news/rss.xml",                     "AI与大模型",   "行业博客"),
    ("Google DeepMind",   "https://deepmind.google/blog/rss.xml",                "AI与大模型",   "行业博客"),
    ("Google AI Blog",    "https://blog.google/technology/ai/rss/",              "AI与大模型",   "行业博客"),
    ("Apple ML",          "https://machinelearning.apple.com/rss.xml",           "AI与大模型",   "行业博客"),
    ("Meta Engineering",  "https://engineering.fb.com/feed/",                    "AI与大模型",   "行业博客"),
    ("NVIDIA Blog",       "https://blogs.nvidia.com/feed/",                      "AI与大模型",   "行业博客"),
    ("Databricks",        "https://www.databricks.com/feed",                     "AI与大模型",   "行业博客"),
    ("MS DevBlogs",       "https://devblogs.microsoft.com/feed/",                "AI与大模型",   "行业博客"),
    ("Lilian Weng",       "https://lilianweng.github.io/index.xml",              "AI与大模型",   "行业博客"),
    ("Alignment Forum",   "https://www.alignmentforum.org/feed.xml",             "AI与大模型",   "行业博客"),
    ("BAIR Berkeley",     "https://bair.berkeley.edu/blog/feed.xml",             "AI与大模型",   "行业博客"),
    ("The Gradient",      "https://thegradient.pub/rss/",                        "AI与大模型",   "行业博客"),
    # ---- 新闻媒体
    ("MIT Tech Review",   "https://www.technologyreview.com/feed/",              "AI与大模型",   "新闻媒体"),
    ("TechCrunch AI",     "https://techcrunch.com/category/artificial-intelligence/feed/", "AI与大模型", "新闻媒体"),
    ("The Verge AI",      "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml", "AI与大模型", "新闻媒体"),
    ("Wired AI",          "https://www.wired.com/feed/tag/ai/latest/rss",        "AI与大模型",   "新闻媒体"),
    ("MarkTechPost",      "https://www.marktechpost.com/feed/",                  "AI与大模型",   "新闻媒体"),
    ("Ars Technica",      "https://feeds.arstechnica.com/arstechnica/index",     "AI与大模型",   "新闻媒体"),
    ("MIT News AI",       "https://news.mit.edu/rss/topic/artificial-intelligence2", "AI与大模型", "新闻媒体"),
    ("量子位",             "https://www.qbitai.com/feed",                          "AI与大模型",   "新闻媒体"),
    ("雷峰网",             "https://www.leiphone.com/feed",                        "AI与大模型",   "新闻媒体"),
    ("IT之家",             "https://www.ithome.com/rss/",                          "AI与大模型",   "新闻媒体"),
    ("Solidot",           "https://www.solidot.org/index.rss",                   "数据科学",     "新闻媒体"),
    ("钛媒体",             "https://www.tmtpost.com/rss",                          "数据科学",     "新闻媒体"),
    # ---- 技术社区
    ("Hacker News",       "https://hnrss.org/frontpage",                         "数据科学",     "技术社区"),
    ("Lobsters",          "https://lobste.rs/rss",                               "数据科学",     "技术社区"),
    ("GitHub Blog",       "https://github.blog/feed/",                           "数据科学",     "技术社区"),
    ("StackOverflow Blog", "https://stackoverflow.blog/feed/",                   "数据科学",     "技术社区"),
    ("InfoQ 中文",         "https://www.infoq.cn/feed",                           "数据科学",     "技术社区"),
    ("掘金",               "https://juejin.cn/rss",                               "数据科学",     "技术社区"),
    ("开源中国",           "https://www.oschina.net/news/rss",                    "数据科学",     "技术社区"),
    ("少数派",             "https://sspai.com/feed",                              "数据科学",     "技术社区"),
    ("极客公园",           "https://www.geekpark.net/rss",                        "数据科学",     "技术社区"),
]

# 分类呈现顺序（2026-10-10：新增「开源项目」「科技动向」并把新闻/GitHub 置前）
CATEGORY_ORDER = ["开源项目", "科技动向", "AI与大模型", "量化分析", "数据科学", "数学建模", "政策与监管动态"]

# ---------------------------------------------------------------- 筛选参数
RECENT_DAYS = 7              # 时效窗口：发布日期在此天数内的条目才进入候选
STALE_DAYS = 60              # 超过此天数视为「本期无更新」，不再入选（含无日期源的兜底）
PER_SOURCE_LIMIT = 8         # 非预印本源：每源最多进入候选的条数
PREPRINT_LIMIT = 12          # 预印本源：单日可产出数百条，给更大池子供模型挑选
NO_DATE_FALLBACK_LIMIT = 4   # 无日期字段的源（NBER / JMLR）：取最新 N 条，靠跨期去重防重复
CAND_SUMMARY_CHARS = 180     # 候选清单里摘要的截断长度（控制模型输入规模）

# 精选密度参数
TOTAL_PICK_LIMIT = 10        # 全天精选总上限（2026-10-10 用户口径：只精选 10 条最有用）
PICK_PER_CATEGORY = {        # 每类精选上限（模型可从候选中最多选这么多；宁少勿滥）
    "开源项目": 4,
    "科技动向": 2,
    "量化分析": 5,
    "AI与大模型": 6,
    "数学建模": 3,
    "数据科学": 4,
    "政策与监管动态": 4,
}
HEADLINE_PER_CATEGORY = 2    # 首页速览每分类条数
BRIEF_CHARS = 150            # 英文要点硬上限（无中文对照时的兜底）
HEADLINE_CHARS = 120         # 首页速览英文要点硬上限（兜底）
ZH_BRIEF_MAX = 260           # 正文中文要点硬上限（正常 130–180 字，此为防跑飞的兜底）
ZH_HEADLINE_MAX = 120        # 速览中文上限：取中文要点前 1–2 句

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")


# ---------------------------------------------------------------- 工具函数
def log(msg):
    line = "[%s] %s" % (datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(os.path.join(LOG_DIR, "daily_brief.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def fetch(url, timeout=20, retries=2):
    """抓取 URL，返回解压后的 bytes；失败抛异常。

    ⚠️ 必须处理压缩：本机实测 Google DeepMind 等源无论是否声明 Accept-Encoding
    都强制返回 gzip 流，urllib 不会自动解压 —— 不解压会导致 XML 解析直接失败。
    """
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA, "Accept": "*/*", "Accept-Encoding": "gzip, deflate"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                enc = (resp.headers.get("Content-Encoding") or "").lower()
            if enc == "gzip" or raw[:2] == b"\x1f\x8b":
                raw = gzip.decompress(raw)
            elif enc == "deflate":
                try:
                    raw = zlib.decompress(raw)
                except zlib.error:
                    raw = zlib.decompress(raw, -zlib.MAX_WBITS)
            return raw
        except Exception as e:  # noqa: BLE001
            last = e
            if attempt < retries:
                time.sleep(2 * (attempt + 1))
    raise last


def parse_date(pub):
    """把各种 RSS/Atom 日期格式解析为 date（CST）；无法解析返回 None。

    实测本机可用源的日期格式至少三类：
      * RFC822  "Thu, 08 Oct 2026 21:00:00 +0000"   （多数 RSS）
      * ISO8601 "2026-10-08T17:54:31-04:00"         （Atom）
      * 纯日期   "2026-10-07"                        （Nature 系 RDF）
    另有只有年份的（JMLR "2026"）—— 无法用于时效判断，返回 None 走无日期兜底。
    """
    s = (pub or "").strip()
    if not s:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(s)
        if dt:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(CST).date()
    except Exception:  # noqa: BLE001
        pass
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=CST)
        return dt.astimezone(CST).date()
    except Exception:  # noqa: BLE001
        pass
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).date()
        except ValueError:
            return None
    return None


def strip_html(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def local_tag(tag):
    return tag.split("}")[-1] if "}" in tag else tag


def _sanitize_xml(text):
    """剔除 XML 1.0 不允许的控制字符 —— 少数源（如 ECB）会混入导致解析失败。"""
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)


def parse_rss(raw, source_name, category, kind):
    """用标准库解析 RSS/Atom（含 RDF），返回条目列表。"""
    text = raw.decode("utf-8", errors="replace").lstrip("\ufeff")
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        root = ET.fromstring(_sanitize_xml(text))

    items = []
    for node in root.iter():
        if local_tag(node.tag) not in ("item", "entry"):
            continue

        title, link, desc, pub = "", "", "", ""
        for child in node:
            t = local_tag(child.tag)
            if t == "title":
                title = strip_html(child.text or "")
            elif t == "link":
                if not link:
                    link = (child.get("href") or child.text or "").strip()
            elif t in ("description", "summary", "abstract", "content"):
                if not desc:
                    desc = strip_html(child.text or "")
            elif t in ("pubDate", "published", "updated", "date"):
                if not pub:
                    pub = strip_html(child.text or "")

        # arXiv RSS 的 description 常含 "arXiv:xxxx Announce Type: new Abstract: ..."
        desc = re.sub(r"^arXiv:\S+\s*Announce Type:\s*\w+\s*", "", desc)
        desc = re.sub(r"^Abstract:\s*", "", desc)
        # NBER 标题常带 " -- by 作者列表"，简报中剥离作者以省版面
        if source_name == "NBER WP":
            title = re.sub(r"\s*--\s*by\s+.*$", "", title).strip()

        if title and link:
            d = parse_date(pub)
            items.append({
                "source": source_name,
                "kind": kind,
                "category": category,
                "title": title,
                "link": link,
                "summary": desc[:600],
                "published": pub,
                "date": d.isoformat() if d else "",
            })
    return items


def parse_github(raw, source_name, category, kind):
    """解析 api.github.com/search/repositories 的 JSON 结果（GitHub 每日热门项目）。

    title = 仓库全名（owner/repo），summary = 简介 + Star/语言，date = 创建日期。
    """
    data = json.loads(raw.decode("utf-8", errors="replace"))
    items = []
    for repo in (data.get("items") or []):
        full = (repo.get("full_name") or repo.get("name") or "").strip()
        url = (repo.get("html_url") or "").strip()
        if not (full and url):
            continue
        desc = (repo.get("description") or "").strip()
        stars = repo.get("stargazers_count")
        lang = (repo.get("language") or "").strip()
        created = (repo.get("created_at") or "")[:10]

        meta = []
        if stars is not None:
            meta.append("Star %s" % stars)
        if lang:
            meta.append(lang)
        summary = desc
        if meta:
            summary = (desc + " " if desc else "") + "（%s）" % " · ".join(meta)

        items.append({
            "source": source_name,
            "kind": kind,
            "category": category,
            "title": full,
            "link": url,
            "summary": summary.strip()[:600],
            "published": created,
            "date": created,
        })
    return items


def collect():
    """并发抓取全部源，返回 (条目列表, 源状态列表)。"""
    all_items, status = [], []

    def one(name, url, category, kind):
        try:
            if "{d5}" in url:  # GitHub 热门：created:>{今天-5 天}
                url = url.replace("{d5}", (datetime.now(CST).date() - timedelta(days=5)).isoformat())
            raw = fetch(url)
            if kind == "开源项目":
                parsed = parse_github(raw, name, category, kind)
            else:
                parsed = parse_rss(raw, name, category, kind)
            return name, kind, category, parsed, None
        except Exception as e:  # noqa: BLE001
            return name, kind, category, [], "%s: %s" % (type(e).__name__, str(e)[:110])

    results = {}
    with ThreadPoolExecutor(max_workers=10) as ex:
        futs = {ex.submit(one, n, u, c, k): n for n, u, c, k in SOURCES}
        for f in as_completed(futs):
            name, kind, category, items, err = f.result()
            results[name] = (kind, category, items, err)

    # 按 SOURCES 原顺序输出日志与状态，保证可读与可复现
    for name, url, category, kind in SOURCES:
        _k, _c, items, err = results.get(name, (kind, category, [], "未执行"))
        if err:
            status.append({"源": name, "类型": kind, "抓取": 0, "入选": 0,
                           "状态": "失败", "说明": err})
            log("  失败: %s -> %s" % (name, err))
        elif not items:
            status.append({"源": name, "类型": kind, "抓取": 0, "入选": 0,
                           "状态": "空", "说明": "HTTP 成功但未解析出条目"})
            log("  空源: %s" % name)
        else:
            all_items.extend(items)
            status.append({"源": name, "类型": kind, "抓取": len(items), "入选": 0,
                           "状态": "成功", "说明": ""})
            log("  成功: %s -> %d 条" % (name, len(items)))
    return all_items, status


def load_recent_links(days=7, exclude_date=None):
    """读取最近 N 天归档里的链接，用于跨期去重。

    exclude_date: 要排除的日期目录名（如 '2026-10-08'）—— 同一天重跑时，
    必须排除当日自己的产物，否则会把上一次的输出当成"历史"去重，
    导致条目数随重跑次数递减（这是 2026-10-08 实测踩到的真实缺陷）。
    """
    seen = set()
    if not os.path.isdir(OUT_ROOT):
        return seen
    cutoff = datetime.now(CST).date() - timedelta(days=days)
    for name in os.listdir(OUT_ROOT):
        d = os.path.join(OUT_ROOT, name)
        if not os.path.isdir(d):
            continue
        if exclude_date and name == exclude_date:
            continue
        try:
            if datetime.strptime(name, "%Y-%m-%d").date() < cutoff:
                continue
        except ValueError:
            continue
        p = os.path.join(d, "items.json")
        if not os.path.isfile(p):
            continue
        try:
            with open(p, encoding="utf-8") as f:
                for it in json.load(f):
                    seen.add(_norm_link(it.get("link", "")))
        except Exception:  # noqa: BLE001
            pass
    return seen


def filter_candidates(items, status, today):
    """规则粗筛：时效过滤 + 每源限额。返回 (候选列表, 各源入选数, 备注)。

    设计要点（均为实测驱动）：
      * **时效窗口**：新闻/博客源给的是「最新 N 条」，可能跨数周（OpenAI 有 1258 条
        全历史、BAIR 停在两个多月前）。不过滤会把旧闻当今日资讯。
      * **无日期兜底**：NBER / JMLR 的 RSS 完全没有日期字段。若按日期过滤会整源归零
        —— 对无日期源改为取最新 N 条，靠跨期去重防止每天重复。
      * **每源限额**：arXiv 单分类单日 447–512 条，不限额会淹没其他来源，
        使「多源覆盖」名存实亡。
    """
    notes = {}
    by_source = {}
    for it in items:
        by_source.setdefault(it["source"], []).append(it)

    picked = []
    for s in status:
        name = s["源"]
        lst = by_source.get(name, [])
        if not lst:
            continue

        dated = [x for x in lst if x.get("date")]
        undated = [x for x in lst if not x.get("date")]

        fresh = []
        for x in dated:
            try:
                d = datetime.strptime(x["date"], "%Y-%m-%d").date()
            except ValueError:
                continue
            if 0 <= (today - d).days <= RECENT_DAYS:
                fresh.append(x)

        # 无日期源（或含无日期条目）兜底：取最新 N 条
        if undated:
            fresh.extend(undated[:NO_DATE_FALLBACK_LIMIT])
            notes[name] = "该源无发布日期字段，按最新 %d 条入选" % min(len(undated), NO_DATE_FALLBACK_LIMIT)

        if not fresh:
            # 有日期但全部超出时效窗口 —— 如实标注「本期无更新」，不算失败
            if dated:
                newest = max(x["date"] for x in dated)
                days = (today - datetime.strptime(newest, "%Y-%m-%d").date()).days
                if days <= STALE_DAYS:
                    notes[name] = "本期无更新（最新 %s）" % newest
                    s["状态"] = "无更新"
                else:
                    notes[name] = "长期未更新（最新 %s）" % newest
                    s["状态"] = "无更新"
            continue

        limit = PREPRINT_LIMIT if s["类型"] == "预印本" else PER_SOURCE_LIMIT
        fresh.sort(key=lambda x: x.get("date") or "", reverse=True)
        fresh = fresh[:limit]

        picked.extend(fresh)
        s["入选"] = len(fresh)

    return picked, notes


def dedupe(items, seen_links):
    """跨期去重 + 期内去重（按规范化链接）。返回 (去重后列表, 丢弃数)。

    ⚠️ 必须同时做**期内去重**：同一论文可能出现在多个源（arXiv cross-list 会把
    一篇论文同时投到 q-fin.PM 与 q-fin.ST），2026-10-08 实测 DSTNet 被收录两次。
    """
    dropped = 0
    out, seen = [], set()
    for it in items:
        k = _norm_link(it["link"])
        if k in seen_links:
            dropped += 1
            continue
        if k in seen:
            dropped += 1
            continue
        seen.add(k)
        out.append(it)
    return out, dropped


def sort_items(items):
    """按分类顺序 + 日期倒序排列（预印本同日则保持源内原序）。"""
    order = {c: i for i, c in enumerate(CATEGORY_ORDER)}
    return sorted(items, key=lambda x: (order.get(x["category"], 99), x.get("date") or ""),
                  reverse=False)


# ---------------------------------------------------------------- 文本蒸馏
# 目标：无中文对照时，把英文摘要压成一句可快速扫读的要点；不做翻译，保留术语原文。
_STOP_LEAD = re.compile(
    r"^(In this (paper|work|article|study)|We (present|propose|introduce|study|show|develop|"
    r"consider|investigate|address|provide|demonstrate|analyze|analyse|introduce)|"
    r"This (paper|work|article|study)|Here we|These results)\b[:,]?\s*",
    re.IGNORECASE,
)


def make_brief(summary, limit=BRIEF_CHARS):
    """从英文摘要中蒸馏一句要点。保留原文语言，只做首句截取 + 引导语剥离 + 硬截断。

    ⚠️ 长度必须在 Python 侧硬控：CSS 的 -webkit-line-clamp 在 Chromium
    headless 打印时不可靠（实测被忽略），靠 CSS 截断会得到满段文字。
    """
    if not summary:
        return ""
    s = summary.strip()

    # 剥离常见引导语（"In this paper, we ..." → "We ..."），**先剥再断句**，
    # 否则引导语会吃掉断句位置、导致要点被拉长
    s = _STOP_LEAD.sub(lambda mm: mm.group(1).strip().capitalize() + " ", s).strip()

    m = re.search(r"[.;:]\s", s)
    if m and m.start() > 30:
        s = s[:m.start()]
    if len(s) > limit:
        head = s[:limit]
        if "," in head:
            s = head.rsplit(",", 1)[0]
        else:
            s = head.rsplit(" ", 1)[0]

    if s:
        s = s[0].upper() + s[1:]
    s = s.rstrip(" ,;:-–—")
    if s and not s.endswith((".", "…", "!", "?", "。")):
        s += " …"
    return s


def _norm_link(link):
    """链接规范化：去锚点、去尾斜杠、统一小写。用于跨源匹配与去重。"""
    return (link or "").split("#")[0].rstrip("/").lower()


def _cut_zh(s, limit):
    """中文文本兜底裁剪：优先在句末收尾，其次在次级标点，最后才硬切。"""
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


def zh_headline(s, limit=ZH_HEADLINE_MAX):
    """速览用：取中文要点的首句；首句过短则续上第二句，使各条长度均衡。

    写作约定 —— `brief_zh` 第一句承载「做了什么/核心结论」，后句补方法与细节；
    因此正文显示全文（更详细），首页速览取前 1–2 句（保持一屏可览）。
    """
    s = (s or "").strip()
    if not s:
        return ""
    sentences = re.findall(r".*?[。！？]", s) or [s]
    first = sentences[0]
    if len(first) >= limit:
        return _cut_zh(first, limit)
    out = first
    for nxt in sentences[1:]:
        if len(out) + len(nxt) > limit:
            if len(out) < limit * 0.6:
                out = _cut_zh(out + nxt, limit)
            break
        out += nxt
    return out


# ---------------------------------------------------------------- 精选通道
def load_picks(out_dir):
    """读取模型产出的 `精选.json`。

    结构：
        {"综述": {"<分类>": "本类当日综述（可选）"},
         "精选": [{"link": "<原样照抄候选清单里的 link>", "category": "…",
                   "title_zh": "…", "brief_zh": "…"}, …]}

    **脚本不做精选判断**（相关性/新颖度须阅读理解，机械规则做不了），
    只负责把模型的选择按链接回溯校验、补齐候选里的元信息（来源/日期/原文标题）。
    无此文件时返回 None，主流程自动退化为「规则排序」。
    """
    p = os.path.join(out_dir, "精选.json")
    if not os.path.isfile(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            picks = data.get("精选") or []
        else:
            picks = data if isinstance(data, list) else []
        return picks
    except Exception as e:  # noqa: BLE001
        log("  精选.json 读取失败（将退化为规则排序）：%s: %s" % (type(e).__name__, e))
        return None


def resolve_picks(picks, candidates):
    """把精选条目按链接回溯到候选，补齐元信息。返回 (可用精选, 被丢弃的链接)。

    ⚠️ 必须校验：2026-10-08 实测过「凭记忆手打编号」导致整批错配 ——
    凡 link 不在当日候选中的，一律丢弃，不信任模型手写的编号。
    """
    cand_map = {}
    for it in candidates:
        cand_map.setdefault(_norm_link(it["link"]), it)

    ok, bad = [], []
    for p in picks:
        k = _norm_link(p.get("link", ""))
        if k in cand_map:
            merged = dict(cand_map[k])
            t = (p.get("title_zh") or "").strip()
            b = (p.get("brief_zh") or "").strip()
            if t:
                merged["title_zh"] = t
            if b:
                merged["brief_zh"] = b
            ok.append(merged)
        else:
            bad.append(p.get("link", ""))
    return ok, bad


def fallback_picks(candidates):
    """无模型精选时的退化：每类按时效取前 N 条（保证简报照常产出，不静默空转）。"""
    by_cat = {}
    for it in candidates:
        by_cat.setdefault(it["category"], []).append(it)
    out = []
    for cat in CATEGORY_ORDER:
        out.extend(by_cat.get(cat, [])[:PICK_PER_CATEGORY.get(cat, 4)])
    return out[:TOTAL_PICK_LIMIT]


def pick_headlines(items):
    """选出首页速览条目：每分类取前 N 条。

    ⚠️ 连续编号的同系列论文（如 "…Data I: …" / "…Data II: …"）会被折叠，
    只保留首条并标注「（同系列另有 N 篇）」，避免速览被近似标题挤占。
    """
    by_cat = {}
    for it in items:
        by_cat.setdefault(it["category"], []).append(it)

    def series_key(title):
        return re.sub(r"\s+(I|II|III|IV|V|1|2|3|4|5)\s*:", " #:", title).strip()

    picked = []
    for cat in CATEGORY_ORDER:
        lst = by_cat.get(cat, [])
        seen_series = {}
        for it in lst:
            k = series_key(it["title"])
            if k in seen_series:
                seen_series[k]["say_series"] = seen_series.get(k, {}).get("say_series", 1) + 1
                continue
            it = dict(it)
            it["_series_key"] = k
            seen_series[k] = it
            if len(seen_series) > HEADLINE_PER_CATEGORY:
                break
            picked.append(it)
        for it in picked:
            if it.get("_series_key") in seen_series:
                n = seen_series[it["_series_key"]].get("say_series", 1)
                if n > 1:
                    it["_series_note"] = "（同系列另有 %d 篇）" % (n - 1)
    return picked


def _meta_html(it, sep=" ｜ "):
    """生成条目元信息行：来源 ｜ 日期 ｜ 链接（用户要求每条附来源与发布日期）。"""
    parts = [html.escape(it.get("source", ""))]
    if it.get("kind"):
        parts.append(html.escape(it["kind"]))
    parts.append(html.escape(it.get("date") or it.get("published") or "日期未标注"))
    m = sep.join(parts)
    m += sep + '<a href="%s">%s</a>' % (html.escape(it["link"]), html.escape(it["link"]))
    return m


def _meta_md(it, sep=" ｜ "):
    parts = [it.get("source", "")]
    if it.get("kind"):
        parts.append(it["kind"])
    parts.append(it.get("date") or it.get("published") or "日期未标注")
    return "`" + sep.join(parts) + "` " + sep + " " + it["link"]


# ---------------------------------------------------------------- 渲染
HTML_TMPL = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>{title}</title>
<style>
  @page {{ size: A4; margin: 13mm 12mm 14mm 12mm; }}
  * {{ box-sizing: border-box; }}
  body {{ font-family: "Microsoft YaHei","PingFang SC","Noto Sans CJK SC",sans-serif;
         color:#1b1f24; font-size:9.4pt; line-height:1.48; margin:0; }}

  .masthead {{ border-bottom:2.5px solid #0b5cad; padding-bottom:6px; margin-bottom:9px;
               display:flex; align-items:baseline; justify-content:space-between; }}
  .masthead h1 {{ font-size:17pt; margin:0; letter-spacing:1px; color:#0b2a52; }}
  .masthead .date {{ font-size:10pt; color:#0b5cad; font-weight:600; }}

  .stats {{ display:flex; gap:13px; font-size:8.3pt; color:#5a6570; flex-wrap:wrap;
            padding-bottom:6px; border-bottom:1px solid #e3e7eb; margin-bottom:10px; }}
  .stats b {{ color:#0b2a52; font-size:9.4pt; }}
  .zhnote {{ color:#8a6d1f; }}

  .brief-h {{ font-size:11pt; color:#0b2a52; margin:0 0 7px 0; padding:3px 0 3px 8px;
              border-left:3.5px solid #c0392b; background:#fdf5f4; letter-spacing:.5px; }}
  .hl {{ margin:0 0 5px 0; padding-left:10px; position:relative; line-height:1.45; }}
  .hl:before {{ content:"▪"; position:absolute; left:0; color:#0b5cad; font-size:8pt; }}
  .hl .cat {{ display:inline-block; background:#e8eff9; color:#0b5cad; font-size:7.2pt;
              padding:0 5px; border-radius:3px; margin-right:5px; line-height:1.5;
              border:1px solid #d3e0f0; }}
  .hl .ttl {{ font-weight:600; color:#12325c; font-size:9.4pt; }}
  .hl .brf {{ color:#5a6570; font-size:8.5pt; }}
  .hl .src {{ color:#98a1a9; font-size:7.3pt; }}

  .section {{ margin-top:12px; }}
  h2 {{ font-size:11.5pt; margin:0 0 6px 0; padding:4px 0 4px 8px;
        border-left:3.5px solid #0b5cad; background:#f4f7fb; color:#0b2a52; }}
  h2 .cnt {{ font-size:8.4pt; color:#7a848e; font-weight:400; }}

  .row {{ margin:0 0 3.6px 0; padding:0 0 3.4px 0; border-bottom:1px solid #eef1f4;
          page-break-inside:avoid; }}
  .row:last-child {{ border-bottom:none; }}
  .row .t {{ font-weight:600; color:#12325c; font-size:9.4pt; line-height:1.3; }}
  .row .t2 {{ color:#98a1a9; font-size:7.1pt; line-height:1.25; margin-top:0.4px; }}
  .row .b {{ color:#3f4a55; font-size:8.6pt; line-height:1.38; margin-top:0.5px; }}
  .row .m {{ color:#8b949e; font-size:7.2pt; margin-top:0.8px; word-break:break-all; }}
  .row .m a {{ color:#0b5cad; text-decoration:none; }}

  .note {{ background:#f7f9fb; border:1px solid #e3e7eb; border-left:3px solid #7a848e;
           padding:6px 9px; font-size:8pt; color:#5a6570; margin-top:12px; }}
  table {{ width:100%; border-collapse:collapse; font-size:7.6pt; margin-top:5px; }}
  th,td {{ border:1px solid #e3e7eb; padding:2px 4px; text-align:left; }}
  th {{ background:#f4f7fb; font-weight:600; color:#0b2a52; }}
  .fail {{ color:#b3261e; }}
  .warn {{ color:#8a6d1f; }}
  .ok {{ color:#1a7f37; }}
  footer {{ margin-top:12px; padding-top:6px; border-top:1px solid #e3e7eb;
            font-size:7.6pt; color:#98a1a9; }}
  .pb {{ page-break-before:always; }}
</style></head><body>

<div class="masthead">
  <h1>每日精选资讯简报</h1>
  <span class="date">{date_str}</span>
</div>

<div class="stats">
  <span>候选 <b>{n_cand}</b></span>
  <span>精选 <b>{n_pick}</b></span>
  <span>源 <b>{n_ok}/{n_src}</b></span>
  <span>分类 <b>{n_cat}</b></span>
  <span>生成 {generated}</span>
  <span class="zhnote">{mode_note}</span>
</div>

<div class="brief-h">要闻速览</div>
{headlines}

<div class="section pb">{body}</div>

<div class="note">
  <b>源状态</b>
  <table>
    <tr><th>源</th><th>类型</th><th>抓取</th><th>入选</th><th>状态</th><th>说明</th></tr>
    {status_rows}
  </table>
  <div style="margin-top:6px">
  <b>覆盖范围声明</b>：本简报仅聚合上表所列源。未被列出的源（如中文党政网站、付费墙期刊、
  本机不可达的站点）本次<strong>未抓取</strong>——"简报中未出现"不等于"该领域无动态"。
  完整源清单与可达性实测记录见 <code>tasks/资讯日报自动化-2026-10-08/源清单.md</code>。
  各条目的来源、发布日期与链接均已随条标注；中文摘要由模型逐条阅读原文撰写（非机械翻译），
  某条缺中文对照时自动退回英文原文蒸馏。完整内容请循链接查阅。
  </div>
</div>

<footer>由自研工具 daily_brief 生成 ｜ 仅供个人知识归档使用 ｜ 条目版权归原作者所有</footer>
</body></html>
"""


def render_html(items, status, meta, date_str):
    """渲染 HTML。items 为最终呈现的条目（精选或退化结果）。"""
    by_cat = {}
    for it in items:
        by_cat.setdefault(it["category"], []).append(it)

    # ---- 首页要闻速览
    hl = []
    if not items:
        hl.append('<div class="note"><b>本次无入选条目</b>：可达源均已完成抓取，'
                  '但条目在时效窗口内已全部出现过（去重后为空），或该日无新内容。</div>')
    else:
        for it in pick_headlines(items):
            brf = it.get("brief_zh")
            brf = zh_headline(brf) if brf else make_brief(it["summary"], HEADLINE_CHARS)
            title = it.get("title_zh") or it["title"]
            note = it.get("_series_note", "")
            src = it.get("source", "") + (" · " + it["date"] if it.get("date") else "")
            hl.append(
                '<div class="hl">'
                '<span class="cat">%s</span>'
                '<span class="ttl">%s</span>'
                '<span class="brf">%s%s</span> '
                '<span class="src">［%s］</span>'
                '</div>' % (html.escape(it["category"]), html.escape(title),
                            html.escape(brf), html.escape(note), html.escape(src))
            )

    # ---- 分栏简讯
    body = []
    for cat in CATEGORY_ORDER:
        lst = by_cat.get(cat, [])
        if not lst:
            continue
        body.append('<h2>%s <span class="cnt">（%d 条）</span></h2>' % (cat, len(lst)))
        for it in lst:
            title = it.get("title_zh") or it["title"]
            brf = _cut_zh(it["brief_zh"], ZH_BRIEF_MAX) if it.get("brief_zh") \
                else make_brief(it["summary"])
            body.append(
                '<div class="row">'
                '<div class="t">%s</div>' % html.escape(title)
                + ('<div class="t2">%s</div>' % html.escape(it["title"])
                   if it.get("title_zh") and it["title_zh"] != it["title"] else '')
                + ('<div class="b">%s</div>' % html.escape(brf) if brf else '')
                + '<div class="m">%s</div></div>' % _meta_html(it)
            )

    ok = sum(1 for s in status if s["状态"] == "成功")
    def _cls(st):
        return "ok" if st == "成功" else ("warn" if st == "无更新" else "fail")
    srows = "".join(
        '<tr><td>%s</td><td>%s</td><td>%d</td><td>%d</td><td class="%s">%s</td><td>%s</td></tr>'
        % (html.escape(s["源"]), html.escape(s.get("类型", "")), s.get("抓取", 0),
           s.get("入选", 0), _cls(s["状态"]), html.escape(s["状态"]),
           html.escape(meta.get(s["源"], s.get("说明", ""))))
        for s in status
    )
    n_cat = len({it["category"] for it in items})

    return HTML_TMPL.format(
        title="每日精选资讯简报 · %s" % date_str,
        date_str=date_str,
        generated=datetime.now(CST).strftime("%H:%M"),
        n_cand=meta.get("_n_cand", 0), n_pick=len(items),
        n_ok=ok, n_src=len(status), n_cat=n_cat,
        mode_note=meta.get("_mode", ""),
        headlines="\n".join(hl), body="\n".join(body), status_rows=srows,
    )


def render_markdown(items, status, meta, date_str):
    by_cat = {}
    for it in items:
        by_cat.setdefault(it["category"], []).append(it)

    L = ["# 每日精选资讯简报 · %s" % date_str, "",
         "> 生成 %s　｜　候选 %d　｜　精选 %d　｜　成功源 %d/%d　｜　%s"
         % (datetime.now(CST).strftime("%Y-%m-%d %H:%M"), meta.get("_n_cand", 0),
            len(items), sum(1 for s in status if s["状态"] == "成功"), len(status),
            meta.get("_mode", "")), "",
         "## 要闻速览", ""]
    for it in pick_headlines(items):
        brf = it.get("brief_zh")
        brf = zh_headline(brf) if brf else make_brief(it["summary"], HEADLINE_CHARS)
        title = it.get("title_zh") or it["title"]
        L.append("- **[%s]** %s — %s%s　`%s%s`"
                 % (it["category"], title, brf, it.get("_series_note", ""),
                    it.get("source", ""), " · " + it["date"] if it.get("date") else ""))
    L.append("")

    for cat in CATEGORY_ORDER:
        lst = by_cat.get(cat, [])
        if not lst:
            continue
        L += ["## %s（%d 条）" % (cat, len(lst)), ""]
        for it in lst:
            L.append("- **%s**" % (it.get("title_zh") or it["title"]))
            if it.get("title_zh") and it["title_zh"] != it["title"]:
                L.append("  - 原文：%s" % it["title"])
            brf = _cut_zh(it["brief_zh"], ZH_BRIEF_MAX) if it.get("brief_zh") \
                else make_brief(it["summary"])
            if brf:
                L.append("  - %s" % brf)
            L.append("  - %s" % _meta_md(it))
        L.append("")

    L += ["## 源状态", "", "| 源 | 类型 | 抓取 | 入选 | 状态 | 说明 |", "|---|---|---|---|---|---|"]
    for s in status:
        L.append("| %s | %s | %d | %d | %s | %s |"
                 % (s["源"], s.get("类型", ""), s.get("抓取", 0), s.get("入选", 0),
                    s["状态"], meta.get(s["源"], s.get("说明", ""))))
    L += ["", "> **覆盖范围声明**：仅聚合上表所列源；未列出的源（中文党政网站、付费墙期刊、"
          "本机不可达站点）本次未抓取——\"简报中未出现\"不等于\"该领域无动态\"。",
          "> 各条目的来源、发布日期与链接均已随条标注；中文摘要由模型逐条阅读原文撰写（非机械翻译），"
          "缺中文对照时退回英文原文蒸馏。",
          "", "---", "", "*由自研工具 daily_brief 生成，仅供个人知识归档使用。*"]
    return "\n".join(L)


def find_browser():
    """定位本机 Chromium 内核浏览器（Chrome 优先，Edge 兜底）。

    候选路径经环境变量拼装，源码内不写死本机绝对路径；可用 CHROME_PATH 显式指定。
    """
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
    for name in ("chrome", "chrome.exe", "msedge", "msedge.exe"):
        p = shutil.which(name)
        if p:
            return p
    return None


def html_to_pdf(html_path, pdf_path):
    """用 Chromium headless 把 HTML 打印成 PDF。返回 True/False。"""
    browser = find_browser()
    if not browser:
        log("  未找到 Chrome/Edge，无法生成 PDF")
        return False
    url = "file:///" + os.path.abspath(html_path).replace("\\", "/")
    cmd = [
        browser, "--headless=new", "--disable-gpu", "--no-sandbox",
        "--no-pdf-header-footer",
        "--print-to-pdf=%s" % os.path.abspath(pdf_path),
        url,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=120)
        if os.path.isfile(pdf_path) and os.path.getsize(pdf_path) > 1000:
            return True
        log("  PDF 生成异常 rc=%s err=%r" % (r.returncode, r.stderr[:200]))
        return False
    except Exception as e:  # noqa: BLE001
        log("  PDF 生成异常 %s: %s" % (type(e).__name__, e))
        return False


# ---------------------------------------------------------------- 主流程
def main():
    now = datetime.now(CST)
    today = now.date()
    date_str = now.strftime("%Y-%m-%d")
    out_dir = os.path.join(OUT_ROOT, date_str)
    os.makedirs(out_dir, exist_ok=True)

    log("=== daily_brief 启动 %s ===" % now.strftime("%Y-%m-%d %H:%M:%S"))
    log("抓取中（%d 个源）…" % len(SOURCES))
    raw_items, status = collect()

    n_ok = sum(1 for s in status if s["状态"] == "成功")
    if n_ok == 0:
        log("致命：全部源失败，不产出报告（失败可见性设计）")
        with open(os.path.join(out_dir, "FAILED.txt"), "w", encoding="utf-8") as f:
            f.write("全部 %d 个源抓取失败 @ %s\n" % (len(status), now.isoformat()))
            for s in status:
                f.write("  %s: %s\n" % (s["源"], s["说明"]))
        return 2

    # ---- 规则粗筛：时效 + 每源限额
    picked, notes = filter_candidates(raw_items, status, today)
    log("抓取 %d 条 → 粗筛后 %d 条（时效窗口 %d 天 / 每源限额 %d）"
        % (len(raw_items), len(picked), RECENT_DAYS, PER_SOURCE_LIMIT))

    # ---- 去重：跨期（最近 7 天归档）+ 期内（跨源重复）
    seen = load_recent_links(days=7, exclude_date=date_str)
    candidates, n_drop = dedupe(picked, seen)
    candidates = sort_items(candidates)
    log("去重 %d 条 → 候选 %d 条" % (n_drop, len(candidates)))

    # ---- 候选清单（供模型精选；同时作为 items.json 供周链路汇总）
    with open(os.path.join(out_dir, "items.json"), "w", encoding="utf-8") as f:
        json.dump(candidates, f, ensure_ascii=False, indent=2)

    cand_list = [{
        "link": it["link"],
        "category": it["category"],
        "source": it["source"],
        "kind": it["kind"],
        "date": it["date"],
        "title": it["title"],
        "summary": it["summary"][:CAND_SUMMARY_CHARS],
    } for it in candidates]
    with open(os.path.join(out_dir, "候选清单.json"), "w", encoding="utf-8") as f:
        json.dump(cand_list, f, ensure_ascii=False, indent=2)

    # ---- 第 2 段：模型精选
    picks_raw = load_picks(out_dir)
    mode = ""
    if picks_raw is None:
        items = fallback_picks(candidates)
        mode = "本期未做模型精选（为规则排序结果）"
        log("未发现 精选.json —— 退化为规则排序，产出 %d 条" % len(items))
        # 首次运行时提示模型下一步动作
        if candidates:
            log("提示：可读取 候选清单.json（%d 条）后写 精选.json，再重跑本脚本以套用精选"
                % len(candidates))
    else:
        items, bad = resolve_picks(picks_raw, candidates)
        if bad:
            log("警告：%d 条精选不在当日候选中（已丢弃）：%s" % (len(bad), bad[:5]))
        if not items:
            log("致命：精选条目无一匹配当日候选，退化为规则排序")
            items = fallback_picks(candidates)
            mode = "精选校验未通过（已退化为规则排序）"
        else:
            mode = "模型精选 %d 条" % len(items)
        items = sort_items(items)
        log("精选生效：%d 条" % len(items))

    meta = dict(notes)
    meta["_n_cand"] = len(candidates)
    meta["_mode"] = mode

    html_text = render_html(items, status, meta, date_str)
    md_text = render_markdown(items, status, meta, date_str)

    html_path = os.path.join(out_dir, "简报-%s.html" % date_str)
    md_path = os.path.join(out_dir, "简报-%s.md" % date_str)
    pdf_path = os.path.join(out_dir, "简报-%s.pdf" % date_str)

    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html_text)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md_text)

    ok = html_to_pdf(html_path, pdf_path)
    log("PDF: %s -> %s" % ("成功" if ok else "失败", pdf_path))
    log("完成：候选 %d 条 / 呈现 %d 条 / 输出 %s" % (len(candidates), len(items), out_dir))

    print("\n输出目录: %s" % out_dir)
    print("  候选清单: %s（%d 条，供精选）" % (os.path.join(out_dir, "候选清单.json"), len(candidates)))
    print("  MD  : %s" % md_path)
    print("  HTML: %s" % html_path)
    print("  PDF : %s (%s)" % (pdf_path, "OK" if ok else "FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
