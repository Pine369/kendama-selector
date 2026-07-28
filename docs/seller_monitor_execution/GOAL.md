# Seller Monitor V0 收口目标

## 最终状态

把当前 Mercari Seller Monitor 收口为经过真实历史证据核对、完整离线验证、可以部署但尚未自动上线的稳定 V0。

完成标准：

- 所有未提交修改经过审查并按主题提交；
- Mercari `latest_window` 语义、窗口恢复和新品幂等行为正确；
- 历史商品离开窗口后重新出现不会误报新品；
- 指定 `partial_failure` 有可审计的脱敏诊断结论，失败不会覆盖有效窗口；
- `--test-notification-from-seller` 接受有效最新窗口且保持零数据库副作用；
- Seller Monitor、原 AI 项目回归、`compileall`、import 副作用检查和 `git diff --check` 全部通过；
- 提交内容不含真实 Token、Cookie、DPoP、seller ID 或商品数据；
- 所有提交推送到 `origin/main`，最终工作区干净；
- `docs/seller_monitor.md` 与实现一致，`STATUS.md` 记录最终状态和人工步骤。

## 明确不做

本轮不做小程序、Web、闲鱼、Yahoo/Rakuten 真实适配、Mercari 全量分页、全量历史价格扫描、独立仓库迁移、云端部署、systemd timer 启用、新架构重构或风格性大改。

## 停止条件

只有需要新的真实 Mercari 访问、真实 PushPlus、真实凭据、第三方登录、timer/云端部署、真实数据删除、不可逆操作、范围扩展、重大业务选择、环境限制或无法安全修复的持续测试失败时，才暂停并请求人工操作。
