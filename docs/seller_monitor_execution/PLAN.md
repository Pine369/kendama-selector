# Seller Monitor V0 执行计划

本文档是本任务工作顺序的唯一事实来源。Milestone 必须按顺序完成；每阶段验证通过并更新 `STATUS.md` 后才能继续。

## Milestone 0：现状审计

- 检查 `git status`、HEAD、本地 `origin/main` 和分支关系；
- 列出修改、未跟踪文件、已有提交和未提交主题；
- 将每个 diff 块归类；
- 扫描凭据、真实卖家和商品数据；
- 更新 `STATUS.md`。

## Milestone 1：旧商品重新进入窗口

- 确认新品资格同时要求“位于首个重叠项之前”和“历史从未出现”；
- 验证 baseline 商品离窗、改价、回到窗口头部仍为 0 新品通知；
- 验证相同标题/图片但新 item ID 在重叠项之前生成一次新品；
- 若有缺陷，做最小修复并运行专项测试。

## Milestone 2：诊断 partial_failure

- 只读检查 `run_e69cd3666ba747618b745909ea293df4` 的数据库、状态和日志；
- 记录检查状态、transport 原因、响应阶段、coverage、完整性、分页/access-wall/HTTP/超时证据；
- 确认失败运行没有覆盖有效窗口；
- 证据不足处明确标记，不通过网络重试补证。

## Milestone 3：完成 latest_window

- 验证 coverage、`complete`、`window_complete`、`has_next` 语义；
- 验证基线、相同窗口、头部新品、尾部补入、无重叠、重启恢复和事件幂等；
- 验证 latest window 永不 `mark_missing`，不改变窗口外商品状态。

## Milestone 4：真实商品测试通知

- 允许 `coverage=latest_window`、`window_complete=True`、`has_next` 任意布尔值；
- 要求非空且全部商品为有效 `on_sale` 商品；
- 验证不写数据库/状态/事件、人工确认、单次发送、Token 隐藏和 GBK 安全；
- 只运行 mock 网络测试。

## Milestone 5：完整验证

- Seller Monitor 全部离线测试；
- 原 AI 项目全部离线回归；
- `compileall`；
- `git diff --check`；
- import 副作用检查；
- 敏感信息扫描。

## Milestone 6：拆分提交

按实际历史和未提交内容保持主题独立：

1. 已有：`feat(seller-monitor): add real-item notification test command`
2. 已有：`feat(seller-monitor): add Mercari latest window monitoring`
3. `fix(seller-monitor): accept latest window for real-item notification test`
4. `fix(seller-monitor): require never-seen items for new listings`
5. `fix(seller-monitor): preserve failed window diagnostics`
6. 执行文档和最终文档状态单独提交，避免混入业务修复

每次提交后核对文件列表、敏感信息和主题专项测试。文档出现混合修改时使用 `git add -p`。

## Milestone 7：最终验证与推送

- 再次运行全部验证；
- 确认工作区干净且提交无秘密/真实商品数据；
- 推送到 `origin/main`；
- 确认本地 `main` 与 `origin/main` 同步；
- 将 `STATUS.md` 更新为最终状态并确保最终文档提交已推送。
