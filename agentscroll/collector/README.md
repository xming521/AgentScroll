# Collector

该目录是 AgentScroll 的独立数据采集与知识落盘层，负责联网获取各平台条目并为每次搜索保存一份 JSON 元数据和一份紧凑知识文本，不包含 Agent Skill、聚类、综合分析和报告渲染。

## 包含的数据源

- 微博：`mcp-server-weibo`，自动获取访客 Cookie，无需开放平台 Token；搜索后通过 MCP 单帖详情能力读取完整正文/长微博，再通过微博评论 API 读取评论。
- 小红书：Playwright/XHR 和站外 URL 发现；正文从 `window.__INITIAL_STATE__.note.noteDetailMap[*].note.desc` 提取，专用 DOM `#detail-desc .note-text` 作为后备；评论从详情页自动发起的签名响应中提取。
- B站：公开搜索 API 和 Playwright 兜底，并通过视频详情 API 读取完整简介、通过 reply API 读取评论。
- 知乎：从站外发现公开 URL，随后用 Playwright 读取正文，并按回答、文章、问题访问 root-comments API。
- 抖音：Playwright 和站外 URL 发现；正文从分享页 `window._ROUTER_DATA` 的 `videoInfoRes.item_list[*].desc` 提取，专用 DOM `.video-msg-container` 作为后备；评论从移动分享评论 API 提取。
- 微信公众号：搜狗微信搜索，并继续访问微信文章页提取正文；仅在文章声明留言区时请求精选留言，无会话权限时标记为 `blocked`。
- 今日头条：服务端渲染搜索页、热榜和站外 URL 发现，并访问移动详情页或来源页提取正文；评论从文章页自身发出的动态签名响应中提取。

## 使用方式

直接调用 Python 接口：

```python
from agentscroll.collector import collect

result = collect(
    "AI Agent",
    scene="informal",
    days=30,
    depth="quick",
    output_dir="outputs/knowledge",
)
```

或者从仓库根目录运行命令行入口：

```bash
python3 -m agentscroll.collector "AI Agent" \
  --scene informal \
  --days 30 \
  --depth quick \
  --output-dir outputs/knowledge
```

命令行状态仍以 JSON 输出。每个平台包含 `items` 和 `error` 两个字段；单个平台失败不会中断其他平台。每次调用会用同一文件名生成一份 `.json` 元数据和一份面向大模型学习的紧凑 `.txt` 知识文件；返回值中的 `knowledge_metadata_file` 和 `knowledge_file` 分别给出两者路径。所有默认产物统一位于 `outputs/`：热榜快照、知识文件、即时分享队列、日志和测试证据分别位于 `outputs/hotlists/`、`outputs/knowledge/`、`outputs/shares/`、`outputs/logs/` 和 `outputs/test_artifacts/`。支持显式目录参数的接口仍可由调用方指定其他目录。

未显式指定 `sources` 时，采集器按场景自动选择平台：

- `formal`（正式）：知乎、微信公众号、今日头条，适合公告、政策、报告、研究、新闻、事件进展和正式分析。
- `informal`（非正式）：微博、小红书、B站、抖音，适合网络梗、流行语、网友表达、二创、吐槽和评论区语境。
- `auto`（默认）：根据查询中的通用意图词判断；没有明显信号的短主题词先走非正式场景做语境发现，较长查询走正式场景。

这里的“正式/非正式”描述内容形态，不代表来源可信度。微信公众号和今日头条仍可能返回自媒体；事实判断仍需结合账号身份和原始证据。显式传入 `sources` 或 `--sources` 时，指定的平台优先于场景路由。

一个搜索生成一对同名文件，跨平台条目统一写入同一条时间线。JSON 元数据保留：

- 查询词、采集时间、场景路由和查询日期范围。
- 每条内容的平台、发布时间、正文和原文 URL。原始 collector 返回保留完整正文，写给模型的 knowledge 正文默认最多 2000 个字符。
- 非零互动量和最多 10 个话题标签。
- 去重后按点赞数降序保留最多 10 条评论；点赞数相同则保持平台原顺序。紧凑 TXT 只写查询词、按时间排列的正文、标签、互动量和评论原文，不展示元数据、原文链接或评论点赞数；微博的“图片评论”“转发微博”等无正文占位评论会被过滤。

知识条目按发布时间升序排列，越新的内容越靠近文件末尾；无法确定发布时间的内容放在所有已知日期内容之前。正文和评论中的换行会压缩为空格，减少格式占用。超过上限的正文按段落或句子边界截断，并标明原始字符数；可用 `AGENTSCROLL_CONTENT_CHAR_LIMIT` 调整，允许范围为 200 至 20000 个字符。

作者、平台内部 ID、图片、相关性解释、抓取状态、正文/评论提取路径和 DOM 证据不会写入知识文件。正文为空或缺少有效原文 URL 的条目也不会混入知识文件。

NewsNow 热榜使用独立的分阶段入口。`fetch_newsnow_hotlists()` 只保存榜单 JSON 和标题 TXT；模型第一轮筛选后，可以调用 `generate_selected_hotlist_knowledge_cards()` 读取对应平台正文和评论，再用一次批量 LLM 请求同时判断证据是否足够、生成最多 20 张知识卡并编排即时分享，不额外调用一次模型。分享队列默认写入 `outputs/shares/`，格式是“大致内容或标题 + 真实 URL + 一条评论”；真实评论由本地 ID 精确回填，没有合适评论时明确标记为“我的评论”。默认还会用本地公共搜索设施为 `needs_research` 并行取得少量网页标题、摘要和 URL，再用一次批量 LLM 请求直接生成替换卡；它不启用 Codex 原生 Web Search，也不重跑全部 20 条。知识卡主轮和补搜轮都默认使用 `effort="xhigh"`，只覆盖这两次请求，不修改全局配置；可分别用 `generation_effort` 和 `supplement_effort` 调整。也可传 `supplement_failed=False` 关闭补搜。一次运行只保存包含全部知识卡的批次 JSON 和批次 TXT，不再按话题拆文件；最终分享队列单独保存。默认“综合”组不包含抖音；抖音仅在显式选择“短视频”组时拉取。

```python
from agentscroll.workflows import (
    generate_selected_hotlist_knowledge_cards,
    select_hotlist_first_pass,
)

snapshot = "outputs/hotlists/example_newsnow_综合.json"
selection = select_hotlist_first_pass(snapshot)
result = generate_selected_hotlist_knowledge_cards(
    snapshot,
    selection,
    output_dir="outputs/knowledge",
)

print(result["complete_count"], result["needs_research_count"])
print(result["batch_json_file"])
print(result["batch_text_file"])
print(result["share_text_file"])
```

每个话题始终保留代表入口；存在 `related_ids` 时，证据采集优先加入不同平台，再按原顺序加入同平台入口，默认每个话题最多 3 个。这条规则用于增加跨平台语境并控制输入规模，不依赖某个样本标题。`generate_hotlist_knowledge_cards()` 也可以直接消费已经采好的 evidence，便于失败重试时避免重新访问平台。需要其他分享目录时可传入 `share_output_dir`。

已有 evidence 和第一轮结果时，可以单独调用 `supplement_hotlist_knowledge_cards(evidence, initial_result)`。补搜的目标是形成聊天谈资，不做逐项严谨事实核验；新闻或指控中的未确认内容仍会保留“网传”“帖子称”等限定。补搜输出会写入新的批次 JSON/TXT，不覆盖第一轮产物，并在 JSON 中保存最多 3 个实际使用的网页来源。默认补搜 `effort="xhigh"`，不会修改全局推理配置。

支持详情访问的平台会为条目增加统一正文信息：

- `content`：详情页/API 中读取到的正文或社交帖发布文案。
- `content_type`：`article-body`、`post-description`、`search-summary` 或 `unavailable`。
- `content_source`：正文的实际读取路径，例如 `bilibili-view-api`、`wechat-article-page`。
- `content_url`：本次读取正文所访问的最终 URL。

`search-summary` 表示详情访问失败后仅保留搜索摘要，不能当作完整正文。B站和抖音等视频平台的正文指发布者文案/完整简介，不包含视频字幕或音视频转写。

微博、小红书、B站、抖音、微信公众号和今日头条的最终返回列表只保留成功读取到详情正文/发布文案的条目。详情页被封、只有搜索摘要、正文为空或少于 20 字的候选会被丢弃；如果所有候选都不可读，该平台返回空列表，由真实测试直接判定失败。

支持评论的平台会增加：

- `comments`：评论原文列表；每条包含 `comment_id`、`text`，以及接口能直接提供的 `created_at`、`likes`、`reply_count`、`parent_comment_id`。不保存评论作者。
- `comments_status`：`readable`、`empty`、`blocked` 或 `unavailable`，避免把被封或要求登录误报成“没有评论”。
- `comments_source` 和 `comment_extraction_evidence`：评论来自哪个平台 API 或浏览器响应。

默认每个帖子保留前 20 条有文字的评论，可用 `AGENTSCROLL_COMMENT_LIMIT` 调整，最大 100。小红书和今日头条的评论请求由页面动态签名，因此需要 Playwright；可用 `AGENTSCROLL_ALLOW_COMMENT_BROWSER=1` 在全局禁用浏览器时单独允许评论读取。

微博采集依赖 `mcp-server-weibo`；其余 HTTP 采集使用 Python 标准库。需要浏览器采集时安装：

```bash
python3 -m pip install playwright
python3 -m playwright install chromium
```

`jieba` 仅用于提升中文相关性排序，不安装时会自动使用 CJK bigram。浏览器采集也可通过 `AGENTSCROLL_DISABLE_BROWSER=1` 禁用。

每个平台每次搜索最终最多返回 10 条内容；微信公众号的 `quick` 模式最多返回 8 条。`default` 和 `deep` 可以扩大候选覆盖或启用更完整的搜索路径，但不会突破 10 条上限。

为避免同一平台被短时间连续访问，详情请求会按平台分别限速，默认随机间隔 `0.8–1.6` 秒。不同平台之间仍可并发，不会互相等待。可按网络环境进一步调慢：

```ini
AGENTSCROLL_DETAIL_DELAY_MIN=0.8
AGENTSCROLL_DETAIL_DELAY_MAX=1.6
```

真实 pytest 默认测试 `informal` 场景中无需有效登录态的微博、B站和抖音；小红书保留为显式测试项，`AGENTSCROLL_LIVE_SOURCES` 可覆盖为任意平台组合。每次运行会自动保存到 `outputs/test_artifacts/live_collectors/<timestamp>/`：

- `01_search/<source>.json`：搜索阶段的摘要、URL 和原始候选。
- `02_content/<source>.json`：逐条访问详情后的完整 `content`、正文来源及不可读状态。
- `03_comments/<source>.json`：正文候选逐条访问评论区/API 后的评论原文和访问状态。
- `04_test_result/<suite>_<source>.json`：最终返回结果和测试通过/失败信息。

严格真实测试同时要求正文可读，并要求有评论区的平台至少有一个帖子真正返回评论原文。测试会设置 `AGENTSCROLL_ALLOW_DETAIL_BROWSER=1`。当抖音公开分享页返回 WAF JavaScript challenge 时，采集器停止继续发送 HTTP 详情请求，并复用一个移动 Chromium 上下文执行挑战后读取同一 `_ROUTER_DATA` 正文字段；它不会退回到 `meta description`。
