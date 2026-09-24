#!/usr/bin/env python
# -*- coding: utf-8 -*-
# [自研工具] verify_push.py
# 名称：verify_push.py
# 用途：对「两类资源分流推送」的结果做独立验收 —— 本地 rev 是否全量进远端且 blob sha 逐一相等、远端独有项是否早于本次推送、自研工具在工具仓的 sha 是否与本地独立重算值一致、同一文件在两仓是否同 sha（跨仓一致性）、发布物是否自证为期望版本、两仓是否 public
# 适用场景：每次推送（push_ontology.py / push_router.py / publish_tools.py）之后执行；纯本地改动、尚未推送时不要用（会把"还没推"误报成缺失）
# 作者：ai-workflow 自研（技能增强-ai-workflow-v3.2.1-2026-09-16，2026-09-16）
# 仓库：https://github.com/Garvin666/ai-workflow-tools/blob/main/scripts/verify_push.py
#
# ⚠️ 标注必须写在**注释行**（`#` 开头）：`push_router.py` 的标注头扫描只认注释行 ——
# 这是 v3.2.0 修 ops.md 正文误命中时收窄的判据。把标注只写在 docstring 里会被门禁判
# 「登记与实现不符」（实机踩过：本文件初版正是这样，被分流器拦下）。
"""verify_push.py —— 推送结果的独立验收器（ai-workflow v3.2.1）

自研标识：自研（本文件为自行编写，非复用第三方、非 fork 既有项目）
名称：verify_push.py
用途：对「两类资源分流推送」的结果做独立验收 —— 见文件头注释行的完整说明
适用场景：每次推送之后执行；纯本地改动、尚未推送时不要用
仓库：https://github.com/Garvin666/ai-workflow-tools/blob/main/scripts/verify_push.py

------------------------------------------------------------------------------
为什么要独立实现（这条是设计前提，不是洁癖）
------------------------------------------------------------------------------
本脚本**不 import、也不复用** push_ontology.py / push_router.py / publish_tools.py 的任何
函数或常量。blob sha 自己算（git 对象格式 `sha1("blob <len>\\0" + data)`）、远端数据自己经
api.github.com 取、本地 tree 自己用 `git ls-tree -z` 解析、自研工具清单自己从 plan.yaml 的
登记表读。理由：用推送脚本自己的逻辑去验推送结果，只能证明「它自洽」，不能证明「它对」。
作者自查不等于审核 —— 这是本工作区反复踩出来的结论。

顺带构成第二个独立实现：`push_router.py` 的判定是**标注头驱动**（先扫文件、再回查登记表），
本脚本是**登记表驱动**（先读登记、再回查文件），方向相反。两者同时通过才算交叉印证。

------------------------------------------------------------------------------
两个已知口径坑（v3.0.1 踩过，此处显式处理，不再依赖"记得"）
------------------------------------------------------------------------------
① 本地侧必须 `git -c core.quotePath=false ls-tree` —— 否则中文路径被转义成 `"…\\346…"`，
   与远端 API 返回的正常中文路径比对会**凭空造出大量"假缺失"**（曾误报 101 项）。
② 混合仓不适用"集合相等"：远端 = 技能本体 ∪ 工作区档案，本地只有前者。
   正确口径 = 「本地 ⊆ 远端」+「远端独有项须机器证明早于本次推送」（用推送前 commit 的
   tree 比对）。**只有"两面都对不上"才是真异常 —— 验收器本身也需要被质疑。**

------------------------------------------------------------------------------
可复用的关键：消灭所有"人工记得改"的常量
------------------------------------------------------------------------------
旧版每轮都是复制一份再手改仓库名/rev/版本号（连续三轮各写一份），而"复制粘贴漏改常量"
恰是作者自查最抓不住的一类错。故本版：
  · 期望版本 ← 从本地 rev 的 SKILL.md frontmatter **自动推导**（不由调用方传）
  · 自研工具清单 ← 从 `tasks/**/plan.yaml` 的 `meta.自研工具` **自动发现**
  · 默认分支 ← 从远端仓库元数据取，不硬编码 main
调用方只需给两件事：**本地 rev** 与 **推送前远端 HEAD**。其余能推就推。

用法：
    python scripts/verify_push.py --rev HEAD --prev-remote 8f14ac61
    python scripts/verify_push.py --rev HEAD --prev-remote 8f14ac61 --expect-remote <sha>
    python scripts/verify_push.py --rev HEAD                      # 跳过判据 3（会显式标 SKIP）

退出码：0 = 全部通过（SKIP 不影响）；1 = 存在 FAIL；2 = 基建错误（缺 pyyaml / 取不到远端）
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    print("[FAIL] 缺少 pyyaml —— 本脚本要用它读 plan.yaml 的 meta.自研工具 登记表。")
    print("       安装：python -m pip install pyyaml（setup_env.ps1 已纳入依赖）")
    sys.exit(2)

sys.stdout.reconfigure(encoding="utf-8")

SK = Path(__file__).resolve().parents[1]          # 技能根，由脚本位置推导，不硬编码
# 本地 git 根：默认与技能根同一个目录，由 `--skill-dir` 改写。
# ⚠️ v3.4.0 补的是**功能缺口，不是放松判据**：此前 `--skill-dir` 只喂给 `read_registrations()`，
# 而 `git()` 把 `cwd` 硬编码成 `SK` —— 于是「指定技能根来验收」这句话在文档上成立、实现上不成立。
# 后果不是"少检了点"，而是**混合仓的根本局限**：`ai-workflow-skill` 同时接受两条本地来源
# （技能根 + 工作区档案），要分别验收就必须能让 git 换根，否则工作区那一侧**根本无法被本脚本验收**。
GIT_ROOT: Path = SK
DEFAULT_ONTOLOGY = os.environ.get("ONTOLOGY_REPO", "Garvin666/ai-workflow-skill")
DEFAULT_TOOLS = os.environ.get("SELFTOOL_REPO", "Garvin666/ai-workflow-tools")
TEXT_EXT = {".py", ".md", ".yaml", ".yml", ".json", ".ps1", ".txt", ".csv", ".html", ".toml", ".cfg", ".ini"}

# 标注头判据（与 v3.1.0 规则一致的**独立**实现）
MARKER_TOOL = "[自研工具]"
MARKER_SKILL = "[自研技能]"
# 标注头须落在**注释行**上（与 push_router.py 的收窄判据同义，此处独立实现）。
# 起因：若"文件里任意位置出现标记就算标注"，则 `ops.md` 里**为说明格式而写出的标记字符串本身**
# 会被误判为"有标注头却没登记"。故代码类文件只认注释行；技能包走 frontmatter `selfbuilt`。
COMMENT_PREFIXES = ("#", "//", "/*", "*", "--")
SELFTOOL_KEYS = ("名称", "用途", "适用场景", "仓库链接")
# 与 checks.py 同义的中间态哨兵：登记时尚未推送。本脚本对它判 SKIP（**不算通过**）——
# 否则"填了待推送"就永不被验收，与规则「交付前必须回填真实链接」冲突。
PENDING = "待推送"


# ---------------------------------------------------------------- 结果记录
ROWS: list[tuple[str, str, str]] = []


def rec(state: str, item: str, note: str = "") -> None:
    ROWS.append((state, item, note))
    mark = {"OK": "[ OK ]", "FAIL": "[FAIL]", "SKIP": "[SKIP]", "WARN": "[WARN]"}[state]
    print("%s %s%s" % (mark, item, (" —— " + note) if note else ""))


def sub(msg: str = "") -> None:
    print(("        " + msg) if msg else "")


class ApiError(RuntimeError):
    def __init__(self, status: int, msg: str) -> None:
        super().__init__(msg)
        self.status = status


# ---------------------------------------------------------------- 取数（本地 git / 远端 API）
def git(*args: str) -> bytes:
    p = subprocess.run(["git", "-c", "core.quotePath=false", *args],
                       cwd=str(GIT_ROOT), capture_output=True)
    if p.returncode:
        raise RuntimeError(p.stderr.decode("utf-8", "replace").strip()[:300])
    return p.stdout


_TRANSPORT: str | None = None


def api(path: str) -> dict:
    """GET api.github.com/<path>。优先 gh 子进程（本机 urllib 直连曾报 ssl.SSLEOFError）。"""
    global _TRANSPORT
    path = path.lstrip("/")
    exe = shutil.which("gh")
    if exe:
        p = subprocess.run([exe, "api", path], capture_output=True)
        if p.returncode == 0:
            if _TRANSPORT is None:
                _TRANSPORT = "gh api（子进程）"
                print("[transport] %s" % _TRANSPORT)
            return json.loads(p.stdout.decode("utf-8"))
        err = p.stderr.decode("utf-8", "replace").strip()
        if p.returncode == 4 or "404" in err or "Not Found" in err:
            raise ApiError(404, err[:200])
        if p.returncode == 1 and ("auth" in err.lower() or "token" in err.lower()):
            raise ApiError(401, err[:200])
        raise RuntimeError("gh api 失败：%s" % err[:200])

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        raise RuntimeError("本机无 gh CLI，且未设置 GITHUB_TOKEN / GH_TOKEN —— 无法取远端数据")
    if _TRANSPORT is None:
        _TRANSPORT = "urllib（回退通道）"
        print("[transport] %s" % _TRANSPORT)
    req = urllib.request.Request("https://api.github.com/" + path, headers={
        "Authorization": "Bearer " + token,
        "Accept": "application/vnd.github+json",
        "User-Agent": "verify_push/1.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise ApiError(e.code, e.read().decode("utf-8", "replace")[:200]) from None


def default_branch(repo: str) -> str:
    return api("repos/%s" % repo)["default_branch"]


def commit_sha(repo: str, ref: str) -> str:
    return api("repos/%s/commits/%s" % (repo, urllib.parse.quote(ref)))["sha"]


def remote_blobs(repo: str, commit: str) -> dict[str, str]:
    tree_sha = api("repos/%s/git/commits/%s" % (repo, commit))["tree"]["sha"]
    return tree_blobs(repo, tree_sha)


def tree_blobs(repo: str, tree_sha: str) -> dict[str, str]:
    t = api("repos/%s/git/trees/%s?recursive=1" % (repo, tree_sha))
    if t.get("truncated"):
        raise RuntimeError("远端 tree 被截断（recursive 未取全量），比对不可信 —— 中止")
    return {e["path"]: e["sha"] for e in t["tree"] if e["type"] == "blob"}


def content_sha(repo: str, path: str, ref: str) -> str | None:
    try:
        return api("repos/%s/contents/%s?ref=%s"
                   % (repo, urllib.parse.quote(path), urllib.parse.quote(ref)))["sha"]
    except ApiError as e:
        if e.status == 404:
            return None      # 该文件不在这个仓里 —— 属正常情况，不是错误
        raise


# ---------------------------------------------------------------- 本地 rev 视图
def local_blobs(rev: str) -> dict[str, str]:
    out = git("ls-tree", "-r", "-z", rev).decode("utf-8")
    d: dict[str, str] = {}
    for ent in out.split("\0"):
        if not ent:
            continue
        meta, path = ent.split("\t", 1)
        parts = meta.split()
        if len(parts) >= 3 and parts[1] == "blob":
            d[path] = parts[2]
    return d


def blob_of(rev: str, path: str) -> bytes:
    return git("cat-file", "blob", "%s:%s" % (rev, path))


def blob_sha(data: bytes) -> str:
    """git blob 对象 sha —— 独立实现，不用任何项目代码。"""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def normalized(data: bytes, path: str) -> tuple[bytes, int]:
    """文本类做 CRLF→LF 归一化，返回 (归一化后字节, 归一化处数)。"""
    if Path(path).suffix.lower() in TEXT_EXT:
        return data.replace(b"\r\n", b"\n"), data.count(b"\r\n")
    return data, 0


# ---------------------------------------------------------------- 登记表驱动：发现自研工具
def repo_of(url: str | None) -> str:
    m = re.search(r"github\.com[/:]+([^/\s]+)/([^/#?\s]+)", url or "")
    return "%s/%s" % (m.group(1), m.group(2)[:-4] if m.group(2).endswith(".git") else m.group(2)) if m else ""


def read_registrations(root: Path) -> list[dict]:
    """从 tasks/**/plan.yaml 的 meta.自研工具 读登记项（工作区文件，非 rev ——
    plan.yaml 按约定不入版本控制）。调用方须先过"工作区干净"门禁，否则登记可能与已推送版本不同步。

    ⚠️ **跳过 `tasks/*/tmp/`**：该目录按约定是中间产物与**测试替身**的投放区（且不入版本控制），
    里面的 plan.yaml 是探针伪造的假登记，不是真实交付承诺。初版没跳过，于是 v3.2.0 为证明门禁
    真伪而造的 4 个 fixture 登记全被当成真缺陷报出来（实机 4 项误报）。
    """
    items: list[dict] = []
    for plan in sorted(root.glob("tasks/**/plan.yaml")):
        rel = plan.relative_to(root).as_posix()
        if "/tmp/" in rel:
            continue
        try:
            data = yaml.safe_load(plan.read_text(encoding="utf-8")) or {}
        except Exception as e:  # noqa: BLE001 —— 单份 plan 坏了不该中断整个验收
            rec("WARN", "plan.yaml 可解析", "%s —— %s" % (rel, str(e)[:80]))
            continue
        for it in ((data.get("meta") or {}).get("自研工具") or []):
            if isinstance(it, dict) and (it.get("名称") or "").strip():
                items.append({"__plan": rel, **it})
    return items


def norm_name(name: str) -> str:
    """登记名规范化：剥掉说明性后缀括号（如 `ai-workflow（技能本体）` → `ai-workflow`）。

    ⚠️ 这是实机跑出来的：v3.1.0 的登记名带了中文括号注释，初版按字面匹配直接判 FAIL ——
    判据错怪了数据。**登记名里的注释是给人看的，不是身份的一部分。**
    """
    return re.sub(r"[（(][^）)]*[）)]\s*$", "", (name or "").strip()).strip()


def resolve_or_report(entry: dict, local: dict[str, str], root: Path) -> str | None:
    """把登记项映射到本地 rev 内的路径；映射不到时**区分两种情形**（别把测试替身当真缺陷）。

    ⚠️ 这条区分是实机跑出来的：v3.2.0 为证明门禁真伪登记了 4 个**测试替身**
    （`marked_ok.py` 等），它们放在 `tasks/*/tmp/`（按约定不入版本控制），初版一律判 FAIL
    → 4 项全是误报。**"登记了不存在的东西"（悬空登记）与"登记了不进仓的测试替身"是两码事**，
    前者是缺陷、后者只需人工确认。
    """
    name = norm_name(entry.get("名称"))
    paths = resolve_entry_path(entry, local, root.name)
    if paths:
        return paths[0]
    wt = sorted(p for p in root.glob("**/" + name) if p.is_file())
    if wt:
        rec("SKIP", "登记项可映射到 rev 内文件：%s" % name,
            "仅存在于工作区（%s）而未入版本控制 —— 尚未提交，或为测试替身/中间产物；"
            "本体仓无从验收，需人工确认" % wt[0].relative_to(root).as_posix())
    else:
        rec("FAIL", "登记项可映射到 rev 内文件：%s" % name,
            "本地 rev 与工作区都找不到同名文件 —— 悬空登记（登记与实现不符）")
    return None


def resolve_entry_path(entry: dict, paths: dict[str, str], root_name: str) -> list[str]:
    """登记项 → 本地 rev 内的候选路径。三级映射：仓内路径 > 名称==文件名 > 技能包目录。

    ⚠️ 技能包要**含根级 SKILL.md**（相对路径就是 `SKILL.md`，没有斜杠，父目录名是空串）
    —— 初版只认 `…/SKILL.md`，于是技能本体自己的登记（`ai-workflow`）被误报为悬空登记。
    """
    name = norm_name(entry.get("名称"))
    hits: list[str] = []
    if "/" in name and name in paths:
        hits.append(name)
    hits += [p for p in paths if Path(p).name == name and p not in hits]
    if not hits:
        for p in paths:
            if not p.endswith("SKILL.md"):
                continue
            parent = Path(p).parent.name
            if parent == name or (parent == "" and root_name == name):
                hits.append(p)
    return hits


def marker_comment_line(data: str) -> str | None:
    """返回第一条**注释行**上的标注行（没有则 None）。"""
    for line in data.splitlines():
        s = line.strip()
        if s.startswith(COMMENT_PREFIXES) and (MARKER_TOOL in s or MARKER_SKILL in s):
            return line
    return None


def annotate_check(entry: dict, rev: str, path: str) -> None:
    """判据：登记（意图）与标注头（事实）是否互相印证。

    ⚠️ 两条口径都是实机跑出来的，别改回去：
    ① 按**语义**判五项，不按字面找「名称：」三字 —— 既有脚本把名称写在标识同一行
       （`[自研工具] push_ontology.py`），字面判据会把这种**合规**写法误判为缺项。
    ② 代码类文件的标记**必须在注释行**（技能包走 frontmatter `selfbuilt`）—— 与门禁同义。
       初版只搜"文件里任意位置出现标记"，于是给一个把标注写在 docstring 里的文件判了 OK，
       而门禁（注释行收窄）判 FAIL → **验收器比门禁松 = 自相矛盾**。判据必须至少与门禁同严。
    """
    name = norm_name(entry.get("名称"))
    data = blob_of(rev, path).decode("utf-8", "replace")
    cl = marker_comment_line(data)
    fm_self = bool(re.search(r"^selfbuilt:\s*true", data, re.M))
    found = bool(cl or fm_self)
    rec("OK" if found else "FAIL", "标注头存在：%s" % path,
        ("注释行命中：%s" % cl.strip()) if cl else
        ("frontmatter selfbuilt: true" if fm_self else
         "已登记但文件里找不到注释行标注 %s/%s，也无 selfbuilt（标注写在 docstring/正文里不算）"
         % (MARKER_TOOL, MARKER_SKILL)))
    if not found:
        return

    pos = data.find(cl.strip()) if cl else 0
    # frontmatter 分支（cl 为 None）：SKILL.md 的 description 很长，含「仓库：链接」的
    # [自研技能] 头可能被挤出小窗口 → 对整份文件头部取大窗口（v4.7.0 仓库整理实测修正）。
    block = data[max(0, pos - 200): pos + 1200] if cl else data[:6000]
    base = Path(path).name
    fm_name = re.search(r"^name:\s*(\S+)", data, re.M)
    has_name = (base in (cl or "")) or (name in (cl or "")) or bool(fm_name and fm_name.group(1) == name)
    checks = [
        ("名称", has_name, "标注行含「%s」或 frontmatter name 匹配" % base),
        ("用途", "用途" in block, "标注块含「用途」"),
        ("适用场景", "适用场景" in block, "标注块含「适用场景」"),
        ("仓库链接", ("仓库" in block) and bool(re.search(r"https?://", block)), "标注块含「仓库」+ http(s) 链接"),
    ]
    lack = [k for k, okv, _ in checks if not okv]
    rec("OK" if not lack else "FAIL", "标注头五项齐全：%s" % path,
        "标识/名称/用途/适用场景/仓库链接 齐" if not lack else
        "缺 " + "/".join(lack) + "（" + "；".join(n for k, okv, n in checks if not okv) + "）")


# ---------------------------------------------------------------- 主流程
def main() -> int:
    ap = argparse.ArgumentParser(description="推送结果独立验收器（与推送脚本零代码共享）")
    ap.add_argument("--rev", default="HEAD", help="本地待验 rev（默认 HEAD）")
    ap.add_argument("--expect-remote", default=None, help="期望的远端 HEAD（不传则以当前远端 HEAD 为准）")
    ap.add_argument("--prev-remote", default=None, help="推送**前**的远端 HEAD —— 用于证明远端独有项早于本次推送")
    ap.add_argument("--repo", default=DEFAULT_ONTOLOGY, help="本体仓（默认 %s）" % DEFAULT_ONTOLOGY)
    ap.add_argument("--tools-repo", default=DEFAULT_TOOLS, help="工具仓（默认 %s）" % DEFAULT_TOOLS)
    ap.add_argument("--skill-dir", default=str(SK),
                    help="技能根，同时用作**本地 git 根**（默认由脚本位置推导）。"
                         "混合仓有两条本地来源时，用本参数切到另一条（如工作区档案根）分别验收")
    ap.add_argument("--expect-version", default=None, help="期望版本号（默认从本地 rev 的 SKILL.md 自动推导）")
    ap.add_argument("--allow-dirty", action="store_true", help="放行工作区未提交改动（默认阻塞）")
    a = ap.parse_args()

    root = Path(a.skill_dir).resolve()
    rev = a.rev

    # `--skill-dir` 同时也是**本地 git 根**（见 GIT_ROOT 处说明）。
    # 不指定时 root == SK，行为与本参数加入前完全一致。
    global GIT_ROOT
    GIT_ROOT = root

    print("=== 推送结果 · 独立验收 ===")
    print("验收器：%s" % Path(__file__).name)
    print("零代码共享：不 import push_ontology.py / push_router.py / publish_tools.py")
    print("本地 rev：%s    技能根（= 本地 git 根）：%s" % (rev, root))
    print("")

    # ---------- 判据 0：验收前提 ----------
    print("-- 判据 0：验收前提（工作区干净 + rev 合法）--")
    dirty = [l for l in git("status", "--porcelain").decode("utf-8").splitlines() if l.strip()]
    if dirty:
        (rec)("WARN" if a.allow_dirty else "FAIL", "工作区无未提交改动",
              "%d 项未提交；登记表读的是工作区文件，与已推送版本可能不同步%s"
              % (len(dirty), "（已用 --allow-dirty 放行）" if a.allow_dirty else ""))
        for l in dirty[:5]:
            sub(l)
    else:
        rec("OK", "工作区无未提交改动")
    try:
        local = local_blobs(rev)
    except RuntimeError as e:
        print("[FAIL] 本地 rev 不可解析：%s" % e)
        return 2
    rec("OK", "本地 rev 可解析", "%s → %d 个 blob" % (rev, len(local)))

    # ---------- 取两仓 HEAD ----------
    try:
        br_o = default_branch(a.repo)
        head_o = commit_sha(a.repo, br_o)
        br_t = default_branch(a.tools_repo)
        head_t = commit_sha(a.tools_repo, br_t)
    except (RuntimeError, ApiError) as e:
        print("[FAIL] 取远端失败：%s" % e)
        return 2

    # ---------- 判据 1：远端 HEAD ----------
    print("")
    print("-- 判据 1：远端本体仓 HEAD --")
    if a.expect_remote:
        rec("OK" if head_o == a.expect_remote else "FAIL", "远端 HEAD == 期望值",
            "实际 %s / 期望 %s" % (head_o[:10], a.expect_remote[:10]))
    else:
        rec("OK", "远端 HEAD 已取到", head_o[:10] + "（未指定 --expect-remote，不做等值断言）")
    if a.prev_remote:
        rec("OK" if head_o != a.prev_remote else "FAIL", "远端 HEAD ≠ 推送前值（确非空跑）",
            "推送前 %s" % a.prev_remote[:10])
    else:
        rec("SKIP", "远端 HEAD ≠ 推送前值", "未传 --prev-remote")

    # ---------- 判据 2：本地 ⊆ 远端，blob sha 逐一相等 ----------
    print("")
    print("-- 判据 2：本地 rev ⊆ 远端（全量逐文件 blob sha 比对）--")
    remote = remote_blobs(a.repo, head_o)
    missing = sorted(p for p in local if p not in remote)
    mismatch = sorted(p for p in local if p in remote and remote[p] != local[p])
    rec("OK" if not missing else "FAIL", "本地全部文件存在于远端",
        "本地 %d 项 / 远端 %d 项 / 缺失 %d 项" % (len(local), len(remote), len(missing)))
    for p in missing[:5]:
        sub("缺失：" + p)
    rec("OK" if not mismatch else "FAIL", "同名文件 blob sha 逐一相等", "不一致 %d 项" % len(mismatch))
    for p in mismatch[:5]:
        sub("%s  本地 %s ≠ 远端 %s" % (p, local[p][:10], remote[p][:10]))

    # ---------- 判据 3：远端独有项必须早于本次推送 ----------
    print("")
    print("-- 判据 3：远端独有项均为推送前既有（混合仓的工作区档案侧）--")
    if not a.prev_remote:
        rec("SKIP", "远端独有项早于本次推送", "未传 --prev-remote —— 无法机器证明，需人工确认")
    else:
        try:
            base = tree_blobs(a.repo, api("repos/%s/git/commits/%s" % (a.repo, a.prev_remote))["tree"]["sha"])
        except (RuntimeError, ApiError) as e:
            rec("FAIL", "推送前 tree 可取", "%s（⚠️ `/git/commits/{sha}` 端点对**缩写 sha** 会 404，请给完整 40 位）" % str(e)[:100])
            base = None
        if base is not None:
            extra = sorted(p for p in remote if p not in local)
            newly = [p for p in extra if p not in base]
            rec("OK" if not newly else "FAIL", "无本次新出现的远端独有项",
                "远端独有 %d 项，其中本次新出现 %d 项" % (len(extra), len(newly)))
            for p in newly[:5]:
                sub("本次新出现：" + p)
            rec("OK" if len(remote) >= len(base) else "FAIL", "远端 blob 数未减少（无意外删除）",
                "%d → %d" % (len(base), len(remote)))

    # ---------- 判据 4 / 6：自研工具仓版本一致 + 跨仓一致性（登记表驱动） ----------
    print("")
    print("-- 判据 4 / 6：自研工具（由 meta.自研工具 自动发现）--")
    entries = read_registrations(root)
    rec("OK" if entries else "SKIP", "读到自研工具登记",
        "%d 项（来自 %d 份 plan.yaml 汇总）" % (len(entries), len({e["__plan"] for e in entries})))
    n_tool, n_ont = 0, 0
    for e in entries:
        name = e["名称"].strip()
        url = e.get("仓库链接") or ""
        target = repo_of(url)
        lack = [k for k in SELFTOOL_KEYS if not str(e.get(k, "")).strip()]
        if lack:
            rec("FAIL", "登记项字段齐全：%s" % name, "缺 " + "/".join(lack))
            continue
        if PENDING in str(url):
            # 与 checks.py selftool 的中间态口径一致：允许"先登记后推送"，但**不当通过** ——
            # 推送后必须回填真实链接再验收，否则本判据永远测不到东西。
            rec("SKIP", "登记项已推送：%s" % name,
                "仓库链接仍为「%s」—— 阶段 6 回填后重跑" % PENDING)
            continue
        if target == a.tools_repo:
            n_tool += 1
            path = resolve_or_report(e, local, root)
            if path is None:
                continue
            annotate_check(e, rev, path)
            raw = blob_of(rev, path)
            norm, ncrlf = normalized(raw, path)
            mine = blob_sha(norm)
            rsha = content_sha(a.tools_repo, path, br_t)
            rec("OK" if rsha == mine else "FAIL", "工具仓版本一致：%s" % path,
                ("远端 %s = 独立重算 %s（%d 字节，CRLF %d 处）" % ((rsha or "不存在")[:10], mine[:10], len(norm), ncrlf))
                if rsha == mine else
                "远端 %s ≠ 独立重算 %s（%d 字节，CRLF %d 处）" % ((rsha or "不存在")[:10], mine[:10], len(norm), ncrlf))
            ssha = content_sha(a.repo, path, br_o)
            if ssha is None:
                rec("SKIP", "跨仓一致性：%s" % path, "该路径不在本体仓内（技能根之外），无跨仓可比")
            else:
                rec("OK" if ssha == rsha else "FAIL", "跨仓一致性：%s" % path,
                    "本体仓 %s / 工具仓 %s%s" % (ssha[:10], (rsha or "None")[:10],
                                              "" if ssha == rsha else "  ← 两仓内容不同源，公开仓可能在放旧版本"))
        elif target == a.repo:
            n_ont += 1
            path = resolve_or_report(e, local, root)
            if path is None:
                continue
            annotate_check(e, rev, path)
            rec("OK", "本体登记归属正确（不进工具仓）", "%s → %s" % (name, target))
            leaked = content_sha(a.tools_repo, path, br_t)
            rec("OK" if leaked is None else "FAIL", "本体未被误推入工具仓：%s" % path,
                "工具仓内无此路径" if leaked is None else "工具仓内竟存在 %s" % leaked[:10])
        else:
            rec("FAIL", "登记链接归属：%s" % name,
                "链接 %s 既不指向本体仓也不指向工具仓" % (url[:60] or "（空）"))
    print()
    print("   汇总：指向工具仓 %d 项 / 指向本体仓 %d 项" % (n_tool, n_ont))

    # ---------- 判据 5：发布物自证 ----------
    print("")
    print("-- 判据 5：发布物自证（远端 SKILL.md 自身 + 它指向的下沉手册）--")
    try:
        lc = blob_of(rev, "SKILL.md").decode("utf-8", "replace")
    except RuntimeError:
        # 本条判据问的是「**期望版本能不能定下来**」，而不是「本地一定有 SKILL.md」。
        # 传了 `--expect-version` 时期望版本已由参数给出，本地 SKILL.md 只是推导的**手段**，
        # 手段用不上不等于判据不成立 —— 故此时记 OK 并说明理由。
        # ⚠️ 注意这不是放松：**不传 `--expect-version` 的默认路径行为完全不变**（仍 FAIL）。
        # 这条修正随 `--skill-dir` 一起是必需的 —— 从工作区档案根验收时那里本就没有 SKILL.md。
        if a.expect_version:
            rec("OK", "期望版本已确定", "%s（来自 --expect-version；本地 rev 无 SKILL.md，未走推导）"
                % a.expect_version)
        else:
            rec("FAIL", "本地 rev 含 SKILL.md", "找不到 SKILL.md，无法推导期望版本")
        lc = ""
    m = re.search(r"^version:\s*([0-9][^\s#]*)", lc, re.M)
    ver = a.expect_version or (m.group(1) if m else None)
    if ver:
        rec("OK", "期望版本已确定", "%s（%s）" % (ver, "来自 --expect-version" if a.expect_version else "从本地 rev 自动推导"))
    else:
        rec("FAIL", "期望版本可确定", "本地 SKILL.md frontmatter 无 version，且未传 --expect-version")
    try:
        rc = base64.b64decode(api("repos/%s/contents/SKILL.md?ref=%s" % (a.repo, br_o))["content"]).decode("utf-8", "replace")
    except (RuntimeError, ApiError) as e:
        rec("FAIL", "远端 SKILL.md 可取", str(e)[:120])
        rc = ""
    if rc and ver:
        rec("OK" if ("version: " + ver) in rc else "FAIL", "远端 SKILL.md 版本 = 期望",
            "命中 version: %s" % ver if ("version: " + ver) in rc else "远端内容里找不到 version: %s" % ver)
        # ⚠️ v3.4.0（P4+P5）：分流细则已由 SKILL.md 下沉到 `references/push-routing.md`，
        # 演进台账：v3.x 拆到 `references/changelog.md`，v4.4.2 再移居 `_archive/changelog.md`（SKILL.md 第 18/506 行口径）。判据随之**迁移到内容的新家** ——
        # 而不是把关键词硬塞回 SKILL.md，那只会让判据退化成"为过门禁而保留的装饰"。
        # 迁移口径：**只增不减**。SKILL.md 侧改查「是否还指着新家」（地址写错=断链，同样致命），
        # 手册侧补查关键词，净判据数由 4 条升到 9 条 —— 验收器不得比门禁松。
        probes = [
            ("SKILL.md", ["selfbuilt: true", "references/push-routing.md", "_archive/changelog.md"]),
            ("references/push-routing.md", ["两类资源的分流推送路由", "触发条件", "判定依据",
                                            "冲突处理策略", "推送后的独立验收"]),
        ]
        for path, kws in probes:
            try:
                txt = base64.b64decode(
                    api("repos/%s/contents/%s?ref=%s" % (a.repo, path, br_o))["content"]
                ).decode("utf-8", "replace")
            except (RuntimeError, ApiError) as e:
                rec("FAIL", "远端 %s 可取" % path, str(e)[:120])
                continue
            for kw in kws:
                rec("OK" if kw in txt else "FAIL", "远端 %s 含「%s」" % (path, kw))

    # ---------- 判据 7：两仓可见性 ----------
    print("")
    print("-- 判据 7：两仓均为 public --")
    for r in (a.repo, a.tools_repo):
        try:
            info = api("repos/%s" % r)
        except (RuntimeError, ApiError) as e:
            rec("FAIL", "%s 可访问" % r, str(e)[:120])
            continue
        rec("OK" if info.get("private") is False else "FAIL", "%s public" % r,
            "visibility=%s" % info.get("visibility"))

    # ---------- 汇总 ----------
    n_ok = sum(1 for s, _, _ in ROWS if s == "OK")
    n_fail = sum(1 for s, _, _ in ROWS if s == "FAIL")
    n_warn = sum(1 for s, _, _ in ROWS if s == "WARN")
    n_skip = sum(1 for s, _, _ in ROWS if s == "SKIP")
    print("")
    print("=== 结果：%s（OK %d / FAIL %d / SKIP %d / WARN %d）==="
          % ("全部通过 FAIL=0" if n_fail == 0 else "有 %d 项 FAIL" % n_fail,
             n_ok, n_fail, n_skip, n_warn))
    if n_skip:
        print("    ⚠️ SKIP 项需人工确认，不当作通过")
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
