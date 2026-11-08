<div align="center">

<img src="./logo.png" width="72" alt="Twitter 推文转发插件图标">

# Twitter 推文转发

将关注的 X/Twitter 推文自动送到 QQ 或 Telegram 会话；也可在聊天中解析链接、翻译推文，并通过 WebUI 管理订阅。

[![AstrBot 插件](https://img.shields.io/badge/AstrBot-Plugin-3178c6?style=flat-square)](https://github.com/AstrBotDevs/AstrBot)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776ab?style=flat-square)](https://www.python.org/)
[![MIT License](https://img.shields.io/badge/License-MIT-238636?style=flat-square)](./LICENSE)

**[快速开始](#快速开始) · [效果展示](#效果展示) · [平台差异](#平台差异) · [指令](#聊天指令) · [配置](#配置参考)**

</div>

![Nitter 或 FxTwitter 获取推文，经插件处理后推送到不同 AstrBot 会话的流程](./asset/readme/hero.svg)

## 效果展示

QQ 支持普通消息与合并转发，Telegram 按帖子发送文字、照片或图片相册；两者均可使用截图模式，将正文渲染成仿推特风格的时间线卡片。

<p align="center"><strong>QQ · 合并转发推送</strong><br><img src="./asset/readme/forward-delivery.png" alt="群聊中收到的推文合并转发消息" width="440"></p>

<p align="center"><strong>截图模式 · 仿推特风格渲染</strong><br><a href="./asset/readme/tweet-screenshot-render.png"><img src="./asset/readme/tweet-screenshot-render.png" alt="截图模式将推文与引用内容渲染为仿推特风格的深色时间线卡片；点击查看原图" width="320"></a></p>

<details>
<summary>展开查看翻译、链接识别与批量关注效果</summary>

**推文翻译**

<img src="./asset/readme/translated-tweet.png" alt="推文翻译后的聊天消息" width="440">

**链接识别**

<img src="./asset/readme/link-recognition.png" alt="在聊天中识别并解析推文链接" width="440">

**批量关注**

<img src="./asset/readme/batch-follow.png" alt="聊天中批量关注多个推主的输入与结果" width="440">

</details>

## 快速开始

1. 将本仓库放入 AstrBot 的插件目录，进入该目录后，在 AstrBot 使用的 Python 环境中安装依赖：

   ```bash
   pip install -r requirements.txt
   ```

2. 在 AstrBot 中连接 QQ 或 Telegram 适配器并加载插件。默认数据源是 Nitter；也可将 `twitter_data_provider` 改为 `fxtwitter`，请配置可用的 `twitter_nitter_url` 或 `twitter_fxtwitter_api_base`，保存后重载插件。
3. 在要接收推送的会话中发送 `/推特关注 <用户名>`，然后用 `/推特测试 <用户名>` 检查消息效果；发送 `/推特列表` 可查看当前会话的订阅。

首次将新推主加入插件时，会以当前最新推文建立增量游标，之后的自动轮询推送新内容；需要立即查看一条推文时可使用 `/推特测试`。

## 主要功能

- **按会话订阅**：QQ、Telegram 的群聊和私聊各自维护订阅与推送开关；Telegram 超级群话题也可独立订阅，支持指令和 WebUI 管理。
- **定时追踪**：从 Nitter 或 FxTwitter 读取增量推文，按旧到新推送；每个推主每轮可限制推送数量。
- **失败补发**：记录各会话的推送结果，失败后只向未完成的会话重试。
- **消息呈现**：支持普通消息、单条或集体合并转发、深浅色截图、图片与视频，以及可选的转帖去重。
- **按需解析与翻译**：手动或自动解析 `twitter.com` / `x.com` 链接；可选择 X 自带翻译（仅 FxTwitter）或 LLM 翻译。
- **推送历史检索**：可选开启，供模型通过工具按需查询本会话最近成功推送的摘要，让机器人“知道”自己最近推送了什么消息。

### WebUI 订阅管理

在支持 **Plugin Pages API** 的 AstrBot 版本中，可从 Dashboard 打开“Twitter 订阅管理”。页面提供会话搜索、订阅开关、R18 与仅媒体设置、群级推送开关和轮询间隔调整；也可清空指定群的订阅，或一键复制当前会话的推主列表。

下图使用**模拟数据**展示 WebUI。

![使用模拟数据预览的 Twitter 订阅管理 WebUI](./asset/readme/webui-preview.png)

选中已连接的群聊后，可单个或批量添加推主。批量输入支持换行、空格、逗号与分号分隔，每批最多 100 个不同的有效账号；执行时可停止后续请求，已成功的添加不会撤销。点开某位推主可查看该会话最近 **5 条成功推送**的文字摘要、时间与原帖链接；记录保存在订阅数据中，插件重载后仍可查看，不保存媒体文件。旧订阅在此功能上线前的推送不会补录。

旧版 AstrBot 若缺少 Plugin Pages API，插件主体仍可加载，订阅可继续通过聊天指令管理。

### 平台差异

两种平台共用关注、测试和解析指令，主要差异如下：

| 项目 | QQ（`aiocqhttp`） | Telegram（`telegram`） |
| --- | --- | --- |
| 订阅会话 | 群聊、私聊 | 群聊、私聊、超级群话题 |
| 消息形式 | 普通消息或合并转发 | 按帖发送文字、照片或图片相册 |
| 集体转发 | 轮询结束后汇总为合并转发 | 轮询结束后逐帖发送 |
| WebUI 群列表 | 自动读取机器人所在群 | 显示已有订阅的群及手动添加的群 |
| 默认视频直发上限 | 256 MB | 50 MB，可独立配置 |

**群组与话题**：Telegram 无法自动列出机器人加入的全部群聊。可在 WebUI 点击“添加 Telegram 群组”，选择机器人并填写完整负数群 ID，例如 `-1001234567890`；机器人需已入群且有发言权限。话题订阅需先在对应话题中通过指令建立，之后可在 WebUI 管理。两种平台的私聊订阅均显示在“其他会话”中。

手动添加的 Telegram 群组在暂无订阅时也会保留；清空订阅后，可点击“删除群组”移除列表记录。

**图片与正文**：Telegram 同一帖的图片超过 10 张时分批发送相册，短正文与帖子链接可附在图片说明中，长正文和视频单独发送。

#### Telegram 视频发送限制

`twitter_telegram_video_max_size_mb` 默认 **50 MB**，与 QQ 的视频上限分别设置。超限时改发视频链接，上传失败时尝试降级为链接，明确限流时先等待冷却；关闭独立媒体发送后，不再追加视频或降级链接。

公共 Bot API 的视频上传上限目前为 [50 MB](https://core.telegram.org/bots/api#sendvideo)。发送更大视频需自建并启用 `--local` 的 Bot API 服务（[最高 2000 MB](https://core.telegram.org/bots/api#using-a-local-bot-api-server)），再提高插件的 Telegram 视频上限。仅调大插件配置无法突破服务端限制，也不能调整适配器的上传超时。网络不稳定时，未超限的视频也可能发送失败。

## 聊天指令

指令作用于**当前会话**，除非下表另有说明。`<用户名>` 可填写推主 ID，关注时也可带 `@`。

| 指令 | 英文别名 | 作用 |
| --- | --- | --- |
| `/推特关注 <用户名> [r18] [媒体]` | `/twitter_follow` | 关注推主；可选允许 R18、仅推送含媒体的内容 |
| `/推特批量关注 <用户1> <用户2> ... [r18] [媒体]` | `/twitter_batch_follow` | 批量关注，选项应用于本次所有推主 |
| `/推特取关 <用户名>` | `/twitter_unfollow` | 取消当前会话中的订阅 |
| `/推特批量取关 <用户1> <用户2> ...` | `/twitter_batch_unfollow` | 批量取消当前会话中的订阅 |
| `/推特列表` | `/twitter_list` | 查看当前会话的订阅：QQ 合并转发，Telegram 文字分段 |
| `/推特推送 <开启\|关闭>` | `/twitter_push` | 开关当前会话的全部推送 |
| `/推特测试 <用户名>` | `/twitter_test` | 立即获取并发送该推主的最新推文 |
| `/推特解析 <推文链接>` | `/twitter_parse` | 手动解析指定推文链接 |
| `/推特清空订阅` | `/twitter_clear_all` | 清空所有会话的订阅；需要 AstrBot 管理员权限 |

`/推特列表` 每段最多 50 条订阅、正文不超过 1000 个字符；名称较长时每段可能少于 50 条。QQ 将各段汇总为合并转发，Telegram 逐段发送文字。

## 配置参考

在 AstrBot 插件配置页面设置以下选项。默认值与当前 `_conf_schema.json` 一致；旧版顶层扁平配置仍可读取。

| 常用配置 | 默认值 | 用途 |
| --- | --- | --- |
| `twitter_data_provider` | `nitter` | 选择 `nitter` 或 `fxtwitter` |
| `twitter_nitter_url` | 空 | 自定义 Nitter 镜像地址；只在 Nitter 模式使用 |
| `twitter_fxtwitter_api_base` | `https://api.fxtwitter.com` | FxTwitter API 地址；只在 FxTwitter 模式使用 |
| `twitter_poll_interval` | `5` | 轮询间隔，单位为分钟 |
| `twitter_poll_max_tweets_per_user` | `5` | 每位推主每轮最多推送的条数 |
| `twitter_text_render_mode` | `text` | `text` 文字，或 `screenshot` 截图 |
| `twitter_translate_enabled` | `false` | 是否翻译推文正文 |

<details>
<summary>展开查看全部配置项</summary>

### 数据源与网络

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `twitter_data_provider` | `nitter` | `nitter` 抓取 HTML；`fxtwitter` 读取 JSON API |
| `twitter_nitter_url` | 空 | 自定义 Nitter 镜像地址 |
| `twitter_fxtwitter_api_base` | `https://api.fxtwitter.com` | FxTwitter API 基础地址 |
| `twitter_proxy` | 空 | 可选代理，例如 `http://127.0.0.1:7890` |
| `twitter_pre_download_media` | `false` | 配置代理后预下载图片与视频封面；失败时回退原 URL |
| `twitter_poll_interval` | `5` | 轮询间隔（分钟），建议不低于 3 |
| `twitter_poll_max_tweets_per_user` | `5` | 每位推主每轮最多推送条数，实际范围为 1–500 |

### 消息格式

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `twitter_use_node` | `true` | QQ 单条推文使用合并转发；Telegram 按帖发送图片相册与文字 |
| `twitter_no_text` | `false` | 推文含媒体时不输出文字 |
| `twitter_text_render_mode` | `text` | `text` 文字，或 `screenshot` 截图 |
| `twitter_screenshot_theme` | `dark` | 截图使用 `dark` 或 `light` 主题 |
| `twitter_send_media_separately` | `true` | 正文或截图之外继续发送原图、视频及视频降级链接 |
| `twitter_image_quality` | `orig` | `large` 缩略图，或 `orig` 原图 |
| `twitter_video_max_size_mb` | `256` | QQ 等非 Telegram 平台的视频直发上限（MB），超过后发送链接 |
| `twitter_telegram_video_max_size_mb` | `50` | Telegram 视频直发上限（MB），详见上方视频发送限制 |
| `twitter_collective_forward` | `false` | 轮询结束后统一发送；QQ 汇总为合并转发，Telegram 逐帖发送；需同时开启 `twitter_use_node` |
| `twitter_collective_max_authors` | `5` | QQ 单条集体转发消息中的最多推主数 |
| `twitter_include_tweet_link` | `true` | 在推送、测试和解析消息末尾附带帖子链接 |

### 内容与翻译

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `twitter_include_retweets` | `true` | 轮询与测试时包含转帖 |
| `twitter_deduplicate_retweets` | `false` | 同一原帖被多个推主转发时按会话去重 |
| `twitter_link_recognition_enabled` | `auto` | `auto` 自动解析、`command` 仅指令解析、`off` 关闭 |
| `twitter_agent_query_enabled` | `false` | 允许 Agent 调用 `twitter_recent_pushes` 查询本会话推送摘要 |
| `twitter_translate_enabled` | `false` | 自动翻译推文正文 |
| `twitter_translate_method` | `llm` | `x` 使用 X 自带翻译，`llm` 使用 LLM 翻译；旧配置默认使用 LLM |
| `twitter_translate_native_lang` | `zh` | X 翻译目标语言代码，如 `zh`、`ja`、`en`；仅选择 X 翻译时显示 |
| `twitter_translate_target_lang` | `简体中文` | LLM 翻译目标语言；仅选择 LLM 翻译时显示 |
| `twitter_translate_provider_id` | 空 | 仅选择 LLM 翻译时显示；留空时依次尝试当前会话与第一个可用 Provider |
| `twitter_translate_timeout_seconds` | `60` | 单条推文翻译的总时限（秒），超时回退原文 |
| `twitter_translate_custom_prompt_enabled` | `false` | 使用自定义翻译 system prompt；仅选择 LLM 翻译时显示 |
| `twitter_translate_custom_prompt` | 内置提示词 | 自定义提示词，支持 `{target_lang}` 变量；仅选择 LLM 翻译时显示 |

**翻译方式说明**：X 自带翻译仅支持 FxTwitter 数据源；使用 Nitter 时选择此方式不翻译。接口未返回译文或翻译失败时保留原文，不自动调用 LLM。上游不支持 **X Article 长文章正文翻译**，文章正文仍显示原文；如需翻译文章，请选择 LLM 翻译并配置可用的 Provider。

</details>

## 使用前了解

- **Nitter 镜像**：nitter镜像站目前已不可用 2026/8/27；需要自行部署时可参考 [Nitter 项目](https://github.com/zedeus/nitter)及[本地部署教程](https://mib7kzqsrf5.feishu.cn/wiki/O1ztwWl3GiBc4AknKvIcyaKsnFb?from=from_copylink)。
- **FxTwitter 数据**：FxTwitter 是第三方公开 JSON API，并非 X/Twitter 官方 API；接口、限流和可用性由其服务决定，因此随时都有可能像Nitter一样挂掉。也可参考 [FxTwitter 官方仓库（FxEmbed）](https://github.com/FxEmbed/FxEmbed) 自行本地部署，并将 `twitter_fxtwitter_api_base` 设置为自建实例的 API 地址。
- **历史补发范围**：已进入重试窗口的推文按保存的条目继续尝试；尚未接纳、且数据源已删除或不再返回的推文，无法保证补齐。停机时间过长或数据源分页不足也会影响历史补发范围。
- **重复发送边界**：群消息发送与本地状态保存无法原子提交。发送成功后进程被强制终止，或本地 KV 持续写入失败时，下次轮询可能再次发送。
- **会话隔离**：私聊和群聊分别保存订阅；在一处取关或关闭推送，不影响其他会话。
- **截图头像缓存**：截图模式会在 AstrBot 插件数据目录的 `avatar_cache` 中保存有界头像缓存，最多 200 项、总计 16 MiB；它不包含推文原图或视频，也不受独立媒体发送开关控制。

## 参考与反馈

本插件参考了 [nonebot-plugin-twitter](https://github.com/nek0us/nonebot-plugin-twitter) 的 Nitter 抓取思路、[astrbot_plugin_rsshub](https://github.com/FlanChanXwO/astrbot_plugin_rsshub) 的订阅管理与 KV 存储模式。

项目代码由 Codex 辅助生成与迭代。遇到问题欢迎提交 [Issue](https://github.com/Ars1027/astrbot_plugin_twitter/issues)，也欢迎提交 Pull Request。代码采用 [MIT License](./LICENSE)。
