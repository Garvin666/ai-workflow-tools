# -*- coding: utf-8 -*-
# [自研工具] build_paper_pdf
# 用途：把 Markdown 单一事实源编译为样式化 HTML，并调用 Chrome/Edge headless 打印为 PDF
# 适用场景：知识库内长文档（论文 / 报告）的 MD→PDF 三态交付，要求封面 / 目录 / 分节分页 /
#          居中公式块 / 中文字体；默认不产出 HTML 交付物（中间 HTML 交付后清理）
#          不适用：需要 LaTeX 数学渲染（本工具不做 KaTeX/MathJax，公式按 Unicode 文本排）
# 作者：ai-workflow 自研（两游走者重逢定理 2026-10-11）
# 仓库：https://github.com/Garvin666/ai-workflow-tools/blob/main/scripts/build_paper_pdf.py
"""
依赖：markdown、pygments；Chrome 或 Edge（headless 打印）。

关键口径：
  - 中间 HTML 只作流水线中转，交付完成后由调用方清理（默认 --keep-html 关闭）。
  - 机器自检三项：① PDF 文件头 %PDF- ② MD 标题清单在 HTML 中零缺失 ③ MD 的 ** 强调对与 HTML <strong> 数量一致（防 CommonMark 强调边界静默失效）。
  - 浏览器定位：经环境变量（ProgramFiles / ProgramFiles(x86) / LOCALAPPDATA）枚举标准安装位，
    源码内不写死本机绝对路径；可用 CHROME_PATH 环境变量显式指定。
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
  font-size: 10.5pt; line-height: 1.8; color: #1f2328; margin: 0;
}
.cover { height: 246mm; display: flex; flex-direction: column; justify-content: center;
         page-break-after: always; text-align: left; padding: 0 4mm; }
.cover .kicker { font-size: 10.5pt; letter-spacing: .26em; color: #6b7280; margin-bottom: 12mm; }
.cover h1.cover-title { font-size: 28pt; line-height: 1.38; margin: 0 0 7mm 0; color: #0f172a;
                        border: none; padding: 0; }
.cover .cover-sub { font-size: 13pt; color: #4b5563; margin-bottom: 20mm; font-weight: 400; }
.cover .cover-meta { font-size: 10pt; color: #6b7280; line-height: 2.0; }
.cover .rule { width: 44mm; height: 3px; background: #1d4ed8; margin: 0 0 11mm 0; }

h1, h2, h3, h4 { color: #0f172a; line-height: 1.45; }
h1 { font-size: 20pt; margin: 0 0 6mm 0; }
h2 { font-size: 16pt; margin: 0 0 5mm 0; padding-bottom: 2.2mm;
     border-bottom: 2px solid #1d4ed8; page-break-before: always; page-break-after: avoid; }
h2:first-of-type { page-break-before: avoid; }
h3 { font-size: 12.5pt; margin: 6.5mm 0 2.4mm 0; color: #1e3a8a; page-break-after: avoid; }
h4 { font-size: 11.5pt; margin: 5mm 0 2mm 0; color: #334155; page-break-after: avoid; }
p { margin: 2.5mm 0; text-align: justify; }
a { color: #1d4ed8; text-decoration: none; }
strong { color: #0f172a; }
hr { border: none; border-top: 1px solid #e5e7eb; margin: 6mm 0; }
ul, ol { margin: 2.4mm 0 2.4mm 0; padding-left: 7mm; }
li { margin: 1.2mm 0; }
blockquote { margin: 3.2mm 0; padding: 2.8mm 5mm; background: #f8fafc;
             border-left: 3px solid #60a5fa; color: #334155; }
blockquote p { margin: 1.2mm 0; }

/* 居中公式 / 论断块 */
div.fml { margin: 3.4mm 0; padding: 2.6mm 4mm; text-align: center;
          font-family: Cambria, "Times New Roman", "Microsoft YaHei", serif;
          font-size: 11pt; line-height: 1.9; color: #0f172a;
          background: #f9fafb; border-left: 3px solid #cbd5e1; border-radius: 3px;
          page-break-inside: avoid; }

table { width: 100%; border-collapse: collapse; margin: 4mm 0; font-size: 9.4pt;
        page-break-inside: avoid; }
th, td { border: 1px solid #d0d7de; padding: 1.7mm 2.2mm; text-align: left; vertical-align: top; }
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
.toc::before { content: "目 录"; display: block; font-size: 16pt; font-weight: 700;
               color: #0f172a; border-bottom: 2px solid #1d4ed8;
               padding-bottom: 2.2mm; margin-bottom: 5mm; }
"""


def split_cover(text: str) -> tuple[str, str]:
    """封面判定：优先按 <div class="cover"> 的 div 配对结束位置切分，退回独占一行的 --- 分隔符。"""
    lines = text.split("\n")
    if lines and lines[0].strip().startswith('<div class="cover"'):
        depth, end = 0, None
        for i, ln in enumerate(lines):
            depth += len(re.findall(r"<div\b", ln)) - len(re.findall(r"</div>", ln))
            if depth == 0:
                end = i
                break
        if end is not None:
            cover = "\n".join(lines[: end + 1]).strip()
            body = "\n".join(lines[end + 1:]).strip()
            body = re.sub(r"^-{3,}\s*\n+", "", body)  # 去掉正文起始处的水平线
            return cover, body
    m = re.search(r"^---\s*$", text, re.M)
    if not m:
        raise SystemExit("[FAIL] 未找到封面与正文的分隔（cover div 或独占一行的 ---）")
    return text[: m.start()].strip(), text[m.end():].strip()


def build_html(cover_md: str, body_md: str, title: str, toc_css: str) -> str:
    md = markdown.Markdown(
        extensions=["extra", "toc", "codehilite", "sane_lists", "md_in_html"],
        extension_configs={
            "toc": {"toc_depth": "2-3", "permalink": False},
            "codehilite": {"guess_lang": False, "css_class": "codehilite"},
        },
    )
    body = md.convert("[TOC]\n\n" + body_md)
    toc_html = md.toc
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>{title}</title>
<style>{CSS}
{toc_css}
</style></head>
<body>
{cover_md}
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
    r = subprocess.run(cmd, capture_output=True, timeout=240)
    if not pdf_path.exists() or pdf_path.stat().st_size < 10_000:
        sys.stderr.write((r.stderr or b"").decode("utf-8", "ignore")[:2000] + "\n")
        raise SystemExit("[FAIL] PDF 生成失败或体积异常")


def verify(pdf_path: Path, html_path: Path, body_md: str) -> None:
    ok = True
    head = pdf_path.read_bytes()[:5]
    assert head == b"%PDF-", f"PDF 文件头异常: {head!r}"
    pages = -1
    try:
        from pypdf import PdfReader
        pages = len(PdfReader(str(pdf_path)).pages)
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] pypdf 不可用，跳过页数核验: {e}")
    html = html_path.read_text(encoding="utf-8")

    heads_md = {m.group(2).strip() for m in re.finditer(r"^(##|###)\s+(.+)$", body_md, re.M)}
    heads_html = {re.sub(r"<[^>]+>", "", m.group(1)).strip()
                  for m in re.finditer(r"<h[23][^>]*>(.+?)</h[23]>", html)}
    missing = sorted(h for h in heads_md if h not in heads_html)

    pairs = body_md.count("**") // 2
    strong = len(re.findall(r"<strong>", html))
    residual = len(re.findall(r"\*\*", re.sub(r"<pre.*?</pre>", "", html, flags=re.S)))

    print(f"[OK] 文件头 %PDF- ；体积 {pdf_path.stat().st_size/1024:.0f} KB ；页数 {pages}")
    print(f"[OK] 标题覆盖：MD {len(heads_md)} 个，HTML 缺失 {len(missing)} 个")
    print(f"[OK] 强调渲染：MD ** 对 {pairs} 个 ；HTML <strong> {strong} 个 ；HTML 残留 ** {residual} 处")
    if missing:
        print("     缺失标题:", missing[:8])
        ok = False
    if strong != pairs or residual != 0:
        print("     [WARN] 强调渲染数量不一致或存在未渲染的 **，需人工核对")
        ok = False
    if not ok:
        raise SystemExit("[FAIL] 机器自检未通过")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--md", required=True, help="Markdown 单一事实源路径")
    ap.add_argument("--out-dir", required=True, help="交付目录")
    ap.add_argument("--name", required=True, help="输出基名（不含扩展名）")
    ap.add_argument("--keep-html", action="store_true", help="保留中间 HTML（默认删除）")
    args = ap.parse_args()

    md_path = Path(args.md).resolve()
    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    text = md_path.read_text(encoding="utf-8")

    cover_md, body_md = split_cover(text)
    md_out = out_dir / f"{args.name}.md"
    if md_out.resolve() != md_path.resolve():
        md_out.write_text((cover_md + "\n\n---\n" + body_md + "\n"), encoding="utf-8")

    html = build_html(cover_md, body_md, args.name, HtmlFormatter().get_style_defs(".codehilite"))
    html_path = out_dir / f".{args.name}.build.html"
    html_path.write_text(html, encoding="utf-8")

    pdf_path = out_dir / f"{args.name}.pdf"
    browser = find_browser()
    print(f"[..] 浏览器: {browser}")
    print_pdf(browser, html_path, pdf_path)
    verify(pdf_path, html_path, body_md)

    if not args.keep_html:
        html_path.unlink(missing_ok=True)
        print("[OK] 中间 HTML 已清理（默认不交付 HTML）")
    print("[DONE]", pdf_path)


if __name__ == "__main__":
    main()
