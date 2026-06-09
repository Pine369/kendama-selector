# kendama-selector

剑玉跨境选品助手

我做跨境剑玉采销，每天要在煤炉上翻几百条商品。
这个工具把我的选品判断写成规则，让 AI 替我做初筛。

## 为什么做这个

跨境采销的核心，是利用信息差和判断力。

剑玉这个品类小众但稳定——日本的玩家圈子在退坑、清仓，
国内的新玩家在找品质二手货。中间的差价里有钱赚。

但问题是：

- 每天有上百件新商品上架，大部分是基础款或残次品
- 真正值得拍的可能只有 3-5 件
- 我做了三年，知道哪些品牌的哪些款式值得买
- 但每天花在"翻商品"上的时间，占了 80%

我想把"翻商品"这件事让 AI 做。
但前提是：AI 必须懂我的判断标准——
什么是 Sulab 的灵魂卖点，什么样的痕迹会让梦园无双腰斩，
Krom 的基础款为什么国内卖不动。

这些知识在我脑子里，不在 AI 训练数据里。

所以这个项目的核心，不是用 AI——
**是把我脑子里的判断标准，结构化成 AI 能读懂的规则**。

## 🏗️ 系统架构

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

    Storage[(daily_pool.json<br/>当日候选池)]

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