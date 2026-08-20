# AgentScroll

**Let AI agents browse the internet like humans do.**

AgentScroll is an internet-awareness layer for AI agents that continuously discovers and reads trending topics, news, memes, and online discussions, keeping agents in sync with what people are talking about.

```text
AgentScroll
├── Scroll      刷热点
├── Search      主动搜索
├── Read        阅读帖子
├── Meme        学习新梗
└── Memory      更新互联网认知
```

## 工作方式

信息源 → 采集 → 标准化与去重 → 补充正文、评论与相关报道 → LLM 分析与汇总 → Briefing / 推送

## 文档

配置、推理、数据采集、NewsNow 接入和真实平台测试见 [技术文档](docs/technical.md)。

采集器的支持平台、输出格式和数据边界见 [Collector 文档](agentscroll/collector/README.md)。

## Acknowledgements

- [last30days-skill-cn](https://github.com/Jesseovo/last30days-skill-cn)
- [NewsNow](https://github.com/ourongxing/newsnow)
