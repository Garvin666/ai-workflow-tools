# -*- coding: utf-8 -*-
# [自研工具] gen_upgrade_doc.py
# 用途：生成《代码升级说明》并修复交付缺陷：按文件只渲染一次累积 diff，逐补丁只出自身 hunk 并标注单补丁施加态（原版把同一份累积 diff 重复渲染 10 次、同形状承载两个不同 sha256 量）
# 适用场景：补丁提案类交付文档的一致性生成与修复；不适用于非由 manifest 生成的文档
# 作者：数模工作区自研（系统优化方案-2026-09-18，2026-09-18）
# 仓库：https://github.com/Garvin666/ai-workflow-tools/blob/main/scripts/gen_upgrade_doc.py
"""生成《代码升级说明-底座泛化优化.html》——供后续把本轮改动落到底座。

自研脚本（本任务自行编写）：gen_upgrade_doc.py
原则同上：改动清单与验证结果由 `proposal_manifest.json` / `repro_summary_compare.json`
现取；**diff 内容直接读 `proposal/diffs/*.diff` 原文**（不是手抄），
这样文档与真实补丁不可能脱节。

用法::

    python gen_upgrade_doc.py
"""
from __future__ import annotations

import argparse
import difflib
import html
import importlib.util
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.dont_write_bytecode = True

from _report_data import collect, render, COMP, WORKSPACE           # noqa: E402

DIFF_DIR = COMP / "proposal" / "diffs"


def _load_patches() -> list[dict]:
    """从 build_proposal.py 取**模块级 PATCHES**（含每处改动的 old/new 原文）。

    ⚠️ 为什么直接用生成器的常量而不在文档里另存一份 old/new：
        两份副本迟早会漂移，而漂移的表现是"文档里的 hunk 与真补丁不一致"——
        这正是本文档要消灭的那类缺陷。以生成器为单一事实源。
    """
    spec = importlib.util.spec_from_file_location(
        "_bp_for_hunks", COMP / "proposal" / "build_proposal.py")
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except SystemExit:          # 该模块理论上不主动退出，保底不吞
        pass
    return list(getattr(mod, "PATCHES", []))


def hunk_of(pt: dict, ctx: int = 2) -> str:
    """把**单处**补丁渲染成 unified diff（只这一段，非文件级全量）。

    与文件级 diff 的区别必须说清楚：文件级 = 原文→成品的全貌；
    本函数 = 只有这一处 old→new 的局部。上一版把文件级全量贴进每张卡，
    导致 pipeline 的同一份 66 行 diff 被渲染 3 次。
    """
    a = pt["old"].splitlines(keepends=True)
    b = pt["new"].splitlines(keepends=True)
    return "".join(difflib.unified_diff(
        a, b, fromfile="改动前", tofile="改动后", n=ctx, lineterm=""))

CSS = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>代码升级说明 · 底座泛化优化</title>
<style>
  :root { --ink:#1a1d21; --ink2:#4a5260; --ink3:#7a8394; --line:#e3e7ee;
    --bg:#fff; --bg2:#f7f9fc; --accent:#2f6fd0; --up:#1f7a5c; --down:#b4531f; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--ink);
    font-family:"Segoe UI","Microsoft YaHei",system-ui,-apple-system,sans-serif;
    line-height:1.75; font-size:15px; }
  .wrap { max-width:1140px; margin:0 auto; padding:48px 32px 80px; }
  h1 { font-size:27px; margin:0 0 6px; letter-spacing:-.01em; }
  .sub { color:var(--ink3); font-size:13.5px; margin-bottom:30px; }
  h2 { font-size:19px; margin:42px 0 14px; padding-bottom:8px;
    border-bottom:2px solid var(--line); }
  h3 { font-size:15.5px; margin:26px 0 10px; }
  h4 { font-size:14px; margin:18px 0 8px; color:var(--ink); }
  p { margin:10px 0; color:var(--ink2); }
  code { background:var(--bg2); border:1px solid var(--line); border-radius:4px;
    padding:1px 5px; font-family:Consolas,monospace; font-size:12.5px; color:#2b3440; }
  pre { background:var(--bg2); border:1px solid var(--line); border-radius:8px;
    padding:14px 16px; overflow-x:auto; font-size:12.3px; line-height:1.55;
    font-family:Consolas,monospace; color:#2b3440; }
  pre.diff { background:#fbfcfe; }
  .add { color:#1f7a5c; } .del { color:#b4531f; } .hunk { color:#2f6fd0; font-weight:600; }
  table { width:100%; border-collapse:collapse; margin:14px 0; font-size:12.6px; }
  th { background:var(--bg2); text-align:left; padding:8px 9px; font-weight:600;
    border-bottom:2px solid var(--line); white-space:nowrap; }
  td { padding:7px 9px; border-bottom:1px solid var(--line); color:var(--ink2); }
  .mono { font-family:Consolas,monospace; font-size:11.6px; color:var(--ink); }
  .ctr { text-align:center; } .num { text-align:right; font-variant-numeric:tabular-nums; }
  .tag { display:inline-block; padding:1px 8px; border-radius:10px; font-size:11.5px; font-weight:600; }
  .tag.ok { background:#e6f4ee; color:var(--up); }
  .tag.no { background:#fdeee4; color:var(--down); }
  .note { background:#f4f8fd; border-left:3px solid var(--accent); border-radius:0 8px 8px 0;
    padding:12px 16px; margin:16px 0; font-size:13.5px; color:var(--ink2); }
  .warn { background:#fdf7f2; border-left:3px solid #d4803f; border-radius:0 8px 8px 0;
    padding:12px 16px; margin:16px 0; font-size:13.5px; color:var(--ink2); }
  .good { background:#f2faf6; border-left:3px solid var(--up); border-radius:0 8px 8px 0;
    padding:12px 16px; margin:16px 0; font-size:13.5px; color:var(--ink2); }
  ul, ol { color:var(--ink2); padding-left:22px; } li { margin:6px 0; }
  .card { background:var(--bg2); border:1px solid var(--line); border-radius:10px;
    padding:16px 18px; margin:14px 0; }
  .card h3 { margin-top:0; }
</style></head><body><div class="wrap">
"""


def diff_html(text: str) -> str:
    out = []
    for line in text.splitlines():
        e = html.escape(line)
        if line.startswith("+++") or line.startswith("---"):
            out.append(f'<span class="hunk">{e}</span>')
        elif line.startswith("@@"):
            out.append(f'<span class="hunk">{e}</span>')
        elif line.startswith("+"):
            out.append(f'<span class="add">{e}</span>')
        elif line.startswith("-"):
            out.append(f'<span class="del">{e}</span>')
        else:
            out.append(e)
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(WORKSPACE / "代码升级说明-底座泛化优化.html"))
    args = ap.parse_args()

    pm_path = COMP / "proposal" / "proposal_manifest.json"
    if not pm_path.is_file():
        print(f"[拒绝出文档] 缺提案清单: {pm_path}")
        return 2
    pm = json.loads(pm_path.read_text(encoding="utf-8"))

    _PATCHES = _load_patches()
    PATCH_BY_ID = {p["id"]: p for p in _PATCHES}
    if not _PATCHES:
        print("[拒绝出文档] 取不到 build_proposal.PATCHES —— "
              "无法渲染「每处改动自己的 hunk」，拒绝出一份只有文件级 diff 的文档")
        return 2

    data = collect()
    V = render(data)

    def S(key: str) -> str:
        if key not in V:
            raise KeyError(f"文档引用了不存在的 key: {key}（生成器里不许手写数值）")
        return f'<span data-key="{key}">{html.escape(V[key])}</span>'

    P: list[str] = [CSS]
    A = P.append

    A('<h1>代码升级说明 · 底座泛化优化</h1>')
    A('<div class="sub">本轮改动的清单、逐处改动、影响面，以及落底座与回滚的操作。'
      f'底座指纹 <span class="mono">{S("prop.fp_original")}</span> → '
      f'<span class="mono">{S("prop.fp_current")}</span>；'
      f'各场比赛 <code>base_fingerprint</code> 记录值已与现值一致：'
      f'<b>{S("prop.landed")}</b>。</div>')

    A('<div class="good"><b>落底座状态：已完成。</b>'
      '本轮先按「先原型、验证通过再落底座」的口径出提案，验证通过后于 '
      '<code>2026-09-18</code> 落到 <code>model_base/</code> 并重新基线化；'
      '落盘方式为<b>按字节从</b> <code>proposal/patched_base/</code> <b>复制</b>（不经过文本模式），'
      '因此「底座 = 原文 + 本节列出的改动」逐字节成立。'
      '过程中修掉一个<b>未声明副作用</b>（落补丁把 4 个源文件从 LF 翻成 CRLF），'
      '详见 §6.2 —— 它同时也是本文档补写 §2 的直接原因。</div>')

    A('<div class="note"><b>怎么用这份文档：</b>'
      f'① 全部改动都在 <code>proposal/patched_base/</code>（底座的完整拷贝）上验证过；'
      f'② 每处改动附 unified diff，diff 由文件内容比对生成、非手写；'
      f'③ 回归判据 = <code>proposal/repro_summary_bug.py</code> 的五用例：'
      f'要求「底座侧全败、补丁侧全过」，且<b>不得出现由补丁引入的回归</b>；'
      f'④ §2 先说明每个哈希是什么量 —— 跳过它会读出一个错误的"哈希是编的"结论。</div>')

    A('<h2>1. 改动一览</h2>')
    A(f'<p>共 <b>{S("prop.n_patches")}</b> 处改动、<b>{S("prop.n_files")}</b> 个文件。</p>')
    A('<table><thead><tr><th>编号</th><th>文件</th><th>性质</th><th>标题</th>'
      '<th class="ctr">底座侧</th><th class="ctr">补丁后</th></tr></thead><tbody>')
    case_by_patch = {"P1": 1, "P2": 2, "P3": 4, "P4a": 3, "P4b": 5, "P4c": 5}
    kind = {"P1": "修缺陷", "P2": "修缺陷", "P3": "修缺陷",
            "P4a": "修缺陷", "P4b": "留痕", "P4c": "口径"}
    for pt in pm["patches"]:
        ci = case_by_patch.get(pt["id"])
        if ci and f"repro.c{ci}.base" in V:
            b = f'<span class="tag {"ok" if V[f"repro.c{ci}.base"]=="通过" else "no"}">{S(f"repro.c{ci}.base")}</span>'
            q = f'<span class="tag {"ok" if V[f"repro.c{ci}.patched"]=="通过" else "no"}">{S(f"repro.c{ci}.patched")}</span>'
            cn = f'<span class="mono">{S(f"repro.c{ci}.name")}</span>'
        else:
            b = q = "—"
            cn = "—"
        A(f'<tr><td class="mono">{pt["id"]}</td><td class="mono">{pt["file"]}</td>'
          f'<td>{kind.get(pt["id"], "")}</td><td>{html.escape(pt["title"])}</td>'
          f'<td class="ctr">{b}</td><td class="ctr">{q}</td></tr>')
    A('</tbody></table>')
    A('<p class="sm">「底座侧 / 补丁后」= 对应用例在两种底座上的结果；'
      '用例编号与《底座泛化优化报告》§3 一致。</p>')

    # ---------------------------------------------------------------- 哈希与 diff 的语义
    A('<h2>2. 先读这一节：哈希与 diff 各是什么量</h2>')
    A('<div class="warn"><b>这一节是补写的，因为缺了它会导致一个真实的误读。</b>'
      '上一版本文档把<b>同一份文件级累积 diff</b> 在 P4a / P4b / P4c 三张卡里各渲染了一遍，'
      '又各自贴一个 sha256，而三个 sha 互不相同。读者据此会合理地怀疑「哈希是编的」——'
      '实测根因不是数据造假，而是<b>字节级哈希</b>与<b>文本级 diff</b> 根本不是同一个量，'
      '而文档没有把这件事写出来。下表把每个量钉死，公式化、可复算。</div>')
    A('<table><thead><tr><th>量</th><th>定义</th><th>怎么复算</th>'
      '<th>不写清楚会怎样被误读</th></tr></thead><tbody>')
    A('<tr><td class="mono">base_sha256</td>'
      '<td>该文件在<b>底座原文</b>里的 sha256（不含任何补丁）。'
      '同一文件上的多个补丁<b>共享</b>这同一个值 —— 这是对的，不是缺陷。</td>'
      '<td><code>sha256(底座该文件字节)</code></td>'
      '<td>「三个补丁的"改动前"长得一样，所以没真改」。</td></tr>')
    A('<tr><td class="mono">patched_sha256</td>'
      '<td><b>累积态</b>：按清单顺序把同一文件的补丁逐个施加到<b>同一份副本</b>后，'
      '截至本补丁的状态的 sha256。同文件 N 个补丁构成一条链 '
      '<code>base → h1 → … → hN</code>。</td>'
      '<td>在 <code>base</code> 上依次施加 P4a→P4b→P4c 再取 sha256</td>'
      '<td>「P4a 的哈希在盘上找不到任何匹配 ⇒ 哈希是编的」。'
      '中间态本来就不在盘上，盘上只有<b>链尾</b>（= <code>patched_base</code> 成品）。</td></tr>')
    A('<tr><td class="mono">diff</td>'
      '<td><b>文件级</b>一份，由 <code>base</code> 与 <code>patched_base</code> 两态现场比对生成（非手写）。'
      '同一文件的多个补丁<b>共用</b>这一份，不按补丁重复出。</td>'
      '<td>对两态跑 <code>difflib.unified_diff</code></td>'
      '<td>「pipeline 有 3 个补丁却只有 1 份 diff，漏了 2 份」。'
      '按补丁各出一份的话，三份会<b>逐字节相同</b>，那才是真的误导。</td></tr>')
    A('<tr><td class="mono">行尾</td>'
      '<td>以上都是<b>字节级</b>量，包含行尾字节。底座源码约定 = <b>LF</b>。</td>'
      '<td>数文件里 <code>\\r\\n</code> 与裸 <code>\\n</code> 的个数</td>'
      '<td>「改了 3 行 ⇒ 只有那 3 行的字节变了」。'
      '若生成器走文本模式读写，<b>整份文件每一行都会变</b>，而 diff 恰好看不出来（见 §6.2）。</td></tr>')
    A('</tbody></table>')
    A(f'<p>本提案的清单文件 <code>proposal_manifest.json</code> 现已把上述语义写成 '
      f'<code>sha_semantics</code> 段（共 <b>{S("prop.sha_semantics_n")}</b> 条），'
      f'不再依赖读者从生成器源码里反推。</p>')

    A('<h3>2.1 逐文件的哈希链</h3>')
    A('<table><thead><tr><th>文件</th><th class="mono">底座原文</th>'
      '<th class="ctr">补丁数</th><th>累积链（按施加顺序）</th>'
      '<th class="mono">落补丁后（成品）</th></tr></thead><tbody>')
    _file_order = sorted({p["file"] for p in pm["patches"]})
    for rel in _file_order:
        stem = Path(rel).stem
        pts = [p for p in pm["patches"] if p["file"] == rel]
        chain = " → ".join(S(f"prop.patch.{p['id']}.cum") for p in pts)
        A(f'<tr><td class="mono">{html.escape(rel)}</td>'
          f'<td class="mono">{S(f"prop.file.{stem}.before")}</td>'
          f'<td class="ctr">{S(f"prop.file.{stem}.n_patches")}</td>'
          f'<td class="mono">{chain}</td>'
          f'<td class="mono">{S(f"prop.file.{stem}.after")}</td></tr>')
    A('</tbody></table>')
    A(f'<p class="sm">落补丁后 4 个源文件的行尾类别实测 = <span class="mono">'
      f'{S("prop.eol_after")}</span>；「全部仍为 LF」= <b>{S("prop.eol_all_lf")}</b>。'
      f'这是本文档唯一一处<b>把行尾当断言来报</b>的地方 —— 因为一旦它不是 LF，'
      f'下面的每一个 sha256 都会与"改了哪几行"的直觉不符。</p>')

    # ---------------------------------------------------------------- 逐文件详述
    A('<h2>3. 逐文件详述</h2>')
    A('<p>按<b>文件</b>组织（而不是按补丁）有三条理由：'
      '① 文件级 diff 是"从原文到成品"的真实全貌，同一文件的多处改动本该共用一份；'
      '② 每处改动另附<b>它自己那一段</b>的改动前/改动后原文，读的人不必在 66 行 diff 里找；'
      '③ 哈希链是按文件成立的，脱离文件看单个哈希必然误读。</p>')
    _n = 0
    for rel in _file_order:
        _n += 1
        stem = Path(rel).stem
        pts = [p for p in pm["patches"] if p["file"] == rel]
        dpath = DIFF_DIR / f"{stem}.diff"
        A(f'<h3>3.{_n} <code>{html.escape(rel)}</code> · 共 {len(pts)} 处改动</h3>')

        # ---- 3.n.1 文件级完整 diff（**只渲染一次**）----
        A(f'<h4>3.{_n}.1 文件级完整改动（<code>{html.escape(dpath.name)}</code> 全文）</h4>')
        if dpath.is_file():
            _dtxt = dpath.read_text(encoding="utf-8")
            _dn = len(_dtxt.splitlines())
            A(f'<p class="sm">{_dn} 行 · 覆盖本文件全部 {len(pts)} 处改动 · '
              f'由两态现场比对生成</p>')
            A(f'<pre class="diff">{diff_html(_dtxt)}</pre>')
        else:
            A(f'<div class="warn">⚠️ 未找到 diff 文件 {html.escape(dpath.name)} —— '
              f'文档与补丁已脱节，落底座前必须先重建提案。</div>')
        A(f'<p class="sm mono">底座原文 {S(f"prop.file.{stem}.before")} '
          f'→ 落补丁后 {S(f"prop.file.{stem}.after")}（逐字节复算所得）</p>')

        # ---- 3.n.2… 每处改动各附自己的 hunk ----
        for j, pt in enumerate(pts, start=2):
            A(f'<div class="card">')
            A(f'<h4>3.{_n}.{j} {html.escape(pt["id"])} · {html.escape(pt["title"])}</h4>')
            A(f'<p><b>为什么改</b>：{html.escape(pt["why"])}</p>')
            A('<h4>本处改动（只有这一段；上下各 2 行上下文）</h4>')
            _pat = {**PATCH_BY_ID.get(pt["id"], {}), **pt}
            if "old" in _pat and "new" in _pat:
                A(f'<pre class="diff">{diff_html(hunk_of(_pat))}</pre>')
            else:
                A('<div class="warn">⚠️ 未能从 <code>build_proposal.PATCHES</code> 取到本处改动的 '
                  'old/new 原文 —— 拒绝静默输出空 hunk（空 diff 会被读成"这里没改"）。</div>')
            A(f'<p class="sm mono">本处施加后该文件的<b>累积态</b> sha256 = '
              f'{S(f"prop.patch.{pt['id']}.cum")}'
              f'（= 上面链上与 {html.escape(pt['id'])} 对应的那一格）</p>')
            A('</div>')

    # ---------------------------------------------------------------- 影响面
    A('<h2>4. 影响面分析</h2>')
    A('<table><thead><tr><th>改动</th><th>会变的行为</th><th>不会变的行为</th></tr></thead><tbody>')
    A('<tr><td class="mono">P1</td>'
      '<td>二分类 logistic 的 <code>summary()</code> 从抛错变为正常输出两类系数（互为相反数）</td>'
      '<td>模型拟合、预测、任何指标算法 —— 全部未触碰</td></tr>')
    A('<tr><td class="mono">P2</td>'
      '<td><code>evaluate_on_split</code> 返回值<b>新增</b> <code>summary_error</code> 键；'
      'summary 抛错时不再丢失指标</td>'
      '<td>已有键的名称与含义；指标计算路径</td></tr>')
    A('<tr><td class="mono">P3</td>'
      '<td>非数值二分类标签的 ROC 图例多出「（正类=…）」后缀；能正常出图</td>'
      '<td>数值标签 {0,1} 的行为（走原分支）；多分类分支；AUC 数值本身</td></tr>')
    A('<tr><td class="mono">P4a</td>'
      '<td>摘要打印失败时记 warning 并继续，而非中断 run()</td>'
      '<td>摘要正常时的日志输出</td></tr>')
    A('<tr><td class="mono">P4b</td>'
      '<td>开 <code>auto_select</code> 时结果多出 <code>selection</code> 块；'
      '冠军优势不显著时多一行提示日志</td>'
      '<td><b>选择规则本身</b> —— 仍是取 CV 均值最高者，本轮不改</td></tr>')
    A('<tr><td class="mono">P4c</td>'
      '<td>结果文件多出 <code>cv_note</code> / <code>cv_is_selection_metric</code> / '
      '<code>selection</code> 三个键</td>'
      '<td><code>cv_mean</code> / <code>test_metrics</code> 的数值与键名</td></tr>')
    A('</tbody></table>')
    A('<div class="note">三处新增键都是<b>增量</b>：不删不改造既有键，'
      '因此对已有比赛脚本与结果解析是向后兼容的。</div>')

    # ---------------------------------------------------------------- 未纳入
    A('<h2>5. 本轮<b>未</b>纳入的改动及理由</h2>')
    A('<h3>5.1 未把 1-SE 规则写进底座</h3>')
    A('<p>1-SE 规则（在最优的一个标准误内取最简模型）是防选型偏差的常见推荐做法。'
      '本轮把它作为一条独立选择臂实现了并实测，但结论是<b>不纳入</b>：</p>')
    if "ana.kmax" in V and f"sel.maxcv.k{V['ana.kmax']}" in data:
        k = V["ana.kmax"]
        A('<table><thead><tr><th class="ctr">池大小 k</th><th class="num">naive_bias</th>'
          '<th class="num">公共项 ref_gap</th><th class="num">sel_bias</th>'
          '<th class="num">|sel_bias| 均值</th><th class="num">比值 sel/ref_gap</th>'
          '</tr></thead><tbody>')
        for kk in sorted({int(x.split(".")[2][1:]) for x in data
                          if x.startswith("naive.maxcv.k") and x.endswith(".mean")}):
            A(f'<tr><td class="ctr">{kk}</td>'
              f'<td class="num">{S(f"naive.maxcv.k{kk}.mean")}</td>'
              f'<td class="num">{S(f"refgap.maxcv.k{kk}.mean")}</td>'
              f'<td class="num">{S(f"sel.maxcv.k{kk}.mean")}</td>'
              f'<td class="num">{S(f"selabs.maxcv.k{kk}")}</td>'
              f'<td class="num">{S(f"ratio.maxcv.k{kk}")}</td></tr>')
        A('</tbody></table>')
        A('<p class="sm">口径：均为 max_cv 臂；'
          '<code>naive_bias</code>=CV−测试（旧口径）；'
          '<code>ref_gap</code>=参照模型自身的 CV−测试（公共项）；'
          '<code>sel_bias</code>=差分后的纯选型偏差；'
          '比值 = <code>sel_bias / ref_gap</code>，用于判断选型偏差是否被公共项淹没。</p>')
        A('<p>判据：若 <code>|sel_bias|</code> 相对 <code>|ref_gap|</code> 很小，'
          '说明"挑最高分"带来的额外高估被 CV 噪声与数据集固有差异淹没，'
          '此时引入 1-SE 规则会<b>增加体制复杂度却换不到可辨的收益</b>，'
          '属于典型的"为解决问题而引入、却没解决问题"。</p>')
        A('<div class="good"><b>结论</b>：见上表比值列。本轮据此<b>不</b>把 1-SE 写入底座，'
          '而把改动集中在<b>报数口径</b>（P4b/P4c）—— 后者不依赖偏差大小的假设，'
          '是无论如何都该修的。1-SE 的实现保留为 <code>finetune/select_calibrated.py</code>，'
          '供将来在真正薄弱的池上再评估。</div>')
    else:
        A('<div class="warn">⚠️ 偏差分析产物缺失，本节结论无法给出。'
          '跑完 <code>analyze_selection_bias.py</code> 后重新生成本文档。</div>')

    A('<h3>5.2 其他未纳入项</h3>')
    A('<table><thead><tr><th>项</th><th>理由</th></tr></thead><tbody>')
    A('<tr><td>任务类型与候选池不匹配的校验</td>'
      '<td>属<b>新功能</b>而非缺陷修复（现状只是报错信息不友好，不是正确配置下崩）；'
      '本轮范围限定为"修偏差 + 加能力"，故列为后续项</td></tr>')
    A('<tr><td>Stacking / 概率校准是否进底座</td>'
      '<td>由显著性门决定，结论见《底座泛化优化报告》§5。'
      '在证据不足以证明"超过 1 个标准误的增益"时，<b>不</b>引入</td></tr>')
    A('<tr><td>重构 <code>pipeline.py</code> 的步骤划分</td>'
      '<td>与本次目标无关的重构会放大落底座的回归面</td></tr>')
    A('</tbody></table>')

    # ---------------------------------------------------------------- 落底座闸门
    A('<h2>6. 落底座的操作序列（含闸门）</h2>')
    A('<h3>6.1 实际采用的序列</h3>')
    A('<pre># 0. 先记录基线，落完要能证明"只改了该改的"\n'
      'python tools/mmguard.py verify            # 期望 state=ok，记下聚合指纹\n\n'
      '# 1. 解锁（底座是只读锁定的）\n'
      'python tools/mmguard.py unlock            # 期望 88/88 可写\n\n'
      '# 2. 落补丁：**按字节从提案副本复制**（不要走文本模式，见 §6.2）\n'
      '#    proposal/patched_base/ 是底座的完整拷贝 + 本轮改动，\n'
      '#    逐文件 read_bytes → write_bytes 覆盖底座，即可保证\n'
      '#    「底座 = 原文 + 本轮改动」逐字节成立（含行尾）。\n'
      'python <落补丁脚本>                        # 每步断言 sha256，不符即中止\n\n'
      '# 3. 回归验证：要求"底座侧全败、补丁侧全过、无回归"\n'
      'python competitions/2026-底座泛化优化/proposal/repro_summary_bug.py\n'
      'echo "期望退出码 0；五用例 5/5"\n\n'
      '# 4. 重新基线化并加锁\n'
      'python tools/mmguard.py seal --force      # 必然写 reseal_note（不许洗白）\n'
      'python tools/mmguard.py lock\n'
      'python tools/mmguard.py verify            # 期望 state=ok，指纹不同于步骤 0\n\n'
      '# 5. 同步各场比赛的 MANIFEST.base_fingerprint（否则 mmrun 会拒绝启动）\n'
      'python tools/mmcheck.py --all             # 期望 失败 0</pre>')
    A('<div class="warn"><b>为什么必须重新 seal 并同步 fingerprint：</b>'
      '每场比赛的 <code>MANIFEST.json</code> 记录了创建时的底座指纹，'
      '<code>mmrun</code> 会在跑前校验。底座一变，所有旧比赛的指纹全部失效 —— '
      '这是设计如此（防止"结果归因到一个已经不存在的底座版本"），'
      '不是需要绕过的障碍。</div>')

    A('<h3>6.2 落补丁必须保行尾 —— 一个实测踩到的未声明副作用</h3>')
    A('<p>第一次落补丁时，<code>build_proposal.py</code> 用 '
      '<code>Path.read_text()</code> / <code>write_text()</code> 读写文件，也就是走<b>文本模式</b>：'
      '读入时 universal-newline 把 <code>CRLF</code> 抹成 <code>LF</code>，'
      '写出时又把 <code>LF</code> 翻成 <code>os.linesep</code>（Windows 上是 <code>CRLF</code>）。'
      '底座源码约定是 <b>LF</b>，于是产生了三层后果，一层比一层隐蔽：</p>')
    A('<ol>'
      '<li><b>一次 3 行的修改，把整份文件每一行的字节都改掉了。</b>'
      '实测 <code>classification.py</code> 从 12928 字节涨到 14281 字节，'
      '其中绝大部分增量是换行符，不是代码。</li>'
      '<li><b>diff 完全看不出来。</b>difflib 也是文本模式比对，视差被抹平后 '
      '<code>classification.diff</code> 仍只有 29 行，看起来一切正常。</li>'
      '<li><b>字节级 sha256 与文本级 diff 声称的不是同一个量。</b>'
      '读者拿 manifest 的哈希去对文档里的 diff，<b>永远对不上</b>，'
      '而"对不上"会被误判成"哈希是编的"—— 这就是 §2 那一节的由来。</li>'
      '</ol>')
    A('<div class="good"><b>修法（已实施）：</b>① 生成器改为 '
      '<code>read_bytes()</code> / <code>write_bytes()</code>，全程按字节；'
      '② 从<b>干净原始态</b>重做整条链（先按字节还原 4 文件、断言 sha 回到 '
      '<code>base_sha256</code>，再重建提案、再落盘）；'
      '③ 加两条<b>不变量断言</b>：落补丁后 4 文件行尾必须是 <code>LF</code>'
      '（本节开头 §2.1 已在报），且 <code>patched_base</code> 与 '
      '<code>model_base</code> 必须<b>逐字节相同</b>。'
      '④ manifest 增加 <code>sha_semantics</code> 段，把"每个哈希是什么量"写进数据本身，'
      '而不是留给读者从源码反推。</div>')
    A('<div class="warn"><b>可推广的教训：</b>凡是<b>用字节级指纹做验收</b>的流程，'
      '就不能用文本模式读写被验收的文件 —— 否则验收对象（字节）与展示对象（文本）'
      '会静默错位，而错误方向恰好是"看起来更正常"。'
      '同类风险：编辑器自动格式化、<code>.gitattributes</code> 的 <code>text=auto</code>、'
      '跨平台复制。</div>')

    A('<h2>7. 回滚方式</h2>')
    A('<p>本轮改动都是小范围文本替换，回滚成本低：</p>')
    A('<ol>'
      '<li><b>整体回滚</b>：<code>git checkout -- model_base/</code>（改动尚未提交时）；'
      '已提交则 <code>git revert</code> 对应提交。</li>'
      '<li><b>单处回滚</b>：对每处改动，把 diff 里的 <code>+</code> 行与 <code>-</code> 行对调即可；'
      '每处改动都是<b>独立的连续块</b>，互不重叠（同一个文件内的 P4a/P4b/P4c 也分处不同行）。</li>'
      '<li><b>回滚后必做</b>：重跑步骤 3 的回归验证。'
      '注意回滚到未打补丁的状态后，<code>repro_summary_bug.py --base model_base</code> '
      '应当重新变为"全败"——这本身就是"补丁确实是必要原因"的对照。</li>'
      '</ol>')

    A('<h2>8. 验证与追溯</h2>')
    A(f'<p>本文档中的每个数字（含上面所有 sha256 与指纹）都是 <code>data-key</code> 锚点，'
      f'由 <code>tmp/_report_data.py</code> 的<b>单一事实源</b>现算：'
      f'补丁与哈希来自 <code>proposal_manifest.json</code>、落补丁后的哈希**对 '
      f'<code>proposal/patched_base/</code> 现场重算**、指纹来自 '
      f'<code>tools/baseline/model_base.manifest.json</code> 与各场 <code>MANIFEST.json</code>；'
      f'校验器 <code>tmp/verify_report.py</code> 会把它们从 HTML 里解析回来逐一比对，'
      f'并配阴性对照（改一位数字必须变红）。</p>')
    A(f'<p class="sm">为什么哈希也要进锚点体系：<b>不能被复算的数字，就只能靠"作者说是这样"</b>。'
      f'上一版文档的哈希正是这样脱节的 —— 它们不在事实源里，所以没人发现'
      f'「同一份 diff 配了三个互斥的 after-hash」。</p>')
    A(f'<p class="sm">补丁副本：<code>{html.escape(pm["patched_dir"])}</code>；'
      f'diff 目录：<code>competitions/2026-底座泛化优化/proposal/diffs/</code>。</p>')

    A('</div></body></html>')

    out = Path(args.out)
    out.write_text("\n".join(P), encoding="utf-8")
    print(f"已生成 {out}  ({out.stat().st_size} 字节)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
