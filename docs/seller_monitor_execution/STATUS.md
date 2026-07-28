# Seller Monitor V0 执行状态

最后更新：2026-07-28（Asia/Shanghai）

## 当前状态

`in_progress` — Milestone 6 已完成，正在执行 Milestone 7 最终验证与推送。

## Milestone

| Milestone | 状态 | 摘要 |
| --- | --- | --- |
| 0 现状审计 | completed | HEAD `22b3c5b`；本地领先 origin/main 2 个已拆分提交；未提交内容分为测试通知、旧商品修复和执行文档三类；敏感扫描通过 |
| 1 旧商品回窗 | completed | 新品现在同时要求窗口前缀资格和 `change.is_new`；17 项专项测试通过，覆盖旧 item ID 回窗改价 0 通知及新 item ID 正常通知 |
| 2 partial_failure | completed | 指定 run 在 `waiting_for_initial_items` 阶段超时，0 个 get_items 响应；失败 run 未写窗口；增加最小修复以保存安全 transport 原因和请求计数 |
| 3 latest_window | completed | 38 项窗口与 transport 测试通过；覆盖 has_next true、头部新品、尾部补入、无重叠、重启、幂等和永不 mark_missing |
| 4 测试通知 | completed | 20 项专项测试通过；接受有效 latest-window 和任意 has_next 布尔值，保持人工确认、单次 mock 发送、GBK 安全及数据库/状态零副作用 |
| 5 完整验证 | completed | Seller Monitor 150 项、原项目 172 项通过；compileall、diff check、import 零副作用和敏感扫描通过 |
| 6 拆分提交 | completed | 测试通知修复 `5d92f7d`、never-seen 修复 `252bbb9`、失败诊断修复 `1223e79` 已按主题独立提交；执行文档单独提交 |
| 7 推送与收口 | in_progress | 正在进行最终全量验证、安全扫描和 origin/main 同步 |

## 已知边界

- 不执行新的真实 Mercari 访问；
- 不发送真实 PushPlus；
- 不启用 timer，不部署云端；
- 不读取或记录真实凭据。

## 审计摘要

- 已有独立提交：`b91a51c` 真实商品测试通知入口、`22b3c5b` latest-window 监控；
- 测试通知修复仅涉及 `seller_monitor/main.py`、对应测试和相关文档段落；
- 旧商品修复仅涉及 `seller_monitor/monitor.py`、对应 latest-window 测试和相关文档段落；
- 失败诊断修复独立保存 transport 安全错误和 FetchResult 请求计数，不改变 Mercari 抓取策略；
- 数字型 Mercari item URL 命中仅来自明确的合成测试 ID，不是真实商品数据。

## partial_failure 脱敏结论

- 历史 run：`run_e69cd3666ba747618b745909ea293df4`；整体 `partial_failure`，单卖家检查 `failed`，事件和通知均为 0；
- transport 日志：`waiting_for_initial_items_failed:TimeoutError`，未捕获首个或筛选后的 `get_items` 响应；
- 该失败结果语义为 `coverage=latest_window`、`window_complete=False`、`has_next=None`；
- 没有持久证据显示 403、429、登录墙或验证码命中；若主文档/access-wall 命中，代码会留下不同错误；附属资源 403 不是此次记录的失败原因；
- 失败 run 没有 `seller_latest_windows` 记录，上一次有效窗口（6 个 identity、limit 30、has_next false）仍为最新窗口；
- 历史 `seller_checks` 的 0 请求计数来自 monitor 丢弃失败 FetchResult 的诊断缺陷；修复后未来失败会保留安全原因和已有计数。

## 剩余人工步骤

自动阶段结束后，人工决定何时进行一次受控 Mercari 恢复扫描、何时部署云端和启用 timer。
