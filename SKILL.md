# Kendama Sourcing Skill V1

本文件是这个项目的运行契约:任何人(包括未来的 Claude/Codex/团队成员)在不读任何一行代码的情况下,
应该能靠这份文档安全地运行、诊断、部署这个 Skill。

---

## 1. 技能定位

**这是一个工具化的选品工作流,不是能自主决策或自动下单的 Agent。** 它只做"抓取 → 筛选 →
评估 → 推送"这条确定性流水线,买不买、要不要调整规则,永远由人工决定。

**它解决什么问题**:剑玉(kendama)跨境采销选品。每天从 Mercari、Yahoo 拍卖、Rakuten 三个日本二手平台
抓取商品,结合人工写好的规则书(`rules.md`/`cases.md`)和 LLM 判断,筛出真正值得复核的候选,推送到
飞书,并把反馈数据结构化留存,用于周期性复盘和以后调整规则。

**典型输入**:
- 三个平台的搜索结果页(标题、价格、链接、图片);
- `rules.md`(品牌/价格区间/成色规则)与 `cases.md`(历史金矿/踩坑案例),作为 LLM 判断的上下文;
- 已经积累的 SQLite 历史(`kendama.db`)和反馈历史。

**典型输出**:
- 飞书交互卡片(单条候选商品,带买入/放弃反馈按钮);
- `daily_pool.json`(当日候选池,供 22:00 汇总用);
- `kendama.db`(`scan_runs`/`listings`/`price_history`/`evaluations`/`feedback_events` 五张表的持久历史);
- `reports/weekly_review_*.md`(周报);
- `personalized_signals.md`(可审核的偏好信号)。

**不负责什么**(必须明确,避免被误用或被误解为"更聪明的系统"):
- **不自动下单、不自动拍卖**——所有候选只是推送到飞书,买不买永远是人工决定;
- **不自动修改 `rules.md`/`cases.md`/利润公式/标签阈值**——这些是人工维护的领域知识和确定性规则,任何调整都需要人工改动对应文件,系统不会自我修改判断标准;
- **不把反馈数据当"模型准确率"**——反馈只反映你个人的买入/放弃决策,不是对 LLM 判断正确性的完整、无偏验证,周报和偏好信号里都会重复这条免责声明;
- **不做 embedding、向量检索、分类器训练**——所有统计都是可解释的计数/占比聚合;
- **不做反爬对抗以外的任何自动化操作**(比如自动登录、自动绕过验证码)。

---

## 2. 正常运行命令

| 命令 | 抓取 | 调用 LLM | 推送飞书 | 写数据库 | 说明 |
|---|---|---|---|---|---|
| `python main.py` | ✅ | ✅ | ✅ | ✅ | 持续运行模式(默认):启动即扫描一次,之后每 `config.yaml` 里配置的分钟数(默认 60)扫描一次,每天 22:00 输出汇总,直到手动终止(Ctrl+C) |
| `python main.py --once` | ✅ | ✅ | ✅ | ✅ | 完整扫描一轮(真实抓取 + 真实 LLM + 真实飞书推送)后直接退出,不进入定时循环 |
| `python main.py --once --platform Mercari --keyword Kendama --max-items 30` | ✅(范围收窄) | ✅ | ✅ | ✅ | 同上,但只扫指定平台/关键词,并覆盖每平台每关键词的抓取上限;不修改 `config.yaml` 本身 |
| `python main.py --migrate-feedback` | ❌ | ❌ | ❌ | ✅(只写 `feedback_events`,只读 `feedback.db`) | 把 `feedback.db` 里既有的历史反馈一次性导入 `kendama.db.feedback_events`;幂等,可重复执行不产生重复历史 |
| `python main.py --weekly-review` | ❌ | ❌ | ❌ | ❌(只读) | 生成最近 7 天复盘报告到 `reports/weekly_review_YYYYMMDD.md` |
| `python main.py --weekly-review --days 30` | ❌ | ❌ | ❌ | ❌(只读) | 生成最近 30 天复盘报告,文件名带 `_d30` 后缀(`weekly_review_YYYYMMDD_d30.md`),不会覆盖默认 7 天的报告 |
| `python main.py --refresh-signals` | ❌ | ❌ | ❌ | ❌(只读) | 生成 `personalized_signals.md`,仅供人工审核,不会被自动注入 LLM prompt |
| `python main.py --status` | ❌ | ❌ | ❌ | ❌(甚至不会创建 `kendama.db`) | 输出只读状态摘要;`kendama.db` 不存在时明确提示"尚未初始化"并正常退出 |
| `python main.py --check-config` | ❌ | ❌ | ❌ | ❌ | 检查当前 Python、关键环境变量、反馈 URL、数据库和 `daily_pool.json`;不产生真实业务副作用 |

`--platform`/`--keyword`/`--max-items` 只能配合 `--once` 使用;`--migrate-feedback`/`--weekly-review`/
`--refresh-signals`/`--status`/`--check-config` 这五个维护命令互斥(一次只能用一个),且都不能与 `--once`/范围参数同时使用。
不带任何参数运行的持续模式行为不受这些新命令影响。

---

## 3. 数据与输出

| 文件/目录 | 内容 | 是否私有运行数据(不得提交 Git) |
|---|---|---|
| `kendama.db` | `scan_runs`/`listings`/`price_history`/`evaluations`/`feedback_events` 五张表的完整历史 | ✅(`.gitignore` 里 `*.db`/`kendama.db`) |
| `daily_pool.json` | 当日候选池,每天 22:00 汇总推送成功后清空 | ✅(`.gitignore` 里 `*.json`) |
| `feedback.db` | 兼容性反馈库:`feedback_server.py` 为兼容旧反馈链路仍会写入,并同时向 `kendama.db.feedback_events` 追加一条历史事件;`--migrate-feedback` 只用于把既有 `feedback.db` 历史幂等导入主库,不会产生重复事件 | ✅(`.gitignore` 里 `*.db`) |
| `reports/` | `--weekly-review` 生成的周报目录 | ✅(`.gitignore` 里 `reports/`) |
| `personalized_signals.md` | `--refresh-signals` 生成的偏好信号,仅供人工审核 | ✅(`.gitignore` 里显式列出) |
| `rules.md` / `cases.md` | 你的真实业务规则和历史案例 | ✅(`.gitignore` 里显式列出;仓库里的 `rules.example.md`/`cases.example.md` 是脱敏示例,可以提交) |
| `run_state.json` | 调度器 heartbeat/运行状态文件:记录最近一次事件(`scan_start`/`scan_finish`/`scheduler_heartbeat`/`process_stop` 等)、状态和 `updated_at` 时间戳,用于判断持续运行模式下调度器是否还活着;持续模式下每 60 秒刷新一次 | ✅(`.gitignore` 里 `*.json` 已覆盖)。这是运行时自动生成的文件,**不应提交、不应手动编辑** |
| `scraper_health.json` | 抓取健康检查的连续 0 计数状态,供 `scraper_health.py` 判断是否需要告警 | ✅(`.gitignore` 里显式列出) |
| `.env` | 真实密钥(DeepSeek/硅基流动/飞书 webhook/反馈签名密钥等) | ✅ 最高优先级私有文件,任何命令都不会读取后打印或写入日志/报告 |

上述文件都已经在 `.gitignore` 里,正常使用 `git add`/`git commit` 不会误提交它们。

---

## 4. 核心数据流

```
抓取 → 白名单 → URL 清洗/去重 → 历史过滤 → LLM → Python 利润标签
→ SQLite 历史 → daily_pool/飞书 → 反馈 → 周报/偏好信号
```

- **抓取**:Playwright 无头浏览器访问三平台搜索结果页,提取标题/价格/链接/图片。
- **白名单**:按 `config.yaml` 里的品牌关键词做大小写无关的子串匹配,过滤掉不相关商品。
- **URL 清洗/去重**:剔除无效 URL,同一轮内同一 URL 只保留抓取顺序中第一条(价格冲突只计数、不静默覆盖)。
- **历史过滤**:跳过"同 URL 且价格未变化"的商品,减少重复送 LLM。
- **LLM**:按批送 DeepSeek(主)/硅基流动(备),只负责识别品牌、判断金矿/踩坑案例、给出国内参考价——不计算利润。
- **Python 利润标签**:用确定性公式算成本/利润,按五档规则打标签(强推/推荐/观望/盲盒/跳过),同时判定是否降价。
- **SQLite 历史**:不论利润正负,只要完成计算就写入 `evaluations`(负利润标记"跳过",不进入推送);同时写 `listings`/`price_history` 记录商品身份和价格轨迹。
- **daily_pool/飞书**:利润 ≥ 0 的候选写入 `daily_pool.json`,取利润前 15 名推送飞书交互卡片。
- **反馈**:点击卡片按钮(带 HMAC 签名)→ `feedback_server.py` 写入 `feedback.db`,并追加一份历史事件到 `kendama.db.feedback_events`。
- **周报/偏好信号**:`--weekly-review`/`--refresh-signals` 只读 `kendama.db`,生成可读的统计报告,不反向影响前面任何一步。

### 4.1 利润五档标签

`ai_filter.py` 的 `assign_tag()` 是标签判定的唯一权威来源(`rules.md` 第七条与代码保持一致,
两者不一致时以代码为准)。LLM 输出的"是否命中金矿案例"**不影响**最终标签,只作为
`is_gold_mine` 字段留档。当前判定:

| 标签 | 利润区间(元) |
|---|---|
| 强推 | ≥ 150 |
| 推荐 | ≥ 80 且 < 150 |
| 观望 | ≥ 10 且 < 80 |
| 盲盒 | ≥ 0 且 < 10 |
| 跳过 | < 0(不推送,但仍写入 `evaluations` 留档) |

---

## 5. 运行边界与故障处理

- **无候选、全负利润不是故障**——这是正常业务结果(市场当下没有好货),`scan_runs.status` 仍然是 `'ok'`,`--weekly-review`/`--status` 能看到 `candidate_count=0` 但这不代表系统出错。
- **抓取为空**:如果某平台连续 3 轮抓到 0 条,`scraper_health.py` 会触发一次"抓取疑似异常"飞书告警,同一段异常只告警一次直到恢复;日志里的 `爬虫未返回任何数据` 是完全没抓到东西的信号。
- **LLM 全部失败 vs 真的没找到候选**:两种情况**可以**通过 `scan_runs.status` 区分。LLM 批次连续失败(重试 3 次后仍失败)会被 `LLMEvaluationError` 捕获,`scan_run.status` 写为 `llm_failed`;正常完成但市场上确实没有候选,`status` 是 `ok`(`candidate_count=0`)。排查时先跑 `venv/bin/python main.py --status`(生产环境用 `.venv/bin/python`),看最新一条 `scan_run` 的 `status` 字段:`llm_failed` 表示 LLM/API 调用本身失败,**不应该**被解读成"没有候选";只有 `status=ok` 且 `candidate_count=0` 才是正常的"市场上没有好货"。日志里的 `主 API 失败,切换备用`/`第 X 批异常 (尝试 Y/3)` 可以进一步定位是主备哪一边出的问题,但不再是判断"是否是 LLM 故障"的唯一手段。
- **飞书推送失败**:`post_to_feishu()` 只记录错误日志,不会让程序崩溃;候选和评估已经在推送之前写入 SQLite,不会因为推送失败而丢失。
- **数据库影子写入失败**:`db.py` 的所有公开函数自己吞掉异常、只记日志,绝不向上抛出,保证抓取/LLM/推送主流程不受数据库问题影响。
- **每日汇总池只在推送成功后清空**:`run_daily_summary()` 只有在飞书推送成功(`push_summary()` 返回 `True`)之后才删除 `daily_pool.json`;推送失败会保留该文件并记一条错误日志,等待下一次 22:00 触发时重试,不会丢数据。
- **反馈签名缺失或不匹配一律拒绝写入**:`feedback_server.py` 的 `signature_valid()` 是 fail-closed 设计——`FEEDBACK_SIGNING_SECRET` 未配置、请求缺 `sig` 参数、或签名对不上,一律返回 403 且不写库。卡片按钮本身在 `FEEDBACK_URL`/`FEEDBACK_SIGNING_SECRET` 未正确配置(非占位符文本)时也不会被生成,不存在"无签名保护的反馈链接"这种中间状态。
- **`--status` 和 `--weekly-review` 用于诊断**:排查问题时,先跑 `--status` 看最新一轮 `scan_run` 的状态和 `PRAGMA foreign_key_check`,再用 `--weekly-review` 看多天趋势。
- **怀疑调度器停了或卡住,先看 `run_state.json`**:持续运行模式每 60 秒写一次 `event=scheduler_heartbeat`,每次扫描开始/结束也会写 `scan_start`/`scan_finish`。如果 `updated_at` 距当前时间已经超过一个扫描周期(`config.yaml` 的 `scan_interval_minutes`)还没更新,说明进程可能已经卡死或退出,应该去查 `systemctl status kendama-scan` 和 `journalctl -u kendama-scan`,而不是先怀疑 LLM 或飞书。`run_state.json` 是运行时自动生成的文件,不应提交到 Git,也不应手动编辑。
- **真实密钥只在 `.env`**:所有命令都不会读取 `.env` 内容后打印到日志/报告/终端;`.env` 已在 `.gitignore` 中,不应该被提交。
- **生产部署 vs 本地**:生产环境只负责无人值守持续运行(`python main.py` 持续模式),推荐用 systemd 托管扫描服务(`main.py`)和反馈服务(`feedback_server.py`)两个独立 unit,崩溃自动重启、开机自启,日志用 `journalctl -u <unit> -f` 查看;本地负责开发、调试,以及 `--status`/`--weekly-review`/`--refresh-signals` 这类不产生真实抓取/LLM/飞书副作用的诊断命令。通用部署流程(本地开发 → `git commit`/`push` → 服务器 `git pull` → 安装依赖 → systemd 管理两个服务)见根目录 [`README.md`](README.md) 的"部署到云服务器"章节。

---

## 6. 验收标准

**V1 当前已具备的能力**:
- 真实抓取(Playwright,三平台)+ 品牌白名单 + LLM 判断 + Python 确定性利润/标签计算 + 飞书推送 + 带签名校验的反馈按钮;
- SQLite 完整历史:`scan_runs`/`listings`/`price_history`/`evaluations`/`feedback_events` 五张表,外键完整,负利润和降价信号都被完整持久化,不再只存在于 `daily_pool.json` 里;
- LLM 前候选清洗(无效 URL 剔除 + 同轮同 URL 去重),减少重复调用浪费;
- 反馈历史从"覆盖写入"升级为"追加留档"(`feedback_events`),并支持旧 `feedback.db` 一次性、幂等导入;
- 降价识别(基于历史价格,不新增额外调用),飞书卡片和汇总里会显示 `【降价 ¥N / P%】`;
- 周报(`--weekly-review`):扫描漏斗、利润分布、反馈复盘(窗口内/外拆分展示,不会把"刚导入的历史反馈"误报成"反馈数据缺失")、数据质量边界;
- 偏好信号(`--refresh-signals`):可解释统计,样本量门槛防止"一条反馈变结论",明确声明仅供人工审核、不自动注入 prompt;
- 只读状态命令(`--status`),不产生任何副作用;
- 命令行入口及互斥规则均有对应的离线测试覆盖。

**后置项(明确不是 V1 阻塞项,可以在后续迭代中按需处理)**:
- 关注卖家监控;
- 平台原生 item_id 解析 / URL 规范化;
- embedding、向量检索、轻量分类器等机器学习二次过滤;
- Agent 调度层(自动化决策/自动执行代理)。
