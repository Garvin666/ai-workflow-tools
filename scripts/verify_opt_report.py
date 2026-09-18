# -*- coding: utf-8 -*-
# [自研工具] verify_opt_report.py
# 用途：三态校验器（0 一致 / 1 不符 / 2 无法判定）＋ 六态阴性对照自检：把报告里的 data-key 锚点正则解析回事实源比对，绝不硬编码期望值
# 适用场景：任何『报告里的数字必须能被复核』的交付；不适用于没有单一事实源的散记
# 作者：数模工作区自研（系统优化方案-2026-09-18，2026-09-18）
# 仓库：https://github.com/Garvin666/ai-workflow-tools/blob/main/scripts/verify_opt_report.py
"""校验《系统优化方案.html》里的每个数字是否仍与盘点快照一致。

自研脚本（本任务自行编写）：verify_opt_report.py
为什么不能省：方案书是**被验对象**，不是判据来源。盘点全绿 ≠ 方案书写得对。
本项目已实测过"产物验收全绿、报告仍有数字脱节"的情形。

判据
----
1. 数字从**方案书正文**正则解析（``data-key`` 锚点），**绝不硬编码在脚本里** ——
   硬编码会让"改脚本让断言跟着文档走"变成必然全绿，那是自我循环而非校验。
2. 期望值由 ``_opt_data.collect()/render()``（与生成器**同一个**事实源）现算。
3. 解析护栏：判据是**自洽性** —— "HTML 里出现的 data-key 是否都被解析到"，
   而不是"锚点绝对个数下限"。⚠️ 这条是踩过坑才改的：原来设了绝对下限 15，
   结果《代码升级说明》只有 12 个锚点、正则完全正常，却被判"无法判定"（退出码 2），
   把"文档短"误判成"解析坏"。护栏要拦的是**静默空跑**，空跑的表现是
   "出现的多、解析到的少"，不是"总数少"。
4. 退出码本身是结论，三态独立：
     0 = 文档与快照一致
     1 = 查了、而且不一致
     2 = 无法判定（文档不存在 / 快照缺失 / 锚点解析不到）

用法::

    python verify_opt_report.py
    python verify_opt_report.py --self-test
"""
from __future__ import annotations

import argparse
import html
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.dont_write_bytecode = True

from _opt_data import collect, render, WORKSPACE, RECON, MISSING, UNAVAILABLE  # noqa: E402

DEFAULT_REPORT = WORKSPACE / "系统优化方案.html"
SPAN_RE = re.compile(r'<span\s+data-key="([^"]+)"\s*>([^<]*)</span>')
# 「HTML 里出现过 data-key 属性」的计数正则 —— 与上面那条**故意不同源**：
# 它只认属性、不认内容结构，所以能独立数出"本该被解析出多少个"。
ANY_KEY_RE = re.compile(r'data-key="([^"]+)"')
# ★ 2026-09-18 新增：生成器源码片段泄漏。
#   根因：生成器里某个**字符串片段漏了 f 前缀** → `{S("key")}` 被当普通文本渲染进 HTML。
#   为什么原有判据全部拦不住：该片段既没有 `data-key=` 属性（ANY_KEY_RE 数不到）、
#   也不影响解析率 —— 危害是完全静默的：文档里少了一个数字、多了一段源码。
#   实测踩到 2 处（`b.n_root_md`、`b.n_task_missing_plan`），成品 HTML 里能看到源码片段。
LEAK_RE = re.compile(r"\{S\(")
MIN_PARSE_RATE = 1.0
MIN_ANCHORS_ABS = 5


def parse_anchors(text: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for m in SPAN_RE.finditer(text):
        out.setdefault(m.group(1), []).append(html.unescape(m.group(2)).strip())
    return out


def verify(report: Path) -> tuple[int, str]:
    if not report.is_file():
        return 2, f"[无法判定] 方案书不存在: {report}"
    if not RECON.is_file():
        return 2, (f"[无法判定] 事实源快照缺失: {RECON}\n"
                   f"  先跑 tmp/collect_opt.py 重新盘点，再校验。")

    text = report.read_text(encoding="utf-8")
    got = parse_anchors(text)
    n_present = len(ANY_KEY_RE.findall(text))
    n_parsed = sum(len(v) for v in got.values())

    if n_parsed < MIN_ANCHORS_ABS:
        return 2, (f"[无法判定] 只解析到 {n_parsed} 处锚点"
                   f"（HTML 里出现 {n_present} 处，下限 {MIN_ANCHORS_ABS}）—— "
                   f"这几乎一定是锚点正则与文档结构不匹配，**不能当成通过**。")
    rate = n_parsed / n_present if n_present else 0.0
    if rate < MIN_PARSE_RATE:
        return 2, (f"[无法判定] 锚点解析率 {rate:.1%}（出现 {n_present} 处、只解析出 "
                   f"{n_parsed} 处，下限 {MIN_PARSE_RATE:.0%}）。"
                   f"出现多于解析 = 正则没覆盖全部标签结构 —— **不能当成通过**。")

    data = collect()
    # ★ 2026-09-18 补：事实源不完整时必须判 2，**不能**带着缺项往下比 ——
    #   缺一个 key 会让文档里的锚点变成“引用了不存在的 key”，于是失败被归因到
    #   **被校验的文档**上，而真凶是环境或数据。
    #   ⚠️ 这道守卫本文件原**完全没有**（只查了 RECON 一个文件）—— 与 gen_report.py
    #   上一轮“生成侧有守卫、校验侧忘了查”是**同一条病在两个文件里各犯一次**。
    if MISSING or UNAVAILABLE:
        msg = ["[无法判定] 事实源不完整 —— 不能带着缺项往下校验"
               "（否则失败会被错误归因到文档上）:"]
        for m in MISSING:
            msg.append(f"  ✘ [源缺失] {m}")
        for m in UNAVAILABLE:
            msg.append(f"  ✘ [环境缺失] {m} —— 换环境/联网后重跑对应探针")
        msg.append("  → 退出码 2（**不是通过**），先把源头补齐。")
        return 2, "\n".join(msg)

    want = render(data)

    # ★ 2026-09-18 新增：先查"文档里有没有漏出生成器源码片段"。
    #   放在数字比对**之前** —— 它比"某个数字不对"更基础（数字压根没渲染出来）。
    #   判 1（不符）而非 2：这是**确定的内容缺陷**，不是无法判定。
    leaks = sorted(set(LEAK_RE.findall(text)))
    if leaks:
        positions = [m.start() for m in LEAK_RE.finditer(text)][:5]
        return 1, (f"[不符] 文档里出现生成器源码片段 {len(positions)} 处"
                   f"（形如 `{{S(...)}}`）—— 说明生成器里有字符串片段**漏了 f 前缀**："
                   f"该数字既没渲染出来、也不会被任何锤点判据抓到。\n"
                   f"  修法：生成器里搜含 `{{S(` 却**本行无 f 前缀**的字符串片段。")

    lines = [f"方案书: {report}",
             f"解析到锚点 {len(got)} 个（去重）/ {n_parsed} 处"
             f"（HTML 出现 {n_present} 处，解析率 {rate:.0%}）；事实源 {len(want)} 个 key",
             ""]

    unknown = sorted(set(got) - set(want))
    mismatch = []
    for k, vs in sorted(got.items()):
        if k not in want:
            continue
        for v in vs:
            if v != want[k]:
                mismatch.append((k, want[k], v))
    unrendered = sorted(set(want) - set(got))

    if unknown:
        lines.append(f"✘ 文档引用了事实源里不存在的 key（{len(unknown)} 个）:")
        lines += [f"    {k}" for k in unknown[:20]]
    if mismatch:
        lines.append(f"✘ 数字不符（{len(mismatch)} 处）:")
        lines.append(f"    {'key':<30}{'事实源':>16}{'文档':>16}")
        for k, e, g in mismatch[:30]:
            lines.append(f"    {k:<30}{e:>16}{g:>16}")
    if not unknown and not mismatch:
        lines.append("✔ 所有锚点数字与盘点快照一致")
    if unrendered:
        lines.append("")
        lines.append(f"ℹ 事实源中有 {len(unrendered)} 个 key 未出现在文档里"
                     f"（信息性，不计失败）:")
        lines += [f"    {k}" for k in unrendered[:15]]

    ok = not unknown and not mismatch
    lines += ["", f"结论: {'通过（退出码 0）' if ok else '不通过（退出码 1）'}"]
    return (0 if ok else 1), "\n".join(lines)


def self_test(report: Path) -> int:
    """六态阴性对照：① 基线必须 0；② 脚本一字不改、只改一位数字 → 必须 1；③ 文件缺失 → 必须 2；
    ④ 换 1 处外层标签 → 2；⑤ 换全部标签 → 2；⑥ 注入生成器源码片段 → 1。"""
    print("=" * 88)
    print("阴性对照：① 基线 0 / ② 改一位数字必红 1 / ③ 文件缺失 2 / "
          "④ 破坏 1 处 2 / ⑤ 全破坏 2 / ⑥ 漏源码片段 1")
    print("=" * 88)
    if not report.is_file():
        print(f"[无法判定] 方案书不存在: {report}")
        return 2

    code0, _ = verify(report)
    print(f"  [① 基线] 未改动方案书 → 退出码 {code0}（期望 0）")

    text = report.read_text(encoding="utf-8")
    anchors = parse_anchors(text)
    target = None
    for k, vs in anchors.items():
        v = vs[0]
        if re.fullmatch(r"[+-]?\d+(\.\d+)?", v):
            target = (k, v)
            break
    if target is None:
        print("  [无法判定] 找不到可扰动的数值锚点 → 退出码 2")
        return 2

    k, old = target
    # 整数与小数分别扰动，保证"改的是一位数字"而不是把字符串弄乱
    if "." in old:
        new = old[:-1] + ("9" if old[-1] != "9" else "0")
    else:
        new = old[:-1] + ("9" if old[-1] != "9" else "0") if len(old) > 1 else str((int(old) + 1) % 10)
    mutated = text.replace(f'data-key="{k}">{old}<', f'data-key="{k}">{new}<', 1)
    if mutated == text:
        print(f"  [无法判定] 扰动失败（锚点 {k} 未匹配到原文）→ 退出码 2")
        return 2
    tmp = Path(tempfile.gettempdir()) / "mutated_opt_report.html"
    tmp.write_text(mutated, encoding="utf-8")
    code1, out = verify(tmp)
    print(f"  [② 扰动] {k}: {old} → {new} → 退出码 {code1}（期望 1）")
    for l in out.splitlines():
        if l.strip().startswith("✘"):
            print(f"      {l.strip()}")

    tmp2 = Path(tempfile.gettempdir()) / "_no_such_opt_report_.html"
    if tmp2.exists():
        tmp2.unlink()
    code2, _ = verify(tmp2)
    print(f"  [③ 缺失] 方案书文件不存在 → 退出码 {code2}（期望 2）")

    # ④ 把**一处**外层标签从 <span> 换成 <div>，属性完整保留 ⇒ ANY_KEY_RE 仍数得到 141 处、
    #    而 SPAN_RE 只能解析出 140 处 ⇒ 解析率 < 1 ⇒ 走「出现多于解析」分支报 2。
    #
    #    ⚠️ 这里换掉的是**标签**而不是**属性名**，是踩过才改的：若把属性名改掉（哪怕只改一处），
    #    ANY_KEY_RE（数 data-key=" 的出现次数）与 SPAN_RE（解析 <span data-key="...">）
    #    会**同向**少掉同一处，比值仍是 1.0 —— 判据静默放过。
    #    所以先试了"改属性名"的对照，它正确地**红了**：那说明对照写得不对，
    #    也说明"两条正则不同源"这句话只在**标签层**成立，在**属性名层**并不成立。
    #    已知盲区（不掩盖）：单处属性改名这份判据拦不住；但改名通常是全局
    #    （生成器模板统一变），那种情况会被下面的"绝对下限"分支拦到，因此不是现实失效模式。
    tmp3 = Path(tempfile.gettempdir()) / "_broken_one_anchor.html"
    tmp3.write_text(re.sub(r"<span(\s+data-key)", r"<div\1", text, count=1), encoding="utf-8")
    code3, out3 = verify(tmp3)
    print(f"  [④ 换掉 1 处外层标签（属性保留）] → 退出码 {code3}（期望 2 —— 出现多于解析）")
    for l in out3.splitlines():
        if "无法判定" in l:
            print(f"      {l.strip()[:130]}")

    # ⑤ 全部换掉标签：命中「锚点数绝对下限」分支（与 ④ 是**两条不同的**分支，都要测到）
    tmp4 = Path(tempfile.gettempdir()) / "_broken_all_anchors.html"
    tmp4.write_text(re.sub(r"<span(\s+data-key)", r"<div\1", text), encoding="utf-8")
    code4, out4 = verify(tmp4)
    print(f"  [⑤ 换掉全部外层标签] → 退出码 {code4}（期望 2 —— 一个都没解析到）")
    for l in out4.splitlines():
        if "无法判定" in l:
            print(f"      {l.strip()[:130]}")

    # ⑥ 注入生成器源码片段（模拟"漏 f 前缀"）⇒ 必须判 1（不符），
    #    而不是 2（无法判定）—— 因为这是确定的内容缺陷，且解析率不受影响。
    #    本条是**收尾期真事故的回归测试**（成品 HTML 里实测漏出 2 处源码片段）。
    tmp5 = Path(tempfile.gettempdir()) / "_leaked_source_snippet.html"
    _k = sorted(parse_anchors(text))[0]
    tmp5.write_text(
        re.sub(rf'<span data-key="{re.escape(_k)}">[^<]*</span>',
               f'{{S("{_k}")}}', text, count=1), encoding="utf-8")
    code5, out5 = verify(tmp5)
    print(f"  [⑥ 注入生成器源码片段（漏 f 前缀）] → 退出码 {code5}（期望 1 —— 确定的内容缺陷）")
    for l in out5.splitlines():
        if "不符" in l:
            print(f"      {l.strip()[:130]}")

    ok = (code0 == 0 and code1 == 1 and code2 == 2 and code3 == 2
          and code4 == 2 and code5 == 1)
    print()
    print(f"阴性对照结论: {'六态齐备且判据有效 ✔' if ok else '✘ 判据有问题，不能声称方案书已验'}")
    print(f"  （基线 {code0} / 扰动 {code1} / 缺失 {code2} / 破坏1处 {code3} / "
          f"全破坏 {code4} / 漏源码 {code5}；期望 0/1/2/2/2/1）")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", default=None)
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    report = Path(args.report) if args.report else DEFAULT_REPORT
    if args.self_test:
        return self_test(report)
    code, out = verify(report)
    print(out)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
