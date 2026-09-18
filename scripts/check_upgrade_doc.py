# -*- coding: utf-8 -*-
# [自研工具] check_upgrade_doc.py
# 用途：《代码升级说明》的机器判据：检查同一份文件级 diff 是否仍被重复渲染，并把上报的校验器跑一遍阴性对照
# 适用场景：与 gen_upgrade_doc.py 成对使用（生成器 + 该缺陷的回归判据）；单独使用价值有限
# 作者：数模工作区自研（系统优化方案-2026-09-18，2026-09-18）
# 仓库：https://github.com/Garvin666/ai-workflow-tools/blob/main/scripts/check_upgrade_doc.py
"""① 跑 verify_report.py --self-test（阴性对照三态）；
   ② 检查《代码升级说明》的结构：同一份文件级 diff 是否还被重复渲染（P0 判据）。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path


def find_ws(start: Path) -> Path:
    p = start.resolve()
    for cand in [p, *p.parents]:
        if (cand / "model_base").is_dir() and (cand / "tools").is_dir():
            return cand
    raise SystemExit("找不到工作区根")


WS = find_ws(Path(__file__).parent)
TMP17 = WS / "tasks" / "底座泛化优化-2026-09-17" / "tmp"
DOC = WS / "代码升级说明-底座泛化优化.html"
sys.dont_write_bytecode = True

print("=" * 92)
print("① 阴性对照（脚本一字不改，只改报告一位数字 → 必须变红）")
print("=" * 92)
env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
r = subprocess.run([sys.executable, str(TMP17 / "verify_report.py"), "--self-test"],
                   cwd=str(TMP17), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
print(r.stdout.decode("utf-8", errors="replace"))
print(f"self-test 退出码 = {r.returncode}")

print()
print("=" * 92)
print("② P0 判据：同一份文件级 diff 是否仍被重复渲染")
print("=" * 92)
t = DOC.read_text(encoding="utf-8")

# 文件级 diff 块的指纹：pipeline.diff 的第一行 @@ 之后的内容足够独特
blocks = re.findall(r'<pre class="diff">(.*?)</pre>', t, re.S)
print(f"文档里 <pre class=\"diff\"> 块共 {len(blocks)} 个")
seen: dict[int, list[int]] = {}
for i, b in enumerate(blocks, 1):
    key = len(b.splitlines())
    seen.setdefault(key, []).append(i)
dup = {k: v for k, v in seen.items() if len(v) > 1}
for k, v in sorted(seen.items()):
    print(f"   块#{v} 各 {k} 行")

# 更强判据：逐字节比较所有块，找出重复内容
norm = [b.strip() for b in blocks]
from collections import Counter
c = Counter(norm)
repeats = {k[:60]: n for k, n in c.items() if n > 1}
print()
if repeats:
    print(f"✘ 仍有 {len(repeats)} 组**内容重复**的 diff 块：")
    for k, n in repeats.items():
        print(f"   ×{n}  {k}…")
else:
    print("✔ 无内容重复的 diff 块 —— 每份 diff 只渲染一次")

# 文件级 diff 的名称在文中出现几次（每份应恰好 1 次作小标题）
# ⚠️ 小标题里套着 <code>，别用 `<h4>[^<]*name` 去匹配 —— 会一路假红（实测踩过）。
#    直接数"文件级完整改动"这一句里出现的文件名。
for name in ["classification.diff", "validator.diff", "plots.diff", "pipeline.diff"]:
    n_h4 = t.count(f"文件级完整改动（<code>{name}</code>")
    print(f"   {name:22s} 作为文件级 diff 小节出现 {n_h4} 次（期望 1）")

print()
print("=" * 92)
print("③ 结构")
print("=" * 92)
for tag in ("h2", "h3", "h4"):
    for m in re.findall(rf"<{tag}[^>]*>(.*?)</{tag}>", t, re.S):
        s = re.sub(r"<[^>]+>", "", m).strip()
        indent = {"h2": "  ", "h3": "      ", "h4": "          "}[tag]
        print(f"{indent}{s}")

print()
print("=" * 92)
print("④ 哈希链表与行尾断言是否真的渲染出来了")
print("=" * 92)
for k in ["prop.fp_original", "prop.fp_current", "prop.landed", "prop.eol_after",
          "prop.eol_all_lf", "prop.sha_semantics_n"]:
    m = re.search(r'data-key="' + re.escape(k) + r'"[^>]*>(.*?)</span>', t, re.S)
    print(f"   {k:24s} = {m.group(1) if m else '（未渲染）'}")
for k in ["prop.file.pipeline.before", "prop.file.pipeline.after",
          "prop.patch.P4a.cum", "prop.patch.P4b.cum", "prop.patch.P4c.cum"]:
    m = re.search(r'data-key="' + re.escape(k) + r'"[^>]*>(.*?)</span>', t, re.S)
    print(f"   {k:32s} = {m.group(1) if m else '（未渲染）'}")
