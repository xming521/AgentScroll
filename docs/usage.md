# AgentScroll CLI 使用参考

本文集中说明 AgentScroll 的配置、CLI、数据格式、平台支持和测试命令。内容处理规则、系统结构、平台采集实现和技术原则见[工作流程与技术说明](workflows.md)。

## 环境与推理配置

项目要求 Python 3.11 及以上。从仓库运行 Python、pytest 或模块入口时使用 `.venv/bin/python`。
项目依赖准备完成后，以可编辑方式安装 AgentScroll 并生成 `agentscroll` 命令：

```bash
uv pip install --python .venv/bin/python -e .
```

首次使用时复制配置模板：

```bash
cp settings.example.jsonc settings.jsonc
```

`settings.example.jsonc` 是分发模板，`settings.jsonc` 是被 Git 忽略的本地真实配置。配置包含推理后端、模型、并发数、API 连接和推理强度。API Key 只从 `api_key_env` 指定的环境变量读取，例如：

```bash
export AGENTSCROLL_LLM_API_KEY='your-api-key'
```

可通过 `AGENTSCROLL_INFERENCE_CONFIG` 指向其他配置文件，也可在子命令前使用 `--config-path`，例如 `agentscroll --config-path custom.jsonc hotlist learn ...`。不得把 API Key、Cookie 或 Token 写入配置模板、文档、日志或测试证据。

## 主题搜索

### CLI

```bash
.venv/bin/agentscroll search "AI Agent" \
  --scene informal \
  --days 30 \
  --depth quick \
  --output-dir outputs/knowledge
```

主要参数：

| 参数 | 含义 |
| --- | --- |
| `TOPIC` | 搜索主题或关键词 |
| `--sources` | 显式平台列表；设置后覆盖场景路由 |
| `--scene` | `auto`、`formal` 或 `informal` |
| `--days` | 回溯的日历天数，默认 30 |
| `--as-of` | 查询终点日期，格式为 `YYYY-MM-DD` |
| `--depth` | 搜索规模：`quick` 每个平台最多 5 条，`default` 最多 10 条，`deep` 最多 20 条 |
| `--output-dir` | 知识文件目录，默认 `outputs/knowledge/` |

三档搜索规模的区别：

| 模式 | 每个平台的候选上限 | 成本 |
| --- | ---: | --- |
| `quick` | 5 条 | 最低 |
| `default` | 10 条 | 中等 |
| `deep` | 20 条 | 最高 |

`quick` 不会跳过小红书或其他平台。平台实际返回数量可能低于对应上限；候选越多，后续详情与评论请求也越多。各档使用的采集路径见[搜索规模](workflows.md#搜索规模)。

各场景的选择规则见[主动搜索流程](workflows.md#主动搜索)。

### 返回值与知识文件

命令行向标准输出打印 JSON。每个平台都有独立的 `items` 和 `error`；单个平台失败不会中断其他平台。

一次搜索生成一对同名文件：

- `.json`：保留查询词、采集时间、场景路由、日期范围及知识条目的紧凑元数据。
- `.txt`：面向模型阅读，只保留查询词、正文、时间、标签、有效互动量和评论原文。

返回值中的 `knowledge_metadata_file` 和 `knowledge_file` 分别给出两份文件的路径。默认写入 `outputs/knowledge/`；显式 `output_dir` 不覆盖输入文件。

知识文件的排序、截断和过滤规则见[知识沉淀](workflows.md#知识沉淀)。

## 正文与评论字段

支持详情访问的平台为条目增加：

- `content`：详情页或 API 读取到的正文或发布文案。
- `content_type`：`article-body`、`post-description`、`search-summary` 或 `unavailable`。
- `content_source`：实际读取路径。
- `content_url`：本次读取正文使用的最终 URL。

`search-summary` 只表示详情访问失败后保留的搜索摘要，不能当作完整正文。

支持评论的平台增加：

- `comments`：评论列表；每条包含 `comment_id`、`text`，以及接口能提供的 `created_at`、`likes`、`reply_count` 和 `parent_comment_id`。
- `comments_status`：`readable`、`empty`、`blocked` 或 `unavailable`。
- `comments_source`：评论读取路径。
- `comment_extraction_evidence`：评论提取证据。

原始条目默认最多保留 20 条有文字的评论，可用 `AGENTSCROLL_COMMENT_LIMIT` 调整，最大 100。

## NewsNow 热榜

### 平台支持

AgentScroll 定义了 27 个固定分组，共覆盖 50 个唯一 Source ID。分类用于选择榜单，不代表 NewsNow 原生支持任意主题标签。

下表完整保留项目记录中的榜单接入和正文解析状态。状态不代表本轮已经完成实时验证。

| 类别     | 平台            | NewsNow 榜单 / 数据流 | Source ID               | 状态       | 正文解析 |
| ------ | ------------- | ---------------- | ----------------------- | -------- | ---- |
| 社区/科技  | V2EX          | 最新分享             | `v2ex-share`            | ✅        |      |
| 综合     | 知乎            | 热榜               | `zhihu`                 | ✅        |      |
| 社媒     | 微博            | 实时热搜             | `weibo`                 | ✅        |      |
| 新闻     | 联合早报          | 实时新闻             | `zaobao`                | ✅        |      |
| 科技社区   | 酷安            | 今日最热             | `coolapk`               | ✅        |      |
| 财经     | MKTNews       | 快讯               | `mktnews-flash`         | ✅        |      |
| 财经     | 华尔街见闻         | 快讯               | `wallstreetcn-quick`    | ✅        |      |
| 财经     | 华尔街见闻         | 最新               | `wallstreetcn-news`     | ✅        |      |
| 财经     | 华尔街见闻         | 最热               | `wallstreetcn-hot`      | ✅        |      |
| 科技/创投  | 36氪           | 快讯               | `36kr-quick`            | ⚠️ CF 禁用 |      |
| 科技/创投  | 36氪           | 人气榜              | `36kr-renqi`            | ⚠️ CF 禁用 |      |
| 短视频    | 抖音            | 热点榜              | `douyin`                | ✅        | X    |
| 体育/社区  | 虎扑            | 主干道热帖            | `hupu`                  | ✅        |      |
| 体育     | 懂球帝           | 头条               | `dongqiudi`             | ✅        |      |
| AI     | AIHOT         | AI 信息流           | `aihot`                 | ✅        |      |
| 社区     | 百度贴吧          | 热议               | `tieba`                 | ✅        |      |
| 新闻     | 今日头条          | 热榜               | `toutiao`               | ✅        |      |
| 科技     | IT之家          | 最新信息流            | `ithome`                | ✅        |      |
| 新闻     | 澎湃新闻          | 热榜               | `thepaper`              | ✅        |      |
| 国际新闻   | 卫星通讯社         | 新闻流              | `sputniknewscn`         | ✅        |      |
| 国际新闻   | 参考消息          | 新闻流              | `cankaoxiaoxi`          | ✅        |      |
| 科技社区   | 远景论坛          | Win11            | `pcbeta-windows11`      | ✅        |      |
| 财经     | 财联社           | 电报               | `cls-telegraph`         | ✅        |      |
| 财经     | 财联社           | 深度               | `cls-depth`             | ✅        |      |
| 财经     | 财联社           | 热门               | `cls-hot`               | ✅        |      |
| 金融/投资  | 雪球            | 热门股票             | `xueqiu-hotstock`       | ✅        |      |
| 财经     | 格隆汇           | 事件               | `gelonghui`             | ✅        |      |
| 财经     | 法布财经 FastBull | 快讯               | `fastbull-express`      | ✅        |      |
| 财经     | 法布财经 FastBull | 头条               | `fastbull-news`         | ✅        |      |
| 科技     | Solidot       | 最新内容             | `solidot`               | ✅        |      |
| 海外科技社区 | Hacker News   | 热门               | `hackernews`            | ✅        |      |
| 产品/创业  | Product Hunt  | 热门产品             | `producthunt`           | ✅        |      |
| 开源     | GitHub        | Trending Today   | `github-trending-today` | ✅        |      |
| 视频/社媒  | Bilibili      | 热搜               | `bilibili-hot-search`   | ✅        |      |
| 短视频    | 快手            | 热榜               | `kuaishou`              | ⚠️ CF 禁用 |      |
| 新闻聚合   | 靠谱新闻          | 新闻流              | `kaopu`                 | ✅        |      |
| 财经     | 金十数据          | 实时资讯             | `jin10`                 | ✅        |      |
| 搜索     | 百度            | 百度热搜             | `baidu`                 | ✅        |      |
| 求职/社区  | 牛客            | 热门               | `nowcoder`              | ✅        |      |
| 科技     | 少数派           | 热门               | `sspai`                 | ✅        |      |
| 开发者社区  | 稀土掘金          | 热门               | `juejin`                | ✅        |      |
| 新闻     | 凤凰网           | 热点资讯             | `ifeng`                 | ✅        |      |
| 社区     | 虫部落           | 最新               | `chongbuluo-latest`     | ✅        |      |
| 社区     | 虫部落           | 最热               | `chongbuluo-hot`        | ✅        |      |
| 影视     | 豆瓣            | 热门电影             | `douban`                | ✅        |      |
| 游戏     | Steam         | 在线人数             | `steam`                 | ✅        |      |
| 新闻     | 腾讯新闻          | 综合早报             | `tencent-hot`           | ✅        |      |
| 网络安全   | FreeBuf       | 网络安全热门           | `freebuf`               | ✅        |      |
| 视频/影视  | 腾讯视频          | 热搜榜              | `qqvideo-tv-hotsearch`  | ✅        |      |
| 视频/影视  | 爱奇艺           | 热播榜              | `iqiyi-hot-ranklist`    | ✅        |      |

只有实际 live 测试读到目标正文或评论，才能确认对应能力当前可用。

### 拉取榜单

不传 `--groups` 时默认使用“综合”组，包括知乎（`zhihu`）、微博（`weibo`）、虎扑（`hupu`）、百度贴吧（`tieba`）和 Bilibili 热搜（`bilibili-hot-search`）。

```bash
# 查看全部类别及其 Source ID
.venv/bin/agentscroll hotlist groups

# 默认获取“综合”组
.venv/bin/agentscroll hotlist fetch

# 获取“财经”和“AI”组，每个榜单最多保留 10 条
.venv/bin/agentscroll hotlist fetch \
  --groups '财经,AI' \
  --per-source-limit 10
```

默认连接 `http://127.0.0.1:4444`，可通过 `AGENTSCROLL_NEWSNOW_BASE_URL` 或 `--base-url` 修改。`--latest` 请求 NewsNow 刷新，但实际刷新时间仍由上游刷新间隔和缓存策略决定。`--output-dir` 修改快照目录，`--no-save` 只返回数据而不保存文件。

拉榜阶段只保存索引，不读取正文或评论。默认生成：

- `outputs/hotlists/*.json`：完整榜单快照。
- `outputs/hotlists/*.txt`：每行一个标题。

榜单合并和去重规则见[拉取与去重](workflows.md#拉取与去重)。

### 快照字段

- `requested_groups`：本次选择的类别，不是新闻主题标签。
- `group_sources`：每个类别展开出的 Source ID。
- `sources`：按 Source ID 保存结果；单个源的失败不会影响其他源。
- `status`：NewsNow 返回的 `success` 或 `cache`；AgentScroll 校验失败时为 `error`。
- `updated_time`：NewsNow 返回的榜单更新时间，不是 AgentScroll 采集时间。
- `items`：规范化榜单条目；包含 `rank`、`title`、`url`、`published_at` 和可选 `extra`。
- `discarded_items`：因缺少有效标题或 HTTP(S) URL 被丢弃的数量。
- `duplicate_items`：因 URL 或规范化标题重复被移除的数量。
- `total_items`：全部成功榜单最终保留的条目数。
- `snapshot_file`：JSON 快照绝对路径；`--no-save` 时不存在。
- `snapshot_text_file`：标题 TXT 的绝对路径，只存在于调用返回结果中。

### 生成知识卡与补搜

```bash
.venv/bin/agentscroll hotlist learn \
  outputs/hotlists/example_newsnow_综合.json \
  --output-dir outputs/knowledge \
  --share-output-dir outputs/shares
```

该命令先做话题级粗筛，再采集正文和评论、生成知识卡，并只对证据不足的话题补搜。只查看粗筛结果而不访问详情页时使用：

```bash
.venv/bin/agentscroll hotlist select \
  outputs/hotlists/example_newsnow_综合.json
```

常用参数与产物：

- 一次最多处理 20 个话题。
- 主轮和补搜轮默认 `effort="xhigh"`，可分别通过 `--generation-effort` 和 `--supplement-effort` 调整，不修改全局配置。
- `--no-supplement` 关闭自动补搜。
- `--share-output-dir` 可以单独指定分享目录。
- 一次运行保存一份批次 JSON、一份批次 TXT 和一份最终分享队列。
- 批次 JSON 记录全部知识卡、模型、耗时、实际 Token 使用量、公共搜索请求数和原生 Web Search 次数。

内部筛选、证据补充和评论回填规则见[热榜学习](workflows.md#热榜学习)。

## 可选依赖与运行限制

浏览器采集需要安装项目的 browser 可选依赖和 Chromium：

```bash
uv pip install --python .venv/bin/python playwright
.venv/bin/python -m playwright install chromium
```

`jieba` 是用于提升中文相关性排序的可选依赖。

常用环境变量：

| 环境变量 | 默认值或作用 |
| --- | --- |
| `AGENTSCROLL_CONTENT_CHAR_LIMIT` | 知识正文上限，默认 2000，范围 200–20000 |
| `AGENTSCROLL_COMMENT_LIMIT` | 原始条目评论上限，默认 20，最大 100 |
| `AGENTSCROLL_DETAIL_DELAY_MIN` | 同平台详情请求最小间隔，默认 1.5 秒 |
| `AGENTSCROLL_DETAIL_DELAY_MAX` | 同平台详情请求最大间隔，默认 3.8 秒 |
| `AGENTSCROLL_DISABLE_BROWSER=1` | 禁用浏览器采集 |
| `AGENTSCROLL_ALLOW_COMMENT_BROWSER=1` | 全局禁用浏览器时，单独允许评论浏览器路径 |
| `AGENTSCROLL_NEWSNOW_BASE_URL` | NewsNow 服务地址 |

每个平台每次搜索的返回上限由 `--depth` 控制；平台实际可返回的结果可能少于该上限。

## 真实平台测试

测试搜索关键词通过 `AGENTSCROLL_LIVE_TOPIC` 设置，未设置时默认 `人工智能`。未设置 `AGENTSCROLL_LIVE_SOURCES` 时，默认测试微博、B站和抖音；小红书因要求有效登录态而保留为显式测试项。

测试依赖可单独安装；浏览器依赖和 Chromium 的安装方式见上一节：

```bash
uv pip install --python .venv/bin/python pytest
```

运行默认 live 测试：

```bash
AGENTSCROLL_RUN_LIVE_TESTS=1 \
AGENTSCROLL_LIVE_TOPIC='codex' \
.venv/bin/python -m pytest \
  -m live \
  -k platform_search_and_detail_and_comments \
  -v
```

显式指定平台：

```bash
AGENTSCROLL_RUN_LIVE_TESTS=1 \
AGENTSCROLL_LIVE_TOPIC='大模型 Agent' \
AGENTSCROLL_LIVE_SOURCES=weibo,xiaohongshu,bilibili,douyin,toutiao \
.venv/bin/python -m pytest \
  -m live \
  -k platform_search_and_detail_and_comments \
  -v
```

只测试单个平台：

```bash
AGENTSCROLL_RUN_LIVE_TESTS=1 \
AGENTSCROLL_LIVE_TOPIC='机器人' \
AGENTSCROLL_LIVE_SOURCES=xiaohongshu \
.venv/bin/python -m pytest \
  -m live \
  -k platform_search_and_detail_and_comments \
  -v
```

浏览器专项测试：

```bash
AGENTSCROLL_RUN_BROWSER_TESTS=1 \
AGENTSCROLL_LIVE_TOPIC='人工智能' \
AGENTSCROLL_LIVE_BROWSER_SOURCES=xiaohongshu,bilibili,douyin \
.venv/bin/python -m pytest -m browser -v
```

可用平台名：`weibo`、`xiaohongshu`、`bilibili`、`zhihu`、`douyin`、`wechat`、`toutiao`。

其他测试参数：

- `AGENTSCROLL_LIVE_DEPTH=quick|default|deep`：搜索规模与 `--depth` 相同，默认 `default`。
- `AGENTSCROLL_LIVE_DAYS=30`：搜索时间范围。
- `AGENTSCROLL_ALLOW_DETAIL_BROWSER=1`：允许正文详情使用浏览器；严格 live 测试会设置该变量。

每次真实测试保存到 `outputs/test_artifacts/live_collectors/<timestamp>/`：

- `01_search/<source>.json`：搜索阶段候选。
- `02_content/<source>.json`：详情正文及读取状态。
- `03_comments/<source>.json`：评论及读取状态。
- `04_test_result/<suite>_<source>.json`：最终结果。

测试只有实际读取到正文才算通过；要求评论的平台还必须至少有一个条目返回真实评论。被拦截、要求登录和确实没有评论必须区分。微信公众号 live 测试只验证文章正文。
