#!/usr/bin/env python
# -*- coding: utf-8 -*-
# [自研工具] palette_check.py
# 用途：配色候选的**数值闸** —— 在改文件之前判定配色可行性、在改完之后证明配色没被改。
#       四条 WCAG 对比度（body/muted/subtle/accent，页面底与纯白卡面**双底各算一次**）+ 主按钮「白字在强调色底上」承载 +
#       全量语义色两两 **CIEDE2000 ΔE00 ≥ 2.0**。令牌**唯一真源 = 交付件**（从 <style> 读），缺令牌直接 exit 3（fail-closed）。
# 适用场景：任何「把一组色值换成另一组」或「约束是色值不许变」的任务 —— 换主题、亮/暗色版、品牌色重排，
#           以及「只加质感、色值不动」这类需要**回归证据**的改造（改造前后读数 diff 为空即硬证据）。
# 作者：ai-workflow 自研（工行杯 Demo 配色与视觉深化任务，2026-09-29 / 2026-09-30）
# 仓库：https://github.com/Garvin666/ai-workflow-tools/blob/main/scripts/palette_check.py
"""palette_check.py —— 配色数值闸（零依赖、单文件、自足）。

判定口径（对齐 WCAG 2.1 与 CIEDE2000）:
    body    ≥4.5   --fg        vs 页面底
    muted   ≥4.5   --fg-muted  vs 页面底
    subtle  ≥3.0   --fg-subtle vs 页面底
    accent  ≥3.0   --accent    vs 页面底
    白字承载 ≥4.5   #FFFFFF     vs --accent   （按钮文字属正文档，不是大字豁免项）
    ΔE00    ≥2.0   语义色两两（层级/表面色按口径排除）

为什么不 import 现成的颜色库
----------------------------
本脚本要能被 clone 下来直接跑，故 WCAG 对比度与 CIEDE2000 全部内联（约 60 行，纯标准库），
并用 Sharma 的 CIEDE2000 标准测试集做 `--selftest`。

为什么不把色值写在脚本里
------------------------
**教训**：早期版本把调色板硬编码在源码里，结果交付件改了色而脚本还是旧值 —— 归档的
「数值闸读数」测的是旧色，不报错，只让证据与产物脱钩。故现在是**从交付件读**，缺令牌即中止。

用法:
    python palette_check.py --html ./frontend/index.html
    python palette_check.py --html ./frontend/index.html --tokens custom_tokens.json
    python palette_check.py --selftest         # 算法自校验（Sharma 测试集 + RGB→Lab 基准值）
    python palette_check.py --html a.html --json   # 机器可读输出（退出码仍为 0/1）

退出码: 0 = PASS（全部闸通过）/ 1 = FAIL / 2 = 用法错误 / 3 = 中止（令牌缺失，fail-closed）
"""
from __future__ import annotations

import argparse
import itertools
import json
import math
import pathlib
import re
import sys

# ── 默认令牌名映射（key = 报告里的简称，value = CSS 变量名）；可用 --tokens 覆盖 ──
DEFAULT_TOKENS = {
    "surface-1": "--surface-1", "surface-2": "--surface-2",
    "surface-3": "--surface-3", "surface-4": "--surface-4",
    "surface-hover": "--surface-hover",
    "line-1": "--line-1", "line-2": "--line-2",
    "fg": "--fg", "fg-muted": "--fg-muted", "fg-subtle": "--fg-subtle",
    "acc": "--accent", "ok": "--ok", "warn": "--warn", "bad": "--bad",
}
# 对比度四闸：判据 id / 令牌 key / 显示名 / 门槛
GATES = [("G3", "fg", "body", 4.5), ("G14", "fg-muted", "muted", 4.5),
         ("G4", "fg-subtle", "subtle", 3.0), ("G5", "acc", "accent", 3.0)]
LAYER_PREFIX = ("surface", "line")   # 层级/表面色不参与「语义色两两」判定


# ══════════════════════════ 颜色算法（内联，零依赖） ══════════════════════════
def parse_color_value(h: str) -> tuple[int, int, int]:
    """#RGB / #RRGGBB / rgb(r,g,b) → 0–255 整数三元组。"""
    s = (h or "").strip()
    m = re.fullmatch(r"#([0-9A-Fa-f]{3})", s)
    if m:
        return tuple(int(c * 2, 16) for c in m.group(1))
    m = re.fullmatch(r"#([0-9A-Fa-f]{6})", s)
    if m:
        v = m.group(1)
        return (int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16))
    m = re.fullmatch(r"rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*(?:,\s*[\d.]+\s*)?\)", s)
    if m:
        return tuple(int(round(float(x))) for x in m.groups())
    raise ValueError("无法解析颜色：%r" % h)


def _lin(c: float) -> float:
    c /= 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def rgb_to_lab(rgb: tuple[int, int, int]) -> tuple[float, float, float]:
    """sRGB(0–255) → CIE L*a*b*（D65）。"""
    r, g, b = (_lin(float(c)) for c in rgb)
    x = (0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047
    y = (0.2126 * r + 0.7152 * g + 0.0722 * b) / 1.00000
    z = (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883

    def f(t: float) -> float:
        return t ** (1 / 3) if t > 216 / 24389 else (841 / 108) * t + 4 / 29

    fx, fy, fz = f(x), f(y), f(z)
    return (116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz))


def _rel_lum(rgb: tuple[int, int, int]) -> float:
    r, g, b = (_lin(float(c)) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    """WCAG 2.1 相对亮度对比度（1–21）。"""
    la, lb = _rel_lum(parse_color_value(a)), _rel_lum(parse_color_value(b))
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def delta_e00(lab1: tuple[float, float, float], lab2: tuple[float, float, float]) -> float:
    """CIEDE2000（Sharma et al. 实现）。入参为 L*a*b* 三元组。"""
    L1, a1, b1 = lab1
    L2, a2, b2 = lab2
    c1, c2 = math.hypot(a1, b1), math.hypot(a2, b2)
    c_bar = (c1 + c2) / 2
    g = 0.5 * (1 - math.sqrt(c_bar ** 7 / (c_bar ** 7 + 25 ** 7))) if c_bar else 0.5
    a1p, a2p = (1 + g) * a1, (1 + g) * a2
    c1p, c2p = math.hypot(a1p, b1), math.hypot(a2p, b2)
    h1p = math.degrees(math.atan2(b1, a1p)) % 360 if (a1p or b1) else 0.0
    h2p = math.degrees(math.atan2(b2, a2p)) % 360 if (a2p or b2) else 0.0
    dLp, dCp = L2 - L1, c2p - c1p
    if c1p * c2p == 0:
        dHp = 0.0
    else:
        d = h2p - h1p
        dHp = d - 360 if d > 180 else (d + 360 if d < -180 else d)
    dHp = 2 * math.sqrt(c1p * c2p) * math.sin(math.radians(dHp) / 2)
    L_bar, c_bar_p = (L1 + L2) / 2, (c1p + c2p) / 2
    if c1p * c2p == 0:
        h_bar_p = h1p + h2p
    elif abs(h1p - h2p) <= 180:
        h_bar_p = (h1p + h2p) / 2
    elif (h1p + h2p) < 360:
        h_bar_p = (h1p + h2p + 360) / 2
    else:
        h_bar_p = (h1p + h2p - 360) / 2
    t = (1 - 0.17 * math.cos(math.radians(h_bar_p - 30))
         + 0.24 * math.cos(math.radians(2 * h_bar_p))
         + 0.32 * math.cos(math.radians(3 * h_bar_p + 6))
         - 0.20 * math.cos(math.radians(4 * h_bar_p - 63)))
    d_theta = 30 * math.exp(-(((h_bar_p - 275) / 25) ** 2))
    rc = 2 * math.sqrt(c_bar_p ** 7 / (c_bar_p ** 7 + 25 ** 7)) if c_bar_p else 0.0
    sl = 1 + (0.015 * (L_bar - 50) ** 2) / math.sqrt(20 + (L_bar - 50) ** 2)
    sc, sh = 1 + 0.045 * c_bar_p, 1 + 0.015 * c_bar_p * t
    rt = -math.sin(math.radians(2 * d_theta)) * rc
    kl, kc, kh = 1.0, 1.0, 1.0
    return math.sqrt((dLp / (kl * sl)) ** 2 + (dCp / (kc * sc)) ** 2
                     + (dHp / (kh * sh)) ** 2 + rt * (dCp / (kc * sc)) * (dHp / (kh * sh)))


def de(a_hex: str, b_hex: str) -> float:
    return delta_e00(rgb_to_lab(parse_color_value(a_hex)), rgb_to_lab(parse_color_value(b_hex)))


# ══════════════════════════ 令牌读取（唯一真源 = 交付件） ══════════════════════════
def read_tokens(path: pathlib.Path, want: dict[str, str]) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    m = re.search(r"<style[^>]*>(.*?)</style>", text, re.S)
    css = m.group(1) if m else text          # 传 .css 文件时整份即 CSS
    out, missing = {}, []
    for key, css_name in want.items():
        mm = re.search(re.escape(css_name) + r"\s*:\s*(#[0-9A-Fa-f]{6}|rgba?\([^)]*\))\s*;", css)
        if not mm:
            missing.append(css_name)
            continue
        out[key] = mm.group(1).upper() if mm.group(1).startswith("#") else mm.group(1)
    if missing:
        print("!! 交付件里找不到这些令牌（不拿缺件当通过）：%s" % ", ".join(missing))
        raise SystemExit(3)
    return out


# ══════════════════════════ 主流程 ══════════════════════════
def run(tokens: dict[str, str], base_bg: str, accent_cands: list[str],
        as_json: bool) -> int:
    lines: list[str] = []
    sem = [k for k in tokens if not k.startswith(LAYER_PREFIX)]
    fails: list[str] = []

    def p(s: str = "") -> None:
        lines.append(s)

    p("令牌唯一真源：交付件（缺令牌即 exit 3）")
    p("  " + "  ".join("%s=%s" % (k, tokens[k]) for k in ("fg", "fg-muted", "fg-subtle", "acc")))
    p()
    p("=" * 74)
    p("一、WCAG 对比度闸（值 = 实测比 / 门槛）")
    p("=" * 74)
    for gid, key, role, thr in GATES:
        r = contrast(tokens[key], base_bg)
        ok = r >= thr
        if not ok:
            fails.append("%s %s %.2f < %.1f" % (gid, role, r, thr))
        p(f"  [{gid}] {role:<6} {tokens[key]} vs {base_bg}  {r:6.2f} / {thr}  {'OK' if ok else 'FAIL'}")

    p()
    p("二、白卡面（#FFFFFF）上的对比度（同一批令牌，卡片承载文字）")
    for gid, key, role, thr in GATES:
        r = contrast(tokens[key], "#FFFFFF")
        ok = r >= thr
        if not ok:
            fails.append("%s %s(白底) %.2f < %.1f" % (gid, role, r, thr))
        p(f"  [{gid}] {role:<6} {tokens[key]} vs #FFFFFF  {r:6.2f} / {thr}  {'OK' if ok else 'FAIL'}")

    p()
    p("=" * 74)
    p("三·零、主按钮「白字在强调色底上」（门槛 4.5 —— 按钮文字是正文档，不适用大字豁免）")
    p("=" * 74)
    rw = contrast("#FFFFFF", tokens["acc"])
    ok = rw >= 4.5
    if not ok:
        fails.append("白字承载 %.2f < 4.5" % rw)
    p(f"  当前 acc={tokens['acc']}：白字对比度 = {rw:5.2f} / 4.5  {'OK' if ok else 'FAIL'}")
    if accent_cands:
        p("\n  候选扫描（需同时满足：白字 ≥4.5、acc vs 底 ≥3.0、与各语义色 ΔE00 ≥2.0）：")
        for cd in dict.fromkeys([tokens["acc"]] + accent_cands):
            r_w = contrast("#FFFFFF", cd)
            r_bg = contrast(cd, base_bg)
            ds = {k: de(cd, tokens[k]) for k in sem if k != "acc"}
            okc = r_w >= 4.5 and r_bg >= 3.0 and (min(ds.values()) if ds else 9) >= 2.0
            p(f"   {cd}  白字{r_w:5.2f}  vs-底{r_bg:5.2f}  "
              f"ΔE00最小={min(ds.values()):5.2f}（{min(ds, key=ds.get)}）  {'← 可用' if okc else ''}")

    p()
    p("=" * 74)
    p("三、语义色两两 CIEDE2000 ΔE00（门槛 ≥2.0；层级/表面色按口径排除）")
    p("=" * 74)
    pairs = []
    for a, b in itertools.combinations(sorted(sem), 2):
        if tokens[a] == tokens[b]:
            continue                      # 同值不算「两个色」
        d = de(tokens[a], tokens[b])
        pairs.append((d, a, b))
        if d < 2.0:
            fails.append("ΔE00 %s↔%s %.2f < 2.0" % (a, b, d))
            p(f"  [FAIL] {a:<12}{tokens[a]}  ↔  {b:<12}{tokens[b]}   ΔE00={d:5.2f}  < 2.0")
    if pairs:
        p(f"  语义色 {len(sem)} 个（去同值后 {len(set(tokens[k] for k in sem))} 个值），"
          f"最小间距 = {min(pairs)[0]:.2f}（{min(pairs)[1]} ↔ {min(pairs)[2]}）")
        for d, a, b in sorted(pairs)[:5]:
            p(f"    · {a:<12}{tokens[a]}  ↔  {b:<12}{tokens[b]}   ΔE00={d:5.2f}")

    p()
    p("=" * 74)
    p("四、层级/表面色明度阶梯（不判 FAIL，只为「分层是否真的看得见」取证）")
    p("=" * 74)
    lad = [k for k in ("surface-1", "surface-2", "surface-3", "surface-4") if k in tokens]
    for a, b in zip(lad, lad[1:]):
        p(f"  {a:<13}{tokens[a]}  ↔  {b:<13}{tokens[b]}   ΔE00={de(tokens[a], tokens[b]):5.2f}")
    if "line-1" in tokens and "surface-3" in tokens:
        p(f"  line-1 vs surface-3 : {de(tokens['line-1'], tokens['surface-3']):5.2f}")
    if "line-1" in tokens and "surface-2" in tokens:
        p(f"  line-1 vs surface-2 : {de(tokens['line-1'], tokens['surface-2']):5.2f}")

    verdict = "PASS（全部闸通过）" if not fails else "FAIL（%d 项）" % len(fails)
    p()
    p("=" * 74)
    p("总判定：" + verdict)
    p("=" * 74)

    if as_json:
        print(json.dumps({"verdict": "PASS" if not fails else "FAIL", "fails": fails,
                          "tokens": tokens, "report": lines}, ensure_ascii=False, indent=2))
    else:
        print("\n".join(lines))
        for f in fails:
            print("  [FAIL] " + f)
    return 0 if not fails else 1


def selftest() -> int:
    """算法自校验：Sharma CIEDE2000 标准测试集 + RGB→Lab 基准值 + 对比度极值。"""
    cases = [  # (lab1, lab2, 期望 ΔE00) — Sharma et al. 补充数据集
        ((50.0000, 2.6772, -79.7751), (50.0000, 0.0000, -82.7485), 2.0425),
        ((50.0000, 3.1571, -77.2803), (50.0000, 0.0000, -82.7485), 2.8615),
        ((50.0000, 2.8361, -74.0200), (50.0000, 0.0000, -82.7485), 3.4412),
        ((50.0000, -1.3802, -84.2814), (50.0000, 0.0000, -82.7485), 1.0000),
        ((60.2574, -34.0099, 36.2677), (60.4626, -34.1751, 39.4387), 1.2644),
        ((22.7233, 20.0904, -46.6940), (23.0331, 14.9730, -42.5619), 2.0373),
    ]
    rc = 0
    print("[自校验] CIEDE2000（Sharma 标准测试集，容差 1e-4）")
    for l1, l2, want in cases:
        got = delta_e00(l1, l2)
        ok = abs(got - want) < 1e-4
        rc |= 0 if ok else 1
        print("   %-8s got %.4f / want %.4f  %s" % ("", got, want, "OK" if ok else "FAIL"))

    # 注：sRGB → XYZ 用的是 4 位小数矩阵，白点处残差约 0.01（a*/b* 非严格 0），
    #     故 a*/b* 容差取 0.02、L* 取 1e-2 —— 这是矩阵精度极限，不是实现错误。
    print("[自校验] sRGB → L*a*b* 基准值（a*/b* 容差 0.02 = 4 位矩阵的白点残差）")
    checks = [((255, 255, 255), 100.0, 0.0, 0.0), ((0, 0, 0), 0.0, 0.0, 0.0),
              ((128, 128, 128), 53.5850, 0.0, 0.0)]
    for rgb, wL, wa, wb in checks:
        L, a, b = rgb_to_lab(rgb)
        ok = abs(L - wL) < 1e-2 and abs(a - wa) < 2e-2 and abs(b - wb) < 2e-2
        rc |= 0 if ok else 1
        print("   rgb%-16s → L*=%.4f a*=%.4f b*=%.4f  %s" % (str(rgb), L, a, b, "OK" if ok else "FAIL"))

    print("[自校验] WCAG 对比度极值")
    c_wb = contrast("#FFFFFF", "#000000")
    ok = abs(c_wb - 21.0) < 1e-6
    rc |= 0 if ok else 1
    print("   白 vs 黑 = %.4f / 21.0  %s" % (c_wb, "OK" if ok else "FAIL"))

    print("[自校验] %s" % ("全部通过" if rc == 0 else "有 FAIL"))
    return rc


def main() -> int:
    ap = argparse.ArgumentParser(description="配色数值闸：WCAG 对比度 + CIEDE2000 ΔE00（零依赖单文件）。")
    ap.add_argument("--html", type=pathlib.Path, help="交付件 HTML（从中读 <style> 的 CSS 变量）")
    ap.add_argument("--css", type=pathlib.Path, help="或直接给 .css 文件")
    ap.add_argument("--tokens", type=pathlib.Path, help="覆盖默认令牌名映射（JSON）")
    ap.add_argument("--base-bg", default=None, help="页面底色（默认取 surface-2）")
    ap.add_argument("--accent-cands", default="", help="强调色候选扫描，逗号分隔")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    ap.add_argument("--selftest", action="store_true", help="算法自校验后退出")
    args = ap.parse_args()

    if args.selftest:
        return selftest()
    src = args.html or args.css
    if not src or not src.exists():
        print("!! 需要 --html 或 --css 指向交付件（令牌唯一真源）")
        return 2
    want = DEFAULT_TOKENS
    if args.tokens:
        want.update(json.loads(args.tokens.read_text(encoding="utf-8")))
    tokens = read_tokens(src, want)
    base_bg = args.base_bg or tokens.get("surface-2")
    if not base_bg:
        print("!! 未拿到页面底色（surface-2），请用 --base-bg 指定")
        return 3
    return run(tokens, base_bg, [c.strip() for c in args.accent_cands.split(",") if c.strip()],
               args.json)


if __name__ == "__main__":
    raise SystemExit(main())
