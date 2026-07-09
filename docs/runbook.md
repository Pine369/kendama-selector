# 运维手册(生产环境)

本文件记录的是**这一套线上部署的实际状态**,不是通用部署模板——通用的部署流程见
根目录 [`README.md`](../README.md) 的"部署到云服务器"章节;这里只写"这台服务器
现在具体长什么样",方便日后排查问题或换人接手时不需要重新还原配置。

---

## 1. 生产环境快照

| 项目 | 值 |
|---|---|
| 云服务商 / 地域 | 腾讯云,东京 |
| 项目路径 | `/home/ubuntu/kendama-selector` |
| 扫描服务 | `kendama-scan.service`(运行 `main.py` 持续模式) |
| 反馈服务 | `kendama-feedback.service`(运行 `feedback_server.py`,监听 `127.0.0.1:5001`) |
| 反馈公网入口 | `https://feedback.pine369.com/feedback` |
| 反向代理 | Caddy,反代 `feedback.pine369.com` → `127.0.0.1:5001` |
| 5001 端口 | **只监听本机,不应直接暴露公网**;对外只能通过 Caddy 的 80/443 访问 |
| 对公网开放的端口 | 22(SSH)/ 80(HTTP,自动跳转/证书验证)/ 443(HTTPS)/ ICMP(Ping) |

> 如果这份快照和服务器实际配置出现偏差(换了域名、换了路径、换了云厂商等),
> 应该第一时间更新这个文件,而不是让文档继续描述过去的状态。

---

## 2. 日常巡检命令

在服务器上,依次执行:

```bash
# 1. 扫描服务是否在跑
sudo systemctl status kendama-scan

# 2. 反馈服务是否在跑
sudo systemctl status kendama-feedback

# 3. Caddy 是否在跑
sudo systemctl status caddy

# 4. 最近日志(各看最近 50 行即可,不需要 -f 常驻)
journalctl -u kendama-scan -n 50 --no-pager
journalctl -u kendama-feedback -n 50 --no-pager
journalctl -u caddy -n 50 --no-pager

# 5. 项目自带的只读状态摘要(不扫描、不调用 LLM、不推飞书)
cd /home/ubuntu/kendama-selector
.venv/bin/python main.py --status

# 6. 启动前配置检查(不扫描、不调用 LLM、不推飞书)
.venv/bin/python main.py --check-config

# 7. 调度器心跳是否还在更新(判断进程是否卡死的最直接方式)
cat run_state.json
# 关注 updated_at 是否比当前时间早了超过一个扫描周期(config.yaml 的 scan_interval_minutes,默认 60 分钟)

# 8. 验证 HTTPS 反馈入口本身是否可用(不会触发真实反馈写入,只看响应)
curl -I https://feedback.pine369.com/feedback

# 9. 验证 5001 确实没有直接暴露在公网
#    在服务器本机执行应该能连上:
curl -I http://127.0.0.1:5001/health
#    从服务器外部(如本地电脑)用公网 IP 直连应该连不上或超时:
curl -I --max-time 5 http://<服务器公网IP>:5001/health
```

**判断标准**:第 9 步,本机 `127.0.0.1:5001` 能连通、外部公网 IP:5001 连不通/超时,
才说明 5001 收口是有效的。如果外部也能连上,说明防火墙/安全组规则被改动过,
需要立即检查并收紧。

---

## 3. 常见故障排查

| 现象 | 排查步骤 |
|---|---|
| **系统看起来停了**(飞书好久没推送) | 1. `systemctl status kendama-scan` 看进程是否存活;2. `cat run_state.json` 看 `updated_at` 是否卡住;3. `journalctl -u kendama-scan -n 100` 看最近报错 |
| **LLM 失败**(怀疑 DeepSeek/硅基流动调用出问题) | 1. `.venv/bin/python main.py --status` 看最新 `scan_run` 的 `status` 是否为 `llm_failed`;2. `llm_failed` 就是 LLM/API 本身出了问题,不是"没有候选";3. 查日志里的 `主 API 失败,切换备用` / `第 X 批异常` 定位主备哪一边失败 |
| **飞书推送失败** | `post_to_feishu()` 只记日志不会让进程崩溃,查 `journalctl -u kendama-scan` 里的 `飞书推送失败`/`飞书网络异常`/`飞书响应非 JSON` 字样;候选本身已经落库,不会因为推送失败丢数据 |
| **反馈按钮打不开 / 点击无反应** | 1. 先看飞书卡片本身是否带按钮——`FEEDBACK_URL`/`FEEDBACK_SIGNING_SECRET` 未配置或是占位符时,卡片会没有按钮;2. `.venv/bin/python main.py --check-config` 会打印 `FEEDBACK_URL` 是否为 HTTPS |
| **HTTPS 失效**(证书过期/域名解析问题) | 1. `curl -I https://feedback.pine369.com/feedback` 看是否报证书错误;2. `systemctl status caddy` + `journalctl -u caddy` 看 Caddy 是否正常续期证书;3. 确认域名 DNS 解析未变更 |
| **5001 意外暴露到公网** | 立即按第 2 节第 9 步验证,如果外部真的能连通,检查云安全组规则和本机防火墙(`ufw`/`iptables`),确认只放行 22/80/443,不应该单独放行 5001 |
| **改 `.env` 后忘记重启服务** | `.env` 变量在进程启动时读取一次,改完必须 `sudo systemctl restart kendama-scan kendama-feedback` 才会生效;建议改完立即跑一次 `--check-config` 确认新值已生效 |

---

## 4. 更新流程

```bash
# 1. 服务器上拉取最新代码
cd /home/ubuntu/kendama-selector
git pull

# 2. 依赖有变更时才需要(平时可跳过)
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium

# 3. 部署前先跑一次配置检查,不产生真实副作用
.venv/bin/python main.py --check-config

# 4. 重启两个服务
sudo systemctl restart kendama-scan kendama-feedback

# 5. 看日志确认正常启动
journalctl -u kendama-scan -n 30 --no-pager
journalctl -u kendama-feedback -n 30 --no-pager
```

**不要覆盖**:`.env`、`kendama.db`、`feedback.db`、`daily_pool.json`、日志、
`run_state.json`。`git pull` 不会动到这些文件(均已 `.gitignore`),但如果手动
用 `scp`/`rsync` 同步整个目录到新服务器,要注意排除它们,避免用旧数据覆盖新数据。

---

## 5. 备份建议

以下只是**建议的命令草案**,本文件不代为执行,是否需要设置成定时任务由你决定。

需要备份的文件:

- `kendama.db` —— 唯一的完整历史数据源(扫描记录/商品/价格轨迹/评估结果/反馈事件),丢失不可恢复
- `.env` —— 真实密钥,丢失需要重新申请/生成
- `daily_pool.json` —— 当日候选池,重要性较低(每天会重新累积),丢失影响当天汇总但不影响历史
- Caddyfile(反代配置)—— 丢失需要重新配置域名/证书路径
- `kendama-scan.service` / `kendama-feedback.service`(systemd unit 文件,通常在 `/etc/systemd/system/`)—— 丢失需要重新编写

示例备份命令(手动执行或加入 cron,执行前请自行确认目标路径存在且有权限):

```bash
BACKUP_DIR=/home/ubuntu/backups/$(date +%Y%m%d)
mkdir -p "$BACKUP_DIR"

cp /home/ubuntu/kendama-selector/kendama.db "$BACKUP_DIR/"
cp /home/ubuntu/kendama-selector/.env "$BACKUP_DIR/"
cp /home/ubuntu/kendama-selector/daily_pool.json "$BACKUP_DIR/" 2>/dev/null || true
cp /etc/caddy/Caddyfile "$BACKUP_DIR/"
cp /etc/systemd/system/kendama-scan.service "$BACKUP_DIR/"
cp /etc/systemd/system/kendama-feedback.service "$BACKUP_DIR/"
```

建议定期清理过旧的备份目录,避免无限占用磁盘。
