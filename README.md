# kendama-selector

剑玉跨境选品助手

我做跨境剑玉采销,每天要在煤炉、日亚、乐天上翻几百条商品。
这个工具把我的选品判断写成规则,让 AI 替我做初筛。

## 为什么做这个

跨境采销的核心,是利用信息差和判断力。

剑玉这个品类小众但稳定，日本的玩家丰富，清仓速度也较快,
有很多轻微使用痕迹的商品会以较低的价格出售，国内的新玩家在找品质二手货。

但问题是:

- 每天有上百件新商品上架,大部分是基础款或残次品
- 真正值得拍的可能只有 3-5 件
- 我做了两年,知道哪些品牌的哪些款式值得买
- 但每天花在"翻商品"上的时间,占了 80%
- 同时为了确保及时购买，搜索、刷新的频率也要很高

我想把找产品这件事让 AI 做。
但前提是:AI 必须懂我的判断标准
什么样的产品有卖点,什么样的痕迹会让价格腰斩,
什么样的产品是坑。

这些知识在我脑子里,不在 AI 训练数据里。

所以这个项目的核心,不是用 AI 帮我做
**是把我脑子里的判断标准,结构化成 AI 能读懂的规则**。

##  系统架构

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
        O1[微信推送<br/>每 30 分钟]
        O2[每日汇总<br/>22:00]
    end

    Storage[(SQLite 数据库<br/>kendama_radar.db)]

    Sources --> B
    B --> C
    C -->|匹配目标品牌| D
    K1 --> D
    K2 --> D
    D --> E
    D --> Storage
    E --> O1
    Storage -->|每日提取去重| O2

    style Knowledge fill:#fff4e6
    style Sources fill:#e6f3ff
    style Outputs fill:#e6ffe6

### 核心流程

1. **抓取**:Playwright 模拟浏览器访问三个平台,提取商品标题、价格、链接、图片
2. **本地预筛**:基于品牌白名单做字符串匹配,过滤 90% 的无关商品。
   这一步省下了大量的 LLM token 消耗
3. **LLM 分批筛选**:按 15 条一批送给 DeepSeek,带上 `rules.md` 和 `cases.md` 做上下文
4. **LLM 汇总**:把所有批次的候选合并,生成手机端友好的精简报告
5. **推送**:通过 PushPlus 推送到微信,每 30 分钟一次
6. **每日汇总**:22:00 触发,把当天所有候选去重后生成全天报告

### 设计决策

- **为什么用 Playwright 而不是 requests**:三个平台都有强反爬,Playwright 模拟真实浏览器更稳定
- **为什么先本地预筛再送 LLM**:Mercari 一次返回 100+ 商品,80% 是无关品牌。
  本地预筛后只剩 15-20 条,LLM 调用成本降低约 75%
- **为什么主备 API**:国内访问海外 API 偶有抖动,主用 DeepSeek 官方,
  备用硅基流动作为兜底,任一可用就不阻塞业务
- **为什么 temperature=0**:LLM 默认有随机性,
  同一商品两次评估可能给出不同结论。设为 0 后输出稳定可复现

