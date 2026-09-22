#!/usr/bin/env bash
# [自研工具] 名称：refs-fetch ｜ 用途：批量抓取 GitHub 开源仓库为本地只读参考资产（浅克隆后删 .git，只留源码快照）｜ 适用场景：建组件/设计参考库、批量下载开源作品作参考、素材快照入库 ｜ 仓库：https://github.com/Garvin666/ai-workflow-tools
#
# 用法：REF_BASE=<目标目录> REF_TMP=<临时目录> bash refs-fetch.sh
#   默认 REF_BASE = 当前目录下的 refs/，REF_TMP = 当前目录下的 .refs-tmp/
#   幂等：已存在且非空的目录会被跳过
#
# 关键点：本机代理（如 127.0.0.1:54711）对 github.com 返回 CONNECT tunnel failed 502，
#   故先 unset 代理再 clone；仍失败则回退 api.github.com tarball（不带 ref = 默认分支）。
set -u
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY all_proxy ALL_PROXY
BASE="${REF_BASE:-$(pwd)/refs}"
TMP="${REF_TMP:-$(pwd)/.refs-tmp}"
LOG="$TMP/fetch.log"
mkdir -p "$BASE" "$TMP"

log() { echo "$(date +%H:%M:%S) $1" | tee -a "$LOG"; }

fetch_one() {
  local repo="$1" name="$2"
  local target="$BASE/$name"
  if [ -d "$target" ] && [ -n "$(ls -A "$target" 2>/dev/null)" ]; then
    log "[SKIP] $name 已存在且非空"; return 0
  fi
  rm -rf "$target"

  # 通道 A：git clone（绕过代理）
  if git clone --depth 1 "https://github.com/$repo.git" "$target" >>"$LOG" 2>&1; then
    rm -rf "$target/.git"
    log "[OK-git] $name"; return 0
  fi
  rm -rf "$target"

  # 通道 B：api.github.com tarball
  log "[FALLBACK] $name → api tarball"
  local tarball="$TMP/$name.tar.gz"
  local token
  token=$(gh auth token 2>/dev/null)
  if curl -sL --max-time 300 \
       -H "Authorization: Bearer $token" \
       -H "Accept: application/vnd.github+json" \
       -o "$tarball" "https://api.github.com/repos/$repo/tarball"; then
    mkdir -p "$target"
    if tar -xzf "$tarball" -C "$target" --strip-components=1 2>>"$LOG"; then
      rm -f "$tarball"
      log "[OK-tar] $name"; return 0
    fi
  fi
  rm -rf "$target"; rm -f "$tarball"
  log "[FAIL] $name"; return 1
}

log "===== v2 开始：3D 方向 ====="
fetch_one pmndrs/react-three-fiber react-three-fiber &
fetch_one google/model-viewer model-viewer &
fetch_one vasturiano/react-force-graph react-force-graph &
wait
fetch_one vasturiano/three-globe three-globe &
wait

log "===== v2：通用 UI 方向 ====="
fetch_one motiondivision/motion motion &
fetch_one TanStack/table tanstack-table &
fetch_one magicuidesign/magicui magicui &
wait
fetch_one radix-ui/primitives radix-primitives &
fetch_one clauderic/dnd-kit dnd-kit &
fetch_one emilkowalski/sonner sonner &
wait
fetch_one emilkowalski/vaul vaul &
wait

log "===== v2 完成 ====="
