#!/usr/bin/env bash
# [自研工具] 名称：refs-check ｜ 用途：对已抓取的参考资产目录做完整性抽查（文件数/体积/README·LICENSE·package.json/.git 残留）并输出 Markdown 表 ｜ 适用场景：参考库入库后自检、批量快照的完整性核对 ｜ 仓库：https://github.com/Garvin666/ai-workflow-tools
#
# 用法：REF_BASE=<目录> REF_OUT=<输出 md> bash refs-check.sh
#   默认 REF_BASE = 当前目录下的 refs/，REF_OUT = <REF_BASE>/抽查结果.md
BASE="${REF_BASE:-$(pwd)/refs}"
OUT="${REF_OUT:-$BASE/抽查结果.md}"

printf "# 参考组件完整性抽查\n\n" > "$OUT"
printf "> 抽查时间：%s\n\n" "$(date '+%Y-%m-%d %H:%M')" >> "$OUT"
printf "| 目录 | 文件数 | 体积 | README | LICENSE | package.json | .git 残留 | 判定 |\n" >> "$OUT"
printf "|---|---|---|---|---|---|---|---|\n" >> "$OUT"

pass=0; fail=0
for d in react-three-fiber drei model-viewer react-force-graph three-globe motion tanstack-table magicui radix-primitives dnd-kit sonner vaul; do
  p="$BASE/$d"
  if [ ! -d "$p" ]; then
    printf "| \`%s\` | — | — | — | — | — | — | **缺失** |\n" "$d" >> "$OUT"
    fail=$((fail+1)); continue
  fi
  files=$(find "$p" -type f 2>/dev/null | wc -l | tr -d ' ')
  size=$(du -sh "$p" 2>/dev/null | cut -f1)
  readme=$( [ -f "$p/README.md" ] && echo ✔ || echo ✘ )
  lic=$( ls "$p" 2>/dev/null | grep -icE "^licen[cs]e" | awk '{print ($1>0)?"✔":"✘"}' )
  pkg=$( [ -f "$p/package.json" ] && echo ✔ || echo ✘ )
  gitres=$( [ -d "$p/.git" ] && echo "有残留" || echo "已清理" )
  if [ "$files" -gt 0 ] && [ "$readme" = "✔" ] && [ "$gitres" = "已清理" ]; then
    verdict="✔ 通过"; pass=$((pass+1))
  else
    verdict="**异常**"; fail=$((fail+1))
  fi
  printf "| \`%s\` | %s | %s | %s | %s | %s | %s | %s |\n" "$d" "$files" "$size" "$readme" "$lic" "$pkg" "$gitres" "$verdict" >> "$OUT"
done
printf "\n**合计：通过 %d 项，异常 %d 项**\n" "$pass" "$fail" >> "$OUT"
cat "$OUT"
