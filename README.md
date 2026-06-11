# kendama-selector

剑玉跨境选品助手

我做跨境剑玉采销,每天要在煤炉上翻几百条商品。
这个工具把我的选品判断写成规则,让 AI 替我做初筛。

> **项目状态**:个人使用中,持续优化。

---

## 为什么做这个

跨境采销的核心,是利用信息差和判断力。

剑玉这个品类小众但稳定。日本玩家圈子大,新品和老款流通活跃,
玩家对品相要求高——有轻微使用痕迹的剑玉,会以远低于全新的价格出售。
而国内的新玩家在找品质二手货,中间的差价里有利润。

但问题是:

- 每天有上百件新商品上架,大部分是基础款或残次品
- 真正值得拍的可能只有 3-5 件,有时甚至没有
- 我做了两年,知道哪些品牌的哪些款式值得买
- 但每天花在"翻商品"上的时间,占了 80%
- 为了第一时间拍下有价值的商品,还得频繁刷新

我想把浏览和初筛这件事让 AI 做。
但前提是:AI 必须懂我的判断标准——
什么样的产品有价值,什么样的痕迹会让价格腰斩,什么样的款式是坑。

这些知识在我脑子里,不在 AI 训练数据里。

所以这个项目的核心,不是用 AI——
**是把我的判断标准和选品经验,结构化成 AI 能读懂的规则**。

---

## 真实效果

<div align="center">
  <img src="https://github.com/user-attachments/assets/71247e74-04cd-4d9b-9d22-4979ae836378" width="800">
</div>

左图为 23:50 系统推送的某商品,右图为约 2 小时后的煤炉页面状态,
商品已被买家拍下(标记 SOLD)。

剑玉中古市场的好货流通速度往往以小时计,
人工浏览很难全天盯盘。

不过单一案例不能证明系统普遍有效。
我会持续记录推送和实际成交的对应关系,后续给出更完整的统计。

---

## 系统架构

```mermaid
flowchart TB
    subgraph Sources["数据源"]
        A1[Mercari]
        A2[Yahoo Auctions]
        A3[Rakuten]
    end

    subgraph Pipeline["处理流水线"]
        B[Playwright 爬虫]
        C[品牌白名单预筛]
        D[LLM 分批筛选]
        E[LLM 汇总报告]
    end

    subgraph Knowledge["领域知识"]
        K1[rules.md<br/>选品规则]
        K2[cases.md<br/>实战案例]
    end

    subgraph Outputs["输出"]
        O1[微信推送<br/>每 60 分钟]
        O2[每日汇总<br/>22:00]
    end

    Storage[(daily_pool.json<br/>当日候选池)]

    Sources --> B
    B --> C
    C -->|匹配目标品牌| D
    K1 --> D
    K2 --> D
    D --> E
    D --> Storage
    E --> O1
    Storage -->|每日去重| O2

    style Knowledge fill:#fff4e6
    style Sources fill:#e6f3ff
    style Outputs fill:#e6ffe6
```

### 核心流程

1. **抓取**:Playwright 模拟浏览器访问三个平台,提取商品标题、价格、链接、图片
2. **本地预筛**:基于品牌白名单做字符串匹配,过滤大部分无关商品。
   这一步省下了大量的 LLM token 消耗
3. **LLM 分批筛选**:按 15 条一批送给 DeepSeek,带上 `rules.md` 和 `cases.md` 作为上下文
4. **LLM 汇总**:把所有批次的候选合并,生成手机端友好的精简报告
5. **推送**:通过 PushPlus 推送到微信,每小时一次
6. **每日汇总**:22:00 触发,把当天所有候选去重后生成全天报告

### 运行方式

本地开发完成后部署到腾讯云轻量应用服务器,
通过 `nohup` 让 Python 进程后台运行,
日志统一写入 `output.log`,可以随时通过 `tail -f` 查看。
即使本地电脑关机,系统仍然每 60 分钟扫描一次。

<div align="center">
  <img src="https://github.com/user-attachments/assets/2a22133c-39b8-4abd-b97e-4836e332ee69" width="700">
</div>

上图为腾讯云服务器上的实时日志:
本轮抓取 302 条 → 本地预筛后剩 64 条 → 5 批送 DeepSeek 评估 → 筛出 24 条候选 → 推送成功。
全过程约 50 秒,DeepSeek API 5 次调用全部 200 OK。

### 设计决策

- **用 Playwright 而不是 requests**:三个平台都有反爬,
  Playwright 模拟真实浏览器更稳定
- **先本地预筛再送 LLM**:泛词搜索一次返回 300+ 商品(本轮 302 条),大部分品牌不在范围内。
  本地白名单匹配可剔除约 80% 无关品牌,实际只送 50-80 条给 LLM,显著降低调用成本。
- **主备 API**:国内访问 API 偶有抖动,主用 DeepSeek 官方,
  备用硅基流动作为兜底,不阻塞业务
- **temperature=0**:LLM 默认有随机性,同一商品两次评估可能给出不同结论。
  设为 0 后输出稳定,便于后续做效果回归

---

## 快速开始

### 环境要求

- Python 3.10+
- 操作系统:macOS / Linux / Windows

### 申请所需的 API Key

| 服务 | 用途 | 申请地址 |
|------|------|---------|
| DeepSeek | 主用 LLM | https://platform.deepseek.com |
| 硅基流动 | 备用 LLM | https://siliconflow.cn |
| PushPlus | 微信推送 | https://www.pushplus.plus |

### 安装

```bash
# 克隆仓库
git clone https://github.com/yourname/kendama-selector.git
cd kendama-selector

# 安装依赖
pip install -r requirements.txt

# 安装 Playwright 浏览器
playwright install chromium

# 配置环境变量
cp .env.example .env
# 编辑 .env,填入你的 API Key
```

### 运行

```bash
python main.py
```

启动后会立即执行一次扫描,然后进入定时模式:
- 每 60 分钟扫描一次
- 每天 22:00 输出全天汇总

### 适配其他品类

`rules.md` 和 `cases.md` 是这个项目的核心。
如果想用这个框架做其他品类,按以下结构改写这两个文件:

- `rules.md`:你的判断规则(品牌、价格区间、款式偏好等)
- `cases.md`:你的实战案例(过去赚到的、踩过的坑)

代码层面不需要改动,LLM 会读这两个文件作为上下文。

### 部署到云服务器

我个人使用腾讯云轻量应用服务器(2 核 2G,Ubuntu)长期运行。
部署后用 `nohup` 让进程后台运行,日志写入 `output.log`:

```bash
# 上传项目到服务器
scp -r kendama-selector/ user@your-server:/home/user/

# SSH 登录服务器
ssh user@your-server
cd kendama-selector

# 安装依赖(只需一次)
pip install -r requirements.txt
playwright install chromium
playwright install-deps  # Linux 需要额外的系统依赖

# 配置 .env 后,后台运行
nohup python main.py > output.log 2>&1 &

# 查看运行日志
tail -f output.log

# 停止运行
ps aux | grep main.py
kill <PID>
```

每 60 分钟自动扫描一次,24 小时不间断。
即使本地电脑关机或断网,系统照常运行。

---

## 运行成本

按当前使用频率估算的月度成本:

| 项目 | 月成本(人民币) | 说明 |
|------|--------------|------|
| 腾讯云轻量服务器 | 约 30 元 | 2 核 2G,新用户首年低价 |
| DeepSeek-V4 API | 约 5-15 元 | 每小时一次,日均约 24 次调用 |
| PushPlus 推送 | 0 元 | 免费额度足够个人使用 |
| 域名/SSL | 0 元 | 不需要 |
| **总计** | **约 35-45 元/月** | |

整套系统的运营成本约等于一次外卖钱,
对个人采销业务来说是合理的杠杆比。

如果用 GPT、claude 替代 DeepSeek,API 成本会上升 5-10 倍,
对剑玉这种客单价不高的品类不划算。
这也是我选 DeepSeek 的核心理由——**便宜且够用,比最强重要**。

---

## 项目结构

```
kendama-selector/
├── main.py              主入口,定时调度
├── scraper.py           三平台爬虫
├── ai_filter.py         LLM 评估与汇总
├── rules.example.md     选品规则(示例)
├── cases.example.md     实战案例(示例)
├── config.yaml          关键词和品牌白名单配置
├── requirements.txt     依赖
├── .env.example         环境变量模板
└── .gitignore
```

---

## 数据闭环与持续优化

这个系统不是"一次性写完就用"——
**它的价值随着真实数据的积累而增长**。

### 当前的做法

每天我会:
1. 看 AI 推送的候选商品,标记是否同意它的判断
2. 实际拍下后,记录最终的国内成交价
3. 把"AI 推荐对了"和"AI 推荐错了"的案例总结成规则
4. 周末更新 `rules.md` 和 `cases.md`

这是一个**用真实反馈持续优化 Prompt 上下文**的过程,
不是模型微调——核心模型(DeepSeek)不变,
变的是喂给模型的领域知识。

### 已落地的迭代

基于历史成交数据,我从 `rules.md` v0 迭代到当前版本,
主要改进:

- 把"品牌优先级"细化到"品牌 + 漆面 + 款式"三层判断
- 增加重量门槛(剑玉玩家对重量敏感,过轻过重都会压价,服从正态分布)
- 把过往交易中的金矿案例和踩坑案例,整理进 `cases.md`

### 下一步计划

- [ ] 把决策反馈从手动记录改为半自动(推送时附带反馈按钮)
- [ ] 加入历史成交数据库,让 LLM 在评估时参考类似商品的真实成交价
- [ ] 简单的复盘面板(每周自动统计准确率)
- [ ] 把 `daily_pool.json` 迁移到 SQLite
- [ ] 为核心纯函数加单元测试

---

## FAQ

**Q: 为什么不直接 fine-tune 一个模型?**

A: Fine-tune 需要大量标注数据和算力,对个人项目不划算。
更重要的是,我的领域知识本来就在持续变化(每周都有新案例),
fine-tune 之后再修改的成本很高。
用 prompt + 规则书的方式,我可以随时改 `rules.md` 立即生效,
迭代速度远高于 fine-tune。

**Q: 为什么不用 LangChain / Dify / Coze 这类框架?**

A: 我这个项目的核心逻辑很简单——
爬虫 → 预筛 → LLM 评估 → 推送。
用框架反而会引入不必要的复杂度。
直接写 Python + OpenAI SDK,代码总量不到 500 行,
调试和修改都很直接。
框架适合更复杂的多 Agent 协作场景,不是这种简单的串行流程。

**Q: 为什么不用 GPT-4 而用 DeepSeek?**

A: 对剑玉这种客单价 300-800 元的品类,API 成本必须算清楚。
DeepSeek-V4 的能力对"按规则筛选商品"这个任务完全够用,
但价格只有 GPT-4 的 1/10 左右。
ToB 销售的本质是 ROI,选模型也一样——**够用且经济**比"最强"重要。

**Q: 适配其他品类需要改什么?**

A: 只需要重写 `rules.md` 和 `cases.md` 两个文件。
代码层面不需要任何改动。
我未来计划把这套框架应用到攀岩装备、滑雪装备等我熟悉的其他品类,
验证"领域知识结构化"这个方法论的可复用性。

**Q: 一轮扫描全流程要多久?**

A: 从启动扫描到推送到手机,约 1 分钟。
其中爬虫约 30-40 秒(三个平台串行),
LLM 评估约 20-30 秒(分批 + 并发限制)。
对"每小时一次"的使用频率来说,完全够用。

---

## 局限与诚实说明

- **不适合大规模商用**:本项目为个人采购场景设计,
  商业化需要更严谨的反爬、数据库和监控
- **领域规则需要重写**:`rules.md` 是我的剑玉经验,
  换品类必须重写,代码框架可复用
- **LLM 评估不是完全可靠**:即使有规则书,LLM 偶尔会产生幻觉或绕过规则,
  最终决策仍需人工把关
- **代码不是工业级**:没有单元测试,没有完整的错误监控,
  作为个人工具够用,作为生产系统还不够

---

## 技术栈

- **语言**:Python 3.10+
- **爬虫**:Playwright (无头浏览器)
- **LLM**:DeepSeek-V4 (主) / DeepSeek-V4 via SiliconFlow (备)
- **调度**:schedule
- **推送**:PushPlus + 微信
- **图片代理**:wsrv.nl (绕过微信内置浏览器的图片防盗链)
- **部署**:腾讯云轻量应用服务器 (Ubuntu, nohup 后台运行)

---

## 致谢

- [Playwright](https://playwright.dev/) —— 无头浏览器抓取
- [DeepSeek](https://www.deepseek.com/) —— LLM 推理
- [PushPlus](https://www.pushplus.plus/) —— 微信消息推送
- [腾讯云轻量应用服务器](https://cloud.tencent.com/product/lighthouse) —— 24/7 部署运行环境
- [wsrv.nl](https://wsrv.nl/) —— 图片代理服务

---

## License

[MIT](LICENSE)

仓库内的 `rules.example.md` 和 `cases.example.md` 是脱敏示例,
不包含完整的商业判断逻辑。
代码框架自由使用,使用导致的任何损失由使用者自行承担。

---

## 开发说明

我没有计算机或软件工程背景。
本项目的代码部分,主要在 AI 协作下完成。

我负责:领域规则(rules.md / cases.md)、需求设计、决策判断、工程整合。
代码实现:Claude (Opus 4.7)、Gemini Pro 和 DeepSeek 提供了大量帮助。

我相信这种"领域专家 + AI 协作"的工作方式会变得越来越普遍。