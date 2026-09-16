#!/usr/bin/env python
# -*- coding: utf-8 -*-
# [自研工具] push_ontology.py
# 用途：把「本体」改动面（技能本体 + 任务档案）增量推送到公开仓 ai-workflow-skill，走 api.github.com Git Data API + base_tree 精确列出改动，并做 blob sha 自证、删除保护与并发基线条校验
# 适用场景：技能本体（SKILL.md/references/assets/scripts/Ledger.md）或任务档案（tasks/）有新增/更新/重命名/删除、需同步到 ai-workflow-skill 时用；推送自研工具到 ai-workflow-tools 不适用（用 publish_tools.py）；只读探查不用
# 作者：ai-workflow 自研（技能增强-ai-workflow-v3.2.0-2026-09-16，2026-09-16）
# 仓库：https://github.com/Garvin666/ai-workflow-tools/blob/main/scripts/push_ontology.py
"""push_ontology.py - 本体（Ontology）增量推送通道（ai-workflow v3.2.0）。

规则来源：用户 2026-09-16 指定「本体相关内容自动推送至 ai-workflow-skill，自研工具
相关内容自动推送至 ai-workflow-tools」。本脚本只负责**本体那一条通道**；分流判定由
`scripts/push_router.py` 负责，二者职责分离。

为什么不用 `git push`：远端 ai-workflow-skill 是**混合仓**（技能本体 ∪ 工作区档案），
且其历史由 API 创建、与本地 git 历史不互通 → 直接 push 会因历史无关被拒。故走
Git Data API：blob → tree(base_tree) → commit(parent=远端当前 HEAD) → PATCH ref。

三个安全设计（都是踩过坑的结论，不是装饰）：
  1. **默认 dry-run**：不加 `--apply` 绝不写远端。旧版 tmp/push_incremental.py 是
     「不加 --dry-run 就真推」，与同族的 publish_tools.py（默认 dry-run）口径相反，
     分流器无法安全包装两条命令 → v3.2.0 统一为默认 dry-run。
  2. **删除保护**：远端删除不可逆，默认只列出待删项不执行，须显式 `--allow-delete`。
  3. **基线条校验**：`--expect-remote <sha>` 要求远端 HEAD 等于该值，否则 FAIL ——
     防「基于过时基线推送」。并发保护另有天然一道：commit 的 parent 取远端当前 HEAD，
     且 PATCH ref 用 force=false，非快进会失败。

blob sha 自证：本地侧内容一律从 `git cat-file blob <rev>:<path>` 取（git 对象库天然 LF），
不用工作区文件字节（那是 CRLF）—— 直接用工作区字节会把 CRLF 推进仓库。新建 blob 后
再把 GitHub 返回的 sha 与本地对象 sha 比对，不符立即抛错（换行符口径问题的机器判据）。

用法：
    python push_ontology.py --base <rev> --head <rev>                # dry-run（默认）
    python push_ontology.py --base <rev> --head <rev> --apply        # 真推
    python push_ontology.py --base <rev> --head <rev> --apply --allow-delete

网络口径：一律走 api.github.com（本机 github.com 主域间歇不通），用 `gh api` 子进程。
凭据：沿用 `gh auth token`，本脚本不接触、不落盘、不打印令牌。
退出码: 0 = 成功（含 dry-run 与幂等跳过）；1 = 有 FAIL；2 = 用法/环境错误
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

DEFAULT_REPO = os.environ.get("ONTOLOGY_REPO", "Garvin666/ai-workflow-skill")
DEFAULT_BRANCH = os.environ.get("ONTOLOGY_BRANCH", "main")


def save_result(result_file: str | None, lines: list, root: Path) -> None:
    """把摘要写入 `--result-file`。两条铁律（都由实机事故推出来，别改回去）：

    ① 相对路径**以 `--root`（技能根）为基准**解析，不随 cwd 走。事故：分流器用
       `cwd=scripts/` 调本脚本，而 `--result-file tasks/…` 是相对路径 → 写到
       `scripts/tasks/…`（父目录不存在）→ 抛异常。
    ② 落盘失败**绝不能改变推送结论**。上述事故里 ref 已经更新成功，却因为写日志抛异常
       而返回退出码 1 → 分流器据此判定「本体通道失败」并中止工具通道。**日志是旁证，
       不是动作**：写不出来就 WARN，动作的结果照实返回。
    """
    if not result_file:
        return
    p0 = Path(result_file)
    p = (root / p0 if not p0.is_absolute() else p0).resolve()
    # 归一化后再判边界：`..` 会被折叠，越出技能根的路径一律拒绝 —— 否则一次误传
    # `--result-file ../tasks/x` 就会在技能根之外**新建目录并写文件**（实测踩到，已清理）。
    try:
        p.relative_to(root.resolve())
    except ValueError:
        print("[WARN] 结果路径越出技能根，拒绝写入（不影响推送结论）：%s" % p)
        return
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("\n".join(lines), encoding="utf-8")
        print("[落盘] %s" % p)
    except OSError as e:
        print("[WARN] 结果落盘失败（不影响推送结论）：%s —— %s" % (p, e))


def find_root(hint: str | None) -> Path:
    """定位本地 git 根。不要硬编码 parents[N] —— 脚本在 tmp/ 与 scripts/ 下层级不同，
    上一版就因硬编码层级而在移动位置后失效。"""
    if hint:
        return Path(hint).resolve()
    p = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                       capture_output=True, cwd=str(Path(__file__).resolve().parent))
    if p.returncode != 0:
        raise RuntimeError("无法探测 git 根，请用 --root 指定：" +
                           p.stderr.decode("utf-8", "replace")[:200])
    return Path(p.stdout.decode("utf-8", "replace").strip())


def git(root: Path, *args):
    p = subprocess.run(["git", "-c", "core.quotePath=false", *args],
                       capture_output=True, cwd=str(root))
    if p.returncode != 0:
        raise RuntimeError("git %s\n%s" % (" ".join(args), p.stderr.decode("utf-8", "replace")))
    return p.stdout


def gh(method: str, api: str, body=None):
    cmd = ["gh", "api", "-X", method, api]
    inp = None
    if body is not None:
        cmd += ["--input", "-"]
        inp = json.dumps(body).encode("utf-8")
    p = subprocess.run(cmd, capture_output=True, input=inp)
    if p.returncode != 0:
        raise RuntimeError("%s %s\n%s" % (method, api, p.stderr.decode("utf-8", "replace")[:400]))
    return p.stdout.decode("utf-8")


def parse_name_status(raw: bytes):
    """git diff --name-status -z → (writes, deletes)。

    ⚠️ 解析必须吃 -z（NUL 分隔）：中文路径在非 -z 输出里会被转义或加引号。
    """
    parts = [p for p in raw.decode("utf-8").split("\0") if p != ""]
    writes, deletes = [], []
    i = 0
    while i < len(parts):
        st = parts[i]
        if st[0] in ("R", "C"):
            deletes.append(parts[i + 1])
            writes.append(parts[i + 2])
            i += 3
        elif st[0] == "D":
            deletes.append(parts[i + 1])
            i += 2
        else:  # A / M / T
            writes.append(parts[i + 1])
            i += 2
    writes_set = set(writes)
    return writes, [d for d in deletes if d not in writes_set]


def main() -> int:
    ap = argparse.ArgumentParser(
        description="本体（技能本体 + 任务档案）增量推送通道 —— 默认 dry-run",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", required=True, help="本地起始 rev（不含该提交的改动）")
    ap.add_argument("--head", required=True, help="本地结束 rev（含该提交的改动）")
    ap.add_argument("--repo", default=DEFAULT_REPO,
                    help=f"目标仓 OWNER/NAME（默认 {DEFAULT_REPO}，可用 ONTOLOGY_REPO 覆盖）")
    ap.add_argument("--branch", default=DEFAULT_BRANCH, help=f"目标分支（默认 {DEFAULT_BRANCH}）")
    ap.add_argument("--root", help="本地 git 根（默认自动探测）")
    ap.add_argument("--apply", action="store_true", help="真正推送（默认只 dry-run）")
    ap.add_argument("--allow-delete", action="store_true",
                    help="允许删除远端文件（默认只列出待删项、不执行 —— 远端删除不可逆）")
    ap.add_argument("--expect-remote", help="要求远端 HEAD 等于该 sha，否则 FAIL（防基于过时基线推送）")
    ap.add_argument("--message", help="提交信息（默认取 --head 的提交信息）")
    ap.add_argument("--result-file", help="把摘要写入该文件（默认只打印）")
    a = ap.parse_args()

    root = find_root(a.root)
    lines: list[str] = []

    def out(s: str = "") -> None:
        print(s)
        lines.append(s)

    out("=== push_ontology（本体 → %s@%s）===" % (a.repo, a.branch))
    out("模式：%s" % ("APPLY（真实推送）" if a.apply else "DRY-RUN（不建 commit、不改 ref）"))
    out("本地 git 根：%s" % root)
    out("改动面：%s..%s" % (a.base, a.head))
    out("")

    # ---- 1. 本地改动面 ----
    writes, deletes = parse_name_status(git(root, "diff", "--name-status", "-z", a.base, a.head))
    out("本地：写入 %d 项 / 删除 %d 项" % (len(writes), len(deletes)))

    # ---- 2. 远端现状 ----
    ref = json.loads(gh("GET", "repos/%s/git/refs/heads/%s" % (a.repo, a.branch)))
    remote_head = ref["object"]["sha"]
    remote_commit = json.loads(gh("GET", "repos/%s/git/commits/%s" % (a.repo, remote_head)))
    base_tree = remote_commit["tree"]["sha"]
    out("远端 HEAD = %s" % remote_head)
    out("base_tree = %s" % base_tree)

    if a.expect_remote and a.expect_remote != remote_head:
        out("[FAIL] 基线条不符：期望 --expect-remote=%s，实际远端 HEAD=%s"
            % (a.expect_remote[:10], remote_head[:10]))
        out("       说明远端在两次推送之间被改动过（并发或他人推送）→ 请先人工核对再决定基线。")
        if a.result_file:
            save_result(a.result_file, lines, root)
        return 1

    tree = json.loads(gh("GET", "repos/%s/git/trees/%s?recursive=1" % (a.repo, base_tree)))
    if tree.get("truncated"):
        raise RuntimeError("base_tree 被截断，需分页处理（本脚本未实现）")
    remote = {e["path"]: e["sha"] for e in tree["tree"] if e["type"] == "blob"}
    remote_shas = set(remote.values())
    out("远端 blob 路径 %d 个" % len(remote))
    out("")

    # ---- 3. 组装 tree items ----
    items = []
    n_new = n_reuse = n_same = n_deleted = n_skipdel = 0
    for rel in writes:
        blob = git(root, "rev-parse", "%s:%s" % (a.head, rel)).decode().strip()
        if remote.get(rel) == blob:
            n_same += 1
            out("  [=] %s  同内容跳过" % rel)
            continue
        if blob in remote_shas:
            n_reuse += 1
            out("  [写] %s\n        复用远端已有 blob  %s" % (rel, blob[:10]))
        else:
            content = git(root, "cat-file", "blob", blob)
            new_blob = json.loads(
                gh("POST", "repos/%s/git/blobs" % a.repo,
                   {"content": content.decode("utf-8"), "encoding": "utf-8"})
            )["sha"]
            if new_blob != blob:
                raise RuntimeError(
                    "blob sha 不符：path=%s local=%s remote=%s（疑似换行符/编码口径不一致）"
                    % (rel, blob, new_blob))
            n_new += 1
            out("  [写] %s\n        新建 blob  %s" % (rel, blob[:10]))
        items.append({"path": rel, "mode": "100644", "type": "blob", "sha": blob})

    for rel in deletes:
        if rel not in remote:
            out("  [删] %s  远端本无，跳过" % rel)
            continue
        if a.allow_delete:
            n_deleted += 1
            out("  [删] %s  ← 真实删除" % rel)
            items.append({"path": rel, "mode": "100644", "type": "blob", "sha": None})
        else:
            n_skipdel += 1
            out("  [删-跳过] %s  远端存在，但未给 --allow-delete → 本次不删" % rel)

    out("")
    out("小计：新建 %d / 复用 %d / 同内容跳过 %d / 删除 %d / 删除跳过 %d"
        % (n_new, n_reuse, n_same, n_deleted, n_skipdel))

    if not a.apply:
        out("")
        out("DRY-RUN 结束：未创建 commit、未改 ref。加 --apply 才真推。")
        if a.result_file:
            save_result(a.result_file, lines, root)
        return 0

    # ---- 4. 建 tree / commit / 改 ref ----
    if not items:
        out("无改动可推，跳过。（远端保持 %s）" % remote_head[:10])
        if a.result_file:
            save_result(a.result_file, lines, root)
        return 0

    new_tree = json.loads(
        gh("POST", "repos/%s/git/trees" % a.repo, {"base_tree": base_tree, "tree": items}))["sha"]
    msg = a.message or git(root, "log", "-1", "--format=%B", a.head).decode("utf-8").strip()
    new_commit = json.loads(
        gh("POST", "repos/%s/git/commits" % a.repo,
           {"message": msg, "tree": new_tree, "parents": [remote_head]}))["sha"]
    gh("PATCH", "repos/%s/git/refs/heads/%s" % (a.repo, a.branch),
       {"sha": new_commit, "force": False})

    out("")
    out("新 tree   = %s" % new_tree)
    out("新 commit = %s" % new_commit)
    out("ref %s 已更新：%s → %s" % (a.branch, remote_head[:10], new_commit[:10]))
    if a.result_file:
        save_result(a.result_file, lines, root)
    return 0


if __name__ == "__main__":
    sys.exit(main())
