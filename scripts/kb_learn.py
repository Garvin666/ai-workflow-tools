#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# [自研工具] kb_learn.py
# 用途：学习引擎——把 learning-judge 判定出的候选知识条目做「证据计数 → 去重 → 门槛判定 → 两段式入库」，
#       长期知识结构化写入个人知识库（~/.workbuddy/kb/），使后续任务经 KB-First 检索吃到历史经验。
#       另含三件「知识生命周期」能力：**冲突登记冻结**（P0-1）／**废止提案**（P0-2）／**证据固化**（P0-3）。
# 适用场景：ai-workflow 阶段 6 收尾的「学习循环」（K2 累积 / K3 蒸馏 / K4 校验 / K5 入库）的唯一机器实现。
# 作者：ai-workflow 自研（技能增强-ai-workflow-v4.8.0-2026-09-25，2026-09-25；
#       P0 修复：学习模型开源对标与优化-2026-09-25，2026-09-25）
# 仓库：https://github.com/Garvin666/ai-workflow-tools
"""kb_learn.py —— 学习模型（learning-model）的**确定性算术半边**

【定位：务必按此理解，勿误用】
  本脚本**不做识别** —— 不从自由文本猜「这条算不算偏好」「置信多少」。类型与置信由
  模型/人给出（`note --kind --confidence`），或由 `judge` 从 V1–V5 维度分**按确定性公式**聚合而来。
  这与 `homework_model.py` 是同一条纪律：**语义归模型/人，算术归代码**。

【它对应手册的哪几段】
  · K3 蒸馏 → 池内同 key 合并 + 可选库内去重（`--kb-check`）
  · K4 校验 → 门槛判定（证据计数 / 置信 / 敏感 / 重复 / 冲突 / 精炼完整）
  · K5 入库 → 两段式：达门槛自动入 KB，其余留池标 `观察`；写入默认 dry-run，`--apply` 才落盘
  全文见 `references/learning-model.md`；判定契约见 `references/learning-judge.md`。

【三条「知识生命周期」能力（P0，2026-09-25 开源对标后补）—— 共同原则：**按危险度分级，而非只按置信度**】
  · P0-1 **冲突登记与冻结**（`note --contradicts`）：「两条是否矛盾」仍归模型判（语义），
    脚本只做**确定性登记** —— 双向标 `冲突` 并**冻结双方**，使之**永不自动进 KB**，等用户裁决。
    ⚠️ 修的是本模块此前最严重的「声称有、实际不可达」：`evaluate()` 的冲突分支曾要求
    `state == "冲突"`，而全程序**零处**能把它置为「冲突」（循环依赖 = 死代码）⇒
    两条互相矛盾的结论会**同时被判「入库」**。取证见
    `tasks/学习模型开源对标与优化-2026-09-25/tmp/probe_findings.txt`。
  · P0-2 **废止走提案**（`note --supersedes` + `retire`）：新增/更新可自动（按置信度），
    **废止/删除一律只提案**（按危险度）。放行后 KB 侧**只加废止标记、绝不删除**（保审计链）。
  · P0-3 **证据固化**（`note --evidence`）：证据原文片段随候选落池 + 追加进
    `archive/<YYYY-MM>.jsonl` ⇒ 原任务目录被归档/清理后，证据仍可复核。
    ⚠️ 快照**同样过敏感扫描**，命中即**整条驳回、不落池也不落档**。

【诚实边界（与手册 §7 同一条纪律）】
  · 阈值 `MIN_EVIDENCE` / `AUTO_COMMIT` / `DUP_THRESHOLD` 与锚点 `ANCHORS` / 温度 `TAU` 都是
    **未拟合先验** ⇒ 不得据此声称「学习准确率 X%」，也不得作为阈值依据。
  · 门槛只保证「同一条重复出现足够多次」，**不保证它是对的** —— 反复出现的偏见会被学成"偏好"，
    这是本模块的结构性风险，只能靠两段式（高置信自动、其余人工）与定期复查缓解。
  · **池内去重仍是字面级**（`sha1(normalize(text))`）：同义改写各成一条，会**稀释证据计数**
    （同一偏好换个措辞就各自够不到 `偏好 ≥2` 门槛）。语义近邻检查属 P1（复用 `kb.py retrieve`），
    本版**未做** —— 不要以为它已经有了。
  · 「语义归类是否判对」**无机器判据**；本脚本只做结构、计数、去重与阈值。
  · **本脚本不产生样本**：没有真实交互留痕时池为空、无候选。
  · **本脚本绝不改写技能本体**（SKILL.md / references/）—— 那是不可逆动作，须用户确认。
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

# ─────────────────────────── 常量（**数值唯一源，手册与 judges.json 均不复制**） ───────────────────────────
KB_KINDS = ("偏好", "领域知识", "流程坑", "事实", "无")
KB_DIMS = ("V1", "V2", "V3", "V4", "V5")
KB_COMBINE_DELTA = 0.15          # top2 概率差 < 该值 → 组合态（`无` 不参与组合）
KB_TAU = 0.055                   # softmax 温度（未拟合先验）
# ⚠️ 参数块的两轮返修史（**这一整块是本模块最容易被"调着看"毁掉的地方，故把历史写死在这里**）：
#   轮 1 —— τ 初值 0.35：实测使**五类 softmax 的 max 结构上不可达 0.70** ⇒ band 恒为 `hold`
#     ⇒ 整条学习管线**永不吸收任何条目**（不是"保守"，是**结构性失效**：功能永远不触发）。
#   轮 2 —— τ 改 0.08（仅调温度）：`偏好` 锚点达标，但 `领域知识`/`流程坑` 仍只有 0.846（mid）
#     ⇒ 暴露**第二因不是温度而是锚点间距** —— "领域知识"与"流程坑"在 V1–V5 上几乎重合
#       （归一化距离仅 0.153），softmax 无论 τ 多小都分不开这两类。
#   修法（本轮）：① 拉开锚点 —— 把这两类在 **V3 纠正强度**（0.10 vs 0.45）与
#     **V4 缺口度**（0.95 vs 0.30）上强分开（语义上也成立：踩坑多伴随用户当场指出，
#     而"领域知识"多是自己实测归纳、不在既有文档覆盖内的）；② τ 取 0.055。
#   判据（可直接复算）：要 max(distribution) ≥ 0.95 需 `d₂ − d₁ ≥ 3.5τ`；
#     重设后**最近锚点对距离 = 0.289**（事实↔无）⇒ τ ≤ 0.0825；取 0.055 留余量。
#     同时"无信息输入（全 0.5）"的 (d₂−d₁) ≈ 0.039 ⇒ max ≈ 0.33 ⇒ 稳落 `hold`（预期行为）。
#   ⚠️ **为什么把这段写进代码而不是只写手册**：参数失配的症状是"**永远走兜底分支**"，
#     它伪装成"运行正常"（无报错、无 FAIL、只是什么都不吸收）。故 `selftest` 必须配一条
#     **结构性断言**（"落在锚点上的输入必须达 auto 带"）—— 见 cmd_selftest ③′。
#     同理，本技能在 `homework-judge.md` §5.1 也记过"参数与样例同源"的循环风险。
KB_ANCHORS = {                   # 各类型在 V1–V5 上的理想位置（未拟合先验）
    "偏好":     (0.90, 0.90, 0.90, 0.50, 0.90),
    "领域知识": (0.85, 0.75, 0.10, 0.95, 0.90),
    "流程坑":   (0.90, 0.60, 0.45, 0.30, 0.90),
    "事实":     (0.50, 0.30, 0.05, 0.40, 0.35),
    "无":       (0.10, 0.05, 0.02, 0.05, 0.08),
}
KB_AUTO_COMMIT = 0.85            # 自动入库的置信门槛
KB_DUP_THRESHOLD = 0.86          # 库内去重相似度门槛
KB_DEFAULT_MIN_EVIDENCE = 1      # 通用最低证据次数
KB_MIN_EVIDENCE = {"偏好": 2}    # 类型级覆盖：偏好门槛更高（单次极可能是一次性情境）
KB_AUTO_BAND = 0.95              # 自采信带（与契约 §5 同源）
KB_MID_BAND = 0.70
KB_SAMPLE_BAND = 0.95            # 检查项 21 抽样人审带（与 checks_core.LEARNING_SAMPLE_BAND 同源）

# ── P0-1/P0-2/P0-3 的三件「知识生命周期」常量 ──
KB_CONFLICT_STATE = "冲突"       # P0-1：冲突登记后的**冻结**态（双向，永不自动入库）
KB_RETIRE_PENDING = "待废止"     # P0-2：废止提案待放行 —— **不写 KB、不改旧文档**
KB_RETIRED_STATE = "已废止"      # P0-2：放行后的终态（KB 侧只加标记，**绝不删除**）
KB_RETIRE_TAG = "已废止"         # P0-2：放行时给 KB 文档补的标签（便于 `kb.py list` 一眼看全）
KB_RETIRE_MARK = "⚠️ 已废止"     # P0-2：写进正文首行的废止标记
KB_EVIDENCE_MAX = 800            # P0-3：单条证据快照字符上限（截断保长度，不是丢弃整条）
KB_EVIDENCE_KEEP = 8             # P0-3：池内保留的最新快照条数（**全量在 archive/**，append-only）
_KEY_RE = re.compile(r"^[0-9a-f]{12}$")

KB_ROUTE_HINT = {                # 完整措辞见 references/learning-judge.md §5
    "偏好": "进 K2 → 累积池（证据门槛最高）→ 达门槛自动入 KB；**不改技能本体**",
    "领域知识": "进 K2 → 池 → 达门槛自动入 KB（category=学习-领域知识）",
    "流程坑": "进 K2 → 池 → 达门槛自动入 KB；另建议同步 quality-gates 反模式清单（走第 6 路，须用户确认）",
    "事实": "进 K2 → 池；**先查 V5**：时效性事实改记工作区 memory，不入 KB",
    "无": "不进池、不产候选；在结论里写明「本任务无可学信号」",
}

# 敏感模式（**驳回路径**：不入池、不入库、不落档，只报规则名、不打印原文）
KB_SENSITIVE_PATTERNS = (
    ("私钥", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("GitHub 令牌", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("OpenAI 风格密钥", re.compile(r"\bsk-[A-Za-z0-9]{16,}\b")),
    ("AWS AccessKey", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Slack 令牌", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("邮箱", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]{2,}\b")),
    ("手机号", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    ("身份证号", re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")),
)

KB_HEADER = "学习沉淀"


# ─────────────────────────── 通用工具 ───────────────────────────
def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def candidate_kb_roots() -> list:
    """KB 根的候选路径（**去重保序**）。

    ⚠️ 为什么不能只写 `Path.home()`：本机实测 `Path.home()` 由 **`USERPROFILE`** 决定
    （= `E:\\WBHomes\\B`，即 WorkBuddy 的会话 home），而个人知识库实际在
    **`~/.workbuddy/kb`**（1866 篇文档）—— 两者不同。
    只认 `Path.home()` 会让 KB 恒判"不可用"，**整条学习管线永远无法入库**
    （与"τ 过大 ⇒ band 恒 hold"同型的**功能性失效**：不报错、只是永远不做）。
    故取候选探测：`HOME` → `USERPROFILE` → `Path.home()`，**取第一个真含 `kb.py` 的**。
    """
    out, seen = [], set()
    for env in ("HOME", "USERPROFILE"):
        v = os.environ.get(env)
        if v:
            out.append(Path(v) / ".workbuddy" / "kb")
    out.append(Path.home() / ".workbuddy" / "kb")
    uniq = []
    for c in out:
        k = str(c).lower()
        if k not in seen:
            seen.add(k)
            uniq.append(c)
    return uniq


def discover_kb_root() -> Path:
    """探测 KB 根：第一个含 `kb.py` 的候选；**都不含则回退首个候选**（由调用方 fail-closed）。"""
    cands = candidate_kb_roots()
    for c in cands:
        if (c / "kb.py").exists():
            return c
    return cands[0]


def default_kb_root() -> Path:
    return discover_kb_root()


def default_pool_dir() -> Path:
    """累积池跟随 KB 根（`<kb_root>/learning`）—— 两者不分裂，避免池与库各在一处。"""
    return discover_kb_root() / "learning"


def normalize(text: str) -> str:
    """归一化：去掉空白/标点与全半角差异，折叠大小写。

    `key` 的稳定性是去重的地基 —— 同一句话换标点不换含义，必须落成同一个 key，
    否则池会以「表述差异」为名无限膨胀（这正是「去重」要防的第一件事）。
    ⚠️ 但它是**字面级**的：同义改写（"别加 emoji" vs "不要用表情符号"）仍各成一条。
    语义级归并属 P1（复用 `kb.py retrieve` 做近邻提示），**本版未做**。
    """
    t = str(text or "")
    t = t.replace("　", " ")
    t = re.sub(r"[\s]+", "", t)
    t = re.sub(r"[，。、；：！？,.;:!?\"'“”‘’（）()\[\]【】{}<>《》\-—_/\\|~`]+", "", t)
    t = t.translate(str.maketrans("０１２３４５６７８９ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ",
                                  "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"))
    return t.lower()


def cand_key(text: str) -> str:
    return hashlib.sha1(normalize(text).encode("utf-8")).hexdigest()[:12]


def resolve_pool_key(rows: list, ref: str) -> tuple:
    """把 `--contradicts` / `--supersedes` 的 `<text|key>` 引用解析成**池内已存在的 key**。

    返回 `(key|None, 错误|None)`。**引用不到就报错，绝不静默新建孤儿条目** ——
    若允许引用不存在的目标，"冲突冻结"会变成"给不存在的条目打标签"，
    看起来成功、实际什么都没冻（这正是本模块要根除的那类**功能性失效**）。

    两种写法都收：**12 位十六进制 key**（精确）或**条目原文**（按 `cand_key` 归一后匹配）。
    """
    s = str(ref or "").strip()
    if not s:
        return None, "空引用"
    k = s.lower() if _KEY_RE.match(s.lower()) else cand_key(s)
    if any(r.get("key") == k for r in rows):
        return k, None
    return None, "池中不存在该条目（key=%s）—— 先 note 它，或直接给 12 位 key" % k


def scan_sensitive(text: str) -> list:
    """返回命中的敏感规则名（**不返回原文** —— 防二次泄露）。"""
    hits = []
    for name, pat in KB_SENSITIVE_PATTERNS:
        if pat.search(str(text or "")):
            hits.append(name)
    return hits


def scan_sensitive_many(texts) -> list:
    """多条文本合并扫描（**证据快照也必须过这一关** —— P0-3 的关键约束）。去重保序。"""
    out = []
    for t in texts:
        for h in scan_sensitive(t):
            if h not in out:
                out.append(h)
    return out


def parse_dims(s: str) -> dict:
    out = {}
    for part in str(s or "").split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise ValueError("--dims 每项须形如 V1=0.9，实际：%r" % part)
        k, v = part.split("=", 1)
        k = k.strip().upper()
        if k not in KB_DIMS:
            raise ValueError("--dims 键须为 %s，实际：%r" % ("/".join(KB_DIMS), k))
        out[k] = float(v)
    miss = [d for d in KB_DIMS if d not in out]
    if miss:
        raise ValueError("--dims 缺 %s" % "/".join(miss))
    return out


def min_evidence_of(kind: str) -> int:
    main = str(kind or "").split("+")[0].strip()
    return KB_MIN_EVIDENCE.get(main, KB_DEFAULT_MIN_EVIDENCE)


def needs_learning_of(kind: str) -> bool:
    """派生口径（**唯一实现**，与检查项 20 同源）：主类型 ≠ `无`。"""
    return str(kind or "").split("+")[0].strip() != "无"


def _combine(dist: dict, delta: float) -> str:
    """top2 差 < delta → `A+B`；**`无` 不参与组合**（"没有"与"有"并列无意义）。"""
    ordered = sorted(dist.items(), key=lambda kv: (-kv[1], kv[0]))
    top, second = ordered[0], ordered[1]
    if (top[1] - second[1]) < delta and "无" not in (top[0], second[0]):
        return "%s+%s" % (top[0], second[0])
    return top[0]


def aggregate(dims: dict, ambiguous: bool = False) -> dict:
    """V1–V5 → 判定字段（与检查项 20 的必填项逐字段同构）。

    公式即 `references/learning-judge.md` §5.1（唯一口径源）：锚点距离 → softmax → 组合取值。
    """
    v = [float(dims[d]) for d in KB_DIMS]
    ds = {}
    for k in KB_KINDS:
        a = KB_ANCHORS[k]
        s = sum((v[i] - a[i]) ** 2 for i in range(len(KB_DIMS)))
        ds[k] = (s ** 0.5) / (len(KB_DIMS) ** 0.5)
    mx = max(ds.values())
    exps = {k: pow(2.718281828459045, -(d - mx) / KB_TAU) for k, d in ds.items()}
    tot = sum(exps.values())
    dist = {k: round(exps[k] / tot, 4) for k in KB_KINDS}
    # round 后可能有 ±2e-4 残差，把残差打到最大项，保证「五键之和 = 1」这条结构判据恒成立
    top = max(dist, key=lambda k: dist[k])
    dist[top] = round(dist[top] + (1.0 - sum(dist.values())), 4)
    kind = _combine(dist, KB_COMBINE_DELTA)
    conf = max(dist.values())
    if ambiguous or conf < KB_MID_BAND:
        band = "hold"
    elif conf >= KB_AUTO_BAND:
        band = "auto"
    else:
        band = "mid"
    return {
        "kind": kind,
        "distribution": dist,
        "confidence": conf,
        "dimensions": {d: float(dims[d]) for d in KB_DIMS},
        "ambiguity": bool(ambiguous),
        "route_hint": KB_ROUTE_HINT.get(kind.split("+")[0], KB_ROUTE_HINT["无"]),
        "needs_learning": needs_learning_of(kind),
        "_model": {"anchors_fitted": False, "tau": KB_TAU, "band": band,
                   "combine_kind_delta": KB_COMBINE_DELTA},
    }


# ─────────────────────────── 累积池（跨会话承载） ───────────────────────────
def _pool_paths(pool_dir: Path) -> dict:
    return {
        "pool": pool_dir / "pool.jsonl",
        "decisions": pool_dir / "decisions.jsonl",
        "state": pool_dir / "state.json",
        "retire": pool_dir / "retire_proposals.jsonl",   # P0-2：废止提案（append-only 事件流）
        "archive_dir": pool_dir / "archive",             # P0-3：证据固化归档（append-only）
    }


def load_pool(pool_dir: Path) -> list:
    """读池。**坏行跳过并计数**（不静默丢弃 —— 调用方负责报出跳过数）。"""
    p = _pool_paths(pool_dir)["pool"]
    rows, bad = [], 0
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                bad += 1
    for r in rows:
        r.setdefault("sources", [])
        r.setdefault("evid_count", 1)
        r.setdefault("state", "观察")
        r.setdefault("evidence", [])        # P0-3：证据快照（旧池无此字段 → 补空）
        r.setdefault("conflict_with", [])   # P0-1：互指的冲突对手
    return rows, bad


def save_pool(pool_dir: Path, rows: list) -> None:
    pool_dir.mkdir(parents=True, exist_ok=True)
    p = _pool_paths(pool_dir)["pool"]
    p.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    st = _pool_paths(pool_dir)["state"]
    st.write_text(json.dumps({
        "updated_at": now_iso(),
        "total": len(rows),
        "thresholds": {"min_evidence": KB_MIN_EVIDENCE,
                       "default_min_evidence": KB_DEFAULT_MIN_EVIDENCE,
                       "auto_commit": KB_AUTO_COMMIT, "dup_threshold": KB_DUP_THRESHOLD,
                       "evidence_max": KB_EVIDENCE_MAX, "fitted": False},
    }, ensure_ascii=False, indent=2), encoding="utf-8")


def log_decision(pool_dir: Path, rec: dict) -> None:
    pool_dir.mkdir(parents=True, exist_ok=True)
    with open(_pool_paths(pool_dir)["decisions"], "a", encoding="utf-8") as f:
        f.write(json.dumps({"time": now_iso(), **rec}, ensure_ascii=False) + "\n")


# ─────────────────────────── P0-3：证据固化 ───────────────────────────
def _snapshot_entries(evidence: list, sources: list) -> tuple:
    """构造 P0-3 的证据快照条目。返回 `(entries, kind)`。

    有 `--evidence` ⇒ 固化**原文片段**（每条截断到 `KB_EVIDENCE_MAX`）；
    没有 ⇒ 退化为 `--source` 的**指针**，并**如实标 `kind=指针`**
    （不把指针冒充成原文 —— 那条区分就是"证据固化"到底有没有做到的判据）。
    """
    out = []
    src = list(evidence) if evidence else list(sources)
    kind = "原文" if evidence else ("指针" if sources else "无")
    for e in src:
        s = str(e)
        out.append({"at": now_iso(), "kind": kind,
                    "text": s[:KB_EVIDENCE_MAX], "truncated": len(s) > KB_EVIDENCE_MAX})
    return out, kind


def _cap_evidence(lst: list) -> list:
    """池内只留**最新** `KB_EVIDENCE_KEEP` 条；**全量在 `archive/<YYYY-MM>.jsonl`**（append-only）。"""
    if len(lst) <= KB_EVIDENCE_KEEP:
        return lst
    return lst[-KB_EVIDENCE_KEEP:]


def _append_archive(pool_dir: Path, rec: dict) -> str:
    """把证据固化记录追加进 `archive/<YYYY-MM>.jsonl`（**append-only，永不改写**）。

    ⚠️ 调用方**必须**已过敏感扫描 —— 归档是把原文落盘，命中凭据时**不得落档**。
    ⚠️ 归档在 `~/.workbuddy/kb/learning/` 下（基础设施例外），**永不推入任何 git 仓**。
    """
    d = _pool_paths(pool_dir)["archive_dir"]
    d.mkdir(parents=True, exist_ok=True)
    fp = d / (datetime.now().strftime("%Y-%m") + ".jsonl")
    with open(fp, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return str(fp)


# ─────────────────────────── P0-2：废止提案 ───────────────────────────
def load_retire_proposals(pool_dir: Path) -> list:
    """读废止提案：**按 id 折叠，后写覆盖前写**。

    文件是 **append-only 事件流**（登记一条、放行再追加一条），故"当前状态"= 同 id 的最后一条；
    这样既拿到状态，又**保住审计链**（谁在什么时候提案/放行的都还在）。
    """
    p = _pool_paths(pool_dir)["retire"]
    latest, order = {}, []
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            i = rec.get("id")
            if not i:
                continue
            if i not in latest:
                order.append(i)
            latest[i] = rec
    out = []
    for i in order:
        r = dict(latest[i])
        r.setdefault("status", "待放行")
        out.append(r)
    return out


def retire_proposal_id(old_key: str, new_key: str) -> str:
    return hashlib.sha1(("%s>%s" % (old_key, new_key)).encode("utf-8")).hexdigest()[:8]


def propose_retire(pool_dir: Path, old_key: str, new_key: str) -> dict:
    """登记一条废止提案（**同一 (旧,新) 只提一次**）。

    ⚠️ **本函数绝不触碰 KB** —— 这是 P0-2 的全部要点：登记 ≠ 执行。
    真正执行只在 `apply_retire(..., apply=True)`，且**只能由用户显式放行触发**。

    ⚠️ **已决提案不自动重开**：同 id 一旦存在（无论 `待放行` / `已放行` / `已驳回`），
    后续 `commit` 只回报其现状、**不再追加记录、不把它拉回 `待废止`**。
    否则会出现两种机器"擅自改变用户决定"的行为：驳回后下次 commit 又自动提议（反复骚扰），
    或放行后下次 commit 把它拉回待废止。**要重开须人工**（清掉该 id 的记录）。
    """
    pid = retire_proposal_id(old_key, new_key)
    for pr in load_retire_proposals(pool_dir):
        if pr.get("id") == pid:
            return {"id": pid, "dedup": True, "status": pr.get("status", "待放行")}
    rec = {"id": pid, "old_key": old_key, "new_key": new_key,
           "old_kb_id": "learn-" + old_key, "new_kb_id": "learn-" + new_key,
           "status": "待放行", "proposed_at": now_iso()}
    pool_dir.mkdir(parents=True, exist_ok=True)
    with open(_pool_paths(pool_dir)["retire"], "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return {"id": pid, "dedup": False, "status": "待放行"}


def apply_retire(kb_root: Path, pool_dir: Path, rows: list, prop: dict, apply: bool) -> dict:
    """放行一条废止提案：KB 侧**加废止标记 + 打标签，绝不删除**；池侧置终态。

    ⚠️ 原则（对齐 `tigerless-labs/agent-memory` 的 "updating never destroys"）：
    **废止是「标记」不是「删除」** —— 保审计链，也让"曾经学错过什么"可回溯。
    这也是"按危险度分级"的落点：删除不可逆 ⇒ 连废止都只走提案，更不用说删了。
    """
    old_key = prop.get("old_key")
    new_key = prop.get("new_key")
    old_id = prop.get("old_kb_id") or ("learn-" + str(old_key))
    old_row = next((r for r in rows if r.get("key") == old_key), None)
    if old_row is None:
        return {"id": prop.get("id"), "action": "跳过",
                "reason": "池中已无该条目（可能已被清理）—— 提案留档，不动作"}
    if str(old_row.get("state")) == KB_RETIRED_STATE:
        return {"id": prop.get("id"), "action": "已废止（幂等跳过）", "kb_id": old_id}
    if not apply:
        return {"id": prop.get("id"), "action": "将废止", "kb_id": old_id,
                "kb_exists": kb_exists(kb_root, old_id),
                "note": "dry-run：KB 侧未改动、提案状态未推进、池未改写"}
    # —— 以下为真正放行的动作 ——
    kb_note = "KB 侧未动（该文档不存在或 KB 不可用）"
    if kb_exists(kb_root, old_id):
        cur, err = _kb_get_content(kb_root, old_id)
        if cur is None:
            # 回退：用池条目重渲染 —— 这条 KB 文档本就是本脚本按 render_content 写入的
            cur = render_content(old_row)
            kb_note = "KB 正文读取失败（%s）⇒ 回退用池条目重渲染" % err
        else:
            kb_note = "KB 正文读取成功，按「原正文 + 废止标记」追加"
        rc, out = kb_run(kb_root, ["update", "--id", old_id, "--content",
                                   _retire_marked_content(cur, new_key)])
        kb_run(kb_root, ["tag", "--id", old_id, "--add", KB_RETIRE_TAG])
        if rc != 0:
            kb_note += "；⚠️ update 返回 rc=%r：%s" % (rc, str(out)[:120])
        else:
            kb_note += "；并打标签『%s』（**未删除**）" % KB_RETIRE_TAG
    old_row["state"] = KB_RETIRED_STATE
    old_row["retired_at"] = now_iso()
    old_row["superseded_by"] = [new_key] if new_key else []
    save_pool(pool_dir, rows)
    pool_dir.mkdir(parents=True, exist_ok=True)
    with open(_pool_paths(pool_dir)["retire"], "a", encoding="utf-8") as f:
        f.write(json.dumps({"id": prop.get("id"), "old_key": old_key, "new_key": new_key,
                            "old_kb_id": old_id, "new_kb_id": prop.get("new_kb_id"),
                            "status": "已放行", "resolved_at": now_iso()},
                           ensure_ascii=False) + "\n")
    log_decision(pool_dir, {"key": old_key, "action": "废止",
                            "reason": "提案 %s 放行：%s" % (prop.get("id"), kb_note),
                            "kb_id": old_id})
    return {"id": prop.get("id"), "action": "已废止", "kb_id": old_id, "kb_note": kb_note}


def reject_retire(pool_dir: Path, rows: list, prop: dict) -> dict:
    """驳回一条废止提案：旧条目**恢复原状态**（有 `kb_id` 即回到 `已入库`，否则 `观察`）。"""
    old_key = prop.get("old_key")
    old_row = next((r for r in rows if r.get("key") == old_key), None)
    pool_dir.mkdir(parents=True, exist_ok=True)
    with open(_pool_paths(pool_dir)["retire"], "a", encoding="utf-8") as f:
        f.write(json.dumps({"id": prop.get("id"), "old_key": old_key,
                            "new_key": prop.get("new_key"),
                            "old_kb_id": prop.get("old_kb_id"),
                            "new_kb_id": prop.get("new_kb_id"),
                            "status": "已驳回", "resolved_at": now_iso()},
                           ensure_ascii=False) + "\n")
    if old_row is None:
        return {"id": prop.get("id"), "action": "已驳回", "note": "池中已无该条目"}
    old_row["state"] = "已入库" if old_row.get("kb_id") else "观察"
    old_row.pop("retire_proposed_by", None)
    save_pool(pool_dir, rows)
    log_decision(pool_dir, {"key": old_key, "action": "驳回废止",
                            "reason": "提案 %s 被驳回，旧条目恢复 state=%s"
                                      % (prop.get("id"), old_row["state"]), "kb_id": None})
    return {"id": prop.get("id"), "action": "已驳回", "restored_state": old_row["state"]}


# ─────────────────────────── KB 适配层 ───────────────────────────
def kb_script(kb_root: Path) -> Path:
    return kb_root / "kb.py"


def kb_run(kb_root: Path, args: list, timeout: int = 120):
    """调 kb.py。返回 (returncode, stdout)；kb.py 缺失 → (None, None) 由调用方 fail-closed。"""
    py = kb_script(kb_root)
    if not py.exists():
        return None, None
    try:
        cp = subprocess.run([sys.executable, str(py)] + args, capture_output=True,
                            text=True, encoding="utf-8", errors="replace", timeout=timeout)
        return cp.returncode, (cp.stdout or "") + (cp.stderr or "")
    except (OSError, subprocess.SubprocessError) as e:
        return None, str(e)


def kb_exists(kb_root: Path, doc_id: str) -> bool:
    rc, out = kb_run(kb_root, ["get", "--id", doc_id])
    return rc == 0


def _kb_get_content(kb_root: Path, doc_id: str) -> tuple:
    """读 KB 文档**正文**。返回 `(content|None, err|None)`。

    ⚠️ `kb.py get` 的输出头部是**固定 4 行**（`# 标题` / `id=… 分类=… 标签=…` /
    `创建=… 更新=…` / 空行），由 `kb.py` 的 `main()` 打印顺序决定（2026-09-25 核对源码）。
    故正文 = 第 5 行起。**解析不出来就返回 None 并由调用方回退重渲染** —— 不猜、不截错。
    """
    rc, out = kb_run(kb_root, ["get", "--id", doc_id], timeout=60)
    if rc != 0 or not out:
        return None, "kb.py get 失败（rc=%r）" % (rc,)
    lines = out.splitlines()
    if len(lines) < 4 or not lines[0].startswith("# "):
        return None, "kb.py get 输出头部形状不符（预期 4 行头部，实得 %d 行）" % len(lines)
    return "\n".join(lines[4:]), None


def _retire_marked_content(old_content: str, new_key) -> str:
    """给正文首行加废止标记（**幂等**：已标记则原样返回，不重复叠加）。"""
    body = old_content or ""
    if body.lstrip().startswith(KB_RETIRE_MARK):
        return body
    mark = "%s（%s，替代条目 learn-%s）" % (KB_RETIRE_MARK, now_iso(), new_key or "（未记录）")
    return mark + "\n\n" + body


def kb_write(kb_root: Path, kb_id: str, title: str, category: str, tags: list,
             content: str, apply: bool) -> str:
    """写入或更新一条 KB 文档（幂等：同 id 复用）。返回 '新增' / '更新' / '将新增' / '将更新'。"""
    exists = kb_exists(kb_root, kb_id)
    if not apply:
        return "将更新" if exists else "将新增"
    if exists:
        kb_run(kb_root, ["update", "--id", kb_id, "--title", title,
                         "--category", category, "--tags", ",".join(tags), "--content", content])
        return "更新"
    kb_run(kb_root, ["add", "--title", title, "--category", category,
                     "--tags", ",".join(tags), "--content", content, "--id", kb_id])
    return "新增"


def render_content(cand: dict) -> str:
    """K3 精炼骨架（结构固定 —— 「适用边界」必填，否则不达精炼完成）"""
    return (
        "【结论】%s\n\n"
        "【依据】%s\n\n"
        "【证据】%d 次（首见 %s，末见 %s）\n\n"
        "【适用边界】%s\n"
        % (cand["text"],
           "；".join(cand["sources"]) or "（未记录来源）",
           int(cand.get("evid_count", 1)),
           cand.get("first_seen", "?"), cand.get("last_seen", "?"),
           cand.get("boundary") or "（未声明）")
    )


def kb_search_duplicate(kb_root: Path, text: str) -> tuple:
    """库内去重（可选）：调 KB-First 同一引擎检索，取最高分。返回 (是否重复, 分数, 命中 id)。"""
    rc, out = kb_run(kb_root, ["retrieve", "--query", text[:200], "--top-k", "3", "--json"])
    if rc != 0 or not out:
        return None, None, None
    try:
        data = json.loads(out[out.index("{"):out.rindex("}") + 1])
    except (ValueError, IndexError):
        return None, None, None
    items = data.get("results") or data.get("items") or []
    best, bid = 0.0, None
    for it in items:
        sc = float(it.get("score") or 0.0)
        if sc > best:
            best, bid = sc, it.get("id")
    return (best >= KB_DUP_THRESHOLD), best, bid


# ─────────────────────────── 子命令 ───────────────────────────
def cmd_judge(args) -> int:
    try:
        dims = parse_dims(args.dims)
    except ValueError as e:
        print("[ERROR] %s" % e, file=sys.stderr)
        return 2
    out = aggregate(dims, ambiguous=args.ambiguous)
    if args.plan_block:
        body = {k: v for k, v in out.items() if not k.startswith("_")}
        print(json.dumps(body, ensure_ascii=False))
    else:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def cmd_note(args) -> int:
    kind = args.kind.strip()
    parts = [p.strip() for p in kind.split("+")]
    if any(p not in KB_KINDS for p in parts):
        print("[ERROR] --kind 只允许 %s 及其 '+' 组合" % "/".join(KB_KINDS), file=sys.stderr)
        return 2
    if "无" in parts:
        print("[ERROR] --kind 含『无』：判为『无』的信号不进池（不得为凑数而记录）", file=sys.stderr)
        return 2
    if not 0.0 <= args.confidence <= 1.0:
        print("[ERROR] --confidence 须在 0–1", file=sys.stderr)
        return 2

    pool_dir = args.pool
    rows, bad = load_pool(pool_dir)
    key = cand_key(args.text)

    # ── P0-1 / P0-2：解析 --contradicts / --supersedes（**引用不到池内条目即拒绝，fail-closed**）──
    c_targets, s_targets, errs = [], [], []
    for ref in list(args.contradicts or []):
        t, e = resolve_pool_key(rows, ref)
        if e:
            errs.append("--contradicts %s" % e)
        elif t == key:
            errs.append("--contradicts 指向本条自身（key=%s）：不得与自己冲突" % key)
        else:
            c_targets.append(t)
    for ref in list(args.supersedes or []):
        t, e = resolve_pool_key(rows, ref)
        if e:
            errs.append("--supersedes %s" % e)
        elif t == key:
            errs.append("--supersedes 指向本条自身（key=%s）：不得替代自己" % key)
        else:
            s_targets.append(t)
    if errs:
        for e in errs:
            print("[ERROR] %s" % e, file=sys.stderr)
        print("[ERROR] 引用不到池内条目即拒绝（fail-closed）：先 note 被指向的条目，或直接给 12 位 key",
              file=sys.stderr)
        return 2

    # ── P0-3：证据固化（**快照同样过敏感扫描**；命中即整条驳回、不入池也不落档）──
    snap, snap_kind = _snapshot_entries(list(args.evidence or []), list(args.source or []))

    hits = scan_sensitive_many([args.text] + [e["text"] for e in snap])
    if hits:
        log_decision(pool_dir, {"key": None, "action": "驳回", "reason": ",".join(hits)})
        print(json.dumps({"action": "驳回", "rules": hits,
                          "note": "命中敏感模式（正文**或证据快照**），不入池、不入库、**不落档**；原文不打印"},
                         ensure_ascii=False))
        return 0

    hit = next((r for r in rows if r.get("key") == key), None)
    if hit:
        hit["evid_count"] = int(hit.get("evid_count", 1)) + 1
        hit["last_seen"] = now_iso()
        hit["confidence"] = max(float(hit.get("confidence", 0.0)), args.confidence)
        for s in (args.source or []):
            if s not in hit["sources"]:
                hit["sources"].append(s)
        if args.boundary:
            hit["boundary"] = args.boundary
        action = "合并"
    else:
        hit = {"key": key, "kind": kind, "text": args.text, "title": args.title or "",
               "confidence": args.confidence, "boundary": args.boundary or "",
               "evid_count": 1, "first_seen": now_iso(), "last_seen": now_iso(),
               "sources": list(args.source or []), "state": "观察",
               "evidence": [], "conflict_with": []}
        rows.append(hit)
        action = "新增"

    # P0-3：入池（截断保最新 N 条）+ 归档（append-only 全量）
    archive_path = None
    if snap:
        hit["evidence"] = _cap_evidence(list(hit.get("evidence") or []) + snap)
        archive_path = _append_archive(pool_dir, {
            "time": now_iso(), "key": key, "kind": kind, "action": action,
            "snapshot_kind": snap_kind, "evidence": snap, "sources": list(args.source or [])})

    # ── P0-1：冲突登记（**双向冻结**）—— 「是否矛盾」由调用方声明（语义），脚本只登记（算术）──
    conflict_registered = []
    if c_targets:
        hit["state"] = KB_CONFLICT_STATE
        hit["conflict_with"] = sorted(set(list(hit.get("conflict_with") or []) + c_targets))
        hit["conflict_at"] = now_iso()
        for t in c_targets:
            tr = next(r for r in rows if r.get("key") == t)
            tr["state"] = KB_CONFLICT_STATE
            tr["conflict_with"] = sorted(set(list(tr.get("conflict_with") or []) + [key]))
            tr["conflict_at"] = now_iso()
        log_decision(pool_dir, {"key": key, "action": "冲突登记",
                                "reason": "与 %s 互相矛盾（双向冻结，待用户裁决）" % ",".join(c_targets),
                                "kb_id": None})
        conflict_registered = c_targets

    # ── P0-2：废止声明（**本步只登记关系**；提案在 commit 产生、放行须用户）──
    if s_targets:
        hit["supersedes"] = sorted(set(list(hit.get("supersedes") or []) + s_targets))

    save_pool(pool_dir, rows)
    log_decision(pool_dir, {"key": key, "action": action,
                            "reason": "kind=%s conf=%.2f" % (kind, args.confidence), "kb_id": None})
    print(json.dumps({"action": action, "key": key, "evid_count": hit["evid_count"],
                      "state": hit["state"], "skipped_bad_lines": bad,
                      "threshold": min_evidence_of(kind),
                      "conflict_with": hit.get("conflict_with") or [],
                      "supersedes": hit.get("supersedes") or [],
                      "evidence_snapshot": {"kind": snap_kind, "n": len(snap),
                                            "archived_to": archive_path},
                      "note": ("已**双向冻结**：双方 state=%s，永不自动入库，须用户裁决" % KB_CONFLICT_STATE)
                              if conflict_registered else ""},
                     ensure_ascii=False))
    return 0


def evaluate(cand: dict, dup_checked: bool, kb_root: Path) -> tuple:
    """门槛判定（K4）。返回 (结论, 理由列表)。

    结论 ∈ `入库` / `观察` / `冲突` / `待废止` / `驳回`。**次序即优先级**：
    敏感（安全）> 冲突冻结与废止提案（状态级）> 六条门槛（证据/置信/边界/库内重复）。

    ⭐ P0-1 修的就是这里：冲突分支曾**不可达**（进入条件 `state == "冲突"` 的唯一途径
    就是走进这个分支本身 —— 循环依赖 = 死代码）。现在 `cmd_note --contradicts` 会把它置真。
    """
    reasons = []
    main = str(cand.get("kind", "")).split("+")[0].strip()
    need = min_evidence_of(cand.get("kind", ""))
    if int(cand.get("evid_count", 1)) < need:
        reasons.append("证据不足（%d/%d）" % (int(cand.get("evid_count", 1)), need))
    if float(cand.get("confidence", 0.0)) < KB_AUTO_COMMIT:
        reasons.append("置信不足（%.2f/%.2f）" % (float(cand.get("confidence", 0.0)), KB_AUTO_COMMIT))
    if not str(cand.get("boundary") or "").strip():
        reasons.append("缺适用边界（精炼未完成）")
    if scan_sensitive(cand.get("text", "")):
        return "驳回", ["命中敏感模式（原文不打印）"]
    # ⭐ P0-1：冲突冻结 —— **门槛结果不生效**（这正是"冻结"的含义）
    if cand.get("state") == KB_CONFLICT_STATE:
        cw = cand.get("conflict_with") or []
        return "冲突", (["与 %s 互相矛盾（**双向冻结中**，门槛结果不生效，须用户裁决）"
                         % ("、".join("learn-" + str(k) for k in cw) if cw else "未知条目")] + reasons)
    # ⭐ P0-2：**终态「已废止」也不得写回 KB** —— 否则下一次 commit 会把已经宣布废止的条目
    #    重新写进去，等于「废止被自动撤销」（本断言就是这么抓出来的：只有"待废止"分支是不够的）。
    if cand.get("state") == KB_RETIRED_STATE:
        sb = cand.get("superseded_by") or []
        return "已废止", (["**已废止**（由 %s 替代）—— 不再写回 KB；如需恢复须人工处理"
                           % ("、".join("learn-" + str(k) for k in sb) if sb else "未知条目")] + reasons)
    # ⭐ P0-2：废止提案待放行 —— **不写 KB、不改旧文档**（废止按危险度分级，须用户放行）
    if cand.get("state") == KB_RETIRE_PENDING:
        sb = cand.get("retire_proposed_by") or []
        return "待废止", (["已提出废止（由 %s 替代），**放行须用户**："
                           "`kb_learn.py retire --approve <id> --apply`"
                           % ("、".join("learn-" + str(k) for k in sb) if sb else "未知条目")] + reasons)
    if dup_checked:
        dup, score, bid = kb_search_duplicate(kb_root, str(cand.get("text", "")))
        if dup:
            reasons.append("库中已有（相似度 %.3f，id=%s）" % (float(score or 0), bid))
    if reasons:
        return "观察", reasons
    return "入库", ["门槛全过（type=%s，证据 %d 次）" % (main, int(cand.get("evid_count", 1)))]


def cmd_commit(args) -> int:
    kb_root = args.kb_root
    pool_dir = args.pool
    rows, bad = load_pool(pool_dir)
    if not rows:
        print(json.dumps({"total": 0, "入库": [], "观察": [], "冲突": [], "待废止": [],
                          "已废止": [], "驳回": [],
                          "废止提案": [], "skipped_bad_lines": bad, "applied": bool(args.apply),
                          "kb_available": kb_script(kb_root).exists(),
                          "kb_check": bool(args.kb_check),
                          "note": "池为空：无可提交候选（本脚本不产生样本）"}, ensure_ascii=False, indent=2))
        return 0

    kb_ok = kb_script(kb_root).exists()
    if args.apply and not kb_ok:
        # fail-closed：KB 不可用就拒绝写入，不假装入库成功（候选原样留池）
        print(json.dumps({"error": "KB 不可用：%s 不存在，本次未入库（fail-closed）" % kb_script(kb_root),
                          "kb_available": False}, ensure_ascii=False), file=sys.stderr)
        return 1

    # ⭐ P0-2：先把 `supersedes` 声明登记成**废止提案**并冻结旧条目。
    #    **提案 ≠ 执行** —— 这一整段只写池侧文件，一次 KB 都不碰。
    proposals = []
    for r in rows:
        for ok in (r.get("supersedes") or []):
            old = next((x for x in rows if x.get("key") == ok), None)
            if old is None:
                proposals.append({"old_key": ok, "new_key": r.get("key"), "action": "跳过",
                                  "reason": "池中已无被替代条目"})
                continue
            p = propose_retire(pool_dir, ok, r.get("key"))
            # ⚠️ 只有**本次新登记**才冻结旧条目。已决提案（放行/驳回）不得被机器拉回 `待废止` ——
            #    否则「驳回」会在下一次 commit 被自动撤销（见 propose_retire 的说明）。
            if (not p.get("dedup")) and p.get("status") == "待放行" \
                    and str(old.get("state")) != KB_RETIRED_STATE:
                old["state"] = KB_RETIRE_PENDING
                old["retire_proposed_by"] = sorted(
                    set(list(old.get("retire_proposed_by") or []) + [r.get("key")]))
            if p.get("dedup"):
                _st = p.get("status", "待放行")
                _act = "已登记（幂等去重）" if _st == "待放行" else "已决（%s，不自动重开）" % _st
            else:
                _act = "新登记"
            proposals.append({"id": p.get("id"), "old_key": ok, "new_key": r.get("key"),
                              "status": p.get("status", "待放行"), "action": _act,
                              "note": "**废止不自动执行**：须 `retire --approve <id> --apply`"})

    res = {"total": len(rows), "入库": [], "观察": [], "冲突": [], "待废止": [], "已废止": [],
           "驳回": [], "废止提案": proposals, "skipped_bad_lines": bad, "applied": bool(args.apply),
           "kb_available": kb_ok, "kb_check": bool(args.kb_check)}
    if not args.kb_check:
        res["note"] = "未做库内去重（--kb-check 关闭）：池内去重仍然生效，与 KB 已有文档的重复不会被发现"

    for r in rows:
        verdict, reasons = evaluate(r, args.kb_check, kb_root)
        # ⚠️ 报告必须**同时**给出 `state_before`（进入本次 commit 前的池状态）与
        #    `state_after`（本次 commit 之后的池状态）——只给一个都会让人误读：
        #    沿用旧写法只给一个含糊的 `state` 时，「入库」桶里的条目显示上一次的「观察」，
        #    读者会把「已入库」误读成「没入库」（机器可读字段不得含糊）。
        item = {"key": r.get("key"), "kind": r.get("kind"),
                "text": str(r.get("text", ""))[:60], "evid_count": r.get("evid_count"),
                "state_before": r.get("state"), "reasons": reasons}
        if verdict == "入库":
            kb_id = "learn-" + str(r.get("key"))
            main = str(r.get("kind", "")).split("+")[0].strip()
            extra = [p for p in str(r.get("kind", "")).split("+")[1:] if p]
            title = str(r.get("title") or r.get("text") or "")[:60]
            act = kb_write(kb_root, kb_id, title, "学习-" + main,
                           ["ai-workflow", KB_HEADER, main] + extra,
                           render_content(r), args.apply)
            item["kb_id"] = kb_id
            item["kb_action"] = act
            # ⚠️ **只有真写了 KB，才可以把池标成 `已入库`、才可以发 `kb_id`。**
            #    否则池会声称「已入库」而 KB 里根本没有这篇文档 —— 这就是「沉默被当成通过」
            #    的反面：**没做的事不得记成做了**。dry-run 也会走到这一支，所以必须由
            #    `--apply` 把关（实测踩中：一次 dry-run 就把 7 条标成已入库并发了假 `learn-*` id）。
            if args.apply:
                r["state"] = "已入库"
                r["kb_id"] = kb_id
            item["state_after"] = r["state"]
            res["入库"].append(item)
            log_decision(pool_dir, {"key": r.get("key"), "action": "入库",
                                    "reason": "；".join(reasons), "kb_id": kb_id})
        else:
            r["state"] = verdict
            item["state_after"] = r["state"]
            res[verdict].append(item)
            log_decision(pool_dir, {"key": r.get("key"), "action": verdict,
                                    "reason": "；".join(reasons), "kb_id": None})

    if proposals:
        log_decision(pool_dir, {"key": None, "action": "废止提案",
                                "reason": "%d 条（待放行）" % len(proposals), "kb_id": None})
    save_pool(pool_dir, rows)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


def cmd_retire(args) -> int:
    """废止提案的**用户闸门**：列出 / 放行 / 驳回。默认只报告（dry-run）。"""
    pool_dir, kb_root = args.pool, args.kb_root
    rows, bad = load_pool(pool_dir)
    props = load_retire_proposals(pool_dir)

    target = args.approve or args.reject
    if target:
        prop = next((p for p in props if p.get("id") == target), None)
        if prop is None:
            print(json.dumps({"error": "未找到提案 id=%s" % target,
                              "available": [p.get("id") for p in props]},
                             ensure_ascii=False), file=sys.stderr)
            return 1
        if args.approve:
            if args.apply and not kb_script(kb_root).exists():
                print(json.dumps({"error": "KB 不可用：%s 不存在，本次未执行（fail-closed）"
                                           % kb_script(kb_root), "kb_available": False},
                                 ensure_ascii=False), file=sys.stderr)
                return 1
            out = apply_retire(kb_root, pool_dir, rows, prop, args.apply)
        else:
            out = reject_retire(pool_dir, rows, prop)
        out["applied"] = bool(args.apply)
        out["proposals_remaining"] = len([p for p in load_retire_proposals(pool_dir)
                                         if p.get("status") == "待放行"])
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    pending = [p for p in props if p.get("status") == "待放行"]
    print(json.dumps({
        "提案总数": len(props), "待放行": len(pending), "提案": props,
        "说明": ("**废止永远走提案**：`retire --approve <id> --apply` 才真正执行 —— "
                 "KB 侧只加废止标记与标签、**绝不删除**（保审计链）；`retire --reject <id>` 驳回。"
                 "不加 `--apply` 为 dry-run（只报告将废止什么）。"),
        "池": str(_pool_paths(pool_dir)["retire"]),
        "skipped_bad_lines": bad,
    }, ensure_ascii=False, indent=2))
    return 0


def cmd_stats(args) -> int:
    rows, bad = load_pool(args.pool)
    by_kind, by_state = {}, {}
    for r in rows:
        main = str(r.get("kind", "")).split("+")[0].strip()
        by_kind[main] = by_kind.get(main, 0) + 1
        st = str(r.get("state", "观察"))
        by_state[st] = by_state.get(st, 0) + 1
    kb_root = args.kb_root
    rc, out = kb_run(kb_root, ["stats"], timeout=60)
    kb_stat = None
    if rc == 0 and out:
        try:
            kb_stat = json.loads(out[out.index("{"):out.rindex("}") + 1])
        except (ValueError, IndexError):
            kb_stat = None
    props = load_retire_proposals(args.pool)
    print(json.dumps({
        "池路径": str(_pool_paths(args.pool)["pool"]),
        "总数": len(rows), "按类型": by_kind, "按状态": by_state,
        "跳过坏行": bad,
        "门槛": {"min_evidence": KB_MIN_EVIDENCE, "default_min_evidence": KB_DEFAULT_MIN_EVIDENCE,
                "auto_commit": KB_AUTO_COMMIT, "dup_threshold": KB_DUP_THRESHOLD,
                "evidence_max": KB_EVIDENCE_MAX, "fitted": False},
        "废止提案": {"总数": len(props),
                     "待放行": len([p for p in props if p.get("status") == "待放行"])},
        "KB 根": str(kb_root),
        "KB 可用": kb_script(kb_root).exists(),
        "KB 统计": kb_stat,
    }, ensure_ascii=False, indent=2))
    return 0


# ─────────────────────────── 自检（含阴性对照） ───────────────────────────
def cmd_selftest(args) -> int:
    fails = []
    n = [0]

    def chk(cond, label):
        n[0] += 1
        if not cond:
            fails.append(label)

    import contextlib
    import io

    def silent_main_out(argv):
        """在**进程内**调 CLI 并捕获 stdout —— 让"缺陷在 CLI 入口已修好"可机器验证。"""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            rc = main(argv)
        return rc, buf.getvalue()

    def silent_main(argv):
        return silent_main_out(argv)[0]

    def jload(s: str) -> dict:
        try:
            return json.loads(s[s.index("{"):s.rindex("}") + 1])
        except (ValueError, IndexError):
            return {}

    def arch_lines(pool_dir: Path) -> int:
        """证据归档**记录数**（按行）—— 用于断言"驳回时不落档"。"""
        d = _pool_paths(pool_dir)["archive_dir"]
        if not d.is_dir():
            return 0
        return sum(len(f.read_text(encoding="utf-8").splitlines()) for f in d.glob("*.jsonl"))

    # ① key 稳定性：同义换标点 → 同 key（正向）
    a = cand_key("报告里不要加 emoji")
    b = cand_key("报告里，不要加 emoji。")
    chk(a == b, "key 稳定性：仅标点差异应同 key")
    # ①′ 阴性对照：不同含义必须不同 key（证明上面不是"恒等函数"）
    chk(a != cand_key("报告里要加 emoji"), "key 稳定性阴性对照：否定词差异必须换 key")

    # ② judge 正向：典型偏好维度
    r1 = aggregate(parse_dims("V1=0.90,V2=0.90,V3=0.85,V4=0.60,V5=0.90"))
    chk(r1["kind"] == "偏好", "judge 正向：典型偏好维度应判『偏好』（实得 %s）" % r1["kind"])
    chk(abs(sum(r1["distribution"].values()) - 1.0) < 1e-6, "judge：五键分布之和须为 1")
    chk(abs(r1["confidence"] - max(r1["distribution"].values())) < 1e-9,
        "judge：confidence 须等于 max(distribution)")
    # ②′ 阴性对照：全低分 → 『无』，且 band 必须改变（证明阈值真的在起作用）
    r2 = aggregate(parse_dims("V1=0.20,V2=0.10,V3=0.05,V4=0.10,V5=0.15"))
    chk(r2["kind"] == "无", "judge 阴性：全低分应判『无』（实得 %s）" % r2["kind"])
    chk(r2["needs_learning"] is False, "派生：kind=无 ⇒ needs_learning 必须 False")

    # ③′ ⭐ 结构性断言（防"永远走兜底分支"伪装成正常）：落在锚点上的输入**必须**能达自采信带。
    #    —— τ 过大会让 band 恒为 hold，管线永不吸收；这条断言就是那类失效的捕手。
    for _k in ("偏好", "领域知识", "流程坑", "事实"):
        _dim = dict(zip(KB_DIMS, KB_ANCHORS[_k]))
        _r = aggregate({d: _dim[d] for d in KB_DIMS})
        chk(_r["kind"] == _k, "结构：落在『%s』锚点上应判回自身（实得 %s）" % (_k, _r["kind"]))
        chk(_r["_model"]["band"] == "auto",
            "结构：落在『%s』锚点上必须达 auto 带（实得 %s —— τ 过大即此处 FAIL）" % (_k, _r["_model"]["band"]))
        chk(_r["confidence"] >= KB_AUTO_BAND, "结构：锚点输入置信须 ≥ %.2f（实得 %.4f）" % (KB_AUTO_BAND, _r["confidence"]))
    _neu = aggregate({d: 0.5 for d in KB_DIMS})
    chk(_neu["_model"]["band"] in ("mid", "hold"),
        "结构阴性对照：无信息输入（全 0.5）不得落入 auto 带（实得 %s）" % _neu["_model"]["band"])

    # ④ 组合合法性：`无` 不参与组合
    fake = {"偏好": 0.40, "领域知识": 0.39, "流程坑": 0.07, "事实": 0.07, "无": 0.07}
    chk("无" not in _combine(fake, 0.15), "组合：`无` 不得参与组合")
    chk("+" in _combine(fake, 0.15), "组合：top2 差 < delta 时应产出 A+B")
    chk(_combine({"偏好": 0.9, "领域知识": 0.05, "流程坑": 0.02, "事实": 0.02, "无": 0.01}, 0.15) == "偏好",
        "组合：差距大时应取单值")

    # ④ 门槛正向 / 阴性对照（≥2 次是偏好专属门槛）
    chk(min_evidence_of("偏好") == 2, "门槛：偏好最低证据应为 2")
    chk(min_evidence_of("领域知识") == 1, "门槛：非偏好最低证据应为 1")
    cand_ok = {"kind": "偏好", "text": "x", "confidence": 0.9, "evid_count": 2,
               "boundary": "适用于所有报告", "state": "观察"}
    chk(evaluate(cand_ok, False, Path("."))[0] == "入库", "门槛正向：双证据偏好应入库")
    cand_low = dict(cand_ok, evid_count=1)
    chk(evaluate(cand_low, False, Path("."))[0] == "观察", "门槛阴性：单次偏好必须不入库")
    cand_nb = dict(cand_ok, boundary="")
    chk(evaluate(cand_nb, False, Path("."))[0] == "观察", "门槛：缺适用边界必须不入库")
    cand_lc = dict(cand_ok, confidence=0.5)
    chk(evaluate(cand_lc, False, Path("."))[0] == "观察", "门槛：置信不足必须不入库")
    # ④′ 阈值真的在起作用（把门槛抬高，结论必须翻转 —— 阴性对照）
    _saved = KB_AUTO_COMMIT
    try:
        globals()["KB_AUTO_COMMIT"] = 0.99
        chk(evaluate(cand_ok, False, Path("."))[0] == "观察", "阴性对照：抬高置信门槛后必须翻转")
    finally:
        globals()["KB_AUTO_COMMIT"] = _saved

    # ⑤ 敏感驳回
    chk(scan_sensitive("token=ghp_" + "a" * 24) == ["GitHub 令牌"], "敏感：GitHub 令牌应命中")
    chk(scan_sensitive("联系 zhang@example.com") == ["邮箱"], "敏感：邮箱应命中")
    chk(scan_sensitive("报告里不要加 emoji") == [], "敏感阴性对照：正常文本不得误报")
    chk(scan_sensitive_many(["正常", "token=ghp_" + "a" * 24]) == ["GitHub 令牌"],
        "P0-3：scan_sensitive_many 必须能扫出**第二条**里的凭据（否则证据快照会绕过扫描）")

    # ⑥ 幂等与计数（临时池，不污染真实池）
    with tempfile.TemporaryDirectory(prefix="kblearn_") as td:
        pool = Path(td)
        rows, _ = load_pool(pool)
        chk(rows == [], "空池：应返回空列表")
        rows.append({"key": cand_key("偏好样本"), "kind": "偏好", "text": "偏好样本",
                     "confidence": 0.9, "evid_count": 1, "boundary": "b", "sources": []})
        save_pool(pool, rows)
        rows2, bad2 = load_pool(pool)
        chk(len(rows2) == 1 and bad2 == 0, "池存取：一条进一条出、零坏行")
        with open(_pool_paths(pool)["pool"], "a", encoding="utf-8") as f:
            f.write("{坏行\n")
        rows3, bad3 = load_pool(pool)
        chk(len(rows3) == 1 and bad3 == 1, "池健壮性：坏行须被跳过并计数（不静默丢弃）")

    # ⑦ KB 适配层不存在时必须 fail-closed（不得假装成功）
    rc, out = kb_run(Path(tempfile.gettempdir()) / "definitely_missing_kb_root", ["stats"])
    chk(rc is None, "KB 缺失：kb_run 必须返回 None（调用方据此 fail-closed）")

    # ⑦′ KB 根探测（**结构性断言**：防"只认 Path.home()"这类环境错配）
    cands = candidate_kb_roots()
    chk(len(cands) >= 1, "KB 根候选：至少 1 个")
    chk(len({str(c).lower() for c in cands}) == len(cands), "KB 根候选：必须去重")
    _d = discover_kb_root()
    if (Path(os.environ.get("USERPROFILE") or "") / ".workbuddy" / "kb" / "kb.py").exists() or \
       (Path(os.environ.get("HOME") or "") / ".workbuddy" / "kb" / "kb.py").exists():
        # 本机存在真实 KB 时：探测结果必须命中它（否则学习管线永远无法入库）
        chk((_d / "kb.py").exists(),
            "KB 根探测：本机存在 kb.py 时，discover_kb_root() 必须命中（实得 %s）" % _d)
        chk(default_pool_dir() == _d / "learning", "累积池：必须跟随 KB 根，不分裂")
    else:
        chk(True, "KB 根探测：本机无 KB，跳过命中断言（非空跑：候选去重已单独断言）")

    # ⑧ ⭐ P0-1 冲突登记：**可达性与双向冻结**（修"循环依赖型死代码"）
    with tempfile.TemporaryDirectory(prefix="kblearn_c_") as td:
        pool = Path(td)
        fak = {"kind": "领域知识", "confidence": 0.95, "evid_count": 3,
               "boundary": "b", "sources": [], "evidence": [], "conflict_with": []}
        kA, kB = cand_key("方案A可行"), cand_key("方案A不可行")
        rowA = dict(fak, key=kA, text="方案A可行", state="观察")
        rowB = dict(fak, key=kB, text="方案A不可行", state="观察")
        # 前置：无冲突登记时门槛全过 ⇒ 应"入库"（证明下面不是被门槛挡住的）
        chk(evaluate(rowA, False, Path("."))[0] == "入库",
            "P0-1 前置：门槛全过且无冲突登记 ⇒ 必须判『入库』")
        kk, ee = resolve_pool_key([rowA, rowB], "方案A不可行")
        chk(kk == kB and ee is None, "P0-1：按**原文**引用必须能解析出 key")
        kk2, ee2 = resolve_pool_key([rowA, rowB], kB)
        chk(kk2 == kB and ee2 is None, "P0-1：按 **12 位 key** 引用必须能解析")
        _k3, _e3 = resolve_pool_key([rowA, rowB], "池里根本没有这句话")
        chk(_k3 is None and bool(_e3), "P0-1：引用不存在的条目必须报错（fail-closed，不得静默新建）")
        # ⑧′ **CLI 端到端**（缺陷就在这里：此前 CLI 无任何入口能把 state 置为"冲突"）
        silent_main(["note", "--text", "方案B可行", "--kind", "领域知识", "--confidence", "0.9",
                     "--boundary", "b", "--pool", str(pool), "--kb-root", str(pool / "nokb")])
        rc1 = silent_main(["note", "--text", "方案B不可行", "--kind", "领域知识", "--confidence", "0.9",
                           "--boundary", "b", "--contradicts", "方案B可行",
                           "--pool", str(pool), "--kb-root", str(pool / "nokb")])
        rows, _ = load_pool(pool)
        d = {r["key"]: r for r in rows}
        kc, kd = cand_key("方案B可行"), cand_key("方案B不可行")
        chk(rc1 == 0, "P0-1 CLI：--contradicts 应 rc=0（实得 %r）" % rc1)
        chk(d.get(kc, {}).get("state") == KB_CONFLICT_STATE and
            d.get(kd, {}).get("state") == KB_CONFLICT_STATE,
            "P0-1 CLI ⭐：新旧条目必须**双向**置为『%s』" % KB_CONFLICT_STATE)
        chk(kd in (d.get(kc, {}).get("conflict_with") or []) and
            kc in (d.get(kd, {}).get("conflict_with") or []),
            "P0-1 CLI：conflict_with 必须**互指**")
        chk(evaluate(d.get(kc, {}), False, Path("."))[0] == "冲突" and
            evaluate(d.get(kd, {}), False, Path("."))[0] == "冲突",
            "P0-1 CLI：两条互相矛盾的结论**都不得入库**（这正是修复前会双双入库的场景）")
        # ⑧″ 引用不存在 ⇒ rc=2 且**不得落池**
        n_before = len(rows)
        rc2 = silent_main(["note", "--text", "方案C", "--kind", "领域知识", "--confidence", "0.9",
                           "--contradicts", "根本不存在的条目",
                           "--pool", str(pool), "--kb-root", str(pool / "nokb")])
        rows_after, _ = load_pool(pool)
        chk(rc2 == 2 and len(rows_after) == n_before,
            "P0-1 CLI：引用不存在 ⇒ rc=2 且**不得落池**（实得 rc=%r，池 %d→%d）"
            % (rc2, n_before, len(rows_after)))
        # ⑧‴ **阴性对照**：同样两条数据，不给 --contradicts ⇒ 必须回到"入库"
        pool2 = Path(td) / "neg"
        silent_main(["note", "--text", "方案D可行", "--kind", "领域知识", "--confidence", "0.95",
                     "--boundary", "b", "--pool", str(pool2), "--kb-root", str(pool2 / "nokb")])
        silent_main(["note", "--text", "方案D可行", "--kind", "领域知识", "--confidence", "0.95",
                     "--boundary", "b", "--pool", str(pool2), "--kb-root", str(pool2 / "nokb")])
        rows_neg, _ = load_pool(pool2)
        chk(all(evaluate(r, False, Path("."))[0] == "入库" for r in rows_neg),
            "P0-1 阴性对照：不给 --contradicts ⇒ 必须仍按门槛『入库』（证明冻结源自登记而非门槛）")

    # ⑨ ⭐ P0-2 废止：**新增可自动、废止只提案**，放行后**只标记不删除**
    with tempfile.TemporaryDirectory(prefix="kblearn_r_") as td:
        pool, fk = Path(td), Path(td) / "kb"
        fk.mkdir()
        (fk / "kb.py").write_text("# stub：仅用于让 kb_script().exists() 为真\n", encoding="utf-8")
        kold, knew = cand_key("旧结论X"), cand_key("新结论Y")
        rows = [
            {"key": kold, "kind": "领域知识", "text": "旧结论X", "confidence": 0.95,
             "evid_count": 2, "boundary": "b", "sources": [], "state": "观察",
             "evidence": [], "conflict_with": []},
            {"key": knew, "kind": "领域知识", "text": "新结论Y", "confidence": 0.95,
             "evid_count": 2, "boundary": "b", "sources": [], "state": "观察",
             "evidence": [], "conflict_with": [], "supersedes": [kold]},
        ]
        save_pool(pool, rows)
        calls = []
        _orig_run, _orig_exists = globals()["kb_run"], globals()["kb_exists"]
        try:
            def _fake_run(root, a, timeout=120):
                calls.append(list(a))
                if a and a[0] == "get":
                    return 0, "# 旧结论X\nid=%s  分类=学习-领域知识  标签=x\n创建=1  更新=2\n\n【结论】旧结论X\n\n【适用边界】b\n" % a[2]
                return 0, "ok"

            globals()["kb_run"] = _fake_run
            globals()["kb_exists"] = lambda root, i: True

            p1 = propose_retire(pool, kold, knew)
            chk(bool(p1.get("id")), "P0-2：propose_retire 必须产出稳定 id")
            chk(propose_retire(pool, kold, knew).get("dedup") is True, "P0-2：同 (旧,新) 提案必须幂等")
            chk(retire_proposal_id(kold, knew) == p1.get("id"), "P0-2：提案 id 必须由 (旧,新) 确定性派生")
            props = load_retire_proposals(pool)
            chk(len(props) == 1 and props[0]["status"] == "待放行", "P0-2：提案初始状态须为『待放行』")
            # ⭐ 关键断言：**登记 ≠ 执行** —— 此刻 KB 侧必须零调用
            chk(calls == [], "P0-2 ⭐：**只登记提案不得触碰 KB**（已有 %d 次调用）" % len(calls))
            out = apply_retire(fk, pool, rows, props[0], apply=False)
            chk(out["action"] == "将废止" and calls == [], "P0-2：放行 dry-run 不得触碰 KB、不得改池")
            # 待放行态必须判『待废止』（不得入库、不得重写 KB）
            pend = dict(rows[0], state=KB_RETIRE_PENDING, retire_proposed_by=[knew])
            chk(evaluate(pend, False, Path("."))[0] == "待废止",
                "P0-2：待放行条目必须判『待废止』（不写 KB、不改旧文档）")
            # 真放行
            out = apply_retire(fk, pool, rows, props[0], apply=True)
            verbs = [c[0] for c in calls if c]
            chk("update" in verbs, "P0-2：真放行必须调 kb.py update（加废止标记）")
            chk("delete" not in verbs, "P0-2 ⭐：**废止绝不删除** —— 调用链中不得出现 delete（实得 %s）" % verbs)
            _upd = next((c for c in calls if c and c[0] == "update"), None)
            _content = _upd[_upd.index("--content") + 1] if _upd and "--content" in _upd else ""
            chk(KB_RETIRE_MARK in _content, "P0-2：更新后正文必须含废止标记『%s』" % KB_RETIRE_MARK)
            chk("【结论】旧结论X" in _content,
                "P0-2 ⭐：废止标记必须**保留原正文**（追加而非覆盖）—— 实得 %r" % _content[:80])
            chk("tag" in verbs, "P0-2：放行时应补标签『%s』（便于 kb.py list 一眼看全）" % KB_RETIRE_TAG)
            chk(out["action"] == "已废止" and rows[0]["state"] == KB_RETIRED_STATE,
                "P0-2：放行后池内旧条目须置终态『%s』" % KB_RETIRED_STATE)
            chk(rows[0].get("superseded_by") == [knew], "P0-2：放行后须记 superseded_by（时间线的读侧）")
            chk(not [p for p in load_retire_proposals(pool) if p["status"] == "待放行"],
                "P0-2：放行后不得再留『待放行』提案")
            chk(evaluate(rows[0], False, Path("."))[0] != "入库", "P0-2：已废止条目不得再被判『入库』")
            # ⑨′ 幂等：重复放行不得二次改 KB
            _n = len(calls)
            out2 = apply_retire(fk, pool, rows, load_retire_proposals(pool)[0], apply=True)
            chk(out2["action"].startswith("已废止") and len(calls) == _n,
                "P0-2：重复放行必须幂等（不得二次改 KB）")
            # ⑨‴ ⭐ 端到端：废止后再 commit，旧条目**不得回到『入库』桶**
            #    （否则"废止"会被下一次 commit 自动撤销 —— 这正是本组断言存在的理由）
            rc_c, out_c = silent_main_out(["commit", "--pool", str(pool), "--kb-root", str(fk)])
            j = jload(out_c)
            _in = [it.get("key") for it in (j.get("入库") or [])]
            chk(rc_c == 0 and kold not in _in,
                "P0-2 端到端：已废止条目再 commit **不得**回到『入库』桶（实得入库=%s）" % _in)
            chk(kold in [it.get("key") for it in (j.get("已废止") or [])],
                "P0-2 端到端：已废止条目必须**显式**列在『已废止』桶（沉默不得与通过混淆）")
            # ⑨‴⁺ ⭐ 报告字段不得含糊 + **「没做的事不得记成做了」**
            #    · 报告须同时给 state_before / state_after（只给一个会被误读）
            #    · dry-run 不得把池标成『已入库』、不得发 kb_id（KB 里没有那篇文档）
            #    ⚠️ commit 内部走 load_pool() 重读盘，测试里的 rows 是**另一份对象** ⇒
            #       断言池状态必须**从盘上重读**，否则读的是陈旧对象 ⇒ **空断言**（本组抓到过一次）
            _rows_dry, _ = load_pool(pool)
            _kd = next(r for r in _rows_dry if r.get("key") == knew)
            chk(bool(j.get("入库")),
                "报告字段前置：本场景『入库』桶必须非空（否则下面几条是空断言）")
            chk(all("state_before" in it and "state_after" in it for it in (j.get("入库") or [])),
                "报告字段：『入库』桶必须**同时**给出 state_before 与 state_after（只给一个会误读）")
            chk(all(it.get("state_after") == "观察" for it in (j.get("入库") or [])),
                "dry-run（上面那次 commit **未加 --apply**）：『入库』桶的 state_after 不得前进"
                " —— 实得 %s" % [it.get("state_after") for it in (j.get("入库") or [])])
            chk(not (_kd.get("state") == "已入库" or _kd.get("kb_id")),
                "dry-run ⭐：**不得**给池条目置『已入库』或发 kb_id"
                "（KB 未写却声称已入库 = 没做的事记成做了；实得 %s / %s）"
                % (_kd.get("state"), _kd.get("kb_id")))
            _rc_ap, _out_ap = silent_main_out(["commit", "--pool", str(pool),
                                               "--kb-root", str(fk), "--apply"])
            _rows_ap, _ = load_pool(pool)
            _krow = next(r for r in _rows_ap if r.get("key") == knew)
            chk(_rc_ap == 0 and _krow.get("state") == "已入库"
                and _krow.get("kb_id") == "learn-" + knew,
                "正向对照 ⭐：`--apply` 之后池条目必须置『已入库』并记 kb_id（实得 %s / %s）"
                % (_krow.get("state"), _krow.get("kb_id")))
            chk(bool(j.get("已废止")) and all(it.get("state_after") == KB_RETIRED_STATE
                                            for it in (j.get("已废止") or [])),
                "报告字段：『已废止』桶 item['state'] 须为终态『%s』（实得 %s）"
                % (KB_RETIRED_STATE, [it.get("state_after") for it in (j.get("已废止") or [])]))
            chk(kold not in [p.get("old_key") for p in (j.get("废止提案") or [])
                             if p.get("status") == "待放行"],
                "P0-2 端到端：已放行的提案**不得**被再次登记成「待放行」（已决不自动重开）")
            # ⑨″ 驳回：旧条目恢复原状态
            rows.append({"key": cand_key("旧结论Z"), "kind": "领域知识", "text": "旧结论Z",
                         "confidence": 0.95, "evid_count": 2, "boundary": "b", "sources": [],
                         "state": KB_RETIRE_PENDING, "retire_proposed_by": [knew],
                         "evidence": [], "conflict_with": []})
            pz = propose_retire(pool, cand_key("旧结论Z"), knew)
            rz = reject_retire(pool, rows, next(p for p in load_retire_proposals(pool)
                                                if p["id"] == pz["id"]))
            chk(rz["action"] == "已驳回" and rows[-1]["state"] == "观察",
                "P0-2：驳回后旧条目必须恢复原状态（实得 %s）" % rz.get("restored_state"))
            # ⑨⁗ ⭐ 驳回后再 commit ⇒ **不得**重新提议（否则是机器在反复推翻用户的「不要」）
            rc_c2, out_c2 = silent_main_out(["commit", "--pool", str(pool), "--kb-root", str(fk)])
            j2 = jload(out_c2)
            chk(not [p for p in (j2.get("废止提案") or []) if p.get("status") == "待放行"],
                "P0-2 端到端：驳回后**不得**被重新提议成「待放行」（实得 %s）"
                % [p.get("status") for p in (j2.get("废止提案") or [])])
            chk(next(r for r in rows if r["key"] == cand_key("旧结论Z"))["state"] != KB_RETIRE_PENDING,
                "P0-2 端到端：驳回后旧条目不得被机器拉回『待废止』")
        finally:
            globals()["kb_run"], globals()["kb_exists"] = _orig_run, _orig_exists

    # ⑩ ⭐ P0-3 证据固化：原文落池 + 归档；**快照过敏感扫描，命中即整条驳回且不落档**
    with tempfile.TemporaryDirectory(prefix="kblearn_e_") as td:
        pool = Path(td)
        rc = silent_main(["note", "--text", "报告不要带 emoji", "--kind", "偏好",
                          "--confidence", "0.9", "--boundary", "报告类交付物",
                          "--source", "tasks/x/tmp/log.txt",
                          "--evidence", "用户原话：以后报告别加表情符号，看着不专业",
                          "--pool", str(pool), "--kb-root", str(pool / "nokb")])
        rows, _ = load_pool(pool)
        chk(rc == 0 and len(rows) == 1, "P0-3 CLI：带 --evidence 的 note 应落池一条（rc=%r）" % rc)
        _ev = (rows[0].get("evidence") or []) if rows else []
        chk(bool(_ev) and _ev[0].get("kind") == "原文", "P0-3：--evidence 必须固化为**原文**快照")
        chk("看着不专业" in (_ev[0].get("text") if _ev else ""), "P0-3：快照必须含证据原文")
        chk(arch_lines(pool) == 1, "P0-3：必须追加写 archive/<YYYY-MM>.jsonl（实得 %d 条）" % arch_lines(pool))
        # ⑩′ 长度上限
        silent_main(["note", "--text", "长证据测试", "--kind", "事实", "--confidence", "0.9",
                     "--boundary", "b", "--evidence", "x" * 3000,
                     "--pool", str(pool), "--kb-root", str(pool / "nokb")])
        rows, _ = load_pool(pool)
        _r = next((r for r in rows if r.get("text") == "长证据测试"), None)
        _t = ((_r.get("evidence") or [{}])[0].get("text", "") if _r else "")
        chk(len(_t) <= KB_EVIDENCE_MAX,
            "P0-3：单条快照须截断到 ≤%d 字符（实得 %d）" % (KB_EVIDENCE_MAX, len(_t)))
        # ⑩″ 无 --evidence 时如实标"指针"，不得冒充原文
        silent_main(["note", "--text", "只有来源指针", "--kind", "事实", "--confidence", "0.9",
                     "--boundary", "b", "--source", "tasks/y/tmp/out.txt",
                     "--pool", str(pool), "--kb-root", str(pool / "nokb")])
        rows, _ = load_pool(pool)
        _r2 = next((r for r in rows if r.get("text") == "只有来源指针"), None)
        chk(((_r2.get("evidence") or [{}])[0].get("kind") if _r2 else None) == "指针",
            "P0-3：无 --evidence 时必须如实标 kind=指针（不得把指针冒充成原文）")
        # ⑩‴ **阴性对照**：凭据藏在证据里 ⇒ 整条驳回、不入池、**不落档**
        _nb, _ab = len(rows), arch_lines(pool)
        rc_bad = silent_main(["note", "--text", "这条不该被记住", "--kind", "事实",
                              "--confidence", "0.9", "--boundary", "b",
                              "--evidence", "token=ghp_" + "a" * 24,
                              "--pool", str(pool), "--kb-root", str(pool / "nokb")])
        rows, _ = load_pool(pool)
        chk(rc_bad == 0 and len(rows) == _nb,
            "P0-3 阴性对照：证据含凭据 ⇒ 整条驳回、**不得落池**（%d→%d）" % (_nb, len(rows)))
        chk(arch_lines(pool) == _ab,
            "P0-3 阴性对照 ⭐：驳回时**不得落档**（归档 %d→%d 条）" % (_ab, arch_lines(pool)))
        chk(not any("这条不该被记住" == r.get("text") for r in rows),
            "P0-3 阴性对照：被驳回的条目不得以任何形式留在池内")

    total = n[0]
    for f in fails:
        print("[FAIL] %s" % f)
    print("=== kb_learn selftest：%d/%d 通过 ===" % (total - len(fails), total))
    return 1 if fails else 0


# ─────────────────────────── 入口 ───────────────────────────
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="kb_learn", description="ai-workflow 学习引擎（K3 蒸馏 / K4 校验 / K5 入库）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("judge", help="V1–V5 维度分 → 判定字段（learning-judge 的聚合半边）")
    p.add_argument("--dims", required=True, help='形如 "V1=0.9,V2=0.85,V3=0.8,V4=0.7,V5=0.9"')
    p.add_argument("--ambiguous", action="store_true", help="Noul：判为模糊/混合")
    p.add_argument("--plan-block", action="store_true", help="只输出可粘进 plan.yaml 的字段（去掉 _model）")

    p = sub.add_parser("note", help="记录一条候选（**类型与置信由模型/人给**，本脚本不猜）")
    p.add_argument("--text", required=True, help="条目正文（一句话结论）")
    p.add_argument("--kind", required=True, help="学习类型：%s（可 + 组合，`无` 除外）" % "/".join(KB_KINDS[:-1]))
    p.add_argument("--confidence", type=float, required=True, help="0–1")
    p.add_argument("--title", default="", help="KB 文档标题（缺省取正文前 60 字）")
    p.add_argument("--boundary", default="", help="适用边界（**必填才可能自动入库**）")
    p.add_argument("--source", action="append", default=[], help="来源指针，可多次（任务目录/实测输出/用户原话摘要）")
    p.add_argument("--evidence", action="append", default=[],
                   help="P0-3 证据原文片段，可多次（**落池并归档**；与 --source 同过敏感扫描）")
    p.add_argument("--contradicts", action="append", default=[], metavar="<text|key>",
                   help="P0-1 声明「本条与该条互相矛盾」：**双向冻结**双方，永不自动入库，待用户裁决")
    p.add_argument("--supersedes", action="append", default=[], metavar="<text|key>",
                   help="P0-2 声明「本条替代该条」：旧条目的**废止只提案**，须 `retire --approve` 放行")

    p = sub.add_parser("commit", help="门槛判定 + 两段式入库（**默认 dry-run**）")
    p.add_argument("--apply", action="store_true", help="真正写入 KB（不加则只报告将入库什么）")
    p.add_argument("--kb-check", action="store_true", help="额外做库内去重（调 kb.py retrieve，较慢）")

    p = sub.add_parser("retire", help="废止提案：列出 / 放行 / 驳回（**废止永不自动执行**）")
    p.add_argument("--approve", default=None, metavar="<id>", help="放行该提案（配 --apply 才真写）")
    p.add_argument("--reject", default=None, metavar="<id>", help="驳回该提案（旧条目恢复原状态）")
    p.add_argument("--apply", action="store_true", help="真正执行（不加则只报告将废止什么）")

    sub.add_parser("stats", help="池与 KB 的统计")

    p = sub.add_parser("selftest", help="自检（含阴性对照）")

    for name in ("judge", "note", "commit", "retire", "stats"):
        sp = sub.choices[name]
        sp.add_argument("--pool", type=Path, default=None, help="累积池目录（默认 ~/.workbuddy/kb/learning）")
        sp.add_argument("--kb-root", type=Path, default=None, help="KB 根（默认 ~/.workbuddy/kb）")
        sp.add_argument("--json", action="store_true", help="预留：输出 JSON（当前各命令默认即 JSON）")
    args = ap.parse_args(argv)
    if getattr(args, "pool", None) is None:
        args.pool = default_pool_dir()
    if getattr(args, "kb_root", None) is None:
        args.kb_root = default_kb_root()

    if args.cmd == "judge":
        return cmd_judge(args)
    if args.cmd == "note":
        return cmd_note(args)
    if args.cmd == "commit":
        return cmd_commit(args)
    if args.cmd == "retire":
        return cmd_retire(args)
    if args.cmd == "stats":
        return cmd_stats(args)
    return cmd_selftest(args)


if __name__ == "__main__":
    raise SystemExit(main())
