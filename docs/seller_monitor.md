# 重点卖家监控 V0

## 范围与边界

`seller_monitor` 是与现有 AI 选品系统独立的程序。它不导入或写入 `main.py`、`ai_filter.py`、`db.py`、`reporting.py`、`kendama.db`、`feedback.db`、`daily_pool.json` 或 `run_state.json`，不读取项目 `.env`，不使用飞书，也不调用 LLM。模型 Token 成本始终为 0。

Mercari 已实现基于系统 Chrome 的最新在售窗口适配；Yahoo! Auctions 和 Rakuten 仍只提供严格的卖家主页 URL 识别、规范化和能力声明，不进行真实抓取。各平台运行状态和业务数据库彼此不会接入现有 AI 选品流程。

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
venv/bin/python -m seller_monitor.main --test-notification
venv/bin/python -m seller_monitor.main --test-notification-from-seller
venv/bin/python -m seller_monitor.main --add-seller "卖家主页 URL 或包含主页 URL 的分享文本"
```

云端使用项目的 `venv/bin/python`。Windows 本地离线验收使用 `.venv\Scripts\python.exe`。所有命令可用 `--config` 和 `--env` 指定独立文件。预览可用 `--preview-output <path>` 改写输出位置。

`--add-seller` 只做离线处理：识别平台、规范化 URL、尽可能从 URL 提取 `seller_id`、展示候选记录并要求确认，然后原子更新 YAML。它不接受昵称、不访问网络、不创建数据库，也不触发扫描。无法严格识别的主页会停止，不猜平台或卖家身份。新增卖家的下一次成功完整扫描自动作为基线；显式执行 `--bootstrap` 更便于审计。

`--test-notification` 只读取 `--env` 指定的独立环境文件，确认 `PUSHPLUS_TOKEN` 非空但不显示其值。命令先展示 Mercari 合成消息，只有人工输入 `y` 或 `yes` 才调用 PushPlus 一次；其他输入均取消。它不读取卖家 YAML、不访问平台、不启动监控器，也不创建或修改数据库及正式通知事件。测试发送不自动重试。

Windows CLI 不要求执行 `chcp 65001` 或设置永久环境变量。UTF-8 输出流正常显示 `¥8,000`；当 stdout/stderr 使用无法编码半角日元符号的 GBK/cp936 严格模式时，控制台自动降级为 `JPY 8,000`，微信 HTML 内容仍保留 `¥8,000`。重定向、`StringIO` 和不支持 `reconfigure()` 的流使用同一安全输出边界。

`--test-notification-from-seller` 是一次性真实商品展示验收入口。它只允许配置中恰好一个启用且未删除的 Mercari 卖家，并只接受 `coverage=latest_window`、`window_complete=True` 的正式 adapter 结果；`has_next=true/false` 均可，因为命令只需一件当前在售商品，不要求遍历全部在售列表。窗口必须非空，且所有候选商品均为 `on_sale` 并具有图片、标题、有效价格和商品链接。命令选择第一件商品，先显示图片域名等摘要并要求 `y/yes` 确认，然后最多发送一次明确标记为测试的消息。它不实例化监控 service 或 repository，不修改数据库、基线、状态文件或正式通知事件，也不自动重试。

## 数据库

SQLite 使用 WAL、外键和唯一索引。八张表完全位于 `seller_monitor.db`：

| 表 | 用途 | 关键约束 |
| --- | --- | --- |
| `monitored_sellers` | 配置、软删除、基线及健康状态 | `seller_key` 主键；平台+原生 ID、平台+URL 唯一 |
| `scan_runs` | 每轮整体状态 | 独立 `run_id`；区分 success/partial_failure/failed |
| `seller_checks` | 每卖家检查与请求计数 | `run_id + seller_key` 唯一 |
| `seller_latest_windows` | 每次 Mercari 最新窗口的有序身份列表 | `seller_key + scan_run_id` 唯一；保留顺序、窗口上限、`has_next` 和 coverage |
| `items` | 最新商品快照 | `platform + identity_key` 唯一 |
| `price_history` | 价格和拍卖条款变化 | `item_row_id + run_id` 唯一 |
| `notification_events` | 待发送及最终通知状态 | 哈希 `event_key` 主键 |
| `notification_attempts` | 每次接口尝试审计 | `event_key + attempt_number` 唯一 |

商品身份按以下顺序生成：平台 `item_id`、规范化商品 URL、最后才是标题与图片 URL 的哈希。真实适配器必须优先返回原生 `item_id`。

`items.listing_type` 接受 `fixed`、`auction` 和 `unknown`。本项目不提供旧实验 schema 的通用迁移框架；如果本地曾创建过旧版 `seller_monitor.db`，应删除该实验数据库并由当前版本重新创建。正式运行数据启用后再另行设计可审计迁移。

## 基线、变化识别与幂等

Mercari V0 使用 `coverage=latest_window`：每次只保存 `status=on_sale`、按页面最新顺序返回的前 30 件商品。第一次有效窗口只写入商品、价格历史和有序窗口，并设置 `baseline_completed_at`，通知数为 0。这里完成的是“最新窗口基线”，不代表抓完卖家的全部在售商品。

基线完成后：

- 当前窗口先查找第一个也存在于上次窗口的商品；只有它之前的陌生商品可生成 `new_listing`。
- 第一个重叠商品之后才进入窗口的陌生商品视为历史补入，只静默写入，不生成通知。
- 两次窗口完全无重叠时不通知，保存当前窗口作为下一轮基准，并把该卖家检查记为 `no_overlap`。
- `latest_window` 永不调用 `mark_missing`，窗口之外的商品不会被改成 `missing`，也不据此判断下架或售出。
- `unknown` 新商品照常生成一次 `new_listing`；其后价格涨跌只写入 `price_history`，不生成价格通知。
- 只有前后类型均为 `fixed`，且价格从高变低时，才生成 `fixed_price_drop`。
- 前后类型均为 `fixed` 的上涨默认只进入快照和 `price_history`；仅当 `notify_price_increase: true` 时生成 `fixed_price_increase`。
- 拍卖当前竞价变化只记录，不通知。
- 只有前后类型均为 `auction`，可明确取得的起拍价/即决价变化才生成 `auction_terms_change`。
- `unknown` 与已知类型之间的切换只更新快照并建立下一轮比较基准，不追溯生成价格事件，也不会把 `unknown` 自动转换为 `fixed`。
- 只有未来明确返回 `coverage=full` 且 `complete=True` 的平台结果，才允许对完整列表中消失的商品调用 `mark_missing`；Mercari 最新窗口模式不适用。

价格变化只比较本轮最新窗口中实际出现的商品。已知商品重新进入窗口时可与数据库旧价格比较；窗口外商品本轮没有实时价格证据，因此 V0 不承诺覆盖其降价。每日深度价格检查和指定商品价格监控属于后续独立能力。

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

测试通知沿用相同状态语义，但完全绕过 `notification_events` 和 `notification_attempts`：`accepted` 仍不代表已送达，其他结果在本次命令中均不自动重试。

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
- 当前数据库约束已接受 `unknown`，且通知卡片显示为“待确认”；不得静默把 `unknown` 当作 `fixed`。

### Mercari V0 浏览器 transport

`MercariAdapter.fetch_seller()` 使用 Playwright 启动系统 Chrome（`channel="chrome"`），每个卖家创建一个新的非持久化 browser context。它只导航一次卖家主页、等待首屏自然产生的 `get_items`、点击一次“仅显示当前在售商品”，然后只解析点击后自然产生的响应；不会主动重放 API、访问商品详情、读取请求头或保存 Cookie/DPoP。

筛选响应必须同时具有 `limit=30`、`status=on_sale`、`with_auction=true` 和 `exclude_archived_item=true`，HTTP 状态为 200，所有商品状态为 `on_sale`，并且 `meta.has_next` 明确、身份和响应结构完整。满足这些条件时返回 `coverage=latest_window`、`window_complete=True`、`complete=False`：`has_next=true` 只表示卖家还有窗口外商品，不会使当前最新 30 件失效，也不会触发翻页。`complete=False` 始终明确表示没有声称抓完全部在售商品。

验证码、登录墙、主页或列表请求 403/429、超时、解析错误、缺失 `has_next` 和身份缺失均返回 `window_complete=False`，不会更新有序窗口、完成基线或生成通知。有效最新窗口即使 `has_next=false` 也仍是 `latest_window`，不会获得调用 `mark_missing` 的权限。

运行诊断只保留导航数、总请求数、`get_items` 请求/响应数、筛选参数白名单、商品数、`has_next` 和安全错误分类；不保留响应正文、真实请求 URL、请求头或浏览器存储。本功能目前只通过 mock Playwright 离线测试，尚未执行真实 bootstrap。

离线 fixture：

```text
tests/fixtures/seller_monitor/mercari/items_page_1_sanitized.json
```

fixture 保留真实字段层级、类型、状态和 `has_next`，但 seller ID、item ID、标题、图片 URL、价格和卖家名称均已替换；token、实验、追踪和个人字段被删除或脱敏。递归检查要求真实 seller ID、全部真实 item ID/标题/图片 URL、个人值、JWT 形态和非 `example.com` 图片域名残留均为 0。
