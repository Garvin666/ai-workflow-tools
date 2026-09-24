# ai-workflow-tools

ai-workflow 自研工具与技能的**公开留痕仓库**（源码同步 + 版本一致）。与本体仓
[ai-workflow-skill](https://github.com/Garvin666/ai-workflow-skill) 成对使用：
本体仓放流程手册与全套脚本，本仓放「声明为自研工具并已登记」的脚本副本，供公开查阅与版本核对。

## 功能说明

工具按用途分三组（每个脚本头部都有 `[自研工具]` 标注：名称 / 用途 / 适用场景 / 仓库）：

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
└── scripts/          # 23 个自研脚本（头部均有 [自研工具] 标注）
```

## 注意事项

- **以本地为准、单向同步**：本仓内容由 `push_router.py` / `publish_tools.py` 从本地技能根与工作区
  单向推送而来，请勿直接在远端编辑；同名文件在本体仓与本仓的 blob sha 必须**逐字节一致**（有机器判据）。
- **同步走通道**：更新用 `publish_tools.py push --file <本地文件> --dest scripts/xxx.py --apply`，
  默认 dry-run，`--apply` 才写；推送后用 `verify_push.py` 做独立验收。
- **凭据**：推送走 `gh auth token` / `GITHUB_TOKEN` 环境变量，脚本不落盘、不打印完整凭据。
- **文本换行**：推送通道对文本类文件做 CRLF→LF 归一化后再比对 sha，克隆后请勿混用两种行尾提交。
