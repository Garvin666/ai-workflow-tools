# -*- coding: utf-8 -*-
# [自研工具] build_book
# 用途：把多分片 Markdown 书稿合并编译为样式化 HTML，并调用 Chrome/Edge headless 打印为 PDF
# 适用场景：知识库内书籍 / 手册类长文档的三态交付（MD → HTML → PDF），要求封面 / 目录 /
#          分章分页 / 代码高亮 / 中文字体
#          不适用：单文件论文（用 build_paper_pdf，带居中公式块与强调渲染对账）
# 作者：ai-workflow 自研（LLM应用开发工程师教程 2026-10-09）
# 仓库：https://github.com/Garvin666/ai-workflow-tools/blob/main/scripts/build_book.py
"""
依赖：markdown、pygments；Chrome 或 Edge（headless 打印）。

用法：
  python build_book.py [--book-dir <书稿分片目录>] [--out-dir <交付目录>]
                       [--name <输出基名>] [--parts part1.md,part2.md,...]
不带参数时按脚本所在位置推导默认值（书稿在 <脚本目录>/book，交付到 <库根>/30-产出/…）。

浏览器定位：经环境变量（ProgramFiles / ProgramW6432 / SystemDrive / LOCALAPPDATA）枚举标准
安装位，源码内不写死本机绝对路径；可用 CHROME_PATH 环境变量显式指定。
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import markdown
from pygments.formatters import HtmlFormatter

_SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_BOOK_DIR = _SCRIPT_DIR / "book"
DEFAULT_OUT_DIR = _SCRIPT_DIR.parents[1] / "30-产出" / "LLM应用开发工程师-学习手册"
DEFAULT_NAME = "大模型应用开发工程师实战教程"
DEFAULT_PARTS = ["part1.md", "part2.md", "part3.md", "part4.md"]


def _browser_candidates() -> list[str]:
    """枚举 Chrome/Edge 候选路径 —— 全部经环境变量拼装，源码内不出现本机绝对路径。"""
    cands: list[str] = []
    explicit = os.environ.get("CHROME_PATH")
    if explicit:
        cands.append(explicit)
    roots: list[str] = []
    for env in ("ProgramFiles", "ProgramW6432"):
        v = os.environ.get(env)
        if v:
            roots.append(v)
    drive = os.environ.get("SystemDrive")
    if drive:
        base = drive if drive.endswith(os.sep) else drive + os.sep   # 防 "C:" 变驱动器相对路径
        roots.append(os.path.join(base, "Program Files"))
        roots.append(os.path.join(base, "Program Files (x86)"))
    for base in roots:
        cands.append(os.path.join(base, "Google", "Chrome", "Application", "chrome.exe"))
        cands.append(os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe"))
    local = os.environ.get("LOCALAPPDATA")
    if local:
        cands.append(os.path.join(local, "Google", "Chrome", "Application", "chrome.exe"))
        cands.append(os.path.join(local, "Microsoft", "Edge", "Application", "msedge.exe"))
    return cands

CSS = """
@page { size: A4; margin: 20mm 18mm 22mm 18mm; }
* { box-sizing: border-box; }
html { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
body {
  font-family: "Microsoft YaHei", "PingFang SC", "Segoe UI", sans-serif;
  font-size: 10.5pt; line-height: 1.78; color: #1f2328; margin: 0;
}
.cover { height: 247mm; display: flex; flex-direction: column; justify-content: center;
         page-break-after: always; text-align: left; padding: 0 6mm; }
.cover .kicker { font-size: 11pt; letter-spacing: .28em; color: #6b7280; margin-bottom: 14mm; }
.cover h1.cover-title { font-size: 30pt; line-height: 1.35; margin: 0 0 8mm 0; color: #0f172a;
                        border: none; padding: 0; }
.cover .cover-sub { font-size: 14pt; color: #4b5563; margin-bottom: 22mm; font-weight: 400; }
.cover .cover-meta { font-size: 10pt; color: #6b7280; line-height: 2.0; }
.cover .rule { width: 46mm; height: 3px; background: #2563eb; margin: 0 0 12mm 0; }

h1, h2, h3, h4 { color: #0f172a; line-height: 1.4; }
h2 { font-size: 17pt; margin: 0 0 5mm 0; padding-bottom: 2.4mm;
     border-bottom: 2px solid #2563eb; }
h2.chapter { page-break-before: always; }
h3 { font-size: 13pt; margin: 7mm 0 2.5mm 0; color: #1e3a8a; }
h4 { font-size: 11.5pt; margin: 5mm 0 2mm 0; color: #334155; }
p { margin: 2.6mm 0; text-align: justify; }
a { color: #2563eb; text-decoration: none; }
strong { color: #0f172a; }
hr { border: none; border-top: 1px solid #e5e7eb; margin: 7mm 0; }
ul, ol { margin: 2.5mm 0 2.5mm 0; padding-left: 7mm; }
li { margin: 1.2mm 0; }
blockquote { margin: 3.5mm 0; padding: 3mm 5mm; background: #f8fafc;
             border-left: 3px solid #60a5fa; color: #334155; }
blockquote p { margin: 1.2mm 0; }

table { width: 100%; border-collapse: collapse; margin: 4mm 0; font-size: 9.6pt;
        page-break-inside: avoid; }
th, td { border: 1px solid #d0d7de; padding: 1.8mm 2.4mm; text-align: left; vertical-align: top; }
th { background: #f1f5f9; font-weight: 600; color: #0f172a; }
tr:nth-child(even) td { background: #fafbfc; }

pre { background: #f6f8fa; border: 1px solid #e1e4e8; border-radius: 4px;
      padding: 3mm 3.6mm; overflow-x: auto; page-break-inside: avoid;
      font-size: 8.6pt; line-height: 1.55; }
pre code { font-family: Consolas, "Cascadia Mono", "Courier New", monospace;
           background: none; padding: 0; border: none; }
code { font-family: Consolas, "Cascadia Mono", "Courier New", monospace;
       background: #f1f5f9; padding: 0.4mm 1.1mm; border-radius: 3px; font-size: 9pt; }
.codehilite { background: #f6f8fa; border: 1px solid #e1e4e8; border-radius: 4px;
              padding: 3mm 3.6mm; page-break-inside: avoid; overflow-x: auto; }
.codehilite pre { border: none; background: none; padding: 0; margin: 0; }

/* 目录 */
.toc { page-break-after: always; }
.toc > ul { list-style: none; padding-left: 0; }
.toc ul { list-style: none; padding-left: 5mm; }
.toc li { margin: 1.6mm 0; }
.toc > ul > li > a { font-weight: 600; color: #0f172a; }
.toc a { color: #334155; }
.toc::before { content: "目 录"; display: block; font-size: 17pt; font-weight: 700;
               color: #0f172a; border-bottom: 2px solid #2563eb;
               padding-bottom: 2.4mm; margin-bottom: 5mm; }
"""


def read_book(book_dir: Path, parts: list[str]) -> tuple[str, str]:
    chunks = []
    for name in parts:
        p = book_dir / name
        if not p.exists():
            raise SystemExit(f"[FAIL] 缺少书稿分片: {p}")
        chunks.append(p.read_text(encoding="utf-8").strip())
    full = "\n\n".join(chunks)
    # 封面 = 首个 --- 之前；正文 = 之后
    idx = full.find("\n---\n")
    if idx == -1:
        raise SystemExit("[FAIL] 未找到封面与正文的分隔符（首个 ---）")
    cover_md, body_md = full[:idx], full[idx + 5:]
    return cover_md, body_md


def build_html(cover_md: str, body_md: str, toc_css: str, basename: str) -> str:
    body_md = "[TOC]\n\n" + body_md
    md = markdown.Markdown(
        extensions=["extra", "toc", "codehilite", "sane_lists"],
        extension_configs={
            "toc": {"toc_depth": "2-3", "permalink": False},
            "codehilite": {"guess_lang": False, "css_class": "codehilite"},
        },
    )
    body = md.convert(body_md)
    toc_html = md.toc

    # 章标题分页
    body = re.sub(
        r'<h2 id="([^"]+)">(第 [^<]+|附录 [^<]+|写在最后)</h2>',
        lambda m: f'<h2 class="chapter" id="{m.group(1)}">{m.group(2)}</h2>',
        body,
    )
    # TOC 容器：标题由 CSS ::before 提供，无需额外节点

    cover = f"""<div class="cover">
  <div class="kicker">LLM APPLICATION ENGINEERING</div>
  <div class="rule"></div>
  <h1 class="cover-title">大模型应用开发工程师<br>实战教程</h1>
  <div class="cover-sub">对标岗位 JD 的系统学习手册</div>
  <div class="cover-meta">
    面向：计算机 / 人工智能 / 软件工程 / 数学相关专业在校生与转岗者<br>
    目标岗位：大模型应用开发工程师 · AI Agent 开发工程师<br>
    版本 v1.0　｜　2026-10-09　｜　约 11 章 + 3 个附录
  </div>
</div>"""

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>{basename}</title>
<style>{CSS}
{toc_css}
</style></head>
<body>
{cover}
{toc_html}
{body}
</body></html>"""


def find_browser() -> str:
    for b in _browser_candidates():
        if os.path.exists(b):
            return b
    for name in ("chrome", "chrome.exe", "msedge", "msedge.exe"):
        p = shutil.which(name)
        if p:
            return p
    raise SystemExit("[FAIL] 未找到 Chrome/Edge，无法打印 PDF（可用 CHROME_PATH 显式指定）")


def print_pdf(browser: str, html_path: Path, pdf_path: Path) -> None:
    cmd = [
        browser, "--headless=new", "--disable-gpu", "--no-sandbox",
        "--no-pdf-header-footer", "--print-to-pdf-no-header",
        f"--print-to-pdf={pdf_path}",
        html_path.as_uri(),
    ]
    r = subprocess.run(cmd, capture_output=True, timeout=180)
    if not pdf_path.exists() or pdf_path.stat().st_size < 10_000:
        sys.stderr.write((r.stderr or b"").decode("utf-8", "ignore")[:2000] + "\n")
        raise SystemExit("[FAIL] PDF 生成失败或体积异常")


def verify(pdf_path: Path, html_path: Path, body_md: str) -> None:
    data = pdf_path.read_bytes()[:5]
    assert data == b"%PDF-", f"PDF 文件头异常: {data!r}"
    pages = -1
    try:
        from pypdf import PdfReader
        pages = len(PdfReader(str(pdf_path)).pages)
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] pypdf 不可用，跳过页数核验: {e}")
    heads_md = {m.group(2).strip() for m in
                re.finditer(r"^(##|###)\s+(.+)$", body_md, re.M)}
    heads_html = {m.group(1).strip() for m in
                  re.finditer(r"<h[23][^>]*>(.+?)</h[23]>", html_path.read_text(encoding="utf-8"))}
    missing = sorted(h for h in heads_md if h not in heads_html)
    print(f"[OK] 文件头 %PDF- ； 体积 {pdf_path.stat().st_size/1024:.0f} KB ； 页数 {pages}")
    print(f"[OK] 章节标题 HTML 覆盖 — 正文 MD {len(heads_md)} 个标题，HTML 缺 {len(missing)} 个")
    if missing:
        print("     缺失:", missing[:8])
        raise SystemExit("[FAIL] 标题清单不一致")
    assert pages >= 30 or pages == -1, f"页数不足: {pages}"


def main() -> None:
    ap = argparse.ArgumentParser(description="多分片 Markdown 书稿 → 样式化 HTML → PDF")
    ap.add_argument("--book-dir", default=str(DEFAULT_BOOK_DIR), help="书稿分片目录（默认 <脚本目录>/book）")
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR), help="交付目录")
    ap.add_argument("--name", default=DEFAULT_NAME, help="输出基名（不含扩展名）")
    ap.add_argument("--parts", default=",".join(DEFAULT_PARTS),
                    help="分片文件名，逗号分隔（默认 part1.md,part2.md,part3.md,part4.md）")
    args = ap.parse_args()

    book_dir = Path(args.book_dir).resolve()
    out_dir = Path(args.out_dir).resolve()
    basename = args.name
    parts = [s.strip() for s in args.parts.split(",") if s.strip()]

    out_dir.mkdir(parents=True, exist_ok=True)
    cover_md, body_md = read_book(book_dir, parts)

    md_out = out_dir / f"{basename}.md"
    md_out.write_text((cover_md + "\n\n---\n" + body_md).strip() + "\n", encoding="utf-8")

    toc_css = HtmlFormatter().get_style_defs(".codehilite")
    html = build_html(cover_md, body_md, toc_css, basename)
    html_path = out_dir / f"{basename}.html"
    html_path.write_text(html, encoding="utf-8")

    pdf_path = out_dir / f"{basename}.pdf"
    browser = find_browser()
    print(f"[..] 使用浏览器: {browser}")
    print_pdf(browser, html_path, pdf_path)
    verify(pdf_path, html_path, body_md)
    print("[DONE]", pdf_path)


if __name__ == "__main__":
    main()
