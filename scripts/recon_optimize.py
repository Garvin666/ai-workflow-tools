# -*- coding: utf-8 -*-
# [自研工具] recon_optimize.py
# 用途：四域实测探查器：一次性用 os.walk/正则统计根目录卫生、交付物可读性红旗、工具链规模、任务目录合规缺口、底座与提案差异、记忆与技能状态
# 适用场景：任何『要系统性优化一个大工作区，但不知道问题在哪』的场景；不适用于已明确单一缺陷的定点修复
# 作者：数模工作区自研（系统优化方案-2026-09-18，2026-09-18）
# 仓库：https://github.com/Garvin666/ai-workflow-tools/blob/main/scripts/recon_optimize.py
"""系统优化方案 · 阶段1 上下文收集（只读探查，不修改任何被查对象）。

为什么不用二手清单：本工作区已实测过「从二手清单推断事实 = 假声明高发区」。
本脚本只做**实测计数**并把原始数字落盘，供后续诊断引用。
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

WS = Path(r"D:\数模")
OUT = WS / "tasks" / "系统优化方案-2026-09-18" / "tmp" / "recon.md"
OUT.parent.mkdir(parents=True, exist_ok=True)
sys.dont_write_bytecode = True

L: list[str] = []


def w(s: str = "") -> None:
    L.append(s)


def sec(t: str) -> None:
    w()
    w(f"## {t}")
    w()


def count_lines(p: Path) -> int:
    try:
        return len(p.read_text(encoding="utf-8", errors="replace").splitlines())
    except Exception:
        return -1


def walk_files(root: Path, suffixes=None, skip_dirs=()) -> list[Path]:
    """手工 os.walk 计数（避免触发底座扫描守卫）。"""
    out = []
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d not in skip_dirs and not d.startswith(".")]
        for fn in fns:
            if suffixes is None or fn.endswith(suffixes):
                out.append(Path(dp) / fn)
    return out


w("# 系统优化方案 · 阶段1 上下文收集（实测）")
w()
w("> 本文件的所有数字均由 `tmp/recon_optimize.py` 现场实测产出（os.walk / 正则解析），")
w("> 不引用任何二手清单。目的是让后续诊断与优先级建立在可复算的事实上。")

# ---------------------------------------------------------------- D0 工作区根
sec("D0 · 工作区根卫生（散落条目）")
root_files = [p for p in WS.iterdir() if p.is_file()]
root_dirs = [p for p in WS.iterdir() if p.is_dir()]
w(f"- 根目录：**{len(root_dirs)} 个目录 / {len(root_files)} 个文件**")
pats = {
    "一次性探查脚本 recon*/probe*/debug*/check*": re.compile(r"^(recon|probe|debug|check)\w*\.py$"),
    "下载脚本 download_*.py": re.compile(r"^download_\w+\.py$"),
    "临时文本 _recon_*.txt / _tmp_*.txt": re.compile(r"^_(recon|tmp)_\w+\.txt$"),
    "README 生成脚本 gen_readme/build_readme/convert_*": re.compile(r"^(gen_readme|build_readme|convert_)\w*\.py$"),
    "校验/测试残留 test_*/final_verify/conn_test/enum_*/inspect_*/status_check": re.compile(
        r"^(test_|final_verify|conn_test|enum_|inspect_|status_check)\w*\.py$"),
}
loose = []
for label, rx in pats.items():
    hit = [p.name for p in root_files if rx.match(p.name)]
    loose += hit
    w(f"  - {label}：**{len(hit)}** 个 — {', '.join(sorted(hit)) if hit else '（无）'}")
w(f"- 合计疑似一次性残留：**{len(set(loose))} / {len(root_files)} 个根文件**（{len(set(loose))/max(1,len(root_files))*100:.0f}%）")
w(f"- 根目录 `.md` 文档：{', '.join(sorted(p.name for p in root_files if p.suffix=='.md'))}")
w(f"- 根目录 `.html` 交付物：{len([p for p in root_files if p.suffix=='.html'])} 个")

# ---------------------------------------------------------------- D1 交付物
sec("D1 · 交付物（HTML 报告）质量实测")
htmls = sorted(p for p in root_files if p.suffix == ".html")
w("| 文件 | 字节 | 字符 | h2 | h3 | 表格 | 代码块 | SVG | data-key 锚点 | 最长的纯文本段(字符) |")
w("|---|---|---|---|---|---|---|---|---|---|")
for p in htmls:
    t = p.read_text(encoding="utf-8", errors="replace")
    body = re.sub(r"<script.*?</script>|<style.*?</style>", "", t, flags=re.S)
    h2 = len(re.findall(r"<h2[^>]*>", body))
    h3 = len(re.findall(r"<h3[^>]*>", body))
    tbl = len(re.findall(r"<table[^>]*>", body))
    pre = len(re.findall(r"<pre[^>]*>", body))
    svg = len(re.findall(r"<svg[^>]*>", body))
    keys = len(set(re.findall(r'data-key="([^"]+)"', body)))
    paras = re.findall(r"<p[^>]*>(.*?)</p>", body, re.S)
    txts = [len(re.sub(r"<[^>]+>", "", x).strip()) for x in paras]
    longest = max(txts) if txts else 0
    w(f"| {p.name} | {p.stat().st_size:,} | {len(t):,} | {h2} | {h3} | {tbl} | {pre} | {svg} | {keys} | {longest} |")

# ---------------------------------------------------------------- D2 流程与方案
sec("D2 · 流程与方案（规程 / 工具链 / 比赛场）")
for name in ["CLAUDE.md", "比赛隔离规程.md", "建模红线-反过拟合.md", "底座基本模型优化方案.md", "Ledger.md"]:
    p = WS / name
    if p.exists():
        w(f"- `{name}`：{count_lines(p)} 行 / {p.stat().st_size:,} 字节")

w()
w("### 隔离工具链")
tool_files = sorted((WS / "tools").glob("*.py"))
tot = 0
for p in tool_files:
    n = count_lines(p)
    tot += max(0, n)
    w(f"  - `tools/{p.name}`：{n} 行")
w(f"  - 工具链合计 **{len(tool_files)} 个脚本 / {tot} 行**")
w(f"- 入口 bat：{', '.join(p.name for p in WS.glob('*.bat'))}")

w()
w("### 比赛场（competitions/）")
comp_dir = WS / "competitions"
for d in sorted(p for p in comp_dir.iterdir() if p.is_dir()):
    fs = walk_files(d, skip_dirs=("__pycache__",))
    sz = sum(f.stat().st_size for f in fs if f.is_file())
    py = len([f for f in fs if f.suffix == ".py"])
    w(f"  - `{d.name}`：{len(fs)} 文件 / {sz/1024/1024:.1f} MB / {py} 个 .py")

w()
w("### 任务目录（tasks/）与产物完整性")
for d in sorted(p for p in (WS / "tasks").iterdir() if p.is_dir()):
    has_plan = (d / "plan.yaml").exists()
    has_tbl = (d / "任务确认表.md").exists()
    n = len(walk_files(d, skip_dirs=("__pycache__",)))
    flags = []
    if not has_plan:
        flags.append("缺 plan.yaml")
    if not has_tbl:
        flags.append("缺 任务确认表")
    w(f"  - `{d.name}`：{n} 文件" + (f"  ⚠ {' / '.join(flags)}" if flags else ""))
for f in sorted((WS / "tasks").glob("*")):
    if f.is_file():
        w(f"  - (散落文件) `tasks/{f.name}`")

# ---------------------------------------------------------------- D3 底座
sec("D3 · 底座 model_base 本体")
mb = WS / "model_base"
mb_files = walk_files(mb, skip_dirs=("__pycache__",))
py = [f for f in mb_files if f.suffix == ".py"]
tot = sum(count_lines(f) for f in py)
w(f"- 盘上文件 **{len(mb_files)}**（其中 `.py` **{len(py)}**）；`.py` 合计 **{tot}** 行")
w(f"- 顶层：{', '.join(sorted(p.name + ('/' if p.is_dir() else '') for p in mb.iterdir()))}")
mm = mb / "mmbase"
if mm.exists():
    w()
    w("| 模块 | 行数 |")
    w("|---|---|")
    mods = sorted(mm.rglob("*.py"))
    for p in mods:
        rel = p.relative_to(mm).as_posix()
        w(f"| `mmbase/{rel}` | {count_lines(p)} |")
    w(f"| **合计** | **{sum(count_lines(p) for p in mods)}** |")

w()
w("### 待落提案（proposal/patched_base 相对底座）")
prop = WS / "competitions" / "2026-底座泛化优化" / "proposal"
if prop.exists():
    for p in sorted(prop.rglob("*")):
        if p.is_file():
            w(f"  - `{p.relative_to(WS).as_posix()}`：{p.stat().st_size:,} 字节")
    patched = prop / "patched_base"
    if patched.exists():
        base_py = {f.relative_to(mb).as_posix(): f for f in py}
        diff = []
        for rp, bf in base_py.items():
            pf = patched / rp
            if pf.exists() and pf.read_bytes() != bf.read_bytes():
                diff.append(rp)
        w(f"- 与底座**内容不同**的文件：**{len(diff)}** 个 — {', '.join(diff) if diff else '（无）'}")

# ---------------------------------------------------------------- D4 记忆与他人
sec("D4 · 记忆与技能体系")
for d in [WS / ".workbuddy" / "memory", WS / ".workbuddy-ai" / "memory"]:
    if not d.exists():
        w(f"- `{d.relative_to(WS).as_posix()}/`：**不存在**")
        continue
    w(f"- `{d.relative_to(WS).as_posix()}/`：")
    for p in sorted(d.iterdir()):
        if p.is_file():
            t = p.read_text(encoding="utf-8", errors="replace")
            flag = "  ⚠ 超 3000" if (p.name == "MEMORY.md" and len(t) > 3000) else ""
            w(f"  - `{p.name}`：{len(t):,} 字符 / {p.stat().st_size:,} 字节{flag}")

sk = Path(r"C:\Users\26717\.workbuddy\skills")
w()
w(f"- 用户级技能根 `{sk}`：{'存在' if sk.exists() else '不存在'}")
if sk.exists():
    rows = []
    for d in sorted(p for p in sk.iterdir() if p.is_dir()):
        sm = d / "SKILL.md"
        ver = "—"
        ac = "—"
        nlines = 0
        if sm.exists():
            head = sm.read_text(encoding="utf-8", errors="replace")
            nlines = len(head.splitlines())
            m = re.search(r"^version:\s*(\S+)", head, re.M)
            ver = m.group(1) if m else "（无）"
            m2 = re.search(r"^agent_created:\s*(\S+)", head, re.M)
            ac = m2.group(1) if m2 else "—"
        nf = len(walk_files(d, skip_dirs=("__pycache__", ".git")))
        rows.append((d.name, ver, ac, nlines, nf))
    w(f"- 技能数：**{len(rows)}**")
    w()
    w("| 技能 | version | agent_created | SKILL.md 行数 | 文件数 |")
    w("|---|---|---|---|---|")
    for r in rows:
        w(f"| `{r[0]}` | {r[1]} | {r[2]} | {r[3]} | {r[4]} |")

OUT.write_text("\n".join(L), encoding="utf-8")
print(f"written: {OUT}")
print(f"lines: {len(L)}")
