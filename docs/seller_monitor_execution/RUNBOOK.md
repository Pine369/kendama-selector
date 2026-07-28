# Seller Monitor V0 执行 Runbook

## 安全边界

- 所有自动验证均使用离线 fixture、mock transport、mock PushPlus 和临时 SQLite；
- 不运行 `--bootstrap`、`--once`、真实通知命令或捕获脚本；
- 不读取或输出 `seller_monitor.env`；
- 历史运行诊断只用 SQLite read-only URI、状态摘要和脱敏日志；
- 不启用 systemd，不部署云端。

## 标准验证命令

Windows 虚拟环境：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test*.py"
.\.venv\Scripts\python.exe -m unittest test_offline_fixes.py
.\.venv\Scripts\python.exe -m compileall -q seller_monitor scripts tests ai_filter.py db.py feedback_server.py main.py reporting.py scraper.py scraper_health.py test_offline_fixes.py
git diff --check
```

专项测试：

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_seller_monitor_latest_window
.\.venv\Scripts\python.exe -m unittest tests.test_seller_monitor_test_notification_from_seller
```

## Git 审核

```powershell
git status --short
git diff
git diff --cached
git show --stat --oneline HEAD
git show --name-only --format=fuller HEAD
```

提交前扫描已暂存内容中的真实 seller ID、凭据赋值、JWT 形态、非示例图片域名和真实商品文本。允许出现 `Cookie`、`DPoP`、`Authorization` 等安全术语，但不允许出现其真实值。

## 历史数据库诊断

- 使用 `mode=ro` 和 `PRAGMA query_only=ON`；
- seller key 仅输出哈希摘要；
- 有序 item identity 仅输出数量和摘要；
- 不查询或显示原生 seller ID、标题、图片和商品 URL；
- 读取操作后核对数据库、状态和日志的修改时间未变化。

## 发布边界

本任务只把代码推送到当前 `origin/main`。云端部署、timer 启用、真实扫描和真实通知均保留为人工步骤。
