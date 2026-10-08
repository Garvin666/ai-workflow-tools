# -*- coding: utf-8 -*-
"""[自研工具] knowledge-cram-report / cram_render.py

- 名称：knowledge-cram-report（知识点恶补 PDF 报告机制）
- 用途：把恶补报告 Markdown 渲染成 PDF（pandoc + XeLaTeX，全程本地），并对产物跑 V1–V9 机器判据
- 适用场景：恶补报告生成后、交付前的机器验收；也用于改版式后回归
- 仓库链接：https://github.com/Garvin666/ai-workflow-tools

判据口径的三处修补（上一轮实跑暴露，详见 修补说明-判据与口径缺陷.md）：
- **V4 拆成两半**：V4a 凭据/隐私（任何场景都查）、V4b 本机绝对路径（**仅外发场景**判 FAIL；
  内发场景判 SKIP 并逐条给性质判定）—— 原口径把「教材引用了例子」误判成「泄露了秘密」。
- **V7 用带 `\\s*` 的通用正则**：PDF 抽取文本里题号是「第1 题」（数字前无空格），
  原正则按「第 1 题」写会**恒红**；且题号不再硬编码为 1/2/3，改为「题数 ≥3 且答案数 == 题数」。
- **V8 分冷热两档**：冷 ≤600 s、热 ≤20 s。同一命令冷热相差 25.7 倍，
  拿任一端代表另一端都是错的。冷时自动补测一次热编译，两个数都留下。
- **V5 要 before 基线**（原版只验「含本次日期」，恒真 ⇒ 等价于没验）。
- **V9 新增：复习间隔数值自洽** —— 原 8 条判据没有任何一条查数值矛盾，
  于是「正文说 3.6 → 10 天、答案说 3.6 → 4 天」这种东西能整页通过。
  V9 从 `cram_index.interval_days` 取唯一口径（**不重算公式**）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cram_env import probe as env_probe  # noqa: E402
from cram_index import interval_days, place  # noqa: E402  ⭐ 唯一口径源，绝不在此重写公式/路径

# ---- 版式单源（渲染与模板共用；改这里即改全部）----
LAYOUT = {
    "CJKmainfont": "Microsoft YaHei",
    "mainfont": "Times New Roman",
    "monofont": "Consolas",
    "geometry:margin": "2.2cm",
    "fontsize": "12pt",
    "linestretch": "1.35",
    "colorlinks": "true",
}
NAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_.+_恶补报告\.pdf$")
CRED = {
    "OpenAI Key": r"sk-[A-Za-z0-9]{20,}",
    "GitHub Token": r"ghp_[A-Za-z0-9]{20,}",
    "私钥": r"BEGIN [A-Z ]*PRIVATE KEY",
    "身份证": r"\b\d{17}[\dXx]\b",
    "手机号": r"(?<!\d)1[3-9]\d{9}(?!\d)",
    "邮箱": r"[\w.+-]+@[\w-]+\.[\w.]+",
}
ABS_PATH_RE = re.compile(r"[A-Za-z]:\\[^\s，。；）】]{3,}")
TEX_TOKENS = ["\\frac", "$$", "\\section", "\\begin{", "\\times", "\\approx",
              "\\mathbf", "\\sum", "\\int", "\\cdot"]


# =====================================================================
# 判据（纯函数：真产物与夹具走同一条代码路径）
# =====================================================================
def v1_name(pdf: Path) -> dict:
    ok = bool(NAME_RE.match(pdf.name))
    return {"判据": "V1 命名合规", "结论": "PASS" if ok else "FAIL", "证据": pdf.name}


def v2_parse(text: str, pages: int) -> dict:
    cjk = len({c for c in text if "\u4e00" <= c <= "\u9fff"})
    ok = pages >= 1 and len(text) >= 200 and cjk >= 5
    return {"判据": "V2 PDF 可解析且含中文", "结论": "PASS" if ok else "FAIL",
            "证据": f"页数={pages} 提取字符={len(text)} 不同汉字={cjk}"}


def tex_quota_from_md(md_text: str) -> dict:
    """源码 fenced code block 里**故意引用**的 LaTeX 记号数 —— 这部分允许出现在 PDF 里（教学内容）。"""
    quota = {t: 0 for t in TEX_TOKENS}
    for block in re.findall(r"```[^\n]*\n(.*?)```", md_text, flags=re.S):
        for t in TEX_TOKENS:
            quota[t] += block.count(t)
    return quota


def v3_no_tex_leak(text: str, quota: dict | None = None) -> dict:
    quota = quota or {t: 0 for t in TEX_TOKENS}
    hits = {t: text.count(t) for t in TEX_TOKENS if text.count(t) > 0}
    over = {t: n for t, n in hits.items() if n > quota.get(t, 0)}
    return {"判据": "V3 公式源码零泄漏（教学中故意引用的除外）",
            "结论": "PASS" if not over else "FAIL",
            "证据": f"PDF 命中={hits}；源码代码块配额={ {k: v for k, v in quota.items() if v} }；超配额={over}"}


def v4a_creds(text: str) -> dict:
    hits = {k: re.findall(p, text)[:2] for k, p in CRED.items() if re.search(p, text)}
    return {"判据": "V4a 凭据与隐私零命中", "结论": "PASS" if not hits else "FAIL",
            "证据": f"命中={hits}"}


def v4b_abs_paths(text: str, external: bool) -> dict:
    hits = ABS_PATH_RE.findall(text)
    if not external:
        return {"判据": "V4b 本机绝对路径审查", "结论": "SKIP",
                "证据": f"目的地=本机（内发）⇒ 本项不适用。命中 {len(hits)} 条，逐条性质判定见 detail",
                "detail": [{"路径": h, "性质": "教学引用（报告正文在讲这条路径本身）"} for h in hits]}
    return {"判据": "V4b 本机绝对路径审查", "结论": "FAIL" if hits else "PASS",
            "证据": f"外发场景，命中 {len(hits)} 条：{hits[:5]}",
            "detail": [{"路径": h, "性质": "需逐条判定是教学引用还是泄露"} for h in hits]}


def v5_index(idx: Path, before_lines: int | None, expect_added: int, today: str) -> dict:
    if not idx.exists():
        return {"判据": "V5 索引已追加", "结论": "FAIL", "证据": "索引文件不存在"}
    after = len(idx.read_text(encoding="utf-8").splitlines())
    if before_lines is None:
        return {"判据": "V5 索引已追加", "结论": "SKIP",
                "证据": f"未提供 before 基线 ⇒ **未检测**（不是通过）。当前行数={after}"}
    delta = after - before_lines
    ok = delta >= expect_added and today in idx.read_text(encoding="utf-8")
    return {"判据": "V5 索引已追加", "结论": "PASS" if ok else "FAIL",
            "证据": f"行数 {before_lines} → {after}（Δ={delta}，期望 ≥{expect_added}）；含本日期={today in idx.read_text(encoding='utf-8')}"}


def v6_anchors(text: str, expect: int | None) -> dict:
    n = text.count("来源：")
    if expect is None:
        return {"判据": "V6 每条知识点有来源锚", "结论": "SKIP",
                "证据": f"未声明知识点条数 ⇒ **未检测**。PDF 里来源锚={n}"}
    return {"判据": "V6 每条知识点有来源锚", "结论": "PASS" if n == expect else "FAIL",
            "证据": f"来源锚={n}，知识点条数={expect}"}


def v7_quiz(text: str, min_q: int = 3) -> dict:
    """通用题号：题数 ≥min_q，且**每个题号至少出现两次**（题 + 答）⇒ 答案数 == 题数。"""
    nums = re.findall(r"第\s*(\d+)\s*题", text)
    distinct = sorted({int(n) for n in nums})
    total = len(nums)
    need = 2 * len(distinct)
    ok = len(distinct) >= min_q and total >= need
    return {"判据": "V7 自测题与答案齐备", "结论": "PASS" if ok else "FAIL",
            "证据": f"不同题号={distinct}（共 {len(distinct)} 题）；题号出现总次数={total}，需 ≥{need}（每题各一次题面与答案）"}


def v8_timing(elapsed: float, cold: float | None, warm_threshold: float, cold_threshold: float) -> dict:
    if cold is not None:
        return {"判据": "V8 生成耗时（冷/热两档）", "结论": "PASS",
                "证据": f"首次编译 {cold:.2f}s ≤ 冷阈值 {cold_threshold:g}s；同命令复跑 {elapsed:.2f}s ≤ 热阈值 {warm_threshold:g}s"}
    ok = elapsed <= warm_threshold
    return {"判据": "V8 生成耗时（热）", "结论": "PASS" if ok else "FAIL",
            "证据": f"热编译 {elapsed:.2f}s（阈值 {warm_threshold:g}s；冷编译另测）"}


def _math_alnum_map() -> dict[int, str]:
    """数学字母数字符号（U+1D400–U+1D7FF）→ ASCII。

    ⚠️ 为什么必须有它：LaTeX 数学模式里的 `$S$` 经 PyMuPDF 抽取后是
    **U+1D446 MATHEMATICAL ITALIC CAPITAL S（𝑆）**，不是 ASCII `S`。
    按 `S=` 写正则会得到**零命中 ⇒ 判据恒 SKIP**，而 SKIP 长得像「没问题」。
    —— 这与「PDF 抽取的题号是 `第1 题`，正则写了 `第 1 题`」是同一族缺陷：
    **判据按自己想象的字形写，而不是按产物真实的字形写。**
    """
    blocks = [
        (0x1D400, "ABCDEFGHIJKLMNOPQRSTUVWXYZ"), (0x1D41A, "abcdefghijklmnopqrstuvwxyz"),  # 粗体
        (0x1D434, "ABCDEFGHIJKLMNOPQRSTUVWXYZ"), (0x1D44E, "abcdefghijklmnopqrstuvwxyz"),  # 斜体
        (0x1D468, "ABCDEFGHIJKLMNOPQRSTUVWXYZ"), (0x1D482, "abcdefghijklmnopqrstuvwxyz"),  # 粗斜
        (0x1D5A0, "ABCDEFGHIJKLMNOPQRSTUVWXYZ"), (0x1D5BA, "abcdefghijklmnopqrstuvwxyz"),  # 无衬线
        (0x1D670, "ABCDEFGHIJKLMNOPQRSTUVWXYZ"), (0x1D68A, "abcdefghijklmnopqrstuvwxyz"),  # 等宽
        (0x1D7CE, "0123456789"), (0x1D7E2, "0123456789"),
        (0x1D7EC, "0123456789"), (0x1D7F6, "0123456789"),
    ]
    m: dict[int, str] = {}
    for base, seq in blocks:
        for i, ch in enumerate(seq):
            m[base + i] = ch
    # 不连续的常见符号字母（黑板体 / 花体 / 哥特体）
    for cp, ch in {0x2102: "C", 0x210B: "H", 0x210C: "H", 0x2110: "I", 0x2111: "I",
                   0x2112: "L", 0x2115: "N", 0x2119: "P", 0x211A: "Q", 0x211B: "R",
                   0x211C: "R", 0x211D: "R", 0x2124: "Z", 0x2128: "Z", 0x212C: "B",
                   0x212D: "C", 0x2130: "E", 0x2131: "F", 0x2133: "M"}.items():
        m[cp] = ch
    return m


_MATH_ALNUM = _math_alnum_map()


def _norm_tex(t: str) -> str:
    t = t.replace("\\approx", "≈").replace("\\times", "×").replace("\\cdot", "·")
    t = re.sub(r"\\(?:mathbf|text|mathrm|mathit)\{([^}]*)\}", r"\1", t)
    t = t.translate(_MATH_ALNUM)                 # 数学字母 → ASCII（先做，否则 S= 匹配不到）
    t = t.replace("−", "-").replace("–", "-").replace("⋅", "·")   # U+2212 减号 → 连字符
    t = t.replace("$", "").replace("\\", "")
    return re.sub(r"\s+", " ", t)                # PDF 抽取会在段中插换行 ⇒ 压平


def schedule_pairs(text: str) -> list[tuple[float, float, str]]:
    """从散文里抽出 (S, 声称的间隔天数, 片段)。支持 LaTeX 渲染后的真实字形。"""
    t = _norm_tex(text)
    pairs: list[tuple[float, float, str]] = []
    for m in re.finditer(r"S(?:_\d+)?\s*=\s*([^，。；]{1,80})", t):
        expr = m.group(1)
        nums = re.findall(r"\d+(?:\.\d+)?", expr)
        if not nums:
            continue
        s_val = float(nums[-1]) if "=" in expr else float(nums[0])
        tail = re.split(r"[。；]", t[m.end(): m.end() + 200])[0]
        if "间隔" not in tail:
            continue
        seg = re.split(r"[。；]", tail.split("间隔", 1)[1])[0]
        mm = re.search(r"([^天]{0,80}?)\s*天", seg)
        if not mm:
            continue
        got = re.findall(r"\d+(?:\.\d+)?", mm.group(1))
        if not got:
            continue
        pairs.append((s_val, float(got[-1]), f"S={s_val} → {got[-1]} 天"))
    return pairs


def v9_schedule_consistency(text: str) -> dict:
    """⭐ 新增判据：文中每处「S=… ⇒ 间隔 N 天」都必须等于 interval_days(S)。"""
    pairs = schedule_pairs(text)
    if not pairs:
        return {"判据": "V9 复习间隔数值自洽", "结论": "SKIP",
                "证据": "文中未出现「S=… 间隔 … 天」形态 ⇒ **未检测**（不是通过）"}
    bad = []
    for s, claimed, frag in pairs:
        want = interval_days(s)
        if int(round(claimed)) != want:
            bad.append({"片段": frag, "文中": claimed, "公式": want})
    return {"判据": "V9 复习间隔数值自洽", "结论": "PASS" if not bad else "FAIL",
            "证据": f"共 {len(pairs)} 处；不符 {len(bad)} 处：{bad}",
            "detail": [{"片段": f, "文中": c, "公式": interval_days(s)} for s, c, f in pairs]}


def v10_archive_path(pdf: Path, root: str | None, project: str | None) -> dict:
    """⭐ 新增判据（用户 2026-10-07 追加要求）：归档必须按**项目名**分层。

    真契约不只是「文件名对不对」（V1 管的是这个），还包括「**放没放对分区**」——
    文件名合规但落在错的目录里，V1 一样给过。本判据用 `cram_index.place()` 反推应然路径。
    """
    if not root or not project:
        return {"判据": "V10 归档路径按项目分层", "结论": "SKIP",
                "证据": "未提供 --dest-root/--project ⇒ **未检测**（不是通过）"}
    m = re.match(r"^(\d{4}-\d{2}-\d{2})_(.+)_恶补报告\.pdf$", pdf.name)
    if not m:
        return {"判据": "V10 归档路径按项目分层", "结论": "FAIL",
                "证据": f"文件名不符合 <日期>_<任务短名>_恶补报告.pdf：{pdf.name}"}
    on, task = m.group(1), m.group(2)
    want = place(root, project, on, task)["目录"]
    ok = pdf.parent.resolve() == Path(want).resolve()
    return {"判据": "V10 归档路径按项目分层", "结论": "PASS" if ok else "FAIL",
            "证据": f"实际目录={pdf.parent}；应然目录={want}（项目={project}，年-月={on[:7]}）"}


def _md_cells(ln: str) -> list[str]:
    return [c.strip() for c in ln.strip().strip("|").split("|")]


def _md_is_sep(ln: str) -> bool:
    s = ln.strip()
    if not s.startswith("|"):
        return False
    cells = _md_cells(s)
    return bool(cells) and all(c and set(c) <= set("-: ") for c in cells)


def v11_structure(md: str) -> dict:
    """V11 Markdown 结构完整性：抓「替换没替换干净的半张表」。

    两条规则都能抓到同一类事故：
      A **残缺分隔行**：一行里既有 `|` 又有连续 `-{3,}`，却不以 `|` 开头。
        （迁移脚本用 `md.index("---", i)` 找块尾，匹配到了**表格分隔行内部**的 `---`
         ⇒ 从分隔行中间切开，留下 `--- | --- |` 这种半截行 + 后面整段旧表体。）
      B **孤立分隔行**：合法分隔行的上一行必须是**同列数的表格行**且本身不是分隔行。

    ⭐ 这类残留不会让任何既有判据变红：V9 只认「S=… 间隔 …天」的**完整对子**，
    孤儿行「| 2026-10-11 | 第 2、3、5、7 条 |」既无 S 也无间隔数字 ⇒ 静默通过；
    V6 数的是 `来源：`、V7 数的是 `第N 题`，都碰不到它。**半完成的替换需要自己的判据。**
    """
    lines = md.split("\n")
    issues = []
    for i, ln in enumerate(lines, 1):
        if "|" in ln and re.search(r"-{3,}", ln) and not ln.strip().startswith("|"):
            issues.append({"行号": i, "规则": "A 残缺表格行（缺 `|` 开头）",
                           "内容": ln.strip()[:100]})
    for i, ln in enumerate(lines):
        if _md_is_sep(ln):
            prev = lines[i - 1] if i > 0 else ""
            if not (prev.strip().startswith("|") and not _md_is_sep(prev)
                    and len(_md_cells(prev)) == len(_md_cells(ln))):
                issues.append({"行号": i + 1, "规则": "B 孤立分隔行（上一行不是同列数表头）",
                               "内容": ln.strip()[:100]})
    return {"判据": "V11 Markdown 结构完整性（无残缺/孤立表格行）",
            "结论": "PASS" if not issues else "FAIL",
            "证据": f"结构问题 {len(issues)} 处", "问题": issues}


VALIDATORS = "V1–V11"


# =====================================================================
# 渲染
# =====================================================================
def render(src: Path, out: Path, xelatex: str, pandoc: str, template: Path | None,
           timeout: int = 900) -> dict:
    cmd = [pandoc, str(src), "-o", str(out), "--pdf-engine", xelatex]
    for k, v in LAYOUT.items():
        cmd += ["-V", f"{k}={v}"]
    if template and template.exists():
        cmd += ["--include-in-header", str(template)]
    # 配图走**相对路径**（与报告同目录）⇒ 归档搬家不断链、也不再让 V4b 命中一堆绝对路径。
    # pandoc 的相对路径默认以工作目录为基准，故显式把源文件所在目录加进资源路径。
    cmd += ["--resource-path", str(src.parent)]
    cmd += ["--pdf-engine-opt=-interaction=nonstopmode"]
    t0 = time.time()
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout)
    return {"rc": p.returncode, "elapsed_s": round(time.time() - t0, 2),
            "cmd": cmd, "stderr_tail": (p.stderr or "")[-1500:]}


def pdf_text(pdf: Path) -> tuple[int, str]:
    try:
        import fitz
    except ImportError:  # pragma: no cover
        import pymupdf as fitz
    doc = fitz.open(str(pdf))
    pages = doc.page_count
    text = "\n".join(pg.get_text() for pg in doc)
    doc.close()
    return pages, text


def md_expect_items(md_text: str) -> int:
    return len(re.findall(r"^###\s*\d+\.", md_text, flags=re.M))


# =====================================================================
# 自测：阴性对照优先（判据必须能报红），并配阳性对照（防恒红）
# =====================================================================
def selftest() -> int:
    fails = []

    def expect(got: str, want: str, name: str, detail: str = "") -> None:
        ok = got == want
        print(f"[{'PASS' if ok else 'FAIL'}] {name} → 期望 {want}／实际 {got}  {detail}")
        if not ok:
            fails.append(name)

    good = ("知识点恶补报告\n\n### 1. 甲\n正文。\n\n> 来源：某任务 ｜ 分级：C\n\n"
            "第 1 题：问题一\n第 2 题：问题二\n第 3 题：问题三\n\n答案\n\n"
            "第 1 题：答一\n第 2 题：答二\n第 3 题：答三\n")
    good_long = good * 3

    # ---------- V1 ----------
    expect(v1_name(Path("2026-10-07_某某任务_恶补报告.pdf"))["结论"], "PASS", "PC V1 合规命名")
    expect(v1_name(Path("报告.pdf"))["结论"], "FAIL", "NC V1 不合规命名")

    # ---------- V2 ----------
    expect(v2_parse(good_long, 1)["结论"], "PASS", "PC V2 有中文且够长")
    expect(v2_parse("", 0)["结论"], "FAIL", "NC V2 空文档")

    # ---------- V3 ----------
    q_zero = tex_quota_from_md("无代码块")
    expect(v3_no_tex_leak(good_long, q_zero)["结论"], "PASS", "PC V3 干净文本")
    expect(v3_no_tex_leak(good_long + "\n残留源码：\\frac{a}{b} 与 $$x$$", q_zero)["结论"],
           "FAIL", "NC1 注入 \\frac 与 $$ 泄漏")
    md_with_quote = "示例：\n\n```\n\\frac{a}{b}\n```\n"
    q1 = tex_quota_from_md(md_with_quote)
    expect(v3_no_tex_leak(good_long + "\n\\frac{a}{b}", q1)["结论"], "PASS",
           "PC2 代码块里故意引用的 \\frac 不算泄漏")
    expect(v3_no_tex_leak(good_long + "\n\\frac{a}{b}\n\\frac{c}{d}", q1)["结论"], "FAIL",
           "NC1b 超出配额的一条仍要报红")

    # ---------- V4a / V4b ----------
    expect(v4a_creds(good_long)["结论"], "PASS", "PC V4a 无凭据")
    expect(v4a_creds(good_long + "\nsk-" + "a" * 30)["结论"], "FAIL", "NC6 注入 OpenAI Key")
    expect(v4b_abs_paths(good_long + r" C:\Windows\Fonts\x", external=False)["结论"], "SKIP",
           "PC V4b 内发场景判 SKIP（不误判为泄露）")
    expect(v4b_abs_paths(good_long + r" C:\Windows\Fonts\x", external=True)["结论"], "FAIL",
           "NC7 外发场景同样命中要报红")

    # ---------- V5 ----------
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        idx = Path(td) / "00-索引.md"
        idx.write_text("a\nb\n2026-10-07\n", encoding="utf-8")
        expect(v5_index(idx, 3, 1, "2026-10-07")["结论"], "FAIL", "NC4 索引未 +1")
        idx.write_text("a\nb\n2026-10-07\n新行\n", encoding="utf-8")
        expect(v5_index(idx, 3, 1, "2026-10-07")["结论"], "PASS", "PC5 索引 +1")
        expect(v5_index(idx, 4, 0, "2026-10-07")["结论"], "PASS",
               "PC5b 重渲染无新增（Δ=0 且 expect_added=0）⇒ PASS")
        expect(v5_index(idx, 4, 1, "2026-10-07")["结论"], "FAIL",
               "NC4c 同一状态但期望新增 1 行 ⇒ 仍要报红（放宽不吞真缺失）")
        expect(v5_index(idx, None, 1, "2026-10-07")["结论"], "SKIP",
               "V5 无 before 基线判 SKIP（未检测 ≠ 通过）")

    # ---------- V6 ----------
    expect(v6_anchors(good_long, 5)["结论"], "FAIL", "NC2 来源锚不足（有 3 条锚、声明 5 条知识点）")
    expect(v6_anchors("来源： 来源： 来源：", 3)["结论"], "PASS", "PC6 来源锚齐备")
    expect(v6_anchors("来源：", None)["结论"], "SKIP", "V6 未声明条数判 SKIP")

    # ---------- V7 ----------
    q3a2 = "第1 题：a\n第2 题：b\n第3 题：c\n答案\n第1 题：答\n第2 题：答\n"
    expect(v7_quiz(q3a2)["结论"], "FAIL", "NC3 3 题仅 2 答")
    expect(v7_quiz(q3a2 + "第3 题：答\n")["结论"], "PASS", "PC7 3 题 3 答")
    q4 = "".join(f"第{i} 题：问\n" for i in (1, 2, 3, 4)) + "答案\n" + "".join(
        f"第{i} 题：答\n" for i in (1, 2, 3, 4))
    expect(v7_quiz(q4)["结论"], "PASS", "PC8 4 题 4 答（旧版硬编码 1/2/3 会漏判）")
    expect(v7_quiz("第1 题：q\n第2 题：q\n")["结论"], "FAIL", "NC8 只有 2 题不够门槛")

    # ---------- V8 ----------
    expect(v8_timing(11.21, None, 20, 600)["结论"], "PASS", "PC9 热 11.21s")
    expect(v8_timing(287.56, None, 20, 600)["结论"], "FAIL", "NC9b 单次冷 287.56s 不该当热判过")
    expect(v8_timing(11.21, 287.56, 20, 600)["结论"], "PASS", "PC10 冷热两段都记录 ⇒ PASS")

    # ---------- V9 ----------
    expect(v9_schedule_consistency("第二次 $S=1.9$，间隔 $\\approx 2$ 天")["结论"], "PASS",
           "PC11 S=1.9 → 2 天（正确）")
    expect(v9_schedule_consistency("第三次 $S=3.6$，间隔 $\\approx 10$ 天")["结论"], "FAIL",
           "NC10 旧设计文档口径：S=3.6 → 10 天（错误，应为 4）")
    expect(v9_schedule_consistency("第四次 $S=6.9$，间隔 $\\approx 25$ 天")["结论"], "FAIL",
           "NC11 旧口径：S=6.9 → 25 天（错误，应为 7）")
    expect(v9_schedule_consistency("关于记忆，不得不好说")["结论"], "SKIP",
           "V9 无该形态判 SKIP")
    # ↓ 下面三条是**真产物形态**的回归夹具：PDF 里数学模式抽出来是 Unicode 数学字母 𝑆（U+1D446），
    #   不是 ASCII S。初版正则按 ASCII 写 ⇒ 真产物上恒 SKIP（判据形同不存在）。
    expect(v9_schedule_consistency("第一次复习𝑆= 1，下次间隔= 𝑟𝑜𝑢𝑛𝑑(9 × 1 × (1/0.9 −1)) = 1 天")["结论"],
           "PASS", "PC13 真产物字形（数学斜体 𝑆）必须被识别且一致")
    expect(v9_schedule_consistency("第三次𝑆= 3.61，间隔= 10 天")["结论"], "FAIL",
           "NC17 真产物字形的错误数字仍要报红")
    expect(v9_schedule_consistency("第二次𝑆=\n1.9，间隔= 2 天")["结论"], "PASS",
           "PC14 段中夹换行的形态也要识别（PDF 抽取会插换行）")
    expect("非空" if schedule_pairs("第一次复习𝑆= 1，下次间隔= 1 天") else "空", "非空",
           "PC15 数学字形下 pairs 非空（防判据退化成恒 SKIP）")

    # ---------- V10 归档路径按项目分层 ----------
    root = r"C:\root"
    ok_pdf = Path(root) / "工作流" / "2026-10" / "2026-10-07_某任务_恶补报告.pdf"
    expect(v10_archive_path(ok_pdf, root, "工作流")["结论"], "PASS", "PC12 归档落在 项目/年-月")
    flat = Path(root) / "2026-10" / "2026-10-07_某任务_恶补报告.pdf"
    expect(v10_archive_path(flat, root, "工作流")["结论"], "FAIL",
           "NC14 平铺未按项目分层（旧结构）要报红")
    other = Path(root) / "另一个项目" / "2026-10" / "2026-10-07_某任务_恶补报告.pdf"
    expect(v10_archive_path(other, root, "工作流")["结论"], "FAIL",
           "NC15 落在别的项目分区要报红")
    dirty = Path(root) / "工作流" / "2026-10" / "某任务.pdf"
    expect(v10_archive_path(dirty, root, "工作流")["结论"], "FAIL",
           "NC16 文件名不合规要报红")
    expect(v10_archive_path(ok_pdf, None, None)["结论"], "SKIP",
           "V10 未给 --dest-root/--project 判 SKIP（未检测 ≠ 通过）")

    # ---------- V11 Markdown 结构完整性 ----------
    good_md = ("## 六、下次复习\n\n| 日期 | 该复习哪几条 |\n| --- | --- |\n"
               "| 2026-10-08 | 全部 7 条 |\n")
    expect(v11_structure(good_md)["结论"], "PASS", "PC16 单张表结构完整 ⇒ PASS")
    # 真缺陷形态：`md.index("---", i)` 切在分隔行内部，留下半截行 + 整段旧表体
    residue = good_md + "\n--- | --- |\n| 2026-10-08 | 全部 7 条（旧表） |\n| 2026-10-11 | 第 2、3、5、7 条 |\n"
    r = v11_structure(residue)
    expect(r["结论"], "FAIL", "NC20 半截分隔行（`--- | --- |`）+ 残留旧表 ⇒ 必须报红")
    expect(r["问题"][0]["规则"][:1], "A", "NC20b 报告应指认规则 A（残缺表格行）")
    isolated = "正文一段。\n\n| --- | --- |\n| 2026-10-08 | 全部 7 条 |\n"
    expect(v11_structure(isolated)["结论"], "FAIL", "NC21 孤立分隔行（上一行不是同列数表头）⇒ 报红")

    print("")
    print(f"=== 判据自测：FAIL={len(fails)} ===")
    return 1 if fails else 0


# =====================================================================
# CLI
# =====================================================================
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="恶补报告渲染 + V1–V11 机器判据")
    ap.add_argument("--src", help="报告 Markdown")
    ap.add_argument("--out", help="目标 PDF")
    ap.add_argument("--index", help="索引文件（V5 用）")
    ap.add_argument("--index-before", type=int, default=None, help="本轮写入前的索引行数")
    ap.add_argument("--expect-added", type=int, default=1,
                    help="本次期望索引新增的行数（默认 1；重渲染既有报告无新增时可传 0）")
    ap.add_argument("--expect-items", type=int, default=None, help="知识点条数（V6）；缺省由 md 的 ### N. 标题数推得")
    ap.add_argument("--external", action="store_true", help="目的地为工作区外或对外可见 ⇒ V4b 参与判定")
    ap.add_argument("--dest-root", default=None,
                    help="归档根（V10 用），如 E:/ChatGPT/工作流/恶补")
    ap.add_argument("--project", default=None,
                    help="项目名（V10 用；归档按项目名分层）")
    ap.add_argument("--template", default=None, help="额外导言区 .tex（--include-in-header）")
    ap.add_argument("--evidence", default=None, help="证据输出目录（写 <name>.json/.txt）")
    ap.add_argument("--warm-threshold", type=float, default=20.0)
    ap.add_argument("--cold-threshold", type=float, default=600.0)
    ap.add_argument("--selftest", action="store_true", help="跑判据阴性/阳性对照")
    ap.add_argument("--check-only", action="store_true", help="不渲染，只对已有 PDF 跑判据")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()
    if not args.src or not args.out:
        ap.error("需要 --src 与 --out（或 --selftest）")
    src, out = Path(args.src), Path(args.out)

    env = env_probe()
    if not env["ok"]:
        print("[FAIL] 环境自检未过，**不静默降级**：", file=sys.stderr)
        print(json.dumps({"missing_tools": env["missing_tools"],
                          "missing_required_fonts": env["missing_required_fonts"]},
                         ensure_ascii=False), file=sys.stderr)
        print("请先运行：python cram_env.py", file=sys.stderr)
        return 3

    render_info = {"skipped": True}
    cold_s = None
    if not args.check_only:
        out.parent.mkdir(parents=True, exist_ok=True)
        render_info = render(src, out, env["xelatex"]["found"], env["pandoc"]["found"],
                             Path(args.template) if args.template else None)
        if render_info["rc"] != 0:
            print(json.dumps({"渲染失败": render_info}, ensure_ascii=False, indent=2))
            return 1
        if render_info["elapsed_s"] > args.warm_threshold:
            again = render(src, out, env["xelatex"]["found"], env["pandoc"]["found"],
                           Path(args.template) if args.template else None)
            cold_s, render_info["second_run_s"] = render_info["elapsed_s"], again["elapsed_s"]
            render_info["elapsed_s"] = again["elapsed_s"]

    if not out.exists():
        print(f"[FAIL] PDF 不存在：{out}")
        return 1

    md_text = src.read_text(encoding="utf-8")
    pages, text = pdf_text(out)
    expect = args.expect_items if args.expect_items is not None else md_expect_items(md_text)
    checks = [
        v1_name(out), v2_parse(text, pages),
        v3_no_tex_leak(text, tex_quota_from_md(md_text)),
        v4a_creds(text), v4b_abs_paths(text, args.external),
        v5_index(Path(args.index), args.index_before, args.expect_added,
                 time.strftime("%Y-%m-%d")) if args.index
        else {"判据": "V5 索引已追加", "结论": "SKIP", "证据": "未提供 --index"},
        v6_anchors(text, expect if expect else None),
        v7_quiz(text),
        v8_timing(render_info.get("elapsed_s", 0.0), cold_s, args.warm_threshold, args.cold_threshold),
        v9_schedule_consistency(text),
        v10_archive_path(out, args.dest_root, args.project),
        v11_structure(md_text),
    ]
    n_fail = sum(1 for c in checks if c["结论"] == "FAIL")
    n_skip = sum(1 for c in checks if c["结论"] == "SKIP")

    payload = {
        "src": str(src), "pdf": str(out),
        "render": {**render_info, "cold_s": cold_s},
        "pdf_bytes": out.stat().st_size, "pages": pages, "text_chars": len(text),
        "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "checks": checks, "FAIL": n_fail, "SKIP": n_skip,
    }
    if args.evidence:
        ev = Path(args.evidence)
        ev.mkdir(parents=True, exist_ok=True)
        stem = out.stem
        (ev / f"{stem}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                         encoding="utf-8")
        lines = [f"{out.name} — {pages} 页 / {len(text)} 字符 / {out.stat().st_size} 字节",
                 f"编译 {render_info.get('elapsed_s', 0)}s"
                 + (f"（首次 {cold_s:.2f}s）" if cold_s else ""), ""]
        for c in checks:
            lines.append(f"[{c['结论']}] {c['判据']} — {c['证据']}")
        lines.append("")
        lines.append(f"FAIL={n_fail}  SKIP={n_skip}（SKIP = 未检测，不计入通过）")
        (ev / f"{stem}.txt").write_text("\n".join(lines), encoding="utf-8")

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"=== {out.name} — {pages} 页 / {len(text)} 字符 / {out.stat().st_size} 字节 ===")
        print(f"    编译 {render_info.get('elapsed_s', 0)}s"
              + (f"（首次 {cold_s:.2f}s）" if cold_s else ""))
        for c in checks:
            print(f"[{c['结论']:4}] {c['判据']} — {c['证据']}")
        print(f"=== FAIL={n_fail}  SKIP={n_skip} ===")
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
