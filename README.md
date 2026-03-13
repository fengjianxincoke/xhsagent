# xhsagent

一个面向小红书 / 抖音的社媒采集 Agent。它会按关键词检索内容，补全帖子详情，用 AI 根据你的自然语言需求做相关性评分，并将结果落库、导出 CSV，必要时同步到飞书。

目前仓库主要包含两类任务：

- 采集任务：检索内容、AI 匹配、存储、导出、飞书同步
- 评论任务：从已导出的 CSV 读取链接，自动发表评论

## 功能概览

- 支持小红书采集，抖音可按配置开启
- 使用自然语言需求 + 关键词列表做内容筛选
- 通过兼容 OpenAI Chat Completions 的接口调用 Claude / 兼容模型进行评分
- 将结果保存到 SQLite，并按平台导出 CSV
- 支持飞书机器人通知和飞书多维表格同步
- 支持从最新 CSV 批量读取链接并自动评论
- 带有终端实时监控面板，便于观察采集状态

## 工作流程

1. 读取 `settings.yaml` 中的需求、关键词、平台和调度配置
2. 打开浏览器并恢复登录态
3. 在小红书 / 抖音中搜索关键词，抓取候选内容
4. 补全帖子详情、作者信息和互动数据
5. 调用 AI 对每条内容进行 0-100 分匹配评分
6. 将符合阈值的数据写入 SQLite
7. 定时导出 CSV，并按需推送飞书
8. 可选地从 CSV 中读取链接，执行自动评论

## 环境要求

- macOS 或可运行 Playwright Chromium 的环境
- Python `3.11`
- 可访问目标社媒平台
- 如需 AI 匹配 / 飞书同步，需要对应 API 网络连通性

## 快速开始

### 方式一：直接使用脚本

仓库自带启动脚本，会自动创建 `.venv`、安装 Python 依赖，并在缺少 Chromium 时执行 Playwright 安装。

```bash
./script/start.sh
```

首次运行建议这样操作：

1. 将 `settings.yaml` 里的 `browser.headless` 设为 `false`
2. 执行 `./script/start.sh`
3. 在弹出的浏览器中完成小红书登录
4. 如果开启了抖音，再完成抖音登录 / 验证
5. 登录态稳定后，可把 `browser.headless` 改回 `true`

### 方式二：手动安装

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m playwright install chromium
python -m pip install -e .
```

安装完成后可使用以下命令：

```bash
xhsagent
python -m xhsagent.main
```

## 配置说明

项目默认读取根目录下的 `settings.yaml`。

一个最小可用配置示例：

```yaml
requirement: "帮我查找适合新手养猫的猫粮推荐内容"

keywords:
  - "猫粮推荐"
  - "幼猫主食"

claude:
  apiKey: "YOUR_API_KEY"
  apiUrl: "https://your-openai-compatible-endpoint/v1/chat/completions"
  model: "claude-haiku-4-5"
  matchThreshold: 60
  maxTokens: 300

douyin:
  enabled: true
  cooldownMinutes: 3

schedule:
  crawlIntervalMinutes: 30
  csvExportIntervalHours: 6
  feishuSyncIntervalMinutes: 5
  maxPostsPerKeyword: 100
  startupDelaySeconds: 3

comments:
  enabled: false
  latestCsvOnly: true
  maxPerPlatform: 0
  xiaohongshu:
    enabled: false
    content: ""
  douyin:
    enabled: false
    content: ""

browser:
  headless: false
  locale: "zh-CN"
  viewportWidth: 1280
  viewportHeight: 900
  minDelayMs: 1500
  maxDelayMs: 4000
  saveSession: true
  sessionFile: "data/session.json"
  xhsSessionFile: "data/session_xhs.json"
  douyinSessionFile: "data/session_douyin.json"
  profileDir: "data/browser-profile"

storage:
  dbPath: "data/posts.db"
  csvOutputDir: "data/exports"
  dedupDays: 7

logging:
  level: "INFO"
  file: "data/agent.log"
```

### 关键配置项

| 配置项 | 说明 |
| --- | --- |
| `requirement` | 你的自然语言需求，AI 会据此判断帖子是否相关 |
| `keywords` | 实际执行搜索的关键词列表 |
| `claude.apiUrl` | OpenAI 兼容的 Chat Completions 接口地址 |
| `claude.matchThreshold` | AI 匹配最低分，低于该值不会保存 |
| `douyin.enabled` | 是否同时抓取抖音 |
| `douyin.cooldownMinutes` | 抖音触发风控后的冷却时间 |
| `schedule.crawlIntervalMinutes` | 每轮采集结束后，等待多久再开始下一轮 |
| `schedule.maxPostsPerKeyword` | 每个平台、每个关键词单轮最多抓取多少条 |
| `comments.enabled` | 是否启用评论任务 |
| `comments.latestCsvOnly` | `true` 时每个平台只读取最新一个 CSV |
| `comments.maxPerPlatform` | 每个平台最多评论多少条，`0` 为不限制 |
| `browser.headless` | 是否无头运行；首次登录建议设为 `false` |
| `storage.dedupDays` | 指定时间窗口内按 `(platform, post_id)` 去重 |

## 运行说明

### 启动采集任务

推荐：

```bash
./script/start.sh
```

等价命令：

```bash
python -m xhsagent.main
```

运行后会：

- 校验小红书登录态
- 如果开启抖音，则额外校验抖音登录态
- 启动 APScheduler
- 执行首轮采集
- 定时导出 CSV
- 按需推送飞书
- 在终端显示实时面板

### 启动评论任务

评论任务会从 `storage.csvOutputDir` 中读取平台 CSV，提取 `帖子链接 / url / 链接` 列，对对应平台执行评论。

推荐：

```bash
./script/start_comments.sh
```

等价命令：

```bash
python -m xhsagent.comment_main
```

启用评论任务前，至少要配置：

- `comments.enabled: true`
- `comments.xiaohongshu.content` 或 `comments.douyin.content`
- 对应平台已经完成登录
- `data/exports/` 中存在可用 CSV

程序会把评论执行结果记录到数据库的 `comment_logs` 表，已经成功评论过的相同 `platform + url + comment_text` 会自动跳过。

## 飞书集成

支持两种能力：

- Webhook 通知：每保存一条帖子时推送卡片消息
- 多维表格同步：定时把未同步帖子批量写入飞书 Bitable

### 配置步骤

1. 在 `settings.yaml` 填写 `feishu.appId`、`feishu.appSecret`、`feishu.appToken`、`feishu.tableId`
2. 如需机器人通知，填写 `feishu.webhookUrl`
3. 创建多维表格字段

```bash
python -m xhsagent.feishu_setup
```

如果你已经执行过 `pip install -e .`，也可以使用：

```bash
xhsagent-feishu-setup
```

4. 将 `feishu.enabled` 设为 `true`
5. 重启 Agent

默认写入的字段包括：

- 标题
- 作者
- 发布时间
- 点赞数
- 收藏数
- 评论数
- 帖子链接
- 搜索关键词
- AI匹配评分
- 匹配理由
- 正文摘要
- 采集时间

## 数据输出

### SQLite

默认数据库路径：

```text
data/posts.db
```

主要表：

- `posts`：采集结果
- `crawl_logs`：采集执行日志
- `comment_logs`：评论执行日志

### CSV

默认导出目录：

```text
data/exports/
```

文件命名格式：

- `xiaohongshu_posts_YYYYMMDD_HHMMSS.csv`
- `douyin_posts_YYYYMMDD_HHMMSS.csv`

CSV 列包含：

- 平台
- 帖子ID
- 标题
- 作者
- 正文摘要
- 发布时间
- 点赞数
- 收藏数
- 评论数
- 帖子链接
- 搜索关键词
- AI匹配评分
- 匹配理由
- 采集时间

### 运行状态文件

- `data/agent.log`：运行日志
- `data/session.json`：总 Session 快照
- `data/session_xhs.json`：小红书 Session
- `data/session_douyin.json`：抖音 Session
- `data/browser-profile/`：浏览器持久化 Profile
- `data/browser-profile-douyin/`：抖音独立 Profile
- `data/debug/`：登录异常、评论失败等调试截图和文本

## 常用脚本

```bash
./script/start.sh                 # 启动采集任务
./script/start_comments.sh        # 启动评论任务
./script/stop.sh                  # 停止采集任务
./script/reset_browser_state.sh   # 重置小红书浏览器状态并备份旧数据
```

`reset_browser_state.sh` 当前主要用于重置小红书主浏览器状态。执行后会保留数据库和导出 CSV，并提示你重新登录。

## 项目结构

```text
.
├── README.md
├── requirements.txt
├── settings.yaml
├── script/
│   ├── reset_browser_state.sh
│   ├── start.sh
│   ├── start_comments.sh
│   └── stop.sh
├── data/
│   ├── exports/
│   └── posts.db
└── xhsagent/
    ├── agent.py
    ├── browser.py
    ├── comment_main.py
    ├── config.py
    ├── csv_exporter.py
    ├── dashboard.py
    ├── database.py
    ├── feishu_exporter.py
    ├── feishu_setup.py
    ├── main.py
    ├── matcher.py
    └── models.py
```

## 注意事项

- 首次登录或登录失效时，务必将 `browser.headless` 设为 `false`
- 如果抖音开启失败，主程序会整体退出，不会只跑小红书
- 评论任务依赖已有 CSV，不会主动触发采集
- 依赖安装和 Playwright 浏览器下载需要联网
- 目标平台页面结构变化后，浏览器自动化逻辑可能需要调整

## License

MIT
