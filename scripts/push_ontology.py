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
  1. **默认 dry-run**：不加 `--apply` 绝不写远端 —— **包括不创建 blob**（v3.4.0 修正：
     此前 blob 的 POST 落在 `if not a.apply` 判断**之前**，于是 dry-run 也会往远端 POST
     一堆 blob。它们只是悬挂对象、不动 ref，破坏性为零，但"绝不写远端"这句话就成了假的，
     且 32 项改动要 POST 29 次，实测一次 dry-run 被拖到 **2 分 37 秒**）。旧版
     tmp/push_incremental.py 是「不加 --dry-run 就真推」，与同族的 publish_tools.py
     （默认 dry-run）口径相反，分流器无法安全包装两条命令 → v3.2.0 统一为默认 dry-run。
     ⚠️ 因此自证的「blob sha 比对」只在 `--apply` 时执行；dry-run 只报出**本地 sha**
     （它由 `git rev-parse <rev>:<path>` 算出，本来就是权威值）。
  2. **删除保护**：远端删除不可逆，默认只列出待删项不执行，须显式 `--allow-delete`。
  3. **基线条校验**：`--expect-remote <sha>` 要求远端 HEAD 等于该值，**否则 FAIL** ——
     防「基于过时基线推送」。并发保护另有天然一道：commit 的 parent 取远端当前 HEAD，
     且 PATCH ref 用 force=false，非快进会失败。
     ⚠️ **v4.14.1 修两处同源缺陷**（此前这条防线会**误报**，并把唯一证据截掉）：
     ① 原为**精确字符串比对** ⇒ 传短 sha（本仓文档与示例的惯用写法）**永远 FAIL**；
     现口径 = 等值 或 **≥7 位前缀匹配**（4–6 位太短易撞，仍拒）。
     ② 失败时把两侧截成 `[:10]` 打印 ⇒ 差异落在第 10 位之后时，报错信息**看起来两侧一模一样**，
     把唯一的差异证据截掉了；现打印**完整** sha ＋ 首个不同字符位置。

blob sha 自证：本地侧内容一律从 `git cat-file blob <rev>:<path>` 取（git 对象库天然 LF），
不用工作区文件字节（那是 CRLF）—— 直接用工作区字节会把 CRLF 推进仓库。新建 blob 后
再把 GitHub 返回的 sha 与本地对象 sha 比对，不符立即抛错（换行符口径问题的机器判据）。

用法：
    python push_ontology.py --base <rev> --head <rev>                # dry-run（默认）
    python push_ontology.py --base <rev> --head <rev> --apply        # 真推
    python push_ontology.py --base <rev> --head <rev> --apply --allow-delete

网络口径：一律走 api.github.com（本机 github.com 主域间歇不通），用 `gh api` 子进程。
凭据：沿用 `gh auth token`，本脚本不接触、不落盘、不打印令牌。
退出码: 0 = 成功（含 dry-run 与幂等跳过）；1 = 有 FAIL；2 = 用法/前置错误；
       3 = **无法判定**（外部不可用 / 能力缺失 —— 远端状态未知，不得记成「判不过」）
"""
import argparse
import base64
import json
import os
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

DEFAULT_REPO = os.environ.get("ONTOLOGY_REPO", "Garvin666/ai-workflow-skill")
DEFAULT_BRANCH = os.environ.get("ONTOLOGY_BRANCH", "main")


class Undecided(RuntimeError):
    """外部不可用 / 能力缺失 ⇒ **结论无法判定**（接口契约退出码 3）。

    v4.44.0 立：此前网络抖一下（`TLS handshake timeout`）就抛 traceback、rc=1，
    与「1 = 得出的结论是不通过」相撞 —— 推送根本没推成，却被记成「判不过」。
    契约按**结论是否可得**分档，故这里必须与「判不过」区分开。
    """



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
        # 网络/鉴权/API 异常一律归「无法判定」：远端状态未知，不能说成「判不过」。
        raise Undecided("%s %s\n%s" % (method, api, p.stderr.decode("utf-8", "replace")[:400]))
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


# --------------------------------------------------------------------------- #
# 自检（--selftest）：只跑**不产生外发动作**的判据
# --------------------------------------------------------------------------- #
def _st_line(name: str, actual, expect) -> bool:
    hit = actual == expect
    print("[%s] %s：实际=%s 期望=%s" % ("PASS" if hit else "MISS", name, actual, expect))
    return hit


def selftest() -> int:
    """回归四条核心判据（全程离线，不建 blob / 不建 commit / 不改 ref）：

      ① name-status 解析：`-z` 分隔、重命名拆成删旧+写新、删除项去重
      ② dry-run 零写：无 `--apply` 时对远端只有 3 次 GET、POST 次数必须为 0
         （这是 v3.4.0「blob 在 dry-run 也被 POST」缺陷的机器回归判据）
      ③ 基线条短 sha 口径：≥7 位前缀通过、不符即 FAIL
      ④ fail-closed：`--apply` + 基线不符 → rc=1 且**零 POST**（不能半推）

    ⚠️ 远端一律由假 `gh` 顶替，且它只认上方列出的 3 个只读端点 —— 一旦代码路径走到
       任何写端点，假 `gh` 直接判 MISS。本函数**不会**执行 git push，也不访问任何 remote。
    """
    import contextlib
    import io
    import tempfile

    ok_n = total = 0

    def chk(name: str, actual, expect) -> None:
        nonlocal ok_n, total
        total += 1
        if _st_line(name, actual, expect):
            ok_n += 1

    # ---- ① name-status 解析 ----
    w, d = parse_name_status(b"A\0a.py\0M\0b.py\0R100\0old.py\0new.py\0D\0c.py\0")
    chk("name-status 解析：写入/删除分类（含重命名拆解）",
        (w, d), (["a.py", "b.py", "new.py"], ["old.py", "c.py"]))
    w2, d2 = parse_name_status(b"M\0d.py\0D\0d.py\0")
    chk("name-status 解析：同一路径既写又删 → 删除项去重", (w2, d2), (["d.py"], []))

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)

        def g(*args):
            return subprocess.run(["git", "-c", "core.quotePath=false", *args],
                                  cwd=str(root), capture_output=True)

        # ---- 假 git 仓：git init + 本地两次 commit，**不设 remote、不 push** ----
        init_ok = g("init", "-q").returncode == 0
        chk("前置：临时 git 仓可建（不设 remote）", init_ok, True)
        if not init_ok:
            print("=== selftest：%d/%d ===" % (ok_n, total))
            return 1 if ok_n != total else 0
        for k, v in (("user.email", "selftest@example.invalid"), ("user.name", "selftest")):
            g("config", k, v)
        (root / "a.txt").write_text("A\n", encoding="utf-8")
        g("add", "-A")
        g("commit", "-qm", "c1")
        (root / "a.txt").write_text("A2\n", encoding="utf-8")
        (root / "b.txt").write_text("B\n", encoding="utf-8")
        g("add", "-A")
        g("commit", "-qm", "c2")

        REMOTE = "a" * 40
        calls: list[str] = []

        def fake_gh(method: str, api: str, body=None):
            calls.append(method)
            if method == "GET" and api.endswith("/git/refs/heads/main"):
                return json.dumps({"object": {"sha": REMOTE}})
            if method == "GET" and "/git/commits/" in api:
                return json.dumps({"tree": {"sha": "b" * 40}})
            if method == "GET" and "/git/trees/" in api:
                return json.dumps({"tree": [], "truncated": False})
            raise AssertionError("selftest 出现预期外的远端调用（疑似写操作）：%s %s" % (method, api))

        def run_main(*extra: str):
            calls.clear()
            argv = sys.argv
            sys.argv = ["push_ontology.py", "--base", "HEAD~1", "--head", "HEAD",
                        "--root", str(root), *extra]
            buf = io.StringIO()
            try:
                with contextlib.redirect_stdout(buf):
                    rc = main()
            finally:
                sys.argv = argv
            return rc, buf.getvalue()

        orig_gh = globals()["gh"]
        globals()["gh"] = fake_gh
        try:
            rc1, _o1 = run_main()
            chk("dry-run（无 --apply）退出码", rc1, 0)
            chk("dry-run 远端读写比：只读 3 次", len(calls), 3)
            chk("dry-run 绝不写远端：POST 次数", calls.count("POST"), 0)

            rc2, o2 = run_main("--expect-remote", REMOTE[:7])
            chk("基线条：≥7 位短 sha 前缀判通过", rc2, 0)
            chk("基线条：短 sha 通过时明示前缀匹配", "短 sha 匹配通过" in o2, True)

            rc3, _o3 = run_main("--expect-remote", "f" * 40)
            chk("阴性对照：--expect-remote 不符 → FAIL（rc=1）", rc3, 1)
            chk("阴性对照：基线不符时零 POST", calls.count("POST"), 0)

            rc4, _o4 = run_main("--apply", "--expect-remote", "f" * 40)
            chk("阴性对照：--apply + 基线不符 → 拒绝且零 POST",
                (rc4, calls.count("POST")), (1, 0))

            # ---- ⑤ 退出码契约（v4.44.0）：外部不可用 → 3；本地前置失败 → 2 ----
            # 刻意走 `__main__` 用的同一条包装 run_cli()，而不是抽个纯映射函数来单测 ——
            # 「映射写对了但没接线」是典型假绿（本技能既有教训）。
            def run_cli_cap(*extra: str) -> tuple[int, str]:
                calls.clear()
                argv = sys.argv
                sys.argv = ["push_ontology.py", "--base", "HEAD~1", "--head", "HEAD",
                            "--root", str(root), *extra]
                err = io.StringIO()
                try:
                    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                        rc = run_cli()
                finally:
                    sys.argv = argv
                return rc, err.getvalue()

            def boom_undecided(method: str, api: str, body=None):
                raise Undecided("模拟远端不可用（selftest）")

            def boom_runtime(method: str, api: str, body=None):
                raise RuntimeError("模拟本地前置失败（selftest）")

            globals()["gh"] = boom_undecided
            rc5, e5 = run_cli_cap()
            chk("契约：外部不可用 → 退出码 3 且标注 UNDECIDED（不得记成判不过）",
                (rc5, "UNDECIDED" in e5), (3, True))
            chk("契约：外部不可用时零 POST", calls.count("POST"), 0)

            globals()["gh"] = boom_runtime
            rc6, e6 = run_cli_cap()
            chk("契约：本地前置失败 → 退出码 2 且标注 USAGE", (rc6, "USAGE" in e6), (2, True))
        finally:
            globals()["gh"] = orig_gh

    print("=== selftest：%d/%d ===" % (ok_n, total))
    return 0 if ok_n == total else 1


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
    ap.add_argument("--expect-remote",
                    help="要求远端 HEAD 等于该 sha，否则 FAIL（防基于过时基线推送）；"
                         "v4.14.1 起接受 ≥7 位短 sha 前缀")
    ap.add_argument("--message", help="提交信息（默认取 --head 的提交信息）")
    ap.add_argument("--result-file", help="把摘要写入该文件（默认只打印）")
    ap.add_argument("--selftest", action="store_true",
                    help="运行内置自检并退出（离线：name-status 解析 / dry-run 零 POST / "
                         "基线条短 sha 与 --apply 前置拒绝）；不推送、不联网、不读凭据")
    # 必须在 parse_args() 之前拦截：--base/--head 是 required=True，先 parse 会因缺参
    # 直接以退出码 2 结束，自检根本跑不到。只扫 argv，不改任何既有参数语义。
    if "--selftest" in sys.argv[1:]:
        return selftest()
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

    if a.expect_remote:
        # v4.14.1 修两处同源缺陷：
        #   ① 原为**精确字符串比对** ⇒ 传短 sha（git 惯例：≥4 位唯一前缀，本仓文档/示例的惯用写法）
        #      永远 FAIL；现口径 = 等值 或 **≥7 位前缀匹配**（4–6 位太短易撞，仍拒）。
        #   ② 失败时把两侧截成 `[:10]` 打印 ⇒ 差异落在第 10 位之后时，报错信息**看起来两侧一模一样**，
        #      把唯一的差异证据截掉了（"固定长度窗口是定时炸弹"的又一例）；
        #      现打印**完整** sha ＋ 首个不同字符位置。
        exp = a.expect_remote.strip()
        if not (exp == remote_head or (len(exp) >= 7 and remote_head.startswith(exp))):
            same = 0
            for x, y in zip(exp, remote_head):
                if x != y:
                    break
                same += 1
            out("[FAIL] 基线条不符：期望 --expect-remote=%s，实际远端 HEAD=%s" % (exp, remote_head))
            out("       长度 %d vs %d；前 %d 个字符相同，自第 %d 位起不同。"
                % (len(exp), len(remote_head), same, same + 1))
            out("       说明远端在两次推送之间被改动过（并发或他人推送）→ 请先人工核对再决定基线。")
            if a.result_file:
                save_result(a.result_file, lines, root)
            return 1
        if exp != remote_head:
            out("（--expect-remote=%s 系远端 HEAD 的前缀，按短 sha 匹配通过）" % exp)

    tree = json.loads(gh("GET", "repos/%s/git/trees/%s?recursive=1" % (a.repo, base_tree)))
    if tree.get("truncated"):
        raise Undecided("base_tree 被截断，需分页处理（本脚本未实现）⇒ 能力缺失，无法判定远端树")
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
            n_new += 1
            # ⚠️ blob 只在**真要推**的时候才创建（v3.4.0 修正）。
            # 此前这段在 `if not a.apply` 分支**之前**执行，于是 dry-run 也会 POST 出去
            # 一堆 blob —— 它们只是悬挂对象、不动 ref，**破坏性为零**，但本脚本文件头写着
            # "不加 --apply 绝不写远端"，那句话就成了假的；而且 32 项改动要 POST 29 次，
            # 实测把一次 dry-run 拖到 **2 分 37 秒**（`gh` 子进程单价 ≈130 ms 起，网络往返另算），
            # 直接吃掉了工具的联调价值。**"只读"要么真只读，要么别叫 dry-run。**
            if a.apply:
                content = git(root, "cat-file", "blob", blob)
                # ⚠️ 任务档案里可能混入二进制文件（PDF/图片等）—— 无脑 `.decode("utf-8")`
                # 会在推送半途裸抛 UnicodeDecodeError（已 POST 的 blob 悬挂、ref 未动）。
                # GitHub blobs API 支持 `encoding: base64`；无论走哪条路，下方
                # `new_blob != blob` 的 sha 比对都是正确性的机器判据，不因编码路径而失效。
                try:
                    body = {"content": content.decode("utf-8"), "encoding": "utf-8"}
                except UnicodeDecodeError:
                    body = {"content": base64.b64encode(content).decode("ascii"),
                            "encoding": "base64"}
                    out("        （二进制内容，按 base64 编码上传）")
                new_blob = json.loads(
                    gh("POST", "repos/%s/git/blobs" % a.repo, body)
                )["sha"]
                if new_blob != blob:
                    raise RuntimeError(
                        "blob sha 不符：path=%s local=%s remote=%s（疑似换行符/编码口径不一致）"
                        % (rel, blob, new_blob))
                out("  [写] %s\n        新建 blob  %s" % (rel, blob[:10]))
            else:
                out("  [写] %s\n        待新建 blob %s（dry-run 不创建；`--apply` 时才 POST）"
                    % (rel, blob[:10]))
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


def run_cli() -> int:
    """入口包装：把异常转成**契约退出码**，不再甩 traceback（§1 四码）。

    为什么包在 main 外层而不是散在各处 try：`gh()` 深处任何一次网络抖动都要落同一条
    映射，散着写必然漏一处；而漏掉的那处恰好是「最外层没人接」→ traceback。
    """
    try:
        return main()
    except Undecided as e:
        print("[ERROR] %s" % e, file=sys.stderr)
        print("[UNDECIDED] 远端状态未知（外部不可用/能力缺失）⇒ 按契约退出 3，"
              "**不要**当成「判不过」", file=sys.stderr)
        return 3
    except RuntimeError as e:
        print("[ERROR] %s" % e, file=sys.stderr)
        print("[USAGE] 本地前置条件不满足（git/参数）⇒ 按契约退出 2", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(run_cli())
