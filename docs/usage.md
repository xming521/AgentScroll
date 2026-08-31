# AgentScroll CLI 使用参考

本文集中说明 AgentScroll 的配置、CLI、数据格式、平台支持和测试命令。内容处理规则、系统结构、平台采集实现和技术原则见[系统设计与内容规则](design.md)。

## 环境与配置

项目要求 Python 3.11 及以上。从仓库运行 Python、pytest 或模块入口时使用 `.venv/bin/python`。
项目依赖准备完成后，以可编辑方式安装 AgentScroll 并生成 `agentscroll` 命令：

```bash
uv pip install --python .venv/bin/python -e .
```

首次使用时复制配置模板：

```bash
cp settings.example.jsonc settings.jsonc
```

`settings.example.jsonc` 是分发模板，`settings.jsonc` 是被 Git 忽略的本地真实配置。配置包含推理参数和热榜定时规则。`max_workers` 控制逐话题知识卡请求和补搜请求的最大并发数。API Key 只从 `api_key_env` 指定的环境变量读取，例如：

```bash
export AGENTSCROLL_LLM_API_KEY='your-api-key'
```

可通过 `AGENTSCROLL_CONFIG` 指向其他配置文件，也可在子命令前使用 `--config-path`，例如 `agentscroll --config-path custom.jsonc hotlist learn ...`。不得把 API Key、Cookie 或 Token 写入配置模板、文档、日志或测试证据。

### 推理审计日志

同步 LLM 调用默认写入 `outputs/logs/llm_audit/YYYY-MM-DD.jsonl`，文件权限为 `600`。每次逻辑调用使用一个 `call_id` 串联以下事件：

- `call.started`：完整 `LLMRequest` 和后端通用参数。
- `attempt.started`：本次实际 provider 请求采用的参数，不重复保存 messages。
- `attempt.finished`：响应或异常、耗时、provider request ID，以及是否重试和重试原因。
- `call.finished`：最终 `LLMResponse`、解析结果、usage、总耗时和最终错误。

审计记录会按结构化字段名遮盖 API Key、Authorization、Cookie、密码和访问令牌；messages 与模型响应按原文保留。直接把 `agentscroll.inference` 用作基础设施库时，默认目录是项目根下的 `logs/llm_audit`，可通过通用环境变量 `LLM_AUDIT_LOG_DIR` 或注入 `LLMAuditLogger` 修改。`LLMAuditLogger(strict=True)` 可让审计写入失败直接阻止请求；默认只告警，不中断推理。

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

`quick` 不会跳过小红书或其他平台。平台实际返回数量可能低于对应上限；候选越多，后续详情与评论请求也越多。各档使用的采集路径见[搜索规模](design.md#搜索规模)。

各场景的选择规则见[主动搜索](design.md#主动搜索)。

### 返回值与知识文件

命令行向标准输出打印 JSON。每个平台都有独立的 `items` 和 `error`；单个平台失败不会中断其他平台。

一次搜索生成一对同名文件：

- `.json`：保留查询词、采集时间、场景路由、日期范围及知识条目的紧凑元数据。
- `.txt`：面向模型阅读，只保留查询词、正文、时间、标签、有效互动量和评论原文。

返回值中的 `knowledge_metadata_file` 和 `knowledge_file` 分别给出两份文件的路径。默认写入 `outputs/knowledge/`；显式 `output_dir` 不覆盖输入文件。

知识文件的排序、截断和过滤规则见[知识沉淀](design.md#知识沉淀)。

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

不传 `--groups` 时默认使用“综合”组，依次包括微博（`weibo`）、虎扑（`hupu`）、百度贴吧（`tieba`）和知乎（`zhihu`）。

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

榜单合并和去重规则见[拉取与去重](design.md#拉取与去重)。

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

### 拉取并学习

`run` 会依次拉取热榜快照并完成知识卡与分享生成：

```bash
.venv/bin/agentscroll hotlist run \
  --groups '综合' \
  --snapshot-output-dir outputs/hotlists \
  --output-dir outputs/knowledge \
  --share-output-dir outputs/shares
```

使用 `--scheduled` 后，命令会读取 `settings.jsonc` 的 `schedule` 配置并在前台等待定时执行，按 `Ctrl+C` 停止：

```bash
.venv/bin/agentscroll hotlist run --groups '综合' --scheduled
```

默认配置使用运行机器的当地时间，每天从 `08:00` 到次日 `00:00` 每 4 小时执行一次，即 `08:00、12:00、16:00、20:00、00:00`。可在配置文件中修改：

```jsonc
"schedule": {
  "every": "4h",
  "start_time": "08:00",
  "end_time": "00:00"
}
```

`every` 由正整数和单位组成，支持分钟 `m`、小时 `h` 和天 `d`，配置的每日规则最长为 `1d`。结束时间早于或等于开始时间时按跨越午夜处理；只有按间隔恰好落在结束时间上的任务才会在该时刻执行。若需要不受每日时间范围限制、并在启动后立即执行，可继续使用 `--every 4h`。

同一进程最多同时执行一轮。某轮执行时间超过间隔时不会并发启动下一轮；程序停止期间错过的任务不会在重启后回放。服务部署时直接以前台方式运行该命令，由 Docker、systemd 等运行环境负责进程保活。

### AstrBot IM 自动分享

即时分享只随 `hotlist run --scheduled` 或 `hotlist run --every ...` 的常驻进程启用；不带定时参数的一次性 `hotlist run` 和 `hotlist learn` 仍只生成文件，不自动发送。平台无关的分享分发器负责批次选择、限流、延迟调度和持久状态，AstrBot transport 只请求 `POST /api/v1/im/message`，再由 AstrBot 按 UMO 将纯文本消息转发到 QQ、Telegram 等已连接平台，不需要安装 AgentScroll 专用的 AstrBot 插件。

先在 AstrBot 中创建带 `im` scope 的 API Key，并取得目标会话的 UMO。UMO 格式为 `platform:message_type:session_id`；也可以在目标会话中使用 AstrBot 的 `/sid` 命令确认。API Key 只写入环境变量：

```bash
export AGENTSCROLL_ASTRBOT_API_KEY='your-astrbot-api-key'
```

再在 `settings.jsonc` 增加：

```jsonc
"sharing": {
  "enabled": true,
  "destinations": [
    {
      "transport": "astrbot",
      "target": "qq:GroupMessage:123456789"
    }
  ],
  "policy": {
    "window_minutes": 60,
    "max_messages_per_window": 2,
    "min_interval_minutes": 10,
    "bypass_score": 4.0
  }
},
"integrations": {
  "astrbot": {
    "base_url": "http://127.0.0.1:6185",
    "api_key_env": "AGENTSCROLL_ASTRBOT_API_KEY"
  }
}
```

每个 `sharing.destinations` 项选择一个 transport，并独立计算限额；`target` 的格式由对应 transport 校验。以上配置会让普通消息每个目标在滚动 60 分钟内最多发送 2 条，且相邻普通消息至少间隔 10 分钟；一个批次只保留评分最高的 2 条普通消息，其余不排队。达到 4.0 分的消息立即发送、条数不限，不受普通限额影响也不占普通额度。新批次会替换上一批尚未发送的普通消息，不形成跨批次积压；某个已选消息发送失败时也不会再用低分条目补位。

分享状态保存在 `outputs/sharing/state.json`，逐日审计写入 `outputs/sharing/YYYY-MM-DD.jsonl`。状态文件记录各目标使用的 transport、普通额度和待执行任务，用于重启恢复；审计日志只保存目标摘要、任务结果和错误类型，不保存 API Key 或消息正文。首次创建状态文件时会把当时最新的分享批次记为基线，不发送此前积累的批次。AstrBot transport 只有建立连接失败时才分别等待 1 秒、3 秒重试；服务返回错误或读取响应时结果不确定均不重试，后者按可能已发送处理以防重复。

### Docker 后台运行

仓库根目录的 `compose.yaml` 会同时启动 AgentScroll 和必需的 NewsNow。AgentScroll 只连接 Compose 内的 `http://newsnow:4444`，并等待 NewsNow 健康检查通过后才启动；NewsNow 数据保存在 `newsnow_data` volume，热榜快照、知识卡和分享队列仍写入仓库的 `outputs/`。

首次启动前准备本地配置：

```bash
cp settings.example.jsonc settings.jsonc
cp .env.example .env
mkdir -p outputs
```

在 `settings.jsonc` 中填写实际模型名，在 `.env` 中填写 `AGENTSCROLL_LLM_API_KEY`。使用 AstrBot transport 自动分享时还要填写 `AGENTSCROLL_ASTRBOT_API_KEY`。Docker 部署使用 API 推理后端，不在容器内提供本机 Codex CLI。Compose 已把 `host.docker.internal` 映射到宿主机，并用 `AGENTSCROLL_ASTRBOT_BASE_URL=http://host.docker.internal:6185` 覆盖本地地址，因此 AstrBot 需要把 Dashboard 的 6185 端口发布到宿主机。然后启动服务：

```bash
docker compose up -d --build
```

默认按 `settings.jsonc` 的 `schedule` 定时学习“综合”组；类别可通过 `.env` 的 `AGENTSCROLL_HOTLIST_GROUPS` 修改。容器时区由 `.env` 的 `TZ` 指定，示例默认为 `Asia/Shanghai`。NewsNow 默认固定到 `v0.0.41`；升级前先核对上游变更，再修改 `NEWSNOW_VERSION`。

查看状态和日志：

```bash
docker compose ps
docker compose logs -f agentscroll
docker compose logs -f newsnow
```

停止服务但保留 NewsNow 数据和本地输出：

```bash
docker compose down
```

已有热榜快照需要重新学习时，直接使用 `learn`：

```bash
.venv/bin/agentscroll hotlist learn \
  outputs/hotlists/example_newsnow_综合.json \
  --output-dir outputs/knowledge \
  --share-output-dir outputs/shares
```

两个命令都会做话题级粗筛，再采集正文和评论、生成知识卡，并只对证据不足的话题补搜。模型选出的代表标题保持不变，正文采集独立优先使用同话题的非知乎入口；只有整组标题都来自知乎时，才批量生成检索词并只到微博补采。最后的主动搜索复用 `agentscroll search` 的采集入口：`news` 搜微博、微信公众号和今日头条，`fun` 搜微博和小红书，每个话题最多保留 3 条实际读到正文的内容。第一轮证据只接受热榜快照日期及其之前连续 7 个自然日内、发布时间可确认的非知乎帖子；主动搜索沿用同一查询日期范围，并过滤已知日期超出范围的内容。

常用参数与产物：

- 第一轮最多选择 15 个话题。
- 热榜快照目录的 `hotlist_history.json` 同时保存近期未选标题和事件真实标题。后续筛选先按 Unicode 兼容字符、大小写和连续空白规范化后跳过完全相同的标题，再在本地为每个新标题召回最多 3 个相似历史事件；没有新标题时不调用筛选模型。历史窗口为快照日期及其之前连续 7 个自然日。
- `hotlist_history.json` 的 `ignored_titles` 保存未选标题及最近出现时间；`events` 中每项保存稳定的 `event_id`、`label`、首末出现时间、最终状态、真实标题数组，以及当前知识卡的 `current_card_file` 和 `current_topic_id`。旧事件仍可保留，但只有窗口内标题参与匹配。
- 第一轮模型在原有一次请求中同时返回 `new`、`update` 和 `seen`。`seen` 不再采集；`new` 生成新卡，`update` 读取历史事件当前卡片后验证是否确有进展。每个当前标题最多召回 3 个历史事件，不限制所有召回标题的合计字符数，也不生成摘要或 signature。
- 主轮和补搜轮默认 `effort="xhigh"`，可分别通过 `--generation-effort` 和 `--supplement-effort` 调整，不修改全局配置。
- `--no-supplement` 关闭自动补搜。
- `--share-output-dir` 可以单独指定分享目录。
- 一次运行先保存一份标题筛选 JSON，再保存最终分享队列；有 `new` 话题时另存一份只包含新卡的批次 JSON 和 TXT。标题筛选文件中的 `items` 记录代表标题 `title`、代表平台 `source`、类别 `label`、事件关系 `relation`、命中的历史标题 `matched_history_title` 和同话题标题 `related_titles`；`seen_items` 单独记录跳过的话题。文件还记录输入标题数、完全相同标题命中数、实际发送数、历史文件、召回数量、历史上下文字符数、所用快照与筛选模型信息；返回值通过 `selection_file` 给出路径。
- 新卡批次 JSON 记录 `complete`、`needs_research`、`rejected` 状态，以及第一轮 `evidence` 和主动搜索 `research_evidence`。成功的 `update` 不进入新批次，而是在原卡中改写 `knowledge` 并替换 `latest_update`；该对象保存更新时间、当前标题、本次进展摘要及本次证据。证据不足或被淘汰的更新不改原卡。
- 每张卡片用顶层 `share_score` 记录 0 分或 1 至 4 分的分享评分，最多保留一位小数；低于 3 分时 `share` 为 `null`，达到 3 分时 `share` 才包含分享文字、来源和评论选择。
- 返回值中的 `complete_count`、`needs_research_count` 和 `rejected_count` 分别统计三种状态；批次 JSON 还记录模型、逐话题模型请求数、实际并发上限、耗时、实际 Token 使用量、失败话题及原因，以及主动搜索的话题数、平台请求数、有效条目数和失败记录。`skipped_empty_evidence_count` 和 `skipped_empty_evidence_topics` 记录第一轮与补搜都没有可读正文、因而跳过模型请求的话题数和话题 ID；`web_search_calls` 固定为 0，表示该流程没有启用 Codex 原生 Web Search。单个话题请求或结果校验失败时保留为 `needs_research`，不会中断其他话题和批次产物。

内部筛选、证据补充和评论回填规则见[热榜学习](design.md#热榜学习)。

## 可选依赖与运行限制

浏览器采集需要安装项目的 browser 可选依赖和 Chromium：

```bash
uv pip install --python .venv/bin/python playwright
.venv/bin/python -m playwright install chromium
```

`jieba` 随项目安装，用于中文分词和词性加权匹配。

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
| `AGENTSCROLL_ASTRBOT_API_KEY` | AstrBot OpenAPI Key；使用 AstrBot transport 时必填 |
| `AGENTSCROLL_ASTRBOT_BASE_URL` | 覆盖 `integrations.astrbot.base_url`；Docker Compose 用它访问宿主机 AstrBot |

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
- `AGENTSCROLL_LIVE_HOTLIST_STAGE=first-pass|cards|full`：热榜测试阶段，默认 `first-pass`。`cards` 读取已有标题筛选结果，跳过标题筛选模型，直接采集材料并生成知识卡。
- `AGENTSCROLL_LIVE_HOTLIST_SELECTION_FILE=<path>`：`cards` 阶段使用的 `hotlist_first_pass_newsnow.json`；未设置时使用最近一次产物。
- `AGENTSCROLL_LIVE_HOTLIST_SNAPSHOT_FILE=<path>`：`cards` 阶段使用的原始 NewsNow 快照；未设置时重新拉取当前快照，并按标题匹配筛选结果。
- `AGENTSCROLL_LIVE_HOTLIST_GROUPS=综合`：热榜测试使用的 NewsNow 分组。

跳过标题筛选，使用已有结果生成知识卡：

```bash
AGENTSCROLL_RUN_LIVE_TESTS=1 \
AGENTSCROLL_LIVE_HOTLIST_STAGE=cards \
AGENTSCROLL_LIVE_HOTLIST_SELECTION_FILE=/path/to/hotlist_first_pass_newsnow.json \
AGENTSCROLL_LIVE_HOTLIST_SNAPSHOT_FILE=/path/to/newsnow_snapshot.json \
.venv/bin/python -m pytest \
  -k test_live_hotlist_learning_by_stage \
  -v
```

每次真实测试保存到 `outputs/test_artifacts/live_collectors/<timestamp>/`：

- `01_search/<source>.json`：搜索阶段候选。
- `02_content/<source>.json`：详情正文及读取状态。
- `03_comments/<source>.json`：评论及读取状态。
- `04_test_result/<suite>_<source>.json`：最终结果。

测试只有实际读取到正文才算通过；要求评论的平台还必须至少有一个条目返回真实评论。被拦截、要求登录和确实没有评论必须区分。微信公众号 live 测试只验证文章正文。
