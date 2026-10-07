# -*- coding: utf-8 -*-
"""[自研工具] knowledge-cram-report / cram_figs.py

- 名称：knowledge-cram-report / 配图生成
- 用途：生成恶补报告的两张标准配图 —— ① 遗忘曲线与间隔增长 ② 机制流程
- 适用场景：报告需要配图时；间隔序列**取自 cram_index.schedule_table**，与判据 V9 同源
- 仓库链接：https://github.com/Garvin666/ai-workflow-tools

⚠️ 为什么配图不能手画数字：图上的「该复习了（N 天）」与柱子高度都是间隔数字。
   一旦它们与 `interval_days()` 分家，就会出现「正文说 4 天、图上画 7 天」这种不上判据的漂移。
   所以这里**直接 import** 调度函数，不重算。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cram_index import (FACTOR, TARGET_RECALL, interval_days,  # noqa: E402
                        retention_at, schedule_table)

plt.rcParams["font.sans-serif"] = ["Microsoft YaHei"]
plt.rcParams["axes.unicode_minus"] = False

INK = "#1f2d3d"
ACC = "#c0392b"
ACC2 = "#2c7a7b"
GRID = "#dfe6ec"


def fig_forgetting(out: Path) -> Path:
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.0), dpi=200)

    ax = axes[0]
    t = np.linspace(0, 40, 500)
    for S, c, lbl, tx in [(1.0, ACC, "稳定度 S = 1.0（刚学完）", (2.4, 0.655)),
                          (3.61, ACC2, "稳定度 S = 3.61（复习 3 次后）", (13.0, 0.815))]:
        ax.plot(t, [retention_at(x, S) for x in t], color=c, lw=2.2, label=lbl)
        t90 = interval_days(S)          # ⭐ 与判据同源，不在这里重算公式
        if t90 <= 40:
            ax.plot([t90, t90], [0, TARGET_RECALL], ls=":", color=c, lw=1.2)
            ax.plot([0, t90], [TARGET_RECALL, TARGET_RECALL], ls=":", color=c, lw=1.2)
            ax.scatter([t90], [TARGET_RECALL], s=34, color=c, zorder=5)
            ax.annotate(f"该复习了（{t90} 天）", xy=(t90, TARGET_RECALL), xytext=tx,
                        textcoords="data", fontsize=9, color=c, ha="left", va="center",
                        arrowprops=dict(arrowstyle="-", color=c, lw=0.8, shrinkA=2, shrinkB=3))
    ax.axhline(TARGET_RECALL, color="#9aa5b1", lw=1, ls="--")
    ax.text(39, TARGET_RECALL + 0.015, f"目标留存 {TARGET_RECALL}", ha="right",
            fontsize=9, color="#5c6b7a")
    ax.set_xlabel("距上次复习的天数")
    ax.set_ylabel("还能记住的概率 R(t)")
    ax.set_title(f"(a) 遗忘曲线：R(t) = (1 + t/({FACTOR:g}·S))^-1，S 越大衰减越慢",
                 fontsize=11, color=INK)
    ax.set_ylim(0.55, 1.02)
    ax.set_xlim(0, 40)
    ax.grid(alpha=0.35, color=GRID)
    ax.legend(fontsize=9, frameon=False, loc="lower right")

    ax = axes[1]
    tab = schedule_table(7)
    n = [r["n"] for r in tab]
    gap = [r["间隔"] for r in tab]
    ax.bar(n, gap, color=ACC, alpha=0.85, width=0.6)
    for x, g in zip(n, gap):
        ax.text(x, g + max(gap) * 0.03, f"{g:.0f}", ha="center", fontsize=9, color=INK)
    ax.set_xlabel("第几次复习")
    ax.set_ylabel("建议间隔（天）")
    ax.set_title("(b) 间隔自己长大：S ← S × 1.9，间隔 = round(9·S·(1/0.9−1))",
                 fontsize=11, color=INK)
    ax.grid(axis="y", alpha=0.35, color=GRID)
    ax.set_xticks(n)
    ax.set_ylim(0, max(gap) * 1.22)

    fig.tight_layout()
    fig.savefig(out, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return out


def fig_flow(out: Path, judge_count: int) -> Path:
    fig, ax = plt.subplots(figsize=(11, 3.5), dpi=200)
    ax.set_xlim(0, 100)
    ax.set_ylim(0, 34)
    ax.axis("off")
    steps = [
        ("① 取料", "plan.yaml\ntrace.jsonl\nLedger", "#eef4fa"),
        ("② 抽取", "可迁移 + 可解释\n两条判据", "#fdeeec"),
        ("③ 去重", "比对 00-索引.md\n全新/增量/复习", "#eef4fa"),
        ("④ 通俗化", "五段式\n定义→为何→算例\n→误解→连接", "#fdf6e3"),
        ("⑤ 渲染", "pandoc +\nxelatex\n(本地)", "#eaf3ee"),
        ("⑥ 归档", "恶补\\项目\\年月\\\n+ 索引 + 复习日", "#eef4fa"),
        ("⑦ 校验", f"V1–V{judge_count}\n{judge_count} 条判据", "#f3eefa"),
    ]
    x0, y0, w, h, gapw = 1.5, 12, 12.4, 12, 1.5
    for i, (name, sub, color) in enumerate(steps):
        x = x0 + i * (w + gapw)
        ax.add_patch(plt.Rectangle((x, y0), w, h, facecolor=color,
                                   edgecolor="#b9c6d2", lw=1.1, zorder=2))
        ax.text(x + w / 2, y0 + h - 3.2, name, ha="center", va="center",
                fontsize=11, color=INK, weight="bold", zorder=3)
        ax.text(x + w / 2, y0 + h / 2 - 2.6, sub, ha="center", va="center",
                fontsize=7.6, color="#44515e", zorder=3, linespacing=1.5)
        if i < len(steps) - 1:
            ax.annotate("", xy=(x + w + gapw - 0.25, y0 + h / 2),
                        xytext=(x + w + 0.25, y0 + h / 2),
                        arrowprops=dict(arrowstyle="-|>", color="#7f8c9b", lw=1.3), zorder=1)
    ax.text(1.5, 29.5, "知识点恶补 PDF 报告机制 · 七步流程", fontsize=12.5,
            color=INK, weight="bold")
    ax.text(1.5, 26.2, "全程本地：不上传任何内容到在线转换服务　｜　归档按项目名分层",
            fontsize=9, color="#5c6b7a")
    ax.text(1.5, 4.5, "触发：任务收尾 / 手动指令　｜　不触发：L0 轻操作、纯对话",
            fontsize=8.6, color="#5c6b7a")
    fig.tight_layout()
    fig.savefig(out, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="生成恶补报告标准配图（间隔数据取自 cram_index）")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--prefix", required=True, help="文件名前缀，如 2026-10-07_某任务")
    ap.add_argument("--judge-count", type=int, default=10, help="流程图上写的判据条数")
    ap.add_argument("--only", choices=["forgetting", "flow"], default=None)
    args = ap.parse_args(argv)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    made = []
    if args.only in (None, "forgetting"):
        made.append(fig_forgetting(out_dir / f"{args.prefix}-fig1-遗忘曲线与间隔增长.png"))
    if args.only in (None, "flow"):
        made.append(fig_flow(out_dir / f"{args.prefix}-fig2-机制七步流程.png",
                             args.judge_count))
    for p in made:
        print(f"[ OK ] {p}  {p.stat().st_size} 字节")
    return 0


if __name__ == "__main__":
    sys.exit(main())
