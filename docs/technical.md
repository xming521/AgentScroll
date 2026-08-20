# AgentScroll 技术文档

本文集中说明 AgentScroll 的配置、推理代码、采集流程、NewsNow 接入和真实平台测试。对外项目介绍见 [README](../README.md)。

## 推理代码与配置

`agentscroll/inference/` 是从 WeClone 的 `weclone/core/inference/` 同步并提交到本仓库的源码快照，用户正常克隆 AgentScroll 即可使用，不需要额外克隆或安装 WeClone：

```python
from agentscroll.inference import LLMRequest, OpenAICompatibleClient
```

仓库提供 `settings.example.jsonc` 作为配置模板。首次使用时复制为本地配置：

```bash
cp settings.example.jsonc settings.jsonc
```

推理参数集中在项目根目录的 `settings.jsonc`，只保留推理后端、模型、并发数、API 连接信息和 Codex 推理强度。该文件已被 Git 忽略，本地修改不会进入提交。使用 API 后端前先填写模型名，并设置配置中 `api_key_env` 指向的环境变量：

```bash
export AGENTSCROLL_LLM_API_KEY='your-api-key'
```

```python
from agentscroll.inference_config import (
    build_configured_client,
    load_inference_settings,
    make_configured_request,
)

settings = load_inference_settings()
request = make_configured_request("分析这些热点", settings)
with build_configured_client(settings) as client:
    response = client.generate(request)
```

可通过 `AGENTSCROLL_INFERENCE_CONFIG` 指向其他配置文件。API Key 只从环境变量读取，不应写入配置文件或提交到 Git。

`.github/workflows/sync-weclone-inference.yml` 只在 GitHub Actions 页面手动触发；检测到变化时会更新快照并创建 PR。本仓库不提供本地同步脚本，推理实现应先在 WeClone 修改，再手动运行该工作流同步。

`agentscroll/prompts/` 只保存提示词常量，不访问网络、文件、collector 或模型。`agentscroll/workflows/` 是应用层入口，负责组合 collector、推理、结果校验和产物保存。

运行产物统一放在 `outputs/` 主目录：`hotlists/`、`knowledge/`、`shares/`、`logs/` 和 `test_artifacts/` 分别保存热榜快照、知识文件、即时分享队列、推理日志和测试证据。支持显式目录参数的函数仍可由调用方指定其他目录。

## 知识文件

每次正式搜索都会在本地生成一个聚合知识文件，默认保存到 `outputs/knowledge/`。文件只保留理解热点、新闻和网络梗所需的正文、发布时间、原文链接、有效互动量、话题标签及点赞数最高的 10 条评论，不保存作者、平台内部 ID、抓取状态或解析证据。需要其他目录时可在命令行传入 `--output-dir`。

第一轮热榜筛选结果可以交给 `generate_selected_hotlist_knowledge_cards()`。它先用平台 collector 构造有限 evidence，再在一次 LLM 请求中为最多 20 个 `news` / `fun` 话题同时完成证据充分性判断、知识卡生成和即时分享编排，不额外调用一次模型。`complete` 会写入可学习内容，并可保存“大致内容或标题 + 真实 URL + 一条评论”到 `outputs/shares/`。模型只选择本地来源 ID 和评论 ID，程序从 evidence 精确回填 URL 与真实评论；没有合适评论时使用模型自拟评论，并在 JSON/TXT 中分别标为 `generated` / “我的评论”，不会冒充网友原话。

`needs_research` 会由本地公共搜索设施并行取得至多 3 个网页标题、摘要和 URL，再合并到一次批量 LLM 请求中直接生成最终替换卡，不再二次生成全部 20 条，也不启用按次计费的 Codex 原生 Web Search。补搜只要求形成聊天谈资，不做穷尽式事实核验；未确认指控仍保留来源限定。知识卡主轮和补搜轮默认都以 `effort="xhigh"` 执行，不改变全局推理配置；可分别传 `generation_effort` 和 `supplement_effort` 覆盖。一次知识卡运行只保存一份批次 JSON 和一份便于检查的批次 TXT，不再为每个话题分别创建文件；批次 JSON 直接包含全部知识卡、模型、耗时、实际 token usage、公共搜索请求数和原生 Web Search 次数。最终分享队列只保存一次，避免第一轮和补搜后重复分享；可用 `share_output_dir` 单独指定目录。可用 `supplement_failed=False` 关闭补搜，或对已有结果单独调用 `supplement_hotlist_knowledge_cards()`。

## 数据处理流程

```text
信息源
 ↓
采集 Fetch
 ↓
统一格式 Normalize {id,title,source}
 ↓
缓存 / 持久化 Persistence
 ↓
去重 / 聚类 / 时序统计
 ↓
过滤（LLM 先生成兴趣结构，然后根据用户兴趣过滤）/ 打分 / Top-K
 ↓
补充正文、评论、相关报道
 ↓
LLM 分析
 ↓
LLM 汇总
 ↓
日报 / Briefing / 推送
```

## 设计验证 TODO

- 只从聚合知识文件分析，模型能否理解“竹知了”这个梗？
- 如果模型需要在群聊中用这个梗活跃气氛，应该怎么用、在什么时候用？

## 真实平台测试

测试搜索关键词通过 `AGENTSCROLL_LIVE_TOPIC` 设置；未设置时默认搜索 `人工智能`。未设置 `AGENTSCROLL_LIVE_SOURCES` 时，默认测试非正式场景中无需有效登录态的微博、B站和抖音；显式设置该变量仍可测试任意平台组合。小红书网页搜索当前要求非 guest 登录，因此保留为显式 live 测试项。

真实测试执行完整流水线：先搜索得到帖子，再访问帖子正文页，最后读取评论区。测试允许自动使用 HTTP、平台 API 或 Playwright 浏览器路径，但仍会拒绝只有搜索摘要或 URL、正文不可读、评论为空或评论区被拦截的结果；微信公众号仅验证真实文章正文，不验证评论。

```bash
# 安装测试依赖（首次运行需要）
.venv/bin/python -m pip install -e ".[test,browser]"
.venv/bin/python -m playwright install chromium

# 默认测试 informal 场景中无需登录的 3 个平台；下一行设置搜索关键词
AGENTSCROLL_RUN_LIVE_TESTS=1 \
AGENTSCROLL_LIVE_TOPIC='codex' \
.venv/bin/python -m pytest \
  -m live \
  -k platform_search_and_detail_and_comments \
  -v

# 只测试指定平台，平台名使用英文并以逗号分隔
AGENTSCROLL_RUN_LIVE_TESTS=1 \
AGENTSCROLL_LIVE_TOPIC='大模型 Agent' \
AGENTSCROLL_LIVE_SOURCES=weibo,xiaohongshu,bilibili,douyin,toutiao \
.venv/bin/python -m pytest \
  -m live \
  -k platform_search_and_detail_and_comments \
  -v

# 只测试单个平台
AGENTSCROLL_RUN_LIVE_TESTS=1 \
AGENTSCROLL_LIVE_TOPIC='机器人' \
AGENTSCROLL_LIVE_SOURCES=xiaohongshu \
.venv/bin/python -m pytest \
  -m live \
  -k platform_search_and_detail_and_comments \
  -v
```

上述小红书单平台命令需要先具备有效登录态；否则测试会如实失败，不会用无关的站外搜索结果代替真实笔记。

可用平台名：`weibo`、`xiaohongshu`、`bilibili`、`zhihu`、`douyin`、`wechat`、`toutiao`。

其他常用参数：

- `AGENTSCROLL_LIVE_DEPTH=quick|default|deep`：搜索深度；测试默认 `default`，以允许 API 失败时启用浏览器搜索。
- `AGENTSCROLL_LIVE_DAYS=30`：搜索最近多少天。
- `AGENTSCROLL_COMMENT_LIMIT=20`：每个帖子最多保存多少条评论。
- `AGENTSCROLL_CONTENT_CHAR_LIMIT=2000`：写入 knowledge JSON/TXT 的单条正文字符上限，允许范围为 200 至 20000；原始 collector 返回不截断。
- `AGENTSCROLL_DETAIL_DELAY_MIN=0.8`、`AGENTSCROLL_DETAIL_DELAY_MAX=1.6`：同一平台内请求的随机等待区间；不同平台仍并发执行。

需要直接测试 Playwright 搜索爬虫时：

```bash
.venv/bin/python -m playwright install chromium

AGENTSCROLL_RUN_BROWSER_TESTS=1 \
AGENTSCROLL_LIVE_TOPIC='人工智能' \
AGENTSCROLL_LIVE_BROWSER_SOURCES=xiaohongshu,bilibili,douyin \
.venv/bin/python -m pytest -m browser -v
```

每次真实测试会自动保存到 `outputs/test_artifacts/live_collectors/<timestamp>/`：

- `01_search/`：搜索结果、摘要和 URL。
- `02_content/`：逐个打开正文页后的正文。
- `03_comments/`：逐个读取评论区后的评论原文和访问状态。
- `04_test_result/`：最终通过或失败结果。


## NewsNow 数据源

| 类别     | 平台            | NewsNow 榜单 / 数据流 | Source ID               | 状态       | 正文解析 | URL 解析 |
| ------ | ------------- | ---------------- | ----------------------- | -------- | ---- | ------ |
| 社区/科技  | V2EX          | 最新分享             | `v2ex-share`            | ✅        |      |        |
| 综合     | 知乎            | 热榜               | `zhihu`                 | ✅        |      |        |
| 社媒     | 微博            | 实时热搜             | `weibo`                 | ✅        |      |        |
| 新闻     | 联合早报          | 实时新闻             | `zaobao`                | ✅        |      |        |
| 科技社区   | 酷安            | 今日最热             | `coolapk`               | ✅        |      |        |
| 财经     | MKTNews       | 快讯               | `mktnews-flash`         | ✅        |      |        |
| 财经     | 华尔街见闻         | 快讯               | `wallstreetcn-quick`    | ✅        |      |        |
| 财经     | 华尔街见闻         | 最新               | `wallstreetcn-news`     | ✅        |      |        |
| 财经     | 华尔街见闻         | 最热               | `wallstreetcn-hot`      | ✅        |      |        |
| 科技/创投  | 36氪           | 快讯               | `36kr-quick`            | ⚠️ CF 禁用 |      |        |
| 科技/创投  | 36氪           | 人气榜              | `36kr-renqi`            | ⚠️ CF 禁用 |      |        |
| 短视频    | 抖音            | 热点榜              | `douyin`                | ✅        | X    | X      |
| 体育/社区  | 虎扑            | 主干道热帖            | `hupu`                  | ✅        |      |        |
| 体育     | 懂球帝           | 头条               | `dongqiudi`             | ✅        |      |        |
| AI     | AIHOT         | AI 信息流           | `aihot`                 | ✅        |      |        |
| 社区     | 百度贴吧          | 热议               | `tieba`                 | ✅        |      |        |
| 新闻     | 今日头条          | 热榜               | `toutiao`               | ✅        |      |        |
| 科技     | IT之家          | 最新信息流            | `ithome`                | ✅        |      |        |
| 新闻     | 澎湃新闻          | 热榜               | `thepaper`              | ✅        |      |        |
| 国际新闻   | 卫星通讯社         | 新闻流              | `sputniknewscn`         | ✅        |      |        |
| 国际新闻   | 参考消息          | 新闻流              | `cankaoxiaoxi`          | ✅        |      |        |
| 科技社区   | 远景论坛          | Win11            | `pcbeta-windows11`      | ✅        |      |        |
| 财经     | 财联社           | 电报               | `cls-telegraph`         | ✅        |      |        |
| 财经     | 财联社           | 深度               | `cls-depth`             | ✅        |      |        |
| 财经     | 财联社           | 热门               | `cls-hot`               | ✅        |      |        |
| 金融/投资  | 雪球            | 热门股票             | `xueqiu-hotstock`       | ✅        |      |        |
| 财经     | 格隆汇           | 事件               | `gelonghui`             | ✅        |      |        |
| 财经     | 法布财经 FastBull | 快讯               | `fastbull-express`      | ✅        |      |        |
| 财经     | 法布财经 FastBull | 头条               | `fastbull-news`         | ✅        |      |        |
| 科技     | Solidot       | 最新内容             | `solidot`               | ✅        |      |        |
| 海外科技社区 | Hacker News   | 热门               | `hackernews`            | ✅        |      |        |
| 产品/创业  | Product Hunt  | 热门产品             | `producthunt`           | ✅        |      |        |
| 开源     | GitHub        | Trending Today   | `github-trending-today` | ✅        |      |        |
| 视频/社媒  | Bilibili      | 热搜               | `bilibili-hot-search`   | ✅        |      |        |
| 短视频    | 快手            | 热榜               | `kuaishou`              | ⚠️ CF 禁用 |      |        |
| 新闻聚合   | 靠谱新闻          | 新闻流              | `kaopu`                 | ✅        |      |        |
| 财经     | 金十数据          | 实时资讯             | `jin10`                 | ✅        |      |        |
| 搜索     | 百度            | 百度热搜             | `baidu`                 | ✅        |      |        |
| 求职/社区  | 牛客            | 热门               | `nowcoder`              | ✅        |      |        |
| 科技     | 少数派           | 热门               | `sspai`                 | ✅        |      |        |
| 开发者社区  | 稀土掘金          | 热门               | `juejin`                | ✅        |      |        |
| 新闻     | 凤凰网           | 热点资讯             | `ifeng`                 | ✅        |      |        |
| 社区     | 虫部落           | 最新               | `chongbuluo-latest`     | ✅        |      |        |
| 社区     | 虫部落           | 最热               | `chongbuluo-hot`        | ✅        |      |        |
| 影视     | 豆瓣            | 热门电影             | `douban`                | ✅        |      |        |
| 游戏     | Steam         | 在线人数             | `steam`                 | ✅        |      |        |
| 新闻     | 腾讯新闻          | 综合早报             | `tencent-hot`           | ✅        |      |        |
| 网络安全   | FreeBuf       | 网络安全热门           | `freebuf`               | ✅        |      |        |
| 视频/影视  | 腾讯视频          | 热搜榜              | `qqvideo-tv-hotsearch`  | ✅        |      |        |
| 视频/影视  | 爱奇艺           | 热播榜              | `iqiyi-hot-ranklist`    | ✅        |      |        |

## Agent 按类别调用 NewsNow

`agentscroll.collector.newsnow` 基于上表的“类别”列定义了 27 个固定分组，共覆盖 50 个唯一 Source ID。Agent 选择类别后，会一次调用该类别下的所有 NewsNow 榜单；这是 AgentScroll 的分组规则，不代表 NewsNow 原生支持 tag 或任意主题查询。

Agent 未指定类别时默认使用“综合”组。该组包含知乎、微博、虎扑、百度贴吧和 Bilibili 热搜，共 5 个榜单；抖音仍保留在可显式选择的“短视频”组中，但默认综合采集不再请求它。

Python Agent 可以直接调用：

```python
from agentscroll.collector import fetch_newsnow_hotlists

# 默认获取“综合”组
result = fetch_newsnow_hotlists()

# 也可以显式选择其他组
result = fetch_newsnow_hotlists(
    groups=["财经", "AI"],
    per_source_limit=10,
)
```

也可以通过独立命令调用，不影响原有的 `python -m agentscroll.collector <topic>` 搜索入口：

```bash
# 查看全部类别及其 Source ID
.venv/bin/python -m agentscroll.collector.newsnow --list-groups

# 不传 --groups 时默认获取“综合”组
.venv/bin/python -m agentscroll.collector.newsnow

# 获取“财经”和“AI”组，每个榜单最多保留 10 条
.venv/bin/python -m agentscroll.collector.newsnow \
  --groups '财经,AI' \
  --per-source-limit 10

```

默认连接 `http://127.0.0.1:4444`，可通过 `AGENTSCROLL_NEWSNOW_BASE_URL` 或 `--base-url` 修改。每次调用会先在全部所选榜单之间按 URL 和标题去重，再新建一对同名本地快照：`outputs/hotlists/*.json` 保留完整数据，`outputs/hotlists/*.txt` 每行只保留一个标题。可通过 `--output-dir` 修改目录，或用 `--no-save` 只返回数据。`--latest` 会传递 `latest=true`，但实际刷新时间仍由 NewsNow 的上游刷新间隔和缓存策略决定。


NewsNow 的 JSON/TXT 快照只负责热榜索引，不在拉榜阶段读取正文或评论。TXT 交给模型筛选标题后，可用原 JSON 和选中标题读取综合组平台的详情并保存 knowledge 文件：

```python
from agentscroll.collector import open_selected_hotlists

details = open_selected_hotlists(
    "outputs/hotlists/20260818-165927-371359+0800_newsnow_综合.json",
    [
        "台风",
        "科比和詹姆斯同是高中学历，为何两者演讲水平差距巨大",
        "JiaQi带飞全队,IG闯骑士之路",
    ],
    posts_per_topic=1,
    output_dir="outputs/knowledge",
)

print(details["knowledge_file"])
print(details["knowledge_metadata_file"])
```

该函数不修改或回写 NewsNow 快照，只在原 JSON 中精确匹配选中标题后才访问平台。微博用 `mcp-server-weibo` 把标题解析为具体 feed；虎扑从帖子页结构化数据读取正文和首屏回复；贴吧先从热榜话题页解析帖子链接，再用统一 Playwright 浏览器设施读取正文、楼层回复和楼中楼；知乎访问热榜给出的问答或文章 URL；Bilibili 用热搜标题定位视频后读取简介和评论。原热榜条目仍作为来源证据保留在返回值中。只处理微博时也可继续使用兼容接口 `open_selected_weibo_hotlists()`。

如果需要把模型筛选和详情采集串成一次调用，可直接传入 NewsNow JSON。该入口严格按 JSON 中的 source/rank 顺序生成临时 ID，模型返回 ID 后在同一列表中还原标题，因此不会依赖 TXT 与 JSON 之间的人工同步：

```python
from agentscroll.workflows import select_and_collect_hotlists

result = select_and_collect_hotlists(
    "outputs/hotlists/20260818-165927-371359+0800_newsnow_综合.json",
    output_dir="outputs/knowledge",
)
```

两种详情入口都会像 `collect()` 一样原子写入一对同名文件：JSON 保留正文 URL、互动量和评论等紧凑元数据，TXT 不含链接，供模型直接读取。返回值中的 `knowledge_metadata_file` 和 `knowledge_file` 分别是这两个文件的路径。

去重时按 Source ID 和榜单排名顺序保留最先出现的条目。URL 会统一协议和域名大小写并忽略 fragment，但保留可能影响内容定位的 query；标题会统一 Unicode 兼容字符、大小写和连续空白。URL 或标题任一重复都会被移除。这些是与具体平台和本次样本无关的通用规则。

返回 JSON 的主要字段：

- `requested_groups`：本次 Agent 选择的类别，不是新闻自身的主题标签。
- `group_sources`：每个所选类别实际展开出的 NewsNow Source ID。
- `sources`：按 Source ID 保存结果；单个源失败只在自己的 `error` 中记录，不影响其他源。
- `status`：NewsNow 对该榜单返回的 `success` 或 `cache`；AgentScroll 校验失败时为 `error`。
- `updated_time`：NewsNow 返回的榜单更新时间戳，不是 AgentScroll 的抓取时间。
- `items`：规范化的榜单条目；`rank` 是当前榜单顺序，`title`/`url` 是标题和原始链接，`published_at` 是 NewsNow 提供的发布时间，`extra` 是 NewsNow 的附加信息。
- `discarded_items`：因缺少有效标题或 HTTP(S) 链接而被丢弃的条目数。
- `duplicate_items`：因 URL 或规范化标题重复而被移除的条目数；顶层是总数，各 Source ID 下是该榜单移除数。
- `total_items`：所有成功榜单最终保留的条目总数。
- `snapshot_file`：本次本地 JSON 快照的绝对路径；使用 `--no-save` 时不存在。
- `snapshot_text_file`：标题专用 TXT 的绝对路径，只存在于调用返回结果中；完整 JSON 文件本身不增加该字段。

热榜快照只保存榜单元数据，不代表已读取文章正文或评论。模型完成标题筛选后，应使用 `open_selected_hotlists()` 补充已支持平台的正文、评论和相关内容。
