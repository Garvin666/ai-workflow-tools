# [自研工具] publish_tools.py
# 用途：把 ai-workflow 任务中自研的工具/技能源码推送到公开 GitHub 仓库，并校验仓库可见性（public）与版本一致（远端 blob sha == 本地）
# 适用场景：本任务自行编写了脚本/技能包、需要公开留痕时用；复用第三方项目、仅改既有文件、一次性内联命令不用（那些不算自研，见 SKILL.md「自研工具与技能：标注与公开留痕」）
# 作者：ai-workflow 自研（技能增强-ai-workflow-v3.1.0-2026-09-15，2026-09-15）
# 仓库：https://github.com/Garvin666/ai-workflow-tools/blob/main/scripts/publish_tools.py
"""publish_tools.py - 自研工具/技能的公开留痕推送与校验（ai-workflow v3.1.0）。

规则来源：用户在 2026-09-15 指定「自研工具必须标注名称/用途/适用场景，并同步上传至
public 仓库，此后每次更新及时推送保持版本一致，交付时附仓库链接」。本脚本把其中的
**推送**与**版本一致校验**做成机器可判定的动作 —— 不靠"我记得推过了"。

子命令:
    init   创建公开仓库（默认 public；本规则不允许 private，故不提供 --private）
           **幂等**：已存在且为 public 则跳过创建；已存在但 private 则 FAIL 并给改可见性指引
    push   推送本地文件到仓库（默认 dry-run，加 --apply 才真写；已存在文件带 sha 更新，幂等）
    verify 校验仓库为 public + 远端 blob sha 与本地一致（版本一致的机器判据）

用法:
    python publish_tools.py init --name ai-workflow-tools --apply
    python publish_tools.py push  --file scripts/foo.py --dest scripts/foo.py --apply
    python publish_tools.py verify --repo Garvin666/ai-workflow-tools --file scripts/foo.py
    SELFTOOL_REPO=owner/repo python publish_tools.py verify

网络口径: 一律走 api.github.com（本机 github.com 主域间歇不通）。请求优先用 `gh api`
子进程（本机 urllib 直连曾报 SSLEOFError），gh 不可用时回退 urllib。
凭据优先级: --token > GITHUB_TOKEN > GH_TOKEN > gh auth token；只读不落盘、不打印完整值。

退出码: 0 = 全部通过；1 = 有 FAIL；2 = 用法/环境错误（凭据缺失、参数不合法）
"""
import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

API = "https://api.github.com"
DEFAULT_REPO = os.environ.get("SELFTOOL_REPO", "Garvin666/ai-workflow-tools")
DEFAULT_BRANCH = os.environ.get("SELFTOOL_BRANCH", "main")
# 文本类扩展名：默认把 CRLF 规范化为 LF 再算 sha —— 工作区文件常是 CRLF，
# 直接按原始字节算会与仓库内的 LF 内容不一致，表现为"每次都判需要推送"。
TEXT_EXT = {".py", ".md", ".yaml", ".yml", ".json", ".txt", ".sh", ".ps1", ".toml",
            ".ini", ".cfg", ".tsv", ".csv", ".html", ".css", ".js", ".ts", ".bat", ".gitignore"}

results: list[tuple[str, str, str]] = []  # (状态, 项, 说明)


def ok(item: str, note: str = "") -> None:
    results.append(("OK", item, note))


def fail(item: str, note: str = "") -> None:
    results.append(("FAIL", item, note))


def skip(item: str, note: str = "") -> None:
    results.append(("SKIP", item, note))


def _looks_like_token(s: str) -> bool:
    s = (s or "").strip()
    return len(s) >= 20 and not any(c.isspace() for c in s)


def _resolve_token(cli: str | None = None) -> str | None:
    """凭据四来源：--token > GITHUB_TOKEN > GH_TOKEN > gh auth token（与 http_fetch.py 同口径）。"""
    for cand in (cli, os.environ.get("GITHUB_TOKEN"), os.environ.get("GH_TOKEN")):
        if cand and _looks_like_token(cand):
            return cand.strip()
    if shutil.which("gh"):
        try:
            p = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=20)
            if p.returncode == 0 and _looks_like_token(p.stdout):
                return p.stdout.strip()
        except Exception:
            pass
    return None


def _api(method: str, path: str, body: dict | None = None, token: str | None = None) -> tuple[int, dict | None, str]:
    """调 GitHub REST API，返回 (status, json, err)。优先 `gh api` 子进程，回退 urllib。

    ⚠️ 用 `--input` 传 JSON body 而非 `-f key=value`：base64 内容可能很长，
    走命令行参数会撞 Windows 的命令行长度上限。
    """
    path = path.lstrip("/")
    if shutil.which("gh"):
        cmd = ["gh", "api", "-X", method, path,
               "-H", "Accept: application/vnd.github+json"]
        tmp_name = None
        if body is not None:
            fd, tmp_name = tempfile.mkstemp(suffix=".json", prefix="aiwf-api-")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(body, fh, ensure_ascii=False)
            cmd += ["--input", tmp_name]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
            err = (p.stderr or "").strip()
            if p.returncode != 0:
                # gh 把 HTTP 状态码写进 stderr，形如 `gh: Not Found (HTTP 404)`。
                # ⚠️ 不能按 token 逐个 isdigit() 判：`404)` 带右括号判不出来，
                # 实测会把真实 404 误报成 HTTP 1 —— 必须用正则取状态码。
                m = re.search(r"HTTP\s+(\d{3})", err) or re.search(r"(?<!\d)(\d{3})(?!\d)", err)
                code = int(m.group(1)) if m else 0
                return code or p.returncode, None, err[:400]
            try:
                return 200, json.loads(p.stdout or "{}"), ""
            except json.JSONDecodeError:
                return 200, None, (p.stdout or "")[:200]
        except Exception as e:  # gh 不可用/超时 → 落到 urllib
            last = str(e)
        finally:
            if tmp_name and os.path.exists(tmp_name):
                os.unlink(tmp_name)
        if not token:
            return 0, None, f"gh 调用失败且无凭据可回退：{last[:200]}"
    if not token:
        return 0, None, "无 gh CLI 且未取得凭据（--token / GITHUB_TOKEN / GH_TOKEN / gh auth token）"
    url = f"{API}/{path}"
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "ai-workflow-publish-tools",
    })
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            raw = resp.read() or b"{}"
            try:
                return resp.status, json.loads(raw), ""
            except json.JSONDecodeError:
                return resp.status, None, raw[:200].decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, None, (e.read() or b"").decode("utf-8", "replace")[:400]
    except Exception as e:
        return 0, None, f"{type(e).__name__}: {e}"[:300]


def _print_results() -> None:
    for st, item, note in results:
        print(f"[{st}] {item}" + (f" — {note}" if note else ""))


def _split_repo(repo: str) -> tuple[str, str]:
    parts = (repo or "").strip().strip("/").split("/")
    if len(parts) != 2 or not all(parts):
        raise ValueError(f"仓库格式应为 OWNER/NAME，实际『{repo}』")
    return parts[0], parts[1]


def blob_sha(data: bytes) -> str:
    """git blob sha1 —— 与 GitHub contents API 返回的 sha 同口径。"""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def read_local(path: Path, normalize: bool = True) -> tuple[bytes, bool]:
    """读本地文件；文本类默认把 CRLF 规范化为 LF（返回是否发生过转换）。"""
    raw = path.read_bytes()
    if normalize and path.suffix.lower() in TEXT_EXT and b"\r\n" in raw:
        return raw.replace(b"\r\n", b"\n"), True
    return raw, False


def _repo_info(owner: str, name: str, token: str | None) -> tuple[dict | None, str]:
    st, data, err = _api("GET", f"/repos/{owner}/{name}", token=token)
    if st == 200 and isinstance(data, dict):
        return data, ""
    if st == 404:
        return None, f"仓库 {owner}/{name} 不存在（404）；需先 `publish_tools.py init --name {name} --apply`"
    return None, f"查询仓库失败（HTTP {st}）：{err[:200]}"


def _remote_sha(owner: str, name: str, dest: str, ref: str, token: str | None) -> tuple[str | None, int, str]:
    st, data, err = _api("GET", f"/repos/{owner}/{name}/contents/{dest}?ref={ref}", token=token)
    if st == 200 and isinstance(data, dict):
        return data.get("sha"), 200, ""
    return None, st, err


def cmd_init(args) -> int:
    """创建公开仓库。本规则要求 public，故不提供 private 选项。"""
    repo = args.repo or DEFAULT_REPO
    try:
        owner, name = _split_repo(repo)
    except ValueError as e:
        print(f"[ERROR] {e}")
        return 2
    if args.name:
        name = args.name
    token = _resolve_token(args.token)
    if not token:
        print("[ERROR] 创建仓库需要凭据：--token / GITHUB_TOKEN / GH_TOKEN / gh auth token")
        print("[HINT]  本机已登录 gh CLI 时无需额外配置；也可用 `gh auth login` 登录。")
        return 2
    # 幂等前置：仓库已存在就不重复创建（旧版会在已存在时抛 422 并报 FAIL，与「幂等可重跑」口径冲突）
    info, ierr = _repo_info(owner, name, token)
    if info is not None:
        url = info.get("html_url") or f"https://github.com/{owner}/{name}"
        if info.get("private") is True:
            print(f"[FAIL] 仓库 {owner}/{name} 已存在但为 **private** —— 规则要求 public。"
                  f"到 {url}/settings 改可见性，或换仓库名。")
            return 1
        print(f"[OK] 仓库已存在且为 public，跳过创建（幂等）：{url}")
        print(f"[OK] private={info.get('private')}（须为 False）")
        return 0
    if not ierr.startswith("仓库") or "不存在" not in ierr:
        # 非 404 的查询失败（网络/权限）→ 不冒险去 POST，避免误建或误判
        print(f"[FAIL] 建仓前无法确认仓库状态：{ierr}")
        return 1
    body = {
        "name": name,
        "description": args.description or "ai-workflow 自研工具与技能的公开留痕仓库（源码同步 + 版本一致）",
        "private": False,          # 规则硬性要求：必须 public
        "auto_init": True,
        "has_issues": True,
    }
    print(f"[INFO] 将创建 **public** 仓库 {owner}/{name}（private=false，本规则不允许私有）")
    if not args.apply:
        print(f"[DRY-RUN] body={json.dumps(body, ensure_ascii=False)}")
        print("[DRY-RUN] 未执行。加 --apply 才真正创建。")
        return 0
    st, data, err = _api("POST", "/user/repos", body=body, token=token)
    if st in (201, 200) and isinstance(data, dict):
        print(f"[OK] 已创建：{data.get('html_url')}")
        print(f"[OK] private={data.get('private')}（须为 False）")
        return 0
    if st == 422:
        # 竞态兜底：前置查询时不存在，POST 时已被创建（如并发执行）→ 复查后按幂等处理
        info2, _ = _repo_info(owner, name, token)
        if info2 is not None and info2.get("private") is not True:
            print(f"[OK] 仓库已存在且为 public（并发建仓竞态），按幂等跳过：{info2.get('html_url')}")
            return 0
        print(f"[FAIL] 创建失败（422）：{err[:200]}")
    else:
        print(f"[FAIL] 创建失败（HTTP {st}）：{err[:300]}")
    return 1


def _dest_for(f: Path, dest: str | None, multi: bool) -> str:
    if not dest:
        return f.name
    dest = dest.strip("/")
    # 多文件且 dest 未带文件名时，按目录前缀处理
    if multi and not Path(dest).suffix:
        return f"{dest}/{f.name}"
    return dest


def cmd_push(args) -> int:
    repo = args.repo or DEFAULT_REPO
    try:
        owner, name = _split_repo(repo)
    except ValueError as e:
        print(f"[ERROR] {e}")
        return 2
    token = _resolve_token(args.token)
    files = [Path(f) for f in args.file]
    missing = [str(f) for f in files if not f.is_file()]
    if missing:
        print(f"[ERROR] 本地文件不存在：{'；'.join(missing)}")
        return 2

    info, ierr = _repo_info(owner, name, token)
    if info is None:
        # ⚠️ 必须计入 results 而非只 print：否则汇总会出现「0/0 通过，FAIL=0」
        # 却带退出码 1 —— 与 checks.py v2.3 修过的"假绿"是同型病。
        fail("仓库可访问", ierr)
        return 1
    if info.get("private") is True:
        fail("仓库可见性", f"{owner}/{name} 为 private —— 规则要求 public，拒绝推送"
                          f"（到 https://github.com/{owner}/{name}/settings 改可见性，或换仓库）")
        return 1
    ok("仓库可见性", f"{owner}/{name} public（private=false）")

    multi = len(files) > 1
    planned, to_write = [], []
    for f in files:
        dest = _dest_for(f, args.dest, multi)
        data, converted = read_local(f, normalize=not args.no_normalize)
        sha = blob_sha(data)
        rsha, st, err = _remote_sha(owner, name, dest, args.branch, token)
        if st == 200 and rsha == sha:
            planned.append((f, dest, "一致，无需推送", None))
            continue
        if st not in (200, 404):
            planned.append((f, dest, f"查询远端失败（HTTP {st}）：{err[:120]}", None))
            fail(f"查询远端 {dest}", f"HTTP {st}：{err[:120]}")
            continue
        note = "新建" if st == 404 else "更新"
        if converted:
            note += "（本地 CRLF 已规范化为 LF 再算 sha）"
        planned.append((f, dest, f"{note}：本地 {sha[:12]} → 远端 {('无' if st == 404 else (rsha or '?')[:12])}", sha))
        to_write.append((f, dest, data, rsha))

    for f, dest, note, _ in planned:
        print(f"[{'SKIP' if '无需推送' in note else 'PLAN'}] {f}  →  {dest}  |  {note}")

    if not args.apply:
        print(f"[DRY-RUN] 共 {len(files)} 个文件，其中 {len(to_write)} 个需写入。加 --apply 才真正推送。")
        return 0

    if not token:
        print("[ERROR] 推送需要凭据：--token / GITHUB_TOKEN / GH_TOKEN / gh auth token")
        return 2
    for f, dest, data, rsha in to_write:
        body = {
            "message": args.message or f"chore(selfbuilt): 同步自研工具 {dest}",
            "content": base64.b64encode(data).decode("ascii"),
            "branch": args.branch,
        }
        if rsha:
            body["sha"] = rsha  # 更新已存在文件必须带原 sha
        st, _data, err = _api("PUT", f"/repos/{owner}/{name}/contents/{dest}", body=body, token=token)
        if st in (200, 201):
            ok(f"已推送 {dest}", f"{len(data)} 字节")
        else:
            fail(f"推送失败 {dest}", f"HTTP {st}：{err[:200]}")
    return 1 if any(r[0] == "FAIL" for r in results) else 0


def cmd_verify(args) -> int:
    repo = args.repo or DEFAULT_REPO
    try:
        owner, name = _split_repo(repo)
    except ValueError as e:
        print(f"[ERROR] {e}")
        return 2
    token = _resolve_token(args.token)
    info, ierr = _repo_info(owner, name, token)
    if info is None:
        fail("仓库可访问", ierr)
        return 1
    priv = info.get("private")
    if priv is True:
        fail("仓库可见性", f"{owner}/{name} 为 private —— 违反「自研工具必须 public」规则")
    else:
        ok("仓库可见性", f"{owner}/{name} public（private={priv}）")

    for f in [Path(x) for x in (args.file or [])]:
        if not f.is_file():
            fail(f"本地文件存在 {f}", "找不到")
            continue
        dest = _dest_for(f, args.dest, len(args.file or []) > 1)
        data, _ = read_local(f, normalize=not args.no_normalize)
        sha = blob_sha(data)
        rsha, st, err = _remote_sha(owner, name, dest, args.branch, token)
        if st == 404:
            fail(f"版本一致 {dest}", "远端无此文件（尚未推送）")
        elif st != 200:
            fail(f"版本一致 {dest}", f"查询失败 HTTP {st}：{err[:120]}")
        elif rsha == sha:
            ok(f"版本一致 {dest}", f"blob sha 一致 {sha[:12]}")
        else:
            fail(f"版本一致 {dest}", f"本地 {sha[:12]} ≠ 远端 {(rsha or '?')[:12]} —— 未同步，请重跑 push --apply")
    return 1 if any(r[0] == "FAIL" for r in results) else 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="ai-workflow 自研工具/技能：公开留痕推送与版本校验（v3.1.0）")
    ap.add_argument("--repo", help=f"目标仓库 OWNER/NAME（默认 {DEFAULT_REPO}，可用 SELFTOOL_REPO 覆盖）")
    ap.add_argument("--token", help="GitHub 凭据（默认按 GITHUB_TOKEN / GH_TOKEN / gh auth token 解析）")
    ap.add_argument("--branch", default=DEFAULT_BRANCH, help=f"目标分支（默认 {DEFAULT_BRANCH}）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="创建**公开**仓库（本规则不允许 private，故无 --private 选项）；幂等：已存在且 public 则跳过")
    p.add_argument("--name", help="仓库名（缺省用 --repo 的 NAME 部分）")
    p.add_argument("--description", help="仓库描述")
    p.add_argument("--apply", action="store_true", help="真正执行（默认 dry-run）")

    p = sub.add_parser("push", help="推送自研文件到公开仓库（默认 dry-run；已存在文件带 sha 更新，幂等）")
    p.add_argument("--file", action="append", required=True, help="本地文件路径（可多次）")
    p.add_argument("--dest", help="仓内目标路径；多文件且无后缀时按目录前缀处理")
    p.add_argument("--message", help="提交信息")
    p.add_argument("--apply", action="store_true", help="真正写入（默认只打印计划）")
    p.add_argument("--no-normalize", action="store_true",
                   help="不做 CRLF→LF 规范化（默认对文本类扩展名做，否则 sha 会与仓库内 LF 内容不一致）")

    p = sub.add_parser("verify", help="校验仓库为 public + 远端 blob sha 与本地一致")
    p.add_argument("--file", action="append", help="要比对的本地文件（可多次；不传则只验仓库可见性）")
    p.add_argument("--dest", help="仓内路径（规则同 push）")
    p.add_argument("--no-normalize", action="store_true", help="同 push")

    args = ap.parse_args()
    if args.cmd == "init":
        code = cmd_init(args)
        for st, item, note in results:
            print(f"[{st}] {item}" + (f" — {note}" if note else ""))
        return code
    if args.cmd == "push":
        code = cmd_push(args)
        _print_results()
        return code
    code = cmd_verify(args)
    print(f"=== publish_tools verify：{sum(1 for r in results if r[0] != 'FAIL')}/{len(results)} 通过，"
          f"FAIL={sum(1 for r in results if r[0] == 'FAIL')} ===")
    _print_results()
    return code


if __name__ == "__main__":
    sys.exit(main())
