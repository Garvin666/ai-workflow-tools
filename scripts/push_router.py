#!/usr/bin/env python
# -*- coding: utf-8 -*-
# [自研工具] push_router.py
# 用途：两类资源的分流推送路由 —— 扫描本地改动面，按规则判定哪些属「本体」（→ ai-workflow-skill）、哪些属「自研工具」（→ ai-workflow-tools），分别调用两条通道推送，并对「标注头」与「plan.yaml 登记表」做交叉校验
# 适用场景：一次改动同时涉及技能本体/任务档案与自研工具、需要按各自规则分流推送时用；只涉及单一类别、或推送非本技能仓时可直接用对应通道脚本（本体 push_ontology.py ／ 自研工具 publish_tools.py）
# 作者：ai-workflow 自研（技能增强-ai-workflow-v3.2.0-2026-09-16，2026-09-16）
# 仓库：https://github.com/Garvin666/ai-workflow-tools/blob/main/scripts/push_router.py
"""push_router.py - 两类资源的分流推送路由（ai-workflow v3.2.0）。

规则来源：用户 2026-09-16 指定「本体（Ontology）相关内容自动推送至 ai-workflow-skill，
自研工具相关内容自动推送至 ai-workflow-tools」，并要求明确四要素：触发条件、判定依据、
推送格式、冲突处理策略。

⚠️ 最容易误解的一点：**分流不是互斥二选一**。
    本体   = 技能仓内改动面的**全量**（技能本体 + 任务档案）
    自研工具 = 上述全量中的**子集**（同时满足「有自研标注头」与「已登记」）
  因此 scripts/ 下的自研脚本属**双属** —— 既作为本体快照进 ai-workflow-skill，
  又作为独立工具进 ai-workflow-tools。两条通道各推一份，内容同源。

两条通道的格式差异（本脚本负责包装，调用方不必记）：
    本体     → push_ontology.py ：api.github.com Git Data API + base_tree 增量（支持删除）
    自研工具 → publish_tools.py ：api.github.com contents API 单文件（逐文件调用，CRLF→LF 归一化）

判定依据（用户 2026-09-16 选定：标注头 + 登记表交叉校验）：
    事实判据 = 文件首部含 `[自研工具]` / `[自研技能]` 标注块（脚本），
               或 SKILL.md frontmatter 含 `selfbuilt: true`（技能包）
    意图判据 = 任意 tasks/**/plan.yaml 的 meta.自研工具 登记项
    两者不一致即 FAIL：
      · 有标注头但登记表无此项 → 「漏登记」
      · 登记项能映射到某文件、但该文件无标注头 → 「登记与实现不符」
      · 登记项无法映射到文件（链接为仓库根，如技能包级登记）→ WARN，不阻塞

触发条件（本脚本按改动类型分别处理）：
    新增 A / 更新 M → 推送（内容一致则幂等跳过）
    重命名 R        → 本体按「删旧＋增新」；自研工具按新路径更新，旧路径删除需显式 --allow-delete
    删除 D          → 本体需 --allow-delete；自研工具不在本脚本自动删除（避免误删公开产物）

冲突处理策略（详见 SKILL.md 同名小节）：
    · 幂等：内容一致 → [=] 跳过，可反复重跑
    · 并发：本体通道以远端当前 HEAD 为 commit parent 且 ref 非强推 → 非快进即失败；
            可用 --expect-remote <sha> 显式要求基线
    · blob sha 自证：本地对象 sha 与远端返回 sha 不符 → 立即报错（换行符/编码口径问题的机器判据）
    · 混合仓保护：远端独有项（工作区档案侧）永不因本次推送被删
    · 删除保护：远端删除不可逆 → 默认只列出，需 --allow-delete 才执行
    · 已知边界（如实登记，不假装有保护）：无跨会话基线时，无法检测「远端被第三方改写」
      这类漂移；如需该保护，请在推送后记录 sha 并用 --expect-remote 显式约束。

用法：
    python push_router.py classify --base <rev> --head <rev>           # 离线分类 + 交叉校验
    python push_router.py classify --files a.py,b.md                   # 指定文件分类（离线）
    python push_router.py push     --base <rev> --head <rev>            # dry-run（默认）
    python push_router.py push     --base <rev> --head <rev> --apply    # 真推两仓

退出码: 0 = 通过（含 dry-run / 幂等跳过）；1 = 有 FAIL（含交叉校验失败，阻塞推送）；2 = 用法/环境错误
"""
import argparse
import os
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

MARKERS = ("[自研工具]", "[自研技能]")
HEADER_KEYS = ("用途", "适用场景", "作者", "仓库")

ONTOLOGY_REPO = os.environ.get("ONTOLOGY_REPO", "Garvin666/ai-workflow-skill")
ONTOLOGY_BRANCH = os.environ.get("ONTOLOGY_BRANCH", "main")
TOOLS_REPO = os.environ.get("SELFTOOL_REPO", "Garvin666/ai-workflow-tools")
TOOLS_BRANCH = os.environ.get("SELFTOOL_BRANCH", "main")

HERE = Path(__file__).resolve().parent


# --------------------------------------------------------------------------- #
# 基础
# --------------------------------------------------------------------------- #
class RouterError(RuntimeError):
    pass


def find_root(hint: str | None) -> Path:
    if hint:
        return Path(hint).resolve()
    p = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, cwd=str(HERE))
    if p.returncode != 0:
        raise RouterError("无法探测 git 根，请用 --root 指定")
    return Path(p.stdout.decode("utf-8", "replace").strip())


def git(root: Path, *args) -> bytes:
    p = subprocess.run(["git", "-c", "core.quotePath=false", *args],
                       capture_output=True, cwd=str(root))
    if p.returncode != 0:
        raise RouterError("git %s\n%s" % (" ".join(args), p.stderr.decode("utf-8", "replace")[:300]))
    return p.stdout


def parse_name_status(raw: bytes):
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
        else:
            writes.append(parts[i + 1])
            i += 2
    return writes, [d for d in deletes if d not in set(writes)]


# --------------------------------------------------------------------------- #
# 判据一：自研标注头（事实）
# --------------------------------------------------------------------------- #
def scan_selftool_header(path: Path) -> dict | None:
    """读文件首部找自研标注块。命中返回 {名称,用途,适用场景,作者,仓库,形式}，否则 None。"""
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if b"\0" in raw[:4096]:          # 二进制文件不解析
        return None
    text = raw.decode("utf-8", "replace")
    lines = text.splitlines()

    # 技能包**优先**判定：SKILL.md 的语义由 frontmatter 决定。若把「扫描正文标记」放在前面，
    # SKILL.md 正文首段的 [自研技能] 输出标注会抢先命中脚本分支，导致技能包分支永不生效
    # ——这个顺序陷阱是真实踩到的（classify 把 SKILL.md 误报成「漏登记」）。
    if path.name == "SKILL.md" and text.startswith("---"):
        end = text.find("\n---", 3)
        fm = text[3:end] if end > 0 else text[:2000]
        if re.search(r"^\s*selfbuilt\s*:\s*true\s*$", fm, re.M):
            name_m = re.search(r"^\s*name\s*:\s*(\S+)", fm, re.M)
            repo_m = re.search(r"^\s*repo\s*:\s*(\S+)", fm, re.M)
            return {"名称": name_m.group(1).strip('"\'') if name_m else path.parent.name,
                    "形式": "技能包 frontmatter",
                    "用途": "（见 frontmatter description）", "适用场景": "（见正文首段标注块）",
                    "作者": "", "仓库": repo_m.group(1).strip('"\'') if repo_m else "",
                    "标记行": 1}

    # ⚠️ 文档类文件（.md）不进脚本分支。两个理由，缺一都会造成假阳性：
    #   ① markdown 的 `#` 是**标题**不是注释 —— 把标题行当「注释行」判据本身就不成立；
    #   ② 本技能族**必须**在文档里写出标注示例（`references/push-routing.md` 的标注模板
    #      就是 `# [自研工具] xxx.py` 开头的代码块），于是「为说明格式而写出的标记字符串
    #      本身」会被判成「有标注头却未登记」→ FAIL 阻塞推送。
    #   修法是**收窄判据**，不是把文档改得躲开门禁 —— 躲避式修法会让人不敢在文档里引用这
    #   个标记（与 v3.1.1「文档引用完整性」同型）。技能包已由上面的 frontmatter 分支覆盖。
    if path.suffix.lower() in (".md", ".markdown"):
        return None

    # 脚本形式：前 40 行内的**注释行**。
    # ⚠️ 必须限定「注释行」，不能只找标记字符串：ops.md 的脚本文档表里为说明标注格式
    # 写了 `[自研工具]` 这个字符串本身（表格行以 | 开头），若不加限定，文档会被判成
    # 「有自研标注头却没登记」→ 假阳性。修法是**收窄判据**，而不是把文档改得躲开门禁
    # —— 躲避式修法会让人不敢在文档里引用这个标记（v3.1.1 的「文档引用完整性」坑同型）。
    for idx, line in enumerate(lines[:40]):
        s = line.strip()
        if not (s.startswith("#") or s.startswith("//") or s.startswith("/*")
                or s.startswith("*") or s.startswith("--")):
            continue
        if any(m in s for m in MARKERS):
            name = ""
            name_m = re.search(r"\[自研[^\]]*\]\s*(\S+)", line)
            if name_m:
                name = name_m.group(1)
            info = {"名称": name, "形式": "脚本标注头", "标记行": idx + 1}
            for key in HEADER_KEYS:
                info[key] = ""
            for l2 in lines[:60]:
                s = l2.strip().lstrip("#").lstrip("*").lstrip("/").strip()
                for key in HEADER_KEYS:
                    if not info.get(key) and (s.startswith(key + "：") or s.startswith(key + ":")):
                        info[key] = s.split("：", 1)[-1].split(":", 1)[-1].strip()
            return info

    return None


# --------------------------------------------------------------------------- #
# 判据二：登记表（意图）
# --------------------------------------------------------------------------- #
def load_registry(skill_root: Path) -> list[dict]:
    if yaml is None:
        raise RouterError("缺少 PyYAML，无法解析 plan.yaml（本脚本不自行造 YAML 解析器）")
    reg: list[dict] = []
    for p in sorted(skill_root.glob("tasks/**/plan.yaml")):
        try:
            d = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        items = (d.get("meta") or {}).get("自研工具") or []
        for it in items:
            if not isinstance(it, dict):
                continue
            reg.append({"名称": str(it.get("名称", "")), "用途": str(it.get("用途", "")),
                        "适用场景": str(it.get("适用场景", "")), "仓库链接": str(it.get("仓库链接", "")),
                        "来源": str(p.relative_to(skill_root)).replace("\\", "/")})
    return reg


def _repo_of(url: str) -> str:
    """从 GitHub URL 提取 OWNER/REPO（取前两段，用于判「是否同一个仓」）。"""
    m = re.search(r"github\.com[/:]([^/\s#?]+/[^/\s#?]+)", url or "")
    return m.group(1).rstrip("/") if m else ""


def entry_matches_path(entry: dict, rel: str, hdr: dict | None = None) -> bool:
    """登记项能否映射到某个本地文件（相对技能根路径）。

    映射优先级（从可靠到宽松）：
      ① 登记链接里的仓内路径（最可靠 —— 是登记时必填的结构化信息）
      ② 名称 == 文件名
      ③ 技能包：SKILL.md frontmatter 的 repo 与登记链接**同仓**
     ⚠️ 刻意**不**依赖名称里是否含「技能本体」这类中文词 —— 那种关键词匹配一改命名就失效，
        属于「看起来能用、其实脆弱」的判据。
    """
    link = entry.get("仓库链接", "")
    m = re.search(r"/blob/[^/]+/(.+?)(?:[?#].*)?$", link)
    if m:
        inrepo = m.group(1)
        if (inrepo == rel or rel.endswith("/" + inrepo)
                or PurePosixPath(rel).name == PurePosixPath(inrepo).name):
            return True
    name = entry.get("名称", "")
    base = PurePosixPath(rel).name
    if name and (name == base or name.split("（")[0].strip() == base):
        return True
    if rel.endswith("SKILL.md") and hdr and hdr.get("仓库"):
        a, b = _repo_of(link), _repo_of(hdr["仓库"])
        if a and b and a == b:
            return True
    return False


# --------------------------------------------------------------------------- #
# 分流判定
# --------------------------------------------------------------------------- #
def entry_targets_tools(entry: dict, tools_repo: str) -> bool:
    """登记项是否指向「自研工具仓」。

    ⚠️ 这条判据是必需的，不是锦上添花：技能本体自身也是一个已登记的「自研技能」，
    但它的登记链接指向 ai-workflow-skill。若只按「有标注头 ∩ 已登记」判定，
    SKILL.md 会被误判成自研工具、进而被推进 tools 仓 —— 分流就错了。
    「待推送」（尚未公开）视为进 tools 仓。
    """
    link = entry.get("仓库链接", "").strip()
    if not link or link == "待推送":
        return True
    return ("github.com/" + tools_repo) in link or tools_repo in link


def build_plan(skill_root: Path, base: str | None, head: str | None,
               files: list[str] | None = None) -> dict:
    if files:
        writes = []
        for f in files:
            p = Path(f)
            if p.is_absolute():
                try:
                    writes.append(str(p.resolve().relative_to(skill_root)).replace("\\", "/"))
                except ValueError:
                    writes.append(str(p).replace("\\", "/"))   # 技能根外：交给下游判 WARN
            else:
                writes.append(f.replace("\\", "/"))
        deletes = []
    else:
        writes, deletes = parse_name_status(
            git(skill_root, "diff", "--name-status", "-z", base, head))

    registry = load_registry(skill_root)

    ontology, selftools, checks = [], [], []
    hdr_map: dict[str, dict | None] = {}
    for rel in writes:
        abs_p = skill_root / rel
        # 本体判据：路径在技能仓内（技能本体 + 任务档案）。技能根外文件不归本脚本管。
        in_repo = not rel.startswith("..") and not Path(rel).is_absolute()
        if not in_repo:
            checks.append(("WARN", rel, "技能根之外的文件 → 不属于本脚本管辖，请直接用 publish_tools.py"))
            continue
        ontology.append(rel)

        hdr = scan_selftool_header(abs_p)
        hdr_map[rel] = hdr
        hit = [e for e in registry if entry_matches_path(e, rel, hdr)]
        hit_tools = [e for e in hit if entry_targets_tools(e, TOOLS_REPO)]
        if hdr and hit_tools:
            selftools.append(rel)
            missing = [k for k in ("名称", "用途", "适用场景", "仓库") if not hdr.get(k)]
            if missing:
                checks.append(("FAIL", rel, "标注头缺项：%s（规则要求五项齐全）" % "、".join(missing)))
            else:
                checks.append(("OK", rel, "标注头齐全，已登记（来源 %s）→ 双属：本体 + 自研工具"
                               % hit_tools[0]["来源"]))
        elif hdr and hit and not hit_tools:
            checks.append(("OK", rel, "有标注头且已登记，但登记链接指向**本体仓**（%s）"
                           "→ 只走本体通道，不推 tools 仓" % hit[0].get("仓库链接", "")[:60]))
        elif hdr and not hit:
            checks.append(("FAIL", rel, "有自研标注头但 plan.yaml 登记表无此项 → 漏登记"
                                        "（用 checks.py selftool 补登记）"))
        elif hit and not hdr:
            checks.append(("FAIL", rel, "登记表有此项（来源 %s）但文件无自研标注头 → 登记与实现不符"
                          % hit[0]["来源"]))
        else:
            checks.append(("OK", rel, "普通本体文件（无自研标注，无需登记）"))

    # 方向二：登记项未映射到本次改动面 —— 汇总一行（逐项刷屏会淹没真正的 FAIL）
    unmapped = [e["名称"] for e in registry
                if not any(entry_matches_path(e, r, hdr_map.get(r)) for r in writes)]
    if unmapped:
        checks.append(("SKIP", "登记表（共 %d 项）" % len(registry),
                       "另有 %d 项未涉及本次改动面（历史登记或本次未改动，非错误）：%s"
                       % (len(unmapped), "、".join(unmapped[:4])
                          + ("…" if len(unmapped) > 4 else ""))))
    return {"writes": writes, "deletes": deletes, "ontology": ontology,
            "selftools": selftools, "checks": checks, "registry": registry}


def print_plan(plan: dict, on_line) -> int:
    on_line("── 分流判定 ──")
    if not plan["writes"]:
        on_line("  （改动面为空）")
    selftool_set = set(plan["selftools"])
    for rel in plan["writes"]:
        tag = "本体+自研工具" if rel in selftool_set else "本体"
        on_line("  [%-12s] %s" % (tag, rel))
    for rel in plan["deletes"]:
        on_line("  [%-12s] %s   （删除项）" % ("删除", rel))
    on_line("")

    on_line("── 交叉校验（标注头 vs plan.yaml 登记表）──")
    n_fail = 0
    for st, item, note in plan["checks"]:
        if st == "FAIL":
            n_fail += 1
        on_line("  [%-4s] %-42s %s" % (st, item, note))
    on_line("  登记表共 %d 项（来自 tasks/**/plan.yaml）" % len(plan["registry"]))
    on_line("")

    on_line("── 推送计划 ──")
    on_line("  本体     → %s@%s ：%d 项（通道 push_ontology.py，Git Data API + base_tree）"
            % (ONTOLOGY_REPO, ONTOLOGY_BRANCH, len(plan["ontology"])))
    on_line("  自研工具 → %s@%s ：%d 项（通道 publish_tools.py，contents API 逐文件）%s"
            % (TOOLS_REPO, TOOLS_BRANCH, len(plan["selftools"]),
               "：" + "、".join(plan["selftools"]) if plan["selftools"] else "（无）"))
    on_line("")
    return n_fail


# --------------------------------------------------------------------------- #
# 子命令
# --------------------------------------------------------------------------- #
def cmd_classify(a) -> int:
    root = find_root(a.root)
    lines: list[str] = []

    def out(s=""):
        print(s)
        lines.append(s)

    out("=== push_router classify（离线，不联网）===")
    out("技能根：%s" % root)
    out("来源：%s" % ("--files 显式指定" if a.files else "git diff %s..%s" % (a.base, a.head)))
    out("")
    plan = build_plan(root, a.base, a.head, a.files)
    n_fail = print_plan(plan, out)
    if n_fail:
        out("[FAIL] 交叉校验有 %d 项失败 → 若执行 push 将被阻塞（先修好再推）" % n_fail)
    else:
        out("[OK] 交叉校验 0 失败")
    if a.result_file:
        save_result(a.result_file, lines, root)
    return 1 if n_fail else 0


def run(cmd: list[str]) -> int:
    print("\n  $ " + " ".join(cmd))
    p = subprocess.run(cmd, cwd=str(HERE))
    return p.returncode


def save_result(result_file: str | None, lines: list, root: Path) -> None:
    """把摘要写入 `--result-file`。两条铁律（均由实机事故推出来，别改回去）：

    ① 相对路径**以技能根为基准**解析。事故：`run()` 用 `cwd=scripts/` 调通道，而传给通道的
       `--result-file tasks/…` 是相对路径 → 通道把日志写到 `scripts/tasks/…`（父目录不存在）
       → 在**推送已成功之后**抛异常 → 退出码 1 → 分流器判定"本体通道失败"并中止工具通道，
       **一次成功的推送被一个日志路径掩盖成失败**。故本函数在 cmd_push 入口就把路径解析成
       绝对路径，既用于自己写盘，也用于传给通道。
    ② 落盘失败**绝不能改变推送结论** —— 日志是旁证，不是动作。写不出来就 WARN。
    """
    if not result_file:
        return
    p0 = Path(result_file)
    p = (root / p0 if not p0.is_absolute() else p0).resolve()
    try:
        p.relative_to(root.resolve())
    except ValueError:
        print("[WARN] 结果路径越出技能根，拒绝写入（不影响推送结论）：%s" % p)
        return
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("\n".join(lines), encoding="utf-8")
    except OSError as e:
        print("[WARN] 结果落盘失败（不影响推送结论）：%s —— %s" % (p, e))


def cmd_push(a) -> int:
    root = find_root(a.root)
    # 统一解析为绝对路径（见 save_result 的事故说明）
    if a.result_file:
        rf = Path(a.result_file)
        a.result_file = str(rf if rf.is_absolute() else (root / rf))
    lines: list[str] = []

    def out(s=""):
        print(s)
        lines.append(s)

    mode = "APPLY（真实推送两仓）" if a.apply else "DRY-RUN（默认；两仓都不会写入）"
    out("=== push_router push（分流推送）===")
    out("技能根：%s" % root)
    out("改动面：%s..%s" % (a.base, a.head))
    out("模式：%s" % mode)
    out("")
    # ⚠️ 真写之前先查工作区是否干净 —— 这不是洁癖，是本轮真实事故换来的：
    # 本体通道按 **commit 内容**推（git cat-file blob <rev>:<path>），工具通道按 **工作区文件**推
    # （publish_tools.py --file <工作区路径>）。若工作区有未提交改动，两条通道就会推出**不同内容**，
    # 同一文件在两仓 sha 不一致、公开仓里可能躺着已修好的 bug 的旧版本。
    if a.apply and not a.allow_dirty:
        dirty = [l for l in git(root, "status", "--porcelain").decode("utf-8").splitlines() if l.strip()]
        if dirty:
            out("[FAIL] 工作区存在未提交改动 → 两条通道可能推不同内容（阻塞推送）")
            out("       本体通道按 commit 内容推、工具通道按工作区文件推，二者必须同源。")
            for l in dirty[:12]:
                out("         " + l)
            if len(dirty) > 12:
                out("         …（共 %d 项）" % len(dirty))
            out("       请先 `git commit`，或显式加 --allow-dirty 承担该风险。")
            if a.result_file:
                save_result(a.result_file, lines, root)
            return 1

    plan = build_plan(root, a.base, a.head)   # push 只支持 rev 模式：本体通道按 commit 范围推
    n_fail = print_plan(plan, out)
    if n_fail:
        code_summary = "[FAIL] 交叉校验 %d 项失败 → 阻塞推送（规则：先补齐标注与登记再推）" % n_fail
        out(code_summary)
        if a.result_file:
            save_result(a.result_file, lines, root)
        return 1

    py = sys.executable
    rc = 0

    # ---- 通道 1：本体 ----
    out("── 通道 1/2：本体 → %s ──" % ONTOLOGY_REPO)
    cmd = [py, str(HERE / "push_ontology.py"), "--base", a.base, "--head", a.head,
           "--repo", a.ontology_repo, "--branch", a.ontology_branch, "--root", str(root)]
    if a.apply:
        cmd.append("--apply")
    if a.allow_delete:
        cmd.append("--allow-delete")
    if a.expect_remote:
        cmd += ["--expect-remote", a.expect_remote]
    if a.result_file:
        cmd += ["--result-file", a.result_file + ".ontology.txt"]
    r1 = run(cmd)
    out("  退出码 = %d" % r1)
    out("")
    if r1 != 0:
        rc = 1
        out("[FAIL] 本体通道失败/阻塞 → 不再继续自研工具通道（避免半推送状态扩大）")
        if a.result_file:
            save_result(a.result_file, lines, root)
        return rc

    # ---- 通道 2：自研工具 ----
    out("── 通道 2/2：自研工具 → %s ──" % TOOLS_REPO)
    if not plan["selftools"]:
        out("  本次改动面无自研工具文件，跳过。")
    else:
        for rel in plan["selftools"]:
            # ⚠️ publish_tools.py 的 --repo / --branch 是**顶层**参数，必须放在子命令**之前**；
            # 放在 push 之后会被 argparse 判 unrecognized → 退出码 2（v3.2.0 实测踩到：
            # dry-run 阶段就暴露了，没有等到真写远端才发现）。
            cmd = [py, str(HERE / "publish_tools.py"),
                   "--repo", a.tools_repo, "--branch", a.tools_branch,
                   "push", "--file", str(root / rel), "--dest", rel]
            if a.apply:
                cmd.append("--apply")
            if a.message:
                cmd += ["--message", a.message]
            r = run(cmd)
            out("  退出码 = %d" % r)
            if r != 0:
                rc = 1
    out("")

    if rc == 0:
        out("分流推送完成：本体 %d 项 → %s ｜ 自研工具 %d 项 → %s"
            % (len(plan["ontology"]), ONTOLOGY_REPO, len(plan["selftools"]), TOOLS_REPO))
        if not a.apply:
            out("（当前是 DRY-RUN，远端未发生任何写入；加 --apply 生效）")
    else:
        out("[FAIL] 存在失败通道，详见上方输出。")
    if a.result_file:
        save_result(a.result_file, lines, root)
    return rc


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="push_router.py",
        description="两类资源的分流推送路由（本体 → ai-workflow-skill ｜ 自研工具 → ai-workflow-tools）",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    # 参数刻意放在子命令上（不放顶层）：否则子命令的默认值会覆盖顶层值，
    # 且 `push --root X` 这种自然写法会被 argparse 拒绝。
    pc = sub.add_parser("classify", help="离线分类 + 交叉校验（不联网、不写远端）")
    pc.add_argument("--root", help="本地 git 根（技能目录；默认自动探测）")
    pc.add_argument("--base", help="起始 rev（不含）")
    pc.add_argument("--head", help="结束 rev（含）")
    pc.add_argument("--files", help="显式文件列表，逗号分隔（相对技能根或绝对路径）")
    pc.add_argument("--result-file", help="输出落盘路径")

    pp = sub.add_parser("push", help="分流推送两仓（默认 dry-run，--apply 才写）")
    pp.add_argument("--root", help="本地 git 根（技能目录；默认自动探测）")
    pp.add_argument("--base", required=True)
    pp.add_argument("--head", required=True)
    pp.add_argument("--apply", action="store_true", help="真正推送（默认 dry-run）")
    pp.add_argument("--allow-delete", action="store_true", help="允许本体通道删除远端文件")
    pp.add_argument("--allow-dirty", action="store_true",
                    help="允许工作区有未提交改动时仍推送（默认阻塞：两条通道会推不同内容）")
    pp.add_argument("--expect-remote", help="要求本体远端 HEAD 等于该 sha，否则 FAIL")
    pp.add_argument("--ontology-repo", default=ONTOLOGY_REPO)
    pp.add_argument("--ontology-branch", default=ONTOLOGY_BRANCH)
    pp.add_argument("--tools-repo", default=TOOLS_REPO)
    pp.add_argument("--tools-branch", default=TOOLS_BRANCH)
    pp.add_argument("--message", help="提交信息（透传给两条通道）")
    pp.add_argument("--result-file", help="输出落盘路径")

    a = ap.parse_args()
    if a.cmd == "classify":
        if not a.files and not (a.base and a.head):
            ap.error("classify 需要 --base/--head，或用 --files 指定文件列表")
        a.files = [s.strip() for s in a.files.split(",")] if a.files else None
    return cmd_classify(a) if a.cmd == "classify" else cmd_push(a)


if __name__ == "__main__":
    sys.exit(main())
