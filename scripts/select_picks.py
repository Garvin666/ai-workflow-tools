# -*- coding: utf-8 -*-
# [自研工具] select_picks
# 用途：从 GitHub 候选池中按「人工分组清单」生成结构化的入选名单 JSON（带 id、指标与剔除项）
# 适用场景：GitHub 开源项目收录类任务的第二步（候选池 → 入选名单）
#          不适用：自动挑选项目（选什么由人定；本脚本只做映射与校验）
# 作者：ai-workflow 自研（开源项目收录 2026-10-11）
# 仓库：https://github.com/Garvin666/ai-workflow-tools/blob/main/scripts/select_picks.py
"""候选池 JSON → 入选名单 JSON。

分组清单由人工给定（下方 GROUPS 是本次清单的定义，作为样例内置；可用 --groups 传 JSON 覆盖）。
脚本只做三件事：按分组顺序编 id、从候选池取实测指标、把不在池中的名字列进 missing（不静默丢）。
`REJECTED` 沿用同一约定：把「命中阈值但被剔除」的项连同理由一并落到产物里，避免下次重复劳动。
"""
import argparse
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

# 分组定义（本次清单样例）：前缀 → (分组名, [仓库全名...])
GROUPS = {
    "Q": ("AI × 量化投资", [
        "microsoft/qlib", "TauricResearch/TradingAgents", "AI4Finance-Foundation/FinRL",
        "openbq-org/OpenBB", "stefan-jansen/machine-learning-for-trading",
        "HKUDS/Vibe-Trading", "ZhuLinsen/daily_stock_analysis", "hummingbot/hummingbot",
        "polakowo/vectorbt", "ranaroussi/quantstats", "PyPortfolio/PyPortfolioOpt",
    ]),
    "A": ("AI 基础与工程", [
        "langchain-ai/langchain", "huggingface/transformers", "ollama/ollama",
        "langgenius/dify", "vllm-project/vllm", "infiniflow/ragflow",
        "browser-use/browser-use", "unclecode/crawl4ai", "hiyouga/LlamaFactory",
        "mem0ai/mem0",
    ]),
    "D": ("数据科学主干", [
        "scikit-learn/scikit-learn", "pandas-dev/pandas", "keras-team/keras",
        "apache/superset", "streamlit/streamlit", "ray-project/ray",
        "apache/airflow", "explosion/spaCy",
    ]),
    "X": ("三者交叉 · AI 数据工具链 / MLOps / 时序", [
        "mlflow/mlflow", "qdrant/qdrant", "HumanSignal/label-studio",
        "fivetran/great_expectations", "autogluon/autogluon", "sktime/sktime",
        "unit8co/darts", "amazon-science/chronos-forecasting",
    ]),
    "R": ("资源索引与学习路径", [
        "wilsonfreitas/awesome-quant", "georgezouq/awesome-ai-in-finance",
        "lukasmasuch/best-of-ml-python",
    ]),
}

# 已剔除 / 不推荐（归档或停更），证据来自 http_fetch.py --github-repo 单仓复核
REJECTED = [
    {"name": "daytonaio/daytona", "stars": 71569, "pushed_at": "2026-07-24", "archived": True,
     "reason": "仓库已归档（archived=true，单仓复核确认），不再接收更新"},
    {"name": "microsoft/nni", "stars": 14367, "pushed_at": "2024-07-03", "archived": True,
     "reason": "仓库已归档（archived=true，单仓复核确认），AutoML 工具链停止维护"},
    {"name": "UFund-Me/Qbot", "stars": 18586, "pushed_at": "2026-03-11", "archived": False,
     "reason": "停更 7 个月，超出「近 6 个月有 push」硬线"},
    {"name": "firmai/financial-machine-learning", "stars": 8809, "pushed_at": "2025-01-03", "archived": False,
     "reason": "停更 21 个月，清单内部分链接已失效"},
    {"name": "quantopian/zipline", "stars": 20148, "pushed_at": "2024-02-13", "archived": False,
     "reason": "停更 32 个月；Quantopian 平台已关停，仅存历史参考价值"},
    {"name": "hudson-and-thames/mlfinlab", "stars": 4939, "pushed_at": "2023-10-02", "archived": False,
     "reason": "停更 36 个月，商业版已转向闭源"},
    {"name": "sinaptik-ai/pandas-ai", "stars": 23867, "pushed_at": "2025-10-28", "archived": False,
     "reason": "停更约 12 个月，超出硬线；同类可替代方案充足"},
    {"name": "myhhub/stock", "stars": 14764, "pushed_at": "2026-04-02", "archived": False,
     "reason": "停更 6 个月，恰在硬线边缘；A 股数据方向已有 simonlin1212/a-stock-data 等活跃替代"},
]

FETCH_NOTE = ("GitHub Search API（http_fetch.py --github-search）多查询合并去重；"
              "归档项经 http_fetch.py --github-repo 单仓复核")


def main() -> None:
    ap = argparse.ArgumentParser(description="候选池 JSON → 入选名单 JSON（按人工分组映射 + 缺失校验）")
    ap.add_argument("--candidates", required=True, help="parse_search.py 产出的候选池 JSON")
    ap.add_argument("--out", required=True, help="入选名单 JSON 输出路径")
    ap.add_argument("--groups", default="", help="分组定义 JSON 文件（形如 {\"Q\": [\"分组名\", [\"owner/repo\", ...]]}）；缺省用内置样例")
    ap.add_argument("--date", default="2026-10-11", help="取数日期，写入产物便于日后复核时效")
    args = ap.parse_args()

    groups = GROUPS
    if args.groups:
        raw = json.loads(Path(args.groups).read_text(encoding="utf-8"))
        groups = {k: (v[0], v[1]) for k, v in raw.items()}

    pool = {r["name"]: r for r in
            json.loads(Path(args.candidates).read_text(encoding="utf-8"))["candidates"]}
    out, missing = [], []
    for prefix, (label, names) in groups.items():
        for i, n in enumerate(names, 1):
            rec = pool.get(n)
            if not rec:
                missing.append(n)
                continue
            out.append({
                "id": f"{prefix}{i:02d}", "group": label,
                "name": n, "stars": rec["stars"], "forks": rec["forks"],
                "license": rec["license"] or "未标注", "language": rec["language"] or "—",
                "pushed_at": rec["pushed_at"][:10], "archived": rec["archived"],
                "url": rec["url"], "description": rec["description"],
            })
    payload = {
        "取数时间": args.date,
        "取数方式": FETCH_NOTE,
        "入选条数": len(out),
        "分组统计": {k: len(v[1]) for k, v in groups.items()},
        "picks": out,
        "rejected": REJECTED,
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"入选 {len(out)} 条，缺失 {missing or '无'}")
    print("分组统计：", payload["分组统计"])
    for r in out:
        print(f"{r['id']} | {r['stars']:>7} | {r['name']}")


if __name__ == "__main__":
    main()
