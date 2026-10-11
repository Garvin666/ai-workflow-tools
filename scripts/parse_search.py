# -*- coding: utf-8 -*-
# [自研工具] parse_search
# 用途：解析并合并多次 GitHub 搜索的 TSV 输出，去重后按「归档 / 停更」两条硬线筛出候选池
# 适用场景：GitHub 开源项目收录、选型、调研类任务的第一步（原始检索结果 → 候选池）
#          不适用：只查单个仓库（直接用 http_fetch.py --github-repo）；对已定稿清单做增量复核
# 作者：ai-workflow 自研（开源项目收录 2026-10-11）
# 仓库：https://github.com/Garvin666/ai-workflow-tools/blob/main/scripts/parse_search.py
"""GitHub 搜索 TSV → 候选池 JSON。

输入 TSV 由 ai-workflow 的 `http_fetch.py --github-search` 产出；多个查询的结果以
`### <查询式>` 分隔行拼接成一份文件即可。

合并规则（踩过坑，勿改）：同一仓库被多个查询命中时字段可能打架 —— 实测出现过同一
仓库在一个查询里标「已归档」、在另一个查询里标「未归档」。因此 **archived 取逻辑或、
pushed_at 取最新**，而不是「首次出现者优先」（后者会让错的那一份胜出）。
"""
import argparse
import json
from pathlib import Path

DEFAULT_STALE_BEFORE = "2026-04-11"   # 近 6 个月硬线，可用 --stale-before 覆盖


def parse(src: Path, stale_before: str) -> dict:
    seen: dict[str, dict] = {}
    cur_query = ""
    for line in src.read_text(encoding="utf-8").splitlines():
        if line.startswith("### "):
            cur_query = line[4:].strip()
            continue
        if not line.strip() or line.startswith("[INFO]"):
            continue
        if line.startswith("full_name\t"):
            continue
        parts = line.split("\t")
        if len(parts) < 9:
            continue
        name, stars, forks, lic, lang, pushed, archived, url, desc = parts[:9]
        try:
            stars_i = int(stars)
        except ValueError:
            continue
        is_archived = archived == "是"
        rec = {
            "name": name, "stars": stars_i, "forks": int(forks or 0),
            "license": lic if lic != "—" else "", "language": lang if lang != "—" else "",
            "pushed_at": pushed, "archived": is_archived,
            "url": url, "description": desc.strip(), "queries": [cur_query],
        }
        if name in seen:
            e = seen[name]
            e["queries"].append(cur_query)
            e["stars"] = max(e["stars"], stars_i)                 # 多查询取最大
            e["archived"] = e["archived"] or is_archived           # 任一查询判归档即归档
            if pushed > e["pushed_at"]:                            # 取最新 push
                e["pushed_at"] = pushed
                e["license"] = rec["license"]
                e["language"] = rec["language"]
            continue
        seen[name] = rec

    allrecs = list(seen.values())
    kept = [r for r in allrecs if not r["archived"] and r["pushed_at"][:10] >= stale_before]
    dropped_archived = [r for r in allrecs if r["archived"]]
    dropped_stale = [r for r in allrecs if not r["archived"] and r["pushed_at"][:10] < stale_before]
    kept.sort(key=lambda r: -r["stars"])
    return {
        "原始去重条数": len(allrecs),
        "通过硬线筛选": len(kept),
        "剔除_已归档": [{"name": r["name"], "stars": r["stars"], "pushed_at": r["pushed_at"]} for r in dropped_archived],
        "剔除_停更": [{"name": r["name"], "stars": r["stars"], "pushed_at": r["pushed_at"]} for r in dropped_stale],
        "硬线_停更阈值": stale_before,
        "candidates": kept,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="GitHub 搜索 TSV → 候选池 JSON（去重 + 双硬线筛选）")
    ap.add_argument("--src", required=True, help="http_fetch.py --github-search 的输出文件（多个查询拼接）")
    ap.add_argument("--out", required=True, help="候选池 JSON 输出路径")
    ap.add_argument("--date", default="", help="取数日期（写入 JSON 的「取数时间」，便于日后复核时效）")
    ap.add_argument("--stale-before", default=DEFAULT_STALE_BEFORE,
                    help=f"停更硬线 YYYY-MM-DD（默认 {DEFAULT_STALE_BEFORE}）")
    ap.add_argument("--quiet", action="store_true", help="不打印候选明细")
    args = ap.parse_args()

    payload = parse(Path(args.src), args.stale_before)
    payload["取数时间"] = args.date
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"去重后 {payload['原始去重条数']} 条 → 硬线筛后 {payload['通过硬线筛选']} 条"
          f"（归档剔除 {len(payload['剔除_已归档'])}，停更剔除 {len(payload['剔除_停更'])}）")
    if args.quiet:
        return
    print("-" * 100)
    for r in payload["candidates"]:
        print(f"{r['stars']:>7} | {r['name']:<62} | {r['pushed_at'][:10]} | "
              f"{r['license'][:12]:<12} | {r['description'][:60]}")
    print("-" * 100)
    print("剔除_已归档：", ", ".join(f"{r['name']}({r['stars']})" for r in payload["剔除_已归档"]))
    print("剔除_停更：")
    for r in payload["剔除_停更"]:
        print(f"   {r['name']}({r['stars']}, 最后push {r['pushed_at'][:10]})")


if __name__ == "__main__":
    main()
