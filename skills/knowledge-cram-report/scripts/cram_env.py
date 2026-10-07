# -*- coding: utf-8 -*-
"""[自研工具] knowledge-cram-report / cram_env.py

- 名称：knowledge-cram-report（知识点恶补 PDF 报告机制）
- 用途：环境自检——核验 pandoc、xelatex 与中文字体是否齐备
- 适用场景：每次生成恶补报告之前的前置门禁；缺依赖时**明确报错**，不静默降级
- 仓库链接：https://github.com/Garvin666/ai-workflow-tools

为什么不静默降级：本机实测过 weasyprint「pip 装了、import 成功、一调用就炸」（缺 GTK 运行库）。
「装上了」不等于「能跑」，所以链路前置必须先探再跑，缺件时给出可执行的补救提示。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

# ---- 依赖登记：名称 → 候选绝对路径（顺序即优先级） ----
PANDOC_CANDIDATES = [
    r"C:\Users\26717\AppData\Local\Pandoc\pandoc.EXE",
    r"C:\Program Files\Pandoc\pandoc.exe",
]
XELATEX_CANDIDATES = [
    r"E:\新建文件夹\miktex\bin\x64\xelatex.exe",
    r"C:\Program Files\MiKTeX\miktex\bin\x64\xelatex.exe",
    r"C:\Users\26717\AppData\Local\Programs\MiKTeX\miktex\bin\x64\xelatex.exe",
]
FONT_DIR = Path(r"C:\Windows\Fonts")
FONT_CANDIDATES = {
    "Microsoft YaHei": ["msyh.ttc", "msyh.ttf", "msyhbd.ttc"],
    "Times New Roman": ["times.ttf", "timesbd.ttf"],
    "Consolas": ["consola.ttf"],
    "DengXian": ["Deng.ttf"],
    "SimSun": ["simsun.ttc"],
    "SimHei": ["simhei.ttf"],
    "KaiTi": ["simkai.ttf"],
}
# 渲染链路必需（其余为可选回退）
FONT_REQUIRED = ["Microsoft YaHei"]
FONT_OPTIONAL = ["Times New Roman", "Consolas", "DengXian", "SimSun", "SimHei", "KaiTi"]

EXIT_OK = 0
EXIT_MISSING = 3  # 与 ai-workflow 的「降级码」同源口径：依赖缺失 => 显式失败


def _probe_tool(candidates: list[str], exe_name: str) -> dict:
    tried = []
    for c in candidates:
        tried.append(c)
        if os.path.isfile(c):
            return {"found": c, "tried": tried, "source": "绝对路径候选"}
    w = shutil.which(exe_name)
    if w:
        return {"found": w, "tried": tried + [f"PATH:{exe_name}"], "source": "PATH"}
    return {"found": None, "tried": tried + [f"PATH:{exe_name}"], "source": None}


def _first_line(cmd: list[str], timeout: int = 90) -> str:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout)
        out = (p.stdout or p.stderr or "").strip()
        return out.splitlines()[0] if out else f"(空输出 rc={p.returncode})"
    except Exception as e:  # pragma: no cover - 环境相关
        return f"MISSING {e!r}"


def probe() -> dict:
    pandoc = _probe_tool(PANDOC_CANDIDATES, "pandoc")
    xelatex = _probe_tool(XELATEX_CANDIDATES, "xelatex")
    fonts = {}
    for name, files in FONT_CANDIDATES.items():
        hit = next((str(FONT_DIR / f) for f in files if (FONT_DIR / f).exists()), None)
        fonts[name] = hit

    if pandoc["found"]:
        pandoc["version"] = _first_line([pandoc["found"], "--version"])
    if xelatex["found"]:
        xelatex["version"] = _first_line([xelatex["found"], "--version"])

    missing_required_fonts = [f for f in FONT_REQUIRED if not fonts.get(f)]
    missing_tools = [n for n, t in (("pandoc", pandoc), ("xelatex", xelatex)) if not t["found"]]
    ok = not missing_tools and not missing_required_fonts

    return {
        "ok": ok,
        "pandoc": pandoc,
        "xelatex": xelatex,
        "fonts": fonts,
        "missing_tools": missing_tools,
        "missing_required_fonts": missing_required_fonts,
        "optional_fonts_absent": [f for f in FONT_OPTIONAL if not fonts.get(f)],
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="恶补报告链路环境自检（pandoc + xelatex + 中文字体）")
    ap.add_argument("--json", action="store_true", help="输出 JSON（供脚本消费）")
    ap.add_argument("--quiet", action="store_true", help="仅在失败时输出")
    args = ap.parse_args(argv)

    r = probe()

    if args.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
    elif not args.quiet or not r["ok"]:
        print("=== 恶补报告链路环境自检 ===")
        for key, label in (("pandoc", "pandoc"), ("xelatex", "xelatex")):
            t = r[key]
            if t["found"]:
                print(f"[ OK ] {label:8} {t.get('version', '?')}  <- {t['found']}")
            else:
                print(f"[FAIL] {label:8} 未找到；已试：{t['tried']}")
        for name, path in r["fonts"].items():
            mark = "OK  " if path else ("FAIL" if name in FONT_REQUIRED else "warn")
            print(f"[{mark}] 字体 {name:18} {path or '缺失'}")
        if not r["ok"]:
            print("")
            print("缺失项不是配置问题，须先补齐再生成报告（本机制**不静默降级**）：")
            for t in r["missing_tools"]:
                if t == "pandoc":
                    print("  · 安装 pandoc（winget install --id JohnMacFarlane.Pandoc）")
                else:
                    print("  · 安装 MiKTeX（https://miktex.org/download）并勾选 XeTeX 支持")
            for f in r["missing_required_fonts"]:
                print(f"  · 字体 {f} 缺失（Windows 自带简体中文字体包）")

    return EXIT_OK if r["ok"] else EXIT_MISSING


if __name__ == "__main__":
    sys.exit(main())
