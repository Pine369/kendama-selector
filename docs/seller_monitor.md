# 重点卖家监控 V0

## 范围与边界

`seller_monitor` 是与现有 AI 选品系统独立的程序。它不导入或写入 `main.py`、`ai_filter.py`、`db.py`、`reporting.py`、`kendama.db`、`feedback.db`、`daily_pool.json` 或 `run_state.json`，不读取项目 `.env`，不使用飞书，也不调用 LLM。模型 Token 成本始终为 0。

当前提交候选是 V0 第一阶段的离线骨架。Mercari、Yahoo! Auctions 和 Rakuten 适配器只实现严格的卖家主页 URL 识别、规范化和能力声明；`fetch_seller()` 会明确拒绝真实访问。拿到代表性卖家 URL 并保存 HTML fixture 后，再实现各平台列表页解析。

## 文件与运行数据

- `seller_monitor.example.yaml`：可提交的配置样例；复制为被忽略的 `seller_monitor.yaml`。
- `seller_monitor.env.example`：PushPlus 样例；复制为被忽略的 `seller_monitor.env`。
- `seller_monitor.db`：首次扫描时创建的独立 SQLite 数据库。
- `seller_monitor_state.json`：每轮运行结束后原子替换的状态摘要。
- `seller_monitor.log`：独立轮转日志。
- `seller_monitor_notification_preview.html`：本地通知预览，默认不纳入 Git。

所有相对运行路径都以 YAML 文件所在目录为基准。`--status` 在数据库不存在时不会创建数据库；`--check-config` 不创建数据库、不访问平台、不发送消息。

## 配置

```yaml
version: 1
settings:
  database_path: seller_monitor.db
  state_path: seller_monitor_state.json
  log_path: seller_monitor.log
  notify_price_increase: false
sellers:
  - seller_key: seller_example
    seller_id: "平台原生卖家 ID，可为空"
    seller_identity_source: url_native_id
    seller_name: 显示名称
    platform: mercari
    seller_url: https://jp.mercari.com/user/profile/example_seller_id
    enabled: true
```

稳定卖家身份为 `seller_key`。未来管理界面应优先以 `platform + seller_id` 确认卖家；当平台无法离线取得原生 ID 时，用规范化主页 URL 生成内部 key。数据库还保留 `deleted_at`、`baseline_completed_at`、`last_success_at` 和 `last_error`。

目前认可的平台值为：

- `mercari`
- `yahoo_auctions`
- `rakuten`

“日拍”需要用真实主页 URL 确认。如果它就是 Yahoo! Auctions，只配置一次 `yahoo_auctions`，不能创建重复平台适配器。

## CLI

在项目根目录使用虚拟环境：

```bash
venv/bin/python -m seller_monitor.main --check-config
venv/bin/python -m seller_monitor.main --status
venv/bin/python -m seller_monitor.main --bootstrap
venv/bin/python -m seller_monitor.main --once
venv/bin/python -m seller_monitor.main --preview-notification
venv/bin/python -m seller_monitor.main --add-seller "卖家主页 URL 或包含主页 URL 的分享文本"
```

云端使用项目的 `venv/bin/python`。Windows 本地离线验收使用 `.venv\Scripts\python.exe`。所有命令可用 `--config` 和 `--env` 指定独立文件。预览可用 `--preview-output <path>` 改写输出位置。

`--add-seller` 只做离线处理：识别平台、规范化 URL、尽可能从 URL 提取 `seller_id`、展示候选记录并要求确认，然后原子更新 YAML。它不接受昵称、不访问网络、不创建数据库，也不触发扫描。无法严格识别的主页会停止，不猜平台或卖家身份。新增卖家的下一次成功完整扫描自动作为基线；显式执行 `--bootstrap` 更便于审计。

## 数据库

SQLite 使用 WAL、外键和唯一索引。七张表完全位于 `seller_monitor.db`：

| 表 | 用途 | 关键约束 |
| --- | --- | --- |
| `monitored_sellers` | 配置、软删除、基线及健康状态 | `seller_key` 主键；平台+原生 ID、平台+URL 唯一 |
| `scan_runs` | 每轮整体状态 | 独立 `run_id`；区分 success/partial_failure/failed |
| `seller_checks` | 每卖家检查与请求计数 | `run_id + seller_key` 唯一 |
| `items` | 最新商品快照 | `platform + identity_key` 唯一 |
| `price_history` | 价格和拍卖条款变化 | `item_row_id + run_id` 唯一 |
| `notification_events` | 待发送及最终通知状态 | 哈希 `event_key` 主键 |
| `notification_attempts` | 每次接口尝试审计 | `event_key + attempt_number` 唯一 |

商品身份按以下顺序生成：平台 `item_id`、规范化商品 URL、最后才是标题与图片 URL 的哈希。真实适配器必须优先返回原生 `item_id`。

## 基线、变化识别与幂等

首次成功且结果完整的卖家检查只写入商品和价格历史，并设置 `baseline_completed_at`，通知数为 0。失败或不完整结果不能完成基线，避免把半页商品误当完整历史。

基线完成后：

- 首次出现的新商品生成 `new_listing`；拍卖商品同样处理。
- 固定价从高价变低价生成 `fixed_price_drop`。
- 固定价上涨默认只进入快照和 `price_history`；仅当 `notify_price_increase: true` 时生成 `fixed_price_increase`。
- 拍卖当前竞价变化只记录，不通知。
- 可明确取得的起拍价/即决价变化生成 `auction_terms_change`。
- 完整列表中消失的商品标记为 `missing`，V0 不通知。

事件键先拼装语义字段，再计算 SHA-256：

- 新上架：`platform | identity_key | new_listing`
- 固定价降价：`platform | identity_key | fixed_price_drop | new_price`
- 显式开启的固定价涨价：`platform | identity_key | fixed_price_increase | new_price`
- 拍卖条款：`platform | identity_key | auction_terms_change | term_type | new_price`

数据库主键保证同一语义事件只能插入一次。发送前使用条件更新把事件从 `pending`/`retryable_failure` 原子改为 `sending`，只有抢占成功的进程能调用 PushPlus。进程如果在 `sending` 状态退出，下一次数据库初始化会把该事件变为 `delivery_unknown`，不自动重发。

软删除卖家后，配置同步不会自动清除 `deleted_at`。恢复时保留原来的基线和商品身份，因此不会把原有历史商品全部视为新商品。
从 YAML 移除的卖家也不会继续扫描，但数据库历史不会删除；重新加入相同 `seller_key` 后仍沿用原基线。

## PushPlus 状态语义

通知只从 `seller_monitor.env` 读取 `PUSHPLUS_TOKEN`，请求固定使用 `template=html`、`channel=wechat`。HTML 中直接包含远程图片的 `<img>` 标签，所以 PushPlus 打开的微信 HTML 页面可以展示图片，而不是只显示图片 URL。图片最终能否加载仍受源站防盗链、HTTPS 和微信图片代理限制；真实测试前应使用一个代表性图片 URL 验证。

状态含义：

- `accepted`：PushPlus HTTP 200 且同步 `code=200`，仅代表服务商接受请求，不代表微信已送达。
- `retryable_failure`：连接尚未建立即可确认失败，可在后续运行重试同一事件记录。
- `rejected`：服务商明确拒绝，不标记成功，也不自动循环发送。
- `delivery_unknown`：读取超时、未知请求异常或发送中进程退出，可能已经被接受；为防重复，不自动重发。

接口尝试和事件最终状态分别写入 `notification_attempts`、`notification_events`。没有 token 时不会实例化通知器，事件保持 `pending`，且不会误写成功。

## systemd timer

采用 timer 每 30 分钟启动一次 oneshot，比 Python 常驻调度更容易从单次失败恢复。示例文件：

- `deploy/kendama-seller-monitor.service`
- `deploy/kendama-seller-monitor.timer`

部署时核对项目路径和虚拟环境路径，再安装：

```bash
sudo cp deploy/kendama-seller-monitor.service /etc/systemd/system/
sudo cp deploy/kendama-seller-monitor.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now kendama-seller-monitor.timer
systemctl list-timers kendama-seller-monitor.timer
```

timer 使用 `OnCalendar=*-*-* *:00,30:00`、`RandomizedDelaySec=300` 和 `Persistent=true`。同一个 oneshot service 处于 active 时，systemd 不会并发启动第二份。单卖家失败记录 `partial_failure` 并继续，进程返回 0；全局故障返回非零，由 service 的 `Restart=on-failure` 恢复。

## 请求量与成本

当前离线阶段真实请求数为 0，LLM Token 为 0。真实列表适配器应优先单次打开卖家列表页，不重复打开商品详情：

- 每卖家每 30 分钟至少 1 次列表请求。
- 每卖家每天 48 次列表请求。
- N 个卖家每天基础请求量约为 `48 × N`，分页或列表字段不足时另加请求。
- timer 的 0～5 分钟随机延迟避免每次固定在整点访问。

真实适配前需要根据每个平台的分页、登录、反爬响应和列表字段完整度重新估算。监控器会分别记录列表页、详情页和网络总请求数，单平台/卖家失败不会停止其他卖家。

## 离线测试

```bash
venv/bin/python -m unittest tests.test_seller_monitor tests.test_seller_monitor_notifier -v
```

测试使用 `tests/fixtures/seller_monitor/snapshots.json` 合成快照，并 mock 所有可能的 HTTP 入口。覆盖基线、相同快照、新商品、固定价降价、涨价、拍卖竞价、新拍卖、卖家隔离、失败/未知通知状态、重启、软删除/恢复、只读 CLI、严格添加卖家、HTML 图片和 PushPlus accepted 语义。

## 下一阶段输入

实现真实解析前，每个平台建议提供 1～2 个确认关注的卖家主页 URL，并标明：

1. 平台名称，尤其说明“日拍”是否就是 Yahoo! Auctions；
2. 页面是否混合普通商品和拍卖商品；
3. 未登录浏览器能否看到完整商品列表；
4. 一条卖家主页分享文本样例；
5. 闲置/售出商品是否仍显示；
6. 允许离线保存用于测试的页面 HTML，以及其中可脱敏的 seller/item 标识。

在得到明确许可前，不访问真实平台，也不发送真实微信消息。

## Mercari 受控捕获结论（2026-07-23）

开发阶段使用 `scripts/capture_mercari_profile.py` 对一个明确授权的卖家主页进行过一次无登录 Playwright 导航。脚本使用全新临时浏览器配置，不复用或保存 Cookie/localStorage，不打开商品详情，不滚动或翻页；图片、字体、媒体和分析请求被阻止。原始候选只保存在仓库外，仓库内仅保留递归脱敏通过的 JSON fixture。

首屏商品来自匿名 XHR REST 响应：

```text
GET https://api.mercari.jp/items/get_items
query names: limit, seller_id, status, with_auction
```

页面还自然产生了一个同结构请求，额外包含 `exclude_archived_item`。响应顶层为 `result`、`meta`、`data`；首屏包含 30 个商品对象，`meta.has_next=true`。可离线解析的字段包括 `id`、`name`、`price`、`thumbnails`、`status`、`seller.id`。已观察到的状态枚举为 `on_sale`、`sold_out`、`trading`。

重要限制：

- 请求没有 Cookie、Authorization 或 CSRF header，但包含由页面上下文生成的 `dpop` header 和 `x-platform`；没有主动重放接口，因此尚不能认定可由普通 HTTP client 匿名复现。
- 首屏响应只有 `has_next`，没有明确 next cursor、总数或下一页 token。不能猜测 `pager_id` 就是 cursor。
- 捕获商品对象没有明确 listing type、auction、current bid、start price 或 buyout 字段；`price` 和 `is_no_price` 不能作为拍卖判据。
- `parse_items_response()` 因此对真实 fixture 返回 `listing_type="unknown"`。只有响应出现明确 `is_auction`、sale type 或 auction 对象时才映射为 `auction`/`fixed`。
- 首屏 `has_next=true` 时 parser 必定返回 `complete=False`。缺失稳定 item ID、重复 ID、空响应或分页信息未知也不能完整。
- 当前数据库约束只接受 `fixed`/`auction`，因此正式 `fetch_seller()` 仍未启用，不能执行完整 bootstrap。需要先取得可靠拍卖类型和分页机制，或经审查扩展数据模型；不得静默把 unknown 当 fixed。

离线 fixture：

```text
tests/fixtures/seller_monitor/mercari/items_page_1_sanitized.json
```

fixture 保留真实字段层级、类型、状态和 `has_next`，但 seller ID、item ID、标题、图片 URL、价格和卖家名称均已替换；token、实验、追踪和个人字段被删除或脱敏。递归检查要求真实 seller ID、全部真实 item ID/标题/图片 URL、个人值、JWT 形态和非 `example.com` 图片域名残留均为 0。
