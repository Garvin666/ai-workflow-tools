# ai-workflow-tools

ai-workflow 自研工具与技能的**公开留痕仓库**（源码同步 + 版本一致）。与本体仓
[ai-workflow-skill](https://github.com/Garvin666/ai-workflow-skill) 成对使用：
本体仓放流程手册与全套脚本，本仓放「声明为自研工具并已登记」的脚本副本，供公开查阅与版本核对。

## 功能说明

工具按用途分**九组**（每个脚本头部都有 `[自研工具]` 标注：名称 / 用途 / 适用场景 / 仓库）：

**① 工作流门禁、推送与守护（与本体仓 `scripts/` 同名同源）**

| 脚本 | 用途 |
| --- | --- |
| gate.py | 运行时闸门：删改既有文件前做范围 × 风险判定，fail-closed |
| guard_constants.py | 跨模块同名常量同源守卫（漂移即 FAIL，自带阴性对照） |
| outbound_scan.py | 出站前扫描：含本机用户名主目录路径即 FAIL |
| push_router.py | 本体仓 / 本仓的分流推送路由（默认 dry-run，交叉校验登记表） |
| push_ontology.py | 本体仓专用通道（Git Data API 增量，支持删除、锁基线） |
| publish_tools.py | 本仓专用通道（单文件 contents API 推送，CRLF 归一化） |
| verify_push.py | 推送后独立验收器（与推送通道零代码共享） |
| git_check.py | 项目 Git 开发规范机器校验（分支 / commit 注释类型） |
| perf_baseline.py | 本地可测耗时基线（环境一致性前置判据 + 同树对照） |
| gen_skill_index.py | 技能库索引生成 / 新鲜度校检 |
| homework_model.py | 作业模式聚合模型（W1–W5 → mode / confidence，纯标准库） |
| laya_client.py | 四个 judge 调用本地 Laya 决策服务的客户端（不可用即降级） |

**② 参考资产管理**

| 脚本 | 用途 |
| --- | --- |
| refs-fetch.sh | 批量抓取 GitHub 开源仓库为本地只读参考资产（浅克隆后删 .git） |
| refs-check.sh | 对已抓取的参考资产目录做完整性抽查并输出 Markdown 表 |
| refs_clean.py | 批量删除参考资产下各项目的 .git，输出清理前后体积对照 |

**③ 建模评估与交付校验（数模工作区沉淀）**

| 脚本 | 用途 |
| --- | --- |
| analyze_selection_bias.py | 把「CV − 留出测试」差距分解为公共项与选型偏差（差分法） |
| kfold_benefit.py | 量化「准入判据升级」的收益与代价：三臂对照 + 受控成本 A/B |
| stability_probe.py | 多划分种子扫描：判据决策翻转率与增益跨种子离散度 |
| build_proposal.py | 把补丁施加到底座拷贝产出提案（命中且仅命中一次，否则报错） |
| repro_summary_bug.py | 缺陷复现 + 补丁有效性验证的「双跑」套路 |
| gen_upgrade_doc.py / check_upgrade_doc.py | 《代码升级说明》生成器 + 其回归判据（成对使用） |
| gen_opt_report.py / verify_opt_report.py | 优化报告渲染（data-key 锚点）+ 三态校验器（成对使用） |
| check_test_access.py | 留出测试集访问台账的两条不变量审计（防泄漏） |

**④ 前端视觉评审与配色数值闸（演示台 / 设计稿改造沉淀）**

| 脚本 | 用途 |
| --- | --- |
| shot_seed.py | 无头截图「播种」脚手架：等就绪标记出现后 click() 页面自带按钮，让默认是空状态的数据页自动进入有数据状态；跑完从备份还原并复核残留（残留非 0 即 exit 3）。**只走用户真会走的路径**，不造元素、不调内部函数 |
| palette_check.py | 配色数值闸：四条 WCAG 对比度（页面底 + 纯白卡面双底各算一次）+ 主按钮白字承载 + 全量语义色两两 CIEDE2000 ΔE00 ≥2.0。令牌**唯一真源 = 交付件**（从 `<style>` 读，缺令牌 exit 3）。零依赖单文件，`--selftest` 跑 Sharma 标准测试集 |

**⑤ 独立自研技能（完整技能包，非单文件脚本）**

| 技能 | 用途 |
| --- | --- |
| skills/knowledge-cram-report/ | 知识点恶补 PDF 报告机制：L2 任务收尾把「可迁移 + 可解释」的知识点抽出来 → 去重 → 按五段式通俗化 → 渲染带公式与自测题的 PDF → **按项目名分层归档**，附 FSRS 形状复习调度与索引台账。自带机器判据 V1–V11（`--selftest` 阴阳对照） |

**⑥ 每日运维（量化平台沉淀）**

| 脚本 | 用途 |
| --- | --- |
| ops_backup.py | 每日分层备份：SQLite 走 `VACUUM INTO` 在线备份、小文件复制、大文件**同盘硬链接**，产出带 sha256 清单的 `manifest.json`；`--drift` 另查硬链接漂移（快照污染 / 链接能力失效 → P1，正常更替只知会） |
| ops_report.py | 每日运维日报与告警登记：汇总站点探活 / 数据链路 / 数据质量 / 资源 / 备份，产出七段日报与三级（P0/P1/P2）告警流水；折入四支巡检结论，**告警去重用稳定机器键而非含当次读数的文案** |
| ops_rotate.py | 每日运维产物轮转器：按保留期清理「本系统自己生成、名字带完整日期」的产物。**白名单容器式**（只删白名单形状，保护清单优先）＋幂等 ＋默认 dry-run |
| ops_probe.py | 每日运维四支巡检**单入口**：`--check {resource\|slot\|quality\|public}` = T1 资源 / T7 收盘档链路汇总 / T8 数据质量复盘 / T10 公网可达性核验，各自落 JSON 产物供日报折入。判据要点：**「拿到 200」≠「拿到目标资源」**（承重件是资产逐文件 sha256 ＋ index 引用集不变量，故 SPA 回退 200+HTML 会被判失败）；**「未设阈值」单列 `unverified`**，不与「通过」同形 |

**⑦ 开源项目收录、筛选与复核（知识库沉淀）**

| 脚本 | 用途 |
| --- | --- |
| parse_search.py | 解析并合并多次 GitHub 搜索的 TSV 输出，去重后按「已归档 / 近 6 个月无提交」两条硬线筛出候选池。**踩坑已固化进代码**：同一仓库被多个查询命中时字段会打架（实测出现过一次标「已归档」、另一次标「未归档」）→ 合并规则取 **archived 逻辑或 + pushed_at 取最新**，而非「首次出现者优先」（后者会让错的那一份胜出、把已归档库当活跃库留下）。参数化：`--src / --out / --date / --stale-before` |
| select_picks.py | 候选池 JSON → 入选名单 JSON：按人工分组编 id、从池中取实测指标、把不在池中的名字列进 `missing`（不静默丢）；`REJECTED`（命中阈值但被剔除的项）连同理由一并落在产物里，避免下次重复劳动。参数化：`--candidates / --out / --groups / --date`；内置的分组定义是某轮真实清单，可作样例 |
| quarterly_recheck.py | 对已定稿的「开源项目精选清单」做**周期时效复核**：拉取条目当前指标，比对归档状态 / 停更硬线 / Star·Fork·许可证漂移，产出可直接照做的复核报告 —— **只报告、不改清单**（清单含判断成分，机器不替人决定去留）。另有 `--lint-only`：**离线**只查格式与内部一致性（不联网、不落文件），新增条目后自检用。参数化：清单路径与复核日期均可指定 |

**⑧ 长文档三态交付（Markdown → 样式化 HTML → PDF）**

| 脚本 | 用途 |
| --- | --- |
| build_paper_pdf.py | Markdown **单一事实源**编译为样式化 HTML，再调 Chrome/Edge headless 打印 PDF：封面 / 目录 / 分节分页 / 居中公式块 / 中文字体。三项机器自检：① 文件头 `%PDF-` ② MD 标题清单在 HTML 中零缺失 ③ MD 的 `**` 强调对与 HTML `<strong>` 数量一致（**防 CommonMark 强调边界静默失效** —— 正文裸单星号 `x*` 会吃掉后一个 `**`，此项就是抓它的）。浏览器定位走环境变量枚举（`ProgramFiles` / `ProgramW6432` / `SystemDrive` / `LOCALAPPDATA`），可用 `CHROME_PATH` 显式指定，源码内不写死本机路径。默认不产出 HTML 交付物（中间 HTML 交付后清理） |
| build_book.py | 多分片 Markdown 书稿合并编译为样式化 HTML，再打印 PDF：封面 / 目录 / 分章分页 / 代码高亮 / 中文字体。参数化：`--book-dir / --out-dir / --name / --parts`，不带参数时按脚本所在位置推导默认值（书稿在 `<脚本目录>/book`）；浏览器定位同 build_paper_pdf。与 build_paper_pdf 的分工：单文件论文走前者（带公式块与强调渲染对账），多分片书籍走本工具 |

**⑨ 知识库日常运维（资讯链路与日清）**

| 脚本 | 用途 |
| --- | --- |
| daily_brief.py | 多源 RSS 聚合（预印本 / 期刊会议 / 行业博客 / 新闻媒体 / 技术社区 / 政策机构）→ 规则粗筛（时效 + 每源限额 + 去重）产出候选 → 模型精选 → 渲染「首页速览 + 分栏简讯」PDF 日报并归档入知识库。工作区根走 `KB_ROOT` 环境变量，缺省时由 `__file__` 上溯两级兜底 |
| weekly_digest.py | 把每日简报素材**蒸馏成周报**：汇总上周 7 天 `items.json` → 跨天去重 → 候选清单 → 模型精选 → 周报 MD/HTML/PDF + 领域笔记素材。两段式（`prepare` 出候选 / 渲染分离），脚本不做语义判断，「最有效」由模型判定。数据源是已落盘的 `items.json` 而非 RSS —— 故日链路不用改，且天然不丢数据 |
| daily_clean.py | 知识库日清清洗：扫描检测 去重 / 死链 / 命名 / 无效残留 / 缺失字段 五类问题；把「白名单内的浏览器测试探针残留」移入**可还原隔离区**（仅移动、绝不删除）；产出中文日清报告与机器可读问题清单。三模式：`--report`（默认，零动作）/ `--apply`（移入隔离区）/ `--restore <manifest>`（还原）。工作区根走 `KB_ROOT` |

## 安装与使用

```bash
# 克隆后按需单文件调用（大部分脚本为纯标准库，零第三方依赖）
git clone https://github.com/Garvin666/ai-workflow-tools.git
python scripts/gate.py check <路径> --base <工作区根> --intent write
python scripts/outbound_scan.py <待外发文件...>
```

推荐与本体仓一起安装（工具的运行入口、参数口径、登记表都在本体侧）：

```bash
git clone https://github.com/Garvin666/ai-workflow-skill.git ~/.workbuddy/skills/ai-workflow
```

## 目录结构

```
ai-workflow-tools/
├── README.md
├── scripts/          # 自研脚本（头部均有 [自研工具] 标注）
└── skills/           # 独立自研技能（完整技能包：SKILL.md + scripts/ + references/ + assets/）
    └── knowledge-cram-report/
```

## 注意事项

- **以本地为准、单向同步**：本仓内容由 `push_router.py` / `publish_tools.py` 从本地技能根与工作区
  单向推送而来，请勿直接在远端编辑；同名文件在本体仓与本仓的 blob sha 必须**逐字节一致**（有机器判据）。
- **同步走通道**：更新用 `publish_tools.py push --file <本地文件> --dest scripts/xxx.py --apply`，
  默认 dry-run，`--apply` 才写；推送后用 `verify_push.py` 做独立验收。
- **凭据**：推送走 `gh auth token` / `GITHUB_TOKEN` 环境变量，脚本不落盘、不打印完整凭据。
- **文本换行**：推送通道对文本类文件做 CRLF→LF 归一化后再比对 sha，克隆后请勿混用两种行尾提交。
