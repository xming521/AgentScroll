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

AgentScroll 通过主动搜索和热榜学习两条流程发现内容，补充正文、评论与相关报道，再生成可学习的知识和即时分享。

完成[推理配置](docs/usage.md#环境与推理配置)后，可以执行一次热榜学习，也可以按配置的时间定时运行：

```bash
agentscroll hotlist run
agentscroll hotlist run --scheduled
```

需要后台运行时，可以使用内置 NewsNow 服务的 Docker Compose 部署：

```bash
cp settings.example.jsonc settings.jsonc
cp .env.example .env
mkdir -p outputs
# 填写模型配置和 API Key 后启动
docker compose up -d --build
```

完整配置和日志命令见[Docker 后台运行](docs/usage.md#docker-后台运行)。

## 文档

- [系统设计与内容规则](docs/design.md)：业务目标、系统边界、平台采集、热点筛选、知识卡和分享规则。
- [CLI 使用参考](docs/usage.md)：配置、命令、数据格式、平台支持和测试命令。

## Acknowledgements

- [last30days-skill-cn](https://github.com/Jesseovo/last30days-skill-cn)
- [NewsNow](https://github.com/ourongxing/newsnow)
