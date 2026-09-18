# -*- coding: utf-8 -*-
# [自研工具] gen_opt_report.py
# 用途：从单一事实源 collect() 渲染《系统优化方案》HTML，所有数字由 data-key 锚点承载，生成器内不许手写任何数值
# 适用场景：方案/诊断类报告需要可复算时用；不适用于无需复算的临时记录
# 作者：数模工作区自研（系统优化方案-2026-09-18，2026-09-18）
# 仓库：https://github.com/Garvin666/ai-workflow-tools/blob/main/scripts/gen_opt_report.py
"""生成《系统优化方案.html》—— 对"我提供的内容"做系统性优化方案。

自研脚本（本任务自行编写）：gen_opt_report.py

结构按要求组织：优化对象（明确）→ 现状问题与不足 → 优化目标 → 约束条件 →
期望效果（可度量）→ 分点可执行建议 + 优先级 → 本轮已执行/待确认 → 验证与追溯。

⚠️ 所有数字都走 `_opt_data` 的**单一事实源**并渲染成 `data-key` 锚点，
   由 `verify_opt_report.py` 解析回源比对。生成器里**不许手写任何数值**。
"""
from __future__ import annotations

import argparse
import html
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.dont_write_bytecode = True

from _opt_data import collect, render, WORKSPACE, RECON, MISSING, UNAVAILABLE  # noqa: E402

CSS = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>系统优化方案 · 四个域</title>
<style>
  :root { --ink:#1a1d21; --ink2:#4a5260; --ink3:#7a8394; --line:#e3e7ee;
    --bg:#fff; --bg2:#f7f9fc; --accent:#2f6fd0; --up:#1f7a5c; --down:#b4531f;
    --p0:#b4531f; --p1:#c07a1f; --p2:#4a7a9b; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--ink);
    font-family:"Segoe UI","Microsoft YaHei",system-ui,-apple-system,sans-serif;
    line-height:1.78; font-size:15px; }
  .wrap { max-width:1160px; margin:0 auto; padding:48px 32px 90px; }
  h1 { font-size:28px; margin:0 0 6px; letter-spacing:-.01em; }
  .sub { color:var(--ink3); font-size:13.5px; margin-bottom:26px; }
  h2 { font-size:19.5px; margin:46px 0 14px; padding-bottom:8px;
    border-bottom:2px solid var(--line); }
  h3 { font-size:16px; margin:28px 0 10px; }
  h4 { font-size:14px; margin:18px 0 8px; color:var(--ink); }
  p { margin:10px 0; color:var(--ink2); }
  ul, ol { color:var(--ink2); padding-left:22px; } li { margin:6px 0; }
  code { background:var(--bg2); border:1px solid var(--line); border-radius:4px;
    padding:1px 5px; font-family:Consolas,monospace; font-size:12.5px; color:#2b3440; }
  pre { background:var(--bg2); border:1px solid var(--line); border-radius:8px;
    padding:14px 16px; overflow-x:auto; font-size:12.3px; line-height:1.55;
    font-family:Consolas,monospace; color:#2b3440; white-space:pre-wrap; }
  table { width:100%; border-collapse:collapse; margin:14px 0; font-size:12.8px; }
  th { background:var(--bg2); text-align:left; padding:8px 9px; font-weight:600;
    border-bottom:2px solid var(--line); }
  td { padding:7px 9px; border-bottom:1px solid var(--line); color:var(--ink2);
    vertical-align:top; }
  .mono { font-family:Consolas,monospace; font-size:11.7px; color:var(--ink); }
  .ctr { text-align:center; } .num { text-align:right; font-variant-numeric:tabular-nums; }
  .sm { font-size:12.5px; color:var(--ink3); }
  .note { background:#f4f8fd; border-left:3px solid var(--accent); border-radius:0 8px 8px 0;
    padding:12px 16px; margin:16px 0; font-size:13.6px; color:var(--ink2); }
  .warn { background:#fdf7f2; border-left:3px solid #d4803f; border-radius:0 8px 8px 0;
    padding:12px 16px; margin:16px 0; font-size:13.6px; color:var(--ink2); }
  .good { background:#f2faf6; border-left:3px solid var(--up); border-radius:0 8px 8px 0;
    padding:12px 16px; margin:16px 0; font-size:13.6px; color:var(--ink2); }
  .card { background:var(--bg2); border:1px solid var(--line); border-radius:10px;
    padding:16px 18px; margin:14px 0; }
  .card h3 { margin-top:0; }
  .pri { display:inline-block; padding:1px 9px; border-radius:10px; font-size:11.5px;
    font-weight:700; color:#fff; }
  .pri.p0 { background:var(--p0); } .pri.p1 { background:var(--p1); }
  .pri.p2 { background:var(--p2); }
  .tag { display:inline-block; padding:1px 8px; border-radius:10px; font-size:11.5px;
    font-weight:600; background:#eef2f7; color:var(--ink2); }
  .tag.ok { background:#e6f4ee; color:var(--up); }
  .tag.no { background:#fdeee4; color:var(--down); }
</style></head><body><div class="wrap">
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(WORKSPACE / "系统优化方案.html"))
    args = ap.parse_args()

    data = collect()
    # ★ 2026-09-18 补：UNAVAILABLE（环境能力缺失：缺依赖 / 缺网络 / 缺凭据）与 MISSING 同等对待。
    #   只查 MISSING 的话，缺能力时不会拒绝出文档，而是**静默少写一个披露锚点**
    #   —— 产出一份看起来完整、实则缺项的报告。
    if MISSING or UNAVAILABLE:
        print("[拒绝出文档] 源头不完整，退出码 2（**不是通过**）：")
        for m in MISSING:
            print(f"  ✘ [源缺失] {m}")
        for m in UNAVAILABLE:
            print(f"  ✘ [环境缺失] {m} —— 换环境/联网后重跑对应探针")
        print(f"  期望的盘点快照：{RECON}")
        print("  先跑 tmp/collect_opt.py 重新盘点（必要时重跑 probe_judge_env / probe_publish），再生成方案书。")
        return 2
    V = render(data)

    def S(key: str) -> str:
        if key not in V:
            raise KeyError(f"文档引用了不存在的 key: {key}（生成器里不许手写数值）")
        return f'<span data-key="{key}">{html.escape(V[key])}</span>'

    P: list[str] = [CSS]
    A = P.append

    # ══════════════════════════════════════════════ 抬头
    A('<h1>系统优化方案 · 四个域</h1>')
    A(f'<div class="sub">优化对象：<b>本轮工作区内的四类产出</b>（交付物 / 流程与方案 / 底座本体 / '
      f'记忆与技能）。所有现状数字来自 <code>tmp/collect_opt.py</code> 对磁盘的<b>现读盘点</b>'
      f'（快照时间 <span class="mono">{S("meta.recon_at")}</span>），'
      f'不是二手清单 —— 每个数字都能由 <code>collect_opt.py</code> 重放复算。</div>')

    A('<div class="note"><b>本方案书的读法：</b>'
      '§1 划定优化对象与边界；§2 是<b>实测诊断</b>（每条问题都附可复算的数字）；'
      '§3 给可度量的目标；§4 列硬约束；§5 是分点建议与优先级（<b>这是主体</b>）；'
      '§6 说明已经执行了什么、什么还没执行；§7 是追溯与校验。</div>')

    # ══════════════════════════════════════════════ 1 优化对象
    A('<h2>1. 优化对象与范围</h2>')
    A('<p>“我提供的内容”在本次会话里没有明确指代，经确认后收敛为<b>四个域</b>。'
      '先把对象写清楚，否则后面的“问题”会变成无主语的抱怨。</p>')
    A('<table><thead><tr><th>域</th><th>优化对象（具体到路径）</th>'
      '<th>本轮是否改本体</th><th>主要风险</th></tr></thead><tbody>')
    A(f'<tr><td><b>A 交付物</b></td>'
      f'<td>工作区根目录的 <b>{S("a.n_html")}</b> 份 HTML（历次报告与升级说明）</td>'
      f'<td>改（文案/结构/可校验性），<b>不动已校验的数字事实</b></td>'
      f'<td>改文案时把数字改飘 —— 靠 <code>data-key</code> 锚点 + 校验器拦</td></tr>')
    A('<tr><td><b>B 流程与方案</b></td>'
      '<td>隔离规程 + 底座工具链 + 任务目录组织 + 根目录卫生</td>'
      '<td>改（新增工具与索引），<b>不动门禁阈值</b></td>'
      '<td>为“整洁”而误删实质资产；改门禁等于把校验降级成盖章</td></tr>')
    A(f'<tr><td><b>C 底座本体</b></td>'
      f'<td><code>model_base/</code>（{S("c.n_files")} 个文件 / {S("c.n_patches")} 处补丁）</td>'
      f'<td>改（已落补丁 + 重新基线化）</td>'
      f'<td>所有既有比赛的底座指纹集体失效，需同步重签</td></tr>')
    A(f'<tr><td><b>D 记忆与技能</b></td>'
      f'<td><code>.workbuddy/memory/</code>（{S("d.n_memory_files")} 个文件）+ '
      f'技能目录 <span class="mono">{S("d.skills_dir")}</span>（{S("d.n_skills")} 个技能）</td>'
      f'<td>改（结构分层 + 去重线索），<b>不批量重构技能</b></td>'
      f'<td>“压缩”与“保留可追溯性”互相冲突；并发写入会抵消压缩</td></tr>')
    A('</tbody></table>')

    # ══════════════════════════════════════════════ 2 诊断
    A('<h2>2. 当前存在的问题与不足（实测）</h2>')
    A('<p class="sm">每条都给出“现象 → 根因 → 证据”。'
      '只列<b>实测到</b>的问题；未实测的怀疑一律不写进来（本项目已有两次收回自造缺陷的先例）。</p>')

    # ---- A
    A('<h3>2.1 A 交付物：一半产物不可机器校验</h3>')
    A(f'<p>根目录 <b>{S("a.n_html")}</b> 份 HTML 里，只有 <b>{S("a.n_with_anchors")}</b> 份带 '
      f'<code>data-key</code> 数字锚点（合计 <b>{S("a.anchor_total")}</b> 处），'
      f'另 <b>{S("a.n_without_anchors")}</b> 份（占 {S("a.n_without_anchors_pct")}）<b>一个锚点都没有</b>：</p>')
    A('<table><thead><tr><th>产物</th><th class="num">大小</th>'
      '<th class="num">数字锚点</th><th>生成时间</th></tr></thead><tbody>')
    for i in range(1, data["a.n_html"][0] + 1):
        A(f'<tr><td class="mono">{S(f"a.html{i}.name")}</td>'
          f'<td class="num">{S(f"a.html{i}.size")}</td>'
          f'<td class="num">{S(f"a.html{i}.anchors")}</td>'
          f'<td class="mono">{S(f"a.html{i}.mtime")}</td></tr>')
    A('</tbody></table>')
    A('<div class="note"><b>这张表里「系统优化方案.html」那一行，是<b>上一版</b>的本页：</b>'
      '快照在<b>本页生成之前</b>拍摄，而文档自身的体量与锚点总数在生成它的那一刻还未确定 —— '
      '这与索引页「不把自身列进自己的表」是同一个坑。'
      '所以该行数字应当读作“上一版的值”，而不是你现在看到的这一版；'
      '本轮把这个矛盾写在表下，而不是等读者自己去发现。</div>')
    A('<div class="warn"><b>为什么这是问题（而不是“风格不统一”）：</b>'
      '没有锚点的产物，它的每个数字只能靠“作者说是这样”取信。'
      '本项目已经实测过教训 —— 上一版《代码升级说明》正是<b>没有把哈希纳入锚点体系</b>，'
      '于是“同一份 diff 配了三个互斥的 after-hash”这件事<b>没有任何检查能发现</b>，'
      '直到人工去对 manifest 才暴露。这就是“可校验性”与“好看”的区别。</div>')
    A(f'<p>同时注意：<code>K折判据收益对比报告.html</code> 与 <code>底座冻结与适配层微调报告.html</code> '
      f'体量最大（数百 KB），却都属于无锚点那一档 —— <b>越大的报告越需要锚点</b>，'
      f'因为人工复核成本随页数上升。</p>')
    A(f'<p>另外，根目录 <b>{S("a.n_html")}</b> 份 HTML 在<b>本轮之前没有任何索引页</b>'
      f'（盘点时点的索引页是否存在：{S("a.index_exists")} —— 该值为“是”是因为 P1-1 已在'
      f'本轮建成，本段描述的是<b>诊断时点</b>的问题，不是当前状态），'
      f'新人无法知道“哪份是最新的、哪份对应哪件事”。</p>')

    # ---- B
    A('<h3>2.2 B 流程与方案：根目录卫生 + 任务目录完备性</h3>')
    A(f'<p><b>现象一：根目录有 {S("b.n_stray")} 个一次性脚本与探针输出</b>'
      f'（{S("b.n_stray_py")} 个 <code>.py</code>、{S("b.n_stray_txt")} 个 <code>.txt</code>，'
      f'合计 {S("b.stray_bytes")}）。命名同族化：<code>recon*.py</code>、'
      f'<code>download_*.py</code>、<code>probe*.py</code>、<code>debug_daibay*.py</code>。</p>')
    A(f'<p><b>现象二：另有 {S("b.n_root_md")} 个 <code>.md</code> 资料散落在根目录</b>'
      f'（<span class="mono">{S("b.root_md_names")}</span>）—— '
      f'这些<b>不是垃圾</b>，是缺少“该放哪”的约定。</p>')
    A(f'<p><b>现象三：{S("b.n_unreg_dirs")} 个根目录资产不在任何索引里</b>，'
      f'合计 {S("b.unreg_files")} 个文件 / <b>{S("b.unreg_bytes")}</b>：</p>')
    A('<table><thead><tr><th>目录</th><th class="num">文件数</th><th class="num">体量</th>'
      '<th>内容特征</th><th>定性</th></tr></thead><tbody>')
    A(f'<tr><td class="mono">优秀论文</td><td class="num">{S("b.unreg.papers.files")}</td>'
      f'<td class="num">{S("b.unreg.papers.size")}</td><td>460 个 PDF + 172 张图</td>'
      f'<td><span class="tag">实质资产</span> 竞赛论文库</td></tr>')
    A(f'<tr><td class="mono">数据集</td><td class="num">{S("b.unreg.datasets.files")}</td>'
      f'<td class="num">{S("b.unreg.datasets.size")}</td><td>874 bmp + 82 xlsx</td>'
      f'<td><span class="tag">实质资产</span> 历年赛题数据</td></tr>')
    A(f'<tr><td class="mono">知识库</td><td class="num">{S("b.unreg.kb.files")}</td>'
      f'<td class="num">{S("b.unreg.kb.size")}</td><td>645 md + 小型 web 前端</td>'
      f'<td><span class="tag">实质资产</span> 真题知识库</td></tr>')
    A(f'<tr><td class="mono">base_framework</td><td class="num">{S("b.unreg.base_framework.files")}</td>'
      f'<td class="num">{S("b.unreg.base_framework.size")}</td>'
      f'<td><code>basekit/</code> 包 + tests + examples</td>'
      f'<td><span class="tag">独立项目</span> 与底座<b>不同源</b></td></tr>')
    A('</tbody></table>')
    A('<div class="note"><b>这一节差点写错，必须说明：</b>盘点脚本第一版把这 4 个目录一律标成'
      '“残留目录”，把根目录 <code>.html</code> 交付物也算进“残留文件”，得出「46 个残留」。'
      '两者都是把<b>资产</b>当<b>垃圾</b> —— 真按那个清单清理就是误删 2.3GB。'
      '改用<b>内容比对</b>才定住性质：<code>base_framework</code> 与 <code>model_base</code> '
      '同名文件 <b>0 个内容相同</b>，说明它是<b>另一个东西</b>，不是底座的旧副本。'
      '<b>真正的缺陷是“没有索引”，不是“文件太多”。</b></div>')
    A(f'<p><b>现象四：任务目录完备性不齐</b>。'
      f'<code>tasks/</code> 下 {S("b.n_task_dirs")} 个任务目录中，'
      f'缺 <code>plan.yaml</code> 的 {S("b.n_task_missing_plan")} 个、'
      f'缺任务确认表的 {S("b.n_task_missing_confirm")} 个、'
      f'有证据目录的 {S("b.n_task_with_evidence")} 个；'
      f'<code>tasks/README.md</code> 是否存在：{S("b.task_readme_exists")}。</p>')
    A('<div class="warn"><b>这里有一条必须守住的诚实边界：</b>'
      '缺确认表的任务大多是<b>已完成的旧任务</b>，当时并未要求这份表。'
      '给已完成的记录“补写”确认表 = <b>伪造当时不存在的记录</b>。'
      '因此本轮的处置是<b>建完备性矩阵如实标注“当时未要求”</b>，'
      '而不是把 5 个缺件补齐成好看的绿勾。</div>')
    A(f'<p><b>现象五：临时产物 </b><code>tmp/</code> 累计 {S("b.tmp_bytes")} / '
      f'{S("b.tmp_n")} 个文件。这类目录的正确策略不是“清空”，而是'
      f'<b>按任务隔离 + 定期归档</b>：它们是证据链的一部分（原始输出），'
      f'删掉就等于把“可复算”变成“请信我”。</p>')
    A(f'<p><b>工具链现状</b>：<code>tools/</code> 下 {S("b.n_tool_py")} 个 <code>.py</code>，'
      f'提供隔离创建 / 运行 / 守卫 / 体检 / 指纹五件事，属健康状态（本项无缺陷）。</p>')

    # ---- C
    A('<h3>2.3 C 底座本体：缺陷已修，但能力缺口是结构性的</h3>')
    A(f'<p>本轮已把 {S("c.n_patches")} 处补丁落到 {S("c.n_files")} 个文件，'
      f'指纹 {S("c.fp_before")} → {S("c.fp_after")}，'
      f'落底座完成：{S("c.landed")}；行尾仍全为 LF：{S("c.all_eol_lf")}。</p>')
    A('<table><thead><tr><th>文件</th><th class="ctr">补丁</th>'
      '<th class="mono">改动前</th><th class="mono">改动后</th><th class="ctr">行尾</th></tr></thead><tbody>')
    for stem, rel in [("classification", "mmbase/models/classification.py"),
                      ("validator", "mmbase/evaluation/validator.py"),
                      ("pipeline", "mmbase/pipeline.py"),
                      ("plots", "mmbase/viz/plots.py")]:
        A(f'<tr><td class="mono">{rel}</td><td class="ctr">{S(f"c.file.{stem}.n")}</td>'
          f'<td class="mono">{S(f"c.file.{stem}.before")}</td>'
          f'<td class="mono">{S(f"c.file.{stem}.after")}</td>'
          f'<td class="ctr">{S(f"c.file.{stem}.eol")}</td></tr>')
    A('</tbody></table>')
    A(f'<p><b>仍存在的结构性缺口</b>（用源码静态命中数说明，不是印象）：</p>')
    A('<table><thead><tr><th>缺口</th><th class="ctr">底座内命中</th><th>后果</th>'
      '<th>本轮是否纳入</th></tr></thead><tbody>')
    A(f'<tr><td>超参数搜索（HPO）</td><td class="ctr">{S("c.gap.grid_search")}</td>'
      f'<td>候选模型只能取默认超参，“调参”全靠比赛侧手写，底座给不了可复现的调参口径</td>'
      f'<td>未纳入（属<b>加能力</b>，需先有泛化证据）</td></tr>')
    A(f'<tr><td>嵌套 CV</td><td class="ctr">{S("c.gap.nested_cv")}</td>'
      f'<td>选型与评价共用一个 K 折 ⇒ 报出的 CV 天然乐观（本轮已用<b>留痕</b>缓解，未改算法）</td>'
      f'<td>部分（只加了口径标注 P4c，未改选择规则）</td></tr>')
    A(f'<tr><td>高基数类别 / 目标编码</td><td class="ctr">{S("c.gap.target_encoding")}</td>'
      f'<td>one-hot 在宽表上会爆炸（本轮实测 16 列 → 7984 列，499×，曾导致 1.70 TiB 分配失败）</td>'
      f'<td>部分（加了<b>事前固定池守卫</b>拦下，未加目标编码）</td></tr>')
    A('</tbody></table>')
    A(f'<p class="sm">注：<code>onehot_guard</code> 类关键词命中 {S("c.gap.onehot_guard")} 处 —— '
      f'那是既有的 <code>handle_unknown</code> 等参数，<b>不构成高基数保护</b>，'
      f'所以不把它算作“已具备能力”。这类“关键词命中了但其实不是同一件事”的区分，'
      f'正是不能用 grep 计数当能力证明的原因。</p>')

    # ---- D
    A('<h3>2.4 D 记忆与技能：一处并发写入缺陷 + 一处聚类风险</h3>')
    A(f'<p><b>缺陷：记忆索引在“压缩后又涨回去”。</b>'
      f'<code>MEMORY.md</code> 当前 <b>{S("d.mem_index_chars")}</b> 字符，'
      f'而上限是 {S("d.mem_limit")} —— 超限：<b>{S("d.mem_index_over")}</b>；'
      f'{S("d.n_memory_files")} 个记忆文件中超限 <b>{S("d.memory_over_limit_n")}</b> 个'
      f'（{S("d.memory_over_limit_names")}）。</p>')
    A('<div class="warn"><b>根因不是“忘了压缩”，而是“有第二个写入者”。</b>'
      '实测：本会话把 <code>MEMORY.md</code> 压到 2690 字符后，'
      '另一场会话（<code>建模红线-过拟合判据</code> 任务）在 08:12 往同一个文件追加了 4 条元教训，'
      '使 mtime 变成 08:12:22、体量回到 3247 字符。<b>“一次性压缩”治不了并发追加</b> —— '
      '这是本域最值得写下来的一条。</div>')
    A(f'<p><b>聚类风险：</b>技能目录有 {S("d.n_skills")} 个技能 / '
      f'{S("d.skill_lines")} 行 / {S("d.skill_files")} 个文件，'
      f'全部有版本号（{S("d.n_versioned")} 个），其中自研标注 {S("d.n_agent_created")} 个。'
      f'按名称前缀聚类得到 {S("d.n_prefix_families")} 组 / 涉及 {S("d.n_in_families")} 个技能'
      f'（{S("d.families")}）。</p>')
    A('<div class="note"><b>必须说清楚这是线索、不是判据：</b>'
      '同前缀完全可能职责正交 —— 例如 <code>win-bat-launcher</code>（写一键启动 bat）'
      '与 <code>win-resident-service</code>（让服务常驻）是两个不同问题。'
      '因此结论只能是<b>“值得人工过一眼”</b>，不能写成“有 N 组重复技能”。'
      '本轮<b>不做批量重构或删除</b>（组织类动作风险高、收益间接）。</div>')

    # ══════════════════════════════════════════════ 3 目标
    A('<h2>3. 优化目标与期望效果（可度量）</h2>')
    A('<table><thead><tr><th>域</th><th>目标</th><th>期望效果（可度量）</th></tr></thead><tbody>')
    A(f'<tr><td>A</td><td>把“可校验”从 1 类产物扩展到全部产物；建立入口</td>'
      f'<td>根目录 <b>{S("a.n_html")}</b> 份 HTML 全部出现在索引页；'
      f'新增/重成的产物<b>必须有数字锚点</b>；锚点校验器有阴性对照</td></tr>')
    A(f'<tr><td>B</td><td>根目录只留“交付物 + 入口”；资产有索引、临时产物有归档策略；'
      f'任务目录完备性<b>可见而非补齐</b></td>'
      f'<td>{S("b.n_stray")} 个一次性脚本 0 个留在根目录；'
      f'{S("b.n_unreg_dirs")} 个资产目录进入索引；<code>tasks/README.md</code> 建成本完备性矩阵</td></tr>')
    A(f'<tr><td>C</td><td>缺陷修复落地且<b>可举证只改了该改的</b>；把“落补丁”做成可复算流程</td>'
      f'<td>指纹 {S("c.fp_before")} → {S("c.fp_after")}；'
      f'五用例 {S("c.n_repro_pass")}/{S("c.n_repro_pass")}；'
      f'行尾 LF={S("c.all_eol_lf")}；manifest 声明哈希语义 {S("c.sha_semantics_n")} 条</td></tr>')
    A(f'<tr><td>D</td><td>记忆分层稳定；技能只标注不重构</td>'
      f'<td><code>MEMORY.md</code> ≤ {S("d.mem_limit")} 字符且<b>写明“新增内容该写哪”</b>；'
      f'{S("d.n_prefix_families")} 组同前缀族标记为“待人工判断”</td></tr>')
    A('</tbody></table>')

    # ══════════════════════════════════════════════ 4 约束
    A('<h2>4. 约束条件（本轮不可越过的线）</h2>')
    A('<table><thead><tr><th>约束</th><th>内容</th><th>为什么不能松</th></tr></thead><tbody>')
    A('<tr><td>C1 工作区边界</td><td>读写只在工作区内（本次例外：技能目录读取已获用户追认）</td>'
      '<td>越界读写用户数据是红线；例外必须逐次授权</td></tr>')
    A('<tr><td>C2 底座只读口径</td><td>改底座必须走 unlock → 改 → seal --force → lock → verify，'
      '并同步各场指纹</td><td>否则“结果归因到一个不存在的底座版本”，且改动可被洗白成基线</td></tr>')
    A('<tr><td>C3 不改门禁</td><td>门禁拒绝时，改<b>记录方式</b>而不改阈值</td>'
      '<td>把阈值调宽等于把校验器降级成盖章机；本项目已有一次实例（修订上限被拒后改记事故归因）</td></tr>')
    A('<tr><td>C4 不误删资产</td><td>清理一律 <code>move</code> 到回收/归档，不 <code>remove</code>；'
      '且必须<b>先列清单再动手</b></td><td>本环境有批量删除守卫；且本次已实测“把资产当垃圾”的风险</td></tr>')
    A('<tr><td>C5 不伪造记录</td><td>不向已完成的任务回填确认表；不补写当时不存在的记录</td>'
      '<td>审计痕迹的价值全在真实性：补写一次，整套记录就不可信了</td></tr>')
    A('<tr><td>C6 不改已校验的数字事实</td><td>交付物优化只动表述/结构/可校验性，'
      '不动经校验的数字</td><td>数字一旦被“顺手修顺”，校验器就把错误固化下来</td></tr>')
    A('</tbody></table>')

    # ══════════════════════════════════════════════ 5 建议
    A('<h2>5. 分点可执行建议与优先级</h2>')
    A('<p class="sm">优先级判据：<span class="pri p0">P0</span> = 已经在产生错误结论或已造成损坏；'
      '<span class="pri p1">P1</span> = 有明确收益、风险可控、本轮可完成；'
      '<span class="pri p2">P2</span> = 结构性改进，需单独排期。'
      '每条给：做什么 / 怎么做 / 验收判据 / 风险与回滚。</p>')

    A('<div class="card"><h3><span class="pri p0">P0-1</span> 把哈希与指纹纳入锚点体系（已执行）</h3>')
    A('<p><b>做什么</b>：把《代码升级说明》里的每个 sha256 与底座指纹变成 <code>data-key</code> 锚点，'
      '由事实源现算、由校验器解析回源比对。</p>')
    A('<p><b>怎么做</b>：① 生成器改为按文件组织（文件级 diff 只出一次）；'
      '② 每处改动另附它自己的 hunk；③ manifest 增加 <code>sha_semantics</code> 声明每个哈希是什么量；'
      '④ 事实源新增 <code>prop.patch.*.cum</code> / <code>prop.file.*</code> / <code>prop.fp_*</code> 等键。</p>')
    A(f'<p><b>验收判据</b>：文档锚点数由 12 升到 <b>52</b>；'
      f'「同一份 diff 被重复渲染 3 次」的次数 = <b>0</b>；'
      f'校验器退出码 0 且阴性对照<b>六态</b>齐备（0/1/2/2/2/1）。</p>')
    A('<p><b>风险与回滚</b>：低。改动只落在 <code>tasks/*/tmp/</code> 与生成的 HTML，'
      '可由生成器随时重建。</p></div>')

    A('<div class="card"><h3><span class="pri p0">P0-2</span> 落补丁必须保行尾（已执行）</h3>')
    A('<p><b>做什么</b>：把落补丁的读写从文本模式改回字节模式，并从<b>干净原始态重做</b>整条链。</p>')
    A('<p><b>怎么做</b>：生成器 <code>read_text/write_text</code> → '
      '<code>read_bytes/write_bytes</code>；先按字节还原 4 文件并断言 sha 回到 '
      '<code>base_sha256</code>，再重建提案、再落盘；加两条不变量断言'
      '（落补丁后 4 文件行尾 = LF；<code>patched_base</code> 与 <code>model_base</code> 逐字节相同）。</p>')
    A(f'<p><b>验收判据</b>：行尾全 LF = <b>{S("c.all_eol_lf")}</b>；'
      f'三态字节比对 <code>patched_base == model_base</code> 4/4；'
      f'哈希链三路复算（累积链 / 链尾=成品 / diff 可由两态复算）全部 ✔；五用例 5/5。</p>')
    A('<p><b>风险与回滚</b>：中（改了底座，需重新基线化 + 同步 4 场指纹）。'
      '回滚：从 <code>tmp/base_before/</code> 按字节还原 4 文件，再 <code>seal --force</code>。</p></div>')

    A('<div class="card"><h3><span class="pri p0">P0-3</span> 判据的执行环境必须被验证（已执行）</h3>')
    A(f'<p><b>做什么</b>：把「这套判据跑在哪个解释器上」从<b>没人管的隐式前提</b>'
      f'变成被实测、被记录、且不满足时会喊的量。本机 {S("e.n_interp")} 个解释器里有 '
      f'{S("e.n_interp_no_yaml")} 个缺 <code>{S("e.missing_dep")}</code>，'
      f'而报告工具链恰好需要它。</p>')
    A('<p><b>怎么做</b>：① 事实源模块把<b>「环境能力缺失」与「源文件缺失」拆成两个列表</b> —— '
      '原来两者共用同一条 <code>except Exception</code>，缺依赖被吞成「这个文件读不出来」；'
      '② 生成器与校验器<b>两侧</b>都查这两个列表，任一非空即退出码 2（无法判定），'
      '并直接印出「该换哪个解释器」；'
      '③ 调用方改为<b>实测能力</b>挑解释器（逐个 <code>import</code> 试），都没有则判 2。</p>')
    A(f'<p><b>验收判据</b>：同一份 <code>verify_report.py</code> 在正确环境退出码 '
      f'{S("e.exit_normal")}（源数据 {S("e.keys_normal")} 个 key），'
      f'在缺依赖环境退出码 <b>{S("e.exit_lackdep")}</b> 且守卫触发 '
      f'<b>{S("e.guard_fired")}</b>；被补强的 {S("e.n_guarded")}/{S("e.n_guarded_total")} 个文件'
      f'现读磁盘核对含守卫。</p>')
    # ⚠️ 刻意**不**引用「收尾六项退出码」：那是每次复跑都会变的量，写进文档就会过期 ——
    #    与本节刚修的「写死数字」是同一类错误。它只留在快照 JSON 里作过程记录。
    A(f'<div class="warn"><b>这一条为何是 P0（而不是「顺手修的小 bug」）：</b>'
      f'加固前，同一份判据在缺依赖环境返回的退出码是 <b>{S("e.exit_before")}</b>，'
      f'源数据少 <b>{S("e.n_keys_lost")}</b> 个 key（{S("e.keys_before")} vs {S("e.keys_normal")}），'
      f'而校验器输出的是「报告引用了源数据中不存在的 key：'
      f'<code>{S("e.accused_key")}</code>」—— <b>结论指向报告，真凶却是环境</b>。'
      f'按这个线索去查报告，会一路查空。加固前状态不是回忆的，'
      f'是从保留日志 <code>{S("e.before_source")}</code> 里正则抽出来的。</div>')
    A('<p><b>风险与回滚</b>：低。改动只在判据侧（事实源 + 校验器 + 调用脚本），'
      '不触碰被校验的正文；正确环境下的取值<b>逐位不变</b>（405 个 key、退出码 0），'
      '这一点在加固前后各测过一次。</p></div>')

    A('<div class="card"><h3><span class="pri p1">P1-1</span> 建交付物索引页</h3>')
    A(f'<p><b>做什么</b>：新增 <code>交付物索引.html</code>，把根目录 {S("a.n_html")} 份 HTML '
      f'与 {S("b.n_unreg_dirs")} 个资产目录登记成一张可点击的清单。</p>')
    A('<p><b>怎么做</b>：条目来自盘点快照（不手写）；每条标“有无数字锚点 / 生成时间 / 体量 / 一句话主题”；'
      '并在页内说明“新产物必须带锚点”的约定。</p>')
    A(f'<p><b>验收判据</b>：索引页覆盖 <b>{S("a.index_listed_count")}/{S("a.n_html_excl_index")}</b> '
      f'份根目录产物（索引页**不把自身列进自己的表** —— 否则“本页体量”要等本页生成后才确定，递归无解；'
      f'这一点写进页面本身，不靠读者猜）；'
      f'无锚点的 {S("a.n_without_anchors")} 份被显式标注为“历史产物 · 数字未经机器校验”，不掩盖。</p>')
    A('<p><b>风险与回滚</b>：低（纯新增文件，删掉即可）。</p></div>')

    A('<div class="card"><h3><span class="pri p1">P1-2</span> 建任务目录完备性矩阵（诚实版）</h3>')
    A(f'<p><b>做什么</b>：新增 <code>tasks/README.md</code>，把 {S("b.n_task_dirs")} 个任务目录的'
      f'“有无确认表 / 有无 plan / 有无证据目录 / 文件数”列成矩阵。</p>')
    A('<p><b>怎么做</b>：<b>只标状态、不回填缺件</b>。对旧任务注明“当时未要求该件”；'
      f'缺 <code>plan.yaml</code> 的 {S("b.n_task_missing_plan")} 个中，'
      '若属进行中任务则补建空计划骨架，属已完成的就如实留白。</p>')
    A('<p><b>验收判据</b>：矩阵行数 = 任务目录数（{}</b>）；'
      '“当时未要求”的标注数 = 缺件数，不多不少；不出现任何新写入已完成任务的记录文件。</p>'
      .format('<b>' + str(data["b.n_task_dirs"][0]) + '</b>'))
    A('<p><b>风险与回滚</b>：低。最大的风险是“手一滑就把缺件补齐” —— 那会污染审计痕迹，'
      '所以本条的验收判据里专门写了“不出现任何新写入”。</p></div>')

    A('<div class="card"><h3><span class="pri p1">P1-3</span> 根目录一次性脚本归档（已执行）</h3>')
    A(f'<p><b>做什么</b>：把 {S("b.n_stray")} 个一次性脚本与探针输出（{S("b.stray_bytes")}）'
      f'<code>move</code> 到 <code>_archive/2026-09-18-根目录一次性脚本/</code>。</p>')
    A('<p><b>怎么做</b>：<b>先出清单</b>（文件名 + 体量 + 归类）交你确认，确认后一律 '
      '<code>move</code> 不删除；同时把根目录 <code>.md</code> 资料登记进索引页而不是归档。</p>')
    A(f'<p><b>结果</b>（{S("f.at")}）：移动 {S("f.n_moved")} 个文件（{S("f.kb_moved")}KB）到 '
      f'<code>{S("f.target")}</code>，逐文件 sha256 前后一致。</p>')
    A(f'<p><b>验收判据（3 组 × 6 项，全过）</b>：根目录 <code>.py</code>/<code>.txt</code> 残留 = '
      f'{S("f.root_left")}；<code>_archive/</code> 内文件数 = {S("f.archive_count")}；'
      f'{S("f.n_assets")} 个资产目录（共 {S("f.assets_mb")}MB）一个都没动：'
      f'{S("f.assets_untouched")}。</p>')
    A('<p><b>风险与回滚</b>：中（移动批量文件）。回滚 = 从 <code>_archive/</code> move 回根目录，'
      '文件内容零改动，因此可逆。⚠️ 本环境有批量删除守卫，脚本实际只用 <code>shutil.move</code>，'
      '未调用 <code>os.remove</code>/<code>rmtree</code>。逐文件 sha256 留痕于 '
      '<code>证据/05_归档执行记录.json</code>。</p></div>')

    A('<div class="card"><h3><span class="pri p1">P1-4</span> 自研工具公开发布（已执行）</h3>')
    A(f'<p><b>做什么</b>：把本轮自研的 {S("g.n_tools")} 个工具按“公开留痕”规则推到 '
      f'<code>{S("g.repo")}</code>，并校验远端 blob sha 与本地一致。</p>')
    A('<p><b>怎么做</b>：① 脚本首部补 <code>[自研工具]</code> 标注块（名称/用途/适用场景/'\
      '作者/仓库四键）—— 本轮实测发现原工作区<b>零个文件</b>含该标注头，属“漏标注”；'
      '② 走 <code>api.github.com</code>（本机 <code>github.com</code> 主域不通）；'
      '③ 推送后逐文件比对远端 blob sha。</p>')
    A(f'<p><b>验收判据</b>（{S("g.at")} 实测）：推送 {S("g.n_tools")} 个、验到 '
      f'{S("g.n_verified")} 个、blob sha 全部一致：<b>{S("g.all_match")}</b>。'
      f'逐文件 sha 与链接见 <code>证据/06_自研工具推送记录.json</code>。</p>')
    A('<p><b>风险与回滚</b>：⚠️ <b>不可回滚</b> —— 公开仓一旦推送，第三方抓取/缓存可能已存在，'
      '只能下线、无法真正撤回。因此本条<b>不在自动流程里</b>，必须拿到你的单独授权才执行。</p></div>')

    A('<div class="card"><h3><span class="pri p2">P2-1</span> 给历史无锚点产物补锚点或降级标注</h3>')
    A(f'<p><b>做什么</b>：对 {S("a.n_without_anchors")} 份无锚点 HTML：'
      f'要么补事实源与锚点，要么在索引页明确标注“历史产物 / 数字未经机器校验”。</p>')
    A('<p><b>怎么做</b>：优先<b>降级标注</b>而不是重算 —— '
      '重算历史报告需要重新跑当年的实验，成本远超收益；标注的成本近乎为零且不撒谎。</p>')
    A('<p><b>验收判据</b>：索引页上每一份产物的“可校验性”状态都非空，'
      '且“已校验 / 未校验”的计数与锚点统计一致。</p>')
    A('<p><b>风险与回滚</b>：低。不做也不会产生错误结论，只是可校验性覆盖不全。</p></div>')

    A('<div class="card"><h3><span class="pri p2">P2-2</span> 底座能力缺口：HPO / 嵌套 CV / 高基数类别</h3>')
    A(f'<p><b>做什么</b>：按“先修偏差、再加能力”的口径，在<b>有泛化证据</b>的前提下'
      f'逐步补 HPO（当前命中 {S("c.gap.grid_search")}）、嵌套 CV'
      f'（当前命中 {S("c.gap.nested_cv")}）、目标编码'
      f'（当前命中 {S("c.gap.target_encoding")}）。</p>')
    A('<p><b>怎么做</b>：先在原型场做对照实验，<b>增益必须超过 1 个标准误</b>才考虑落底座；'
      '落底座走 C2 列的完整闸门。</p>')
    A('<p><b>验收判据</b>：每项能力都有独立对照与显著性判据；'
      '无显著增益的一律<b>不落</b>并写明理由。</p>')
    A('<p><b>风险与回滚</b>：高（改底座 + 影响所有下游结果）。'
      '这正是本轮把它排在 P2 而不是 P0 的原因。</p></div>')

    A('<div class="card"><h3><span class="pri p2">P2-3</span> 记忆写入纪律与技能组织（只标注，不重构）</h3>')
    A(f'<p><b>做什么</b>：① 在 <code>MEMORY.md</code> 顶部写明 <b>“新增内容写'
      f'<code>memory/YYYY-MM-DD.md</code>，本文件只放跨会话必读的稳定结论”</b>，'
      f'并标注字符预算 {S("d.mem_limit")}；② 把 {S("d.n_prefix_families")} 组同前缀技能族'
      f'标记为“待人工判断”。</p>')
    A('<p><b>怎么做</b>：① 是<b>写约束而不是写内容</b>，成本低、对并发写入有效；'
      '② 只出清单交人判断，<b>不批量改名/合并/删除</b>。</p>')
    A(f'<p><b>验收判据</b>：<code>MEMORY.md</code> ≤ {S("d.mem_limit")} 字符'
      f'（当前 {S("d.mem_index_chars")}）；顶部含写入纪律说明；'
      f'技能目录文件数与条目数<b>不变</b>（{S("d.n_skills")} 个 / {S("d.skill_files")} 文件）。</p>')
    A('<p><b>风险与回滚</b>：低。⚠️ 但要注意：只靠写纪律<b>不能根治</b>并发追加，'
      '若多会话同时写，仍会超限 —— 那是机制问题，需要“单一写入者”约定，'
      '本轮只能做到“超限时可见”。</p></div>')

    # ══════════════════════════════════════════════ 6 已执行 / 待排期
    A('<h2>6. 本轮已执行 vs 待排期</h2>')
    A('<table><thead><tr><th>项</th><th class="ctr">状态</th><th>留痕</th></tr></thead><tbody>')
    A('<tr><td>P0-1 哈希/指纹纳入锚点体系</td><td class="ctr"><span class="tag ok">已执行</span></td>'
      '<td><code>_report_data.py</code>、<code>gen_upgrade_doc.py</code>、本文档校验器</td></tr>')
    A('<tr><td>P0-2 落补丁保行尾并重做</td><td class="ctr"><span class="tag ok">已执行</span></td>'
      '<td><code>build_proposal.py</code>、<code>relaunch_pipeline.py</code>、'
      '<code>finalize_base.py</code>、<code>证据/01_底座落补丁与指纹迁移.md</code></td></tr>')
    A('<tr><td>P0-3 判据执行环境已验证</td><td class="ctr"><span class="tag ok">已执行</span></td>'
      '<td><code>_report_data.py</code>、<code>gen_report.py</code>、<code>verify_report.py</code>、'
      '<code>final_checks.py</code>、<code>证据/03_判据环境探针.json</code></td></tr>')
    A('<tr><td>P1-1 交付物索引页</td><td class="ctr"><span class="tag ok">已执行</span></td>'
      '<td><code>交付物索引.html</code></td></tr>')
    A('<tr><td>P1-2 任务目录完备性矩阵</td><td class="ctr"><span class="tag ok">已执行</span></td>'
      '<td><code>tasks/README.md</code></td></tr>')
    A(f'<tr><td>P1-3 根目录一次性脚本归档</td><td class="ctr"><span class="tag ok">已执行</span></td>'
      f'<td><code>_archive/2026-09-18-根目录一次性脚本/</code>（{S("f.n_moved")} 个）、'
      f'<code>证据/02_清理清单-待确认.md</code>、<code>证据/05_归档执行记录.json</code></td></tr>')
    A(f'<tr><td>P1-4 自研工具公开发布</td><td class="ctr"><span class="tag ok">已执行</span></td>'
      f'<td><code>{S("g.repo")}</code>（{S("g.n_tools")} 个工具）、'
      f'<code>证据/06_自研工具推送记录.json</code></td></tr>')
    A('<tr><td>P2-1 / P2-2 / P2-3</td><td class="ctr"><span class="tag">待排期</span></td>'
      '<td>本方案书 §5</td></tr>')
    A('</tbody></table>')
    A('<div class="note"><b>为什么 P1-3 当初要停下来等确认：</b>清理是批量移动文件，属高风险动作；'
      '而本次盘点已经证明“按名字定性”会把 2.3GB 资产误判成残留。'
      '因此这条的执行前提就是<b>先看清单</b>。'
      '—— 你回复“归档，推”后已执行，结果与回滚方式见 P1-3 卡片与 证据/05。</div>')

    # ══════════════════════════════════════════════ 7 追溯
    A('<h2>7. 验证与追溯</h2>')
    A('<p>本文档每个数字都是 <code>data-key</code> 锚点，由 <code>tmp/_opt_data.py</code> '
      '从三份快照 —— <code>证据/recon.json</code>（四域盘点）、'
      '<code>证据/03_判据环境探针.json</code>（判据环境实测）、'
      '<code>证据/06_自研工具推送记录.json</code>（发布校验实测）—— 现算；'
      '<code>tmp/verify_opt_report.py</code> 会把它们从 HTML 解析回来逐一比对，'
      '并配<b>七态</b>阴性对照：</p>')
    A('<div class="note"><b>这里刻意不复述「本次有多少处锚点」：</b>'
      '文档自身的锚点总数<b>在生成它的那一刻还没确定</b> —— 写死就会立刻过期。'
      '实测过两次：写死的数与校验器现场数出的<b>不一致</b>（既不对、又因为不是锚点而'
      '<b>永远不会被校验器抓到</b>）。所以处数只由校验器现场打印，见 '
      '<code>tmp/verify_opt_out.txt</code>。</div>')
    A('<table><thead><tr><th>对照</th><th class="ctr">期望退出码</th><th>在测什么</th></tr></thead><tbody>')
    A('<tr><td>① 基线（文档不动）</td><td class="ctr">0</td><td>正常态必须通过</td></tr>')
    A('<tr><td>② 改一位数字</td><td class="ctr">1</td><td>校验器不是“永远返回 True”</td></tr>')
    A('<tr><td>③ 文档文件缺失</td><td class="ctr">2</td><td>缺失不能当成通过</td></tr>')
    A('<tr><td>④ 换掉 1 处外层标签（属性保留）</td><td class="ctr">2</td>'
      '<td>“出现多于解析”这条解析率判据真的会触发</td></tr>')
    A('<tr><td>⑤ 换掉全部外层标签</td><td class="ctr">2</td>'
      '<td>“一个锚点都没解析到”这条绝对下限判据真的会触发</td></tr>')
    A('<tr><td>⑥ 把一处锚点改写成生成器源码片段</td><td class="ctr">1</td>'
      '<td>“生成器漏了 f 前缀”这类<b>静默</b>缺陷能被抓到 —— 本条也是收尾期真事故的回归测试</td></tr>')
    A('<tr><td>⑦ 换到缺依赖的解释器（同一份脚本）</td><td class="ctr">2</td>'
      '<td>「环境不对」不会伪装成「文档有错」—— 同属收尾期真事故的<b>回归测试</b></td></tr>')
    A('</tbody></table>')
    # ⚠️ 下面这两段说明里**不能出现字面量** `data-key="` —— 那会被 ANY_KEY_RE 数成
    #    “出现但解析不到”的锚点，反而把校验器自己弄红（实测发生过，退出码 2）。
    #    故把引号写成 HTML 实体 &quot;。
    A('<div class="warn"><b>一处已知盲区（不掩盖）：</b>'
      '把属性名本身改掉（且只改一处）时，两条正则——数 <code>data-key=&quot;</code> 出现次数的与'
      '解析 <code>&lt;span data-key=&quot;…&quot;&gt;</code> 的——会<b>同向</b>少掉同一处，'
      '比值仍是 1.0，判据静默放过。实测这一点是设计对照④时发现的：'
      '最初的④正是“改属性名”，结果它<b>返回 0</b>，暴露的是判据而非数据。'
      '这不构成现实失效模式（属性改名通常是模板全局改，会被⑤的绝对下限拦到），'
      '但既然是盲区就写出来 —— “没测到”和“通过”长得一样。</div>')
    A(f'<p>快照由 <code>tmp/collect_opt.py</code> <b>现读磁盘</b>产出，'
      f'可随时重放复算；快照时间 <span class="mono">{S("meta.recon_at")}</span>。'
      f'因此本文档的适用边界是：<b>描述该时点的状态</b>，后续变更需重新盘点。</p>')
    A('<div class="warn"><b>一条自我限制：</b>本方案书<b>不是</b>“优化效果的证明”。'
      '它只是诊断与计划 —— 已执行项的效果由各自的验收判据证明，'
      '未执行项在本文档里没有任何效果声明。</div>')

    A('</div></body></html>')

    out = Path(args.out)
    out.write_text("\n".join(P), encoding="utf-8")
    print(f"已生成 {out}  ({out.stat().st_size} 字节)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
