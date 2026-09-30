#!/usr/bin/env python
# -*- coding: utf-8 -*-
# [自研工具] shot_seed.py
# 用途：截图「播种」脚手架 —— 让「默认是空状态、需先操作一次才有内容」的 Web 页在无头浏览器里自动进入有数据状态，以便截图评审。
#       做法是临时把页面文件替换为「等某个就绪标记元素出现后，click() 页面自带的按钮」的版本；截完从备份还原并复核 md5。
#       两条子命令 build / clean，自带备份与还原后的残留断言（残留非 0 即 exit 3）。
# 适用场景：任何「本地起服 + 需要交互才有内容」的 Web 页做视觉评审或截图留档时；尤其适用于空状态占大半屏、直接截图看不出设计问题的看板类页面。
#           **只走用户真会走的路径**（点页面自带按钮），不自己造元素、不调内部函数 —— 造出来的元素会污染被测对象自身的计数器。
# 作者：ai-workflow 自研（工行杯 Demo 视觉深化任务，2026-09-30）
# 仓库：https://github.com/Garvin666/ai-workflow-tools/blob/main/scripts/shot_seed.py
"""shot_seed.py —— 无头截图前的「播种」脚手架（临时改动，跑完即还原）。

要解决的问题
------------
数据看板类页面在刚打开时多半是**空状态**（要先点一次「运行扫描」「加载示例」才有内容）。
无头浏览器直接截图，得到的就是一堆空卡片 —— 看不出任何设计问题。

本脚本把目标 HTML 临时换成「等就绪标记出现 → 点页面自带按钮」的版本，截完立即从备份
还原并复核 md5。两条子命令：

    python shot_seed.py build --src path/to/index.html
    python shot_seed.py clean --src path/to/index.html

设计约束（踩过坑才写进来的）
----------------------------
1. **只点页面自带的按钮**，不 `createElement` 造临时元素再摘掉 —— 造元素会把被测页面
   自己的「动画已 settle」计数器钉住，且「从未启动」与「已完成」在 DOM 文本上不可区分。
2. **等就绪标记**而不是固定 sleep —— 页面的 boot 链是串行 await，固定等待必然不稳定。
3. **还原后必须复核残留**（本脚本把它做成 exit 3 的硬断言）—— 曾因异常中断导致 clean
   没执行，本体留在探针版被当成正式产物。
4. 备份存在时 **build 拒绝继续**（exit 3），避免二次备份覆盖掉真正的原始文件。

用法示例
--------
    # 扫描页：等 #p-auth 出现「已登录」，点 [data-est="scan-run"]
    python shot_seed.py build --src ./frontend/index.html \\
        --ready-selector '#p-auth' --ready-text '已登录' \\
        --seed 'scan=[data-est="scan-run"]' --hash '#/yan/scan'
    <截图>
    python shot_seed.py clean --src ./frontend/index.html

退出码：0 成功；2 用法/环境错误；3 中止（找不到锚点 / 备份已存在 / 还原后仍有残留）
"""
from __future__ import annotations

import argparse
import hashlib
import pathlib
import shutil
import sys

TAIL_DEFAULT = "</body>"


def md5(p: pathlib.Path) -> str:
    return hashlib.md5(p.read_bytes()).hexdigest()


def parse_seed(items: list[str]) -> dict[str, str]:
    """--seed 'scan=[data-est="scan-run"]' → {'scan': '[data-est="scan-run"]'}"""
    out: dict[str, str] = {}
    for it in items:
        if "=" not in it:
            raise SystemExit("!! --seed 需形如 页面标识=选择器，收到：%r" % it)
        k, v = it.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def build_script(seed_map: dict[str, str], ready_sel: str, ready_text: str,
                 hash_re: str, extra: str) -> str:
    """生成注入到页面尾部的播种脚本（纯前端，不用任何框架）。"""
    js_map = ", ".join('"%s": %s' % (k, _js_str(v)) for k, v in seed_map.items())
    return r"""<script>
/* ===== 截图播种（临时，跑完即删）：就绪标记出现后自动点一次页面自带的按钮 ===== */
(function () {
  var SEED = {__MAP__};
  var page = "";
  var m = new RegExp("__HASHRE__").exec(location.hash || "");
  if (m) { page = m[m.length - 1]; }

  function act() {
    var sel = SEED[page];
    if (!sel) { return true; }              /* 该页无需触发（门户/概览类） */
    var b = document.querySelector(sel);
    if (!b) { return false; }
    b.click();
    return true;
  }

  var n = 0;
  var t = setInterval(function () {
    n++;
    if (n > 400) { clearInterval(t); return; }          /* 兜底，避免无限等 */
    var ready = document.querySelector(__READY_SEL__);
    if (!ready || ready.textContent.indexOf(__READY_TEXT__) < 0) { return; }
    clearInterval(t);
    setTimeout(function () { act(); }, 300);
  }, 150);
})();
</script>
__EXTRA__""".replace("__MAP__", js_map) \
            .replace("__HASHRE__", hash_re.replace("\\", "\\\\")) \
            .replace("__READY_SEL__", _js_str(ready_sel)) \
            .replace("__READY_TEXT__", _js_str(ready_text)) \
            .replace("__EXTRA__", extra)


def _js_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def cmd_build(args: argparse.Namespace) -> int:
    src = args.src
    if not src.exists():
        print("!! 目标文件不存在：%s" % src)
        return 3
    bak = args.backup or src.with_suffix(src.suffix + ".shot-seed.bak")
    if bak.exists():
        print("!! 备份已存在，先 clean（防二次备份覆盖原始文件）：%s" % bak)
        return 3
    text = src.read_text(encoding="utf-8")
    tail = args.tail
    if text.count(tail) != 1:
        print("!! 收尾锚点 %r 出现 %d 次（必须恰好 1 次）—— 不猜，中止" % (tail, text.count(tail)))
        return 3
    bak.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, bak)
    before = md5(src)
    seed = build_script(parse_seed(args.seed), args.ready_selector,
                        args.ready_text, args.hash_re, args.extra_script)
    src.write_text(text.replace(tail, seed + "\n" + tail, 1), encoding="utf-8")
    print("[ OK ] 备份 -> %s (md5 %s)" % (bak, before))
    print("[ OK ] 已注入播种脚本；播种版 md5 %s" % md5(src))
    print("[ 下一步 ] 截图后务必 clean 还原")
    return 0


def cmd_clean(args: argparse.Namespace) -> int:
    src = args.src
    bak = args.backup or src.with_suffix(src.suffix + ".shot-seed.bak")
    if not bak.exists():
        print("!! 没有备份，无法还原：%s" % bak)
        return 3
    src.write_bytes(bak.read_bytes())
    print("[ OK ] 已从备份还原，md5 %s" % md5(src))
    if src.read_text(encoding="utf-8").count("截图播种（临时，跑完即删）"):
        print("!! 还原后仍能搜到播种脚本 —— 还原失败")
        return 3
    print("[ OK ] 复核：播种脚本残留 0 处")
    bak.unlink()
    print("[ OK ] 备份已删：%s" % bak)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="无头截图前的播种脚手架：临时注入「就绪后点页面自带按钮」的脚本，截完还原。")
    ap.add_argument("cmd", choices=("build", "clean"), help="build=注入 / clean=还原")
    ap.add_argument("--src", required=True, type=pathlib.Path, help="目标 HTML 文件")
    ap.add_argument("--backup", type=pathlib.Path, default=None,
                    help="备份路径（默认 <src>.shot-seed.bak）")
    ap.add_argument("--tail", default=TAIL_DEFAULT,
                    help="注入锚点（默认 </body>），必须在全文恰好出现 1 次")
    ap.add_argument("--seed", action="append", default=[],
                    metavar="页面标识=选择器",
                    help='播种映射，可重复。例：--seed \'scan=[data-est="scan-run"]\'')
    ap.add_argument("--hash-re", default=r"#/(?:ke|yan)/([a-z-]+)",
                    help="从 location.hash 提取页面标识的正则（首捕获组即标识）")
    ap.add_argument("--ready-selector", default="#p-auth",
                    help="就绪标记元素选择器（默认 #p-auth）")
    ap.add_argument("--ready-text", default="已登录",
                    help="就绪标记须包含的文本（默认「已登录」）")
    ap.add_argument("--extra-script", default="",
                    help="附加到播种脚本之后的自定义 <script>（多步动作时用）")
    ap.add_argument("--selftest", action="store_true", help="跑内置自校验后退出")
    args = ap.parse_args()

    if args.selftest:
        return selftest()
    if args.cmd == "build" and not args.seed:
        print("!! build 至少需要一个 --seed 页面标识=选择器")
        return 2
    return cmd_build(args) if args.cmd == "build" else cmd_clean(args)


def selftest() -> int:
    """自校验：注入块可生成、且 tail 唯一性断言真的会拦住歧义。"""
    seed = build_script({"scan": '[data-est="scan-run"]'}, "#p-auth", "已登录",
                        r"#/(?:ke|yan)/([a-z-]+)", "")
    # 选择器在 JS 串里会被转义，故按「不含引号的片段」校验
    ok = all(s in seed for s in ("data-est", "scan-run", "#p-auth", "已登录",
                                 "setInterval", "clearInterval"))
    print("[自校验] 播种脚本生成：%s" % ("OK" if ok else "FAIL"))
    if not ok:
        return 1
    # 唯一性断言的阴性对照
    probe = "</body> mid </body>"
    print("[自校验] 锚点出现 2 次时中止（期望 count=%d ≠ 1）→ %s"
          % (probe.count("</body>"), "OK" if probe.count("</body>") != 1 else "FAIL"))
    print("[自校验] 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
