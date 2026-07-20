<div align="center">


<img src="https://github.com/Ars1027/astrbot_plugin_twitter/blob/master/logo.png" width="256" alt="icon">

# Twitter 推文转发插件

[![AstrBot](https://img.shields.io/badge/AstrBot-Plugin-ff69b4?style=for-the-badge)](https://github.com/AstrBotDevs/AstrBot)
[![License](https://img.shields.io/badge/License-MIT-green.svg?style=for-the-badge)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.10+-blue.svg?style=for-the-badge&color=76bad9)](https://www.python.org/)

_✨ 支持 Nitter 与 FxTwitter API 双数据源的 Twitter 推文转发插件，提供 Dashboard 订阅管理、多会话独立订阅、定时推送、链接识别、合并转发与推文翻译。 ✨_

</div>

---

## 效果展示

<!-- 📸 将下方占位图替换为实际截图 -->
<table align="center" width="100%">
  <tr>
    <td align="center" width="50%" valign="top">
      <p><b>推文推送（合并转发）</b></p>
      <img src="https://free.picui.cn/free/2026/04/25/69ec6994774ef.png" alt="推送效果" width="100%">
    </td>
    <td align="center" width="50%" valign="top">
      <p><b>推文翻译效果</b></p>
      <img src="https://free.picui.cn/free/2026/04/25/69ec6994486f4.png" alt="翻译效果" width="100%">
    </td>
  </tr>
  <tr>
    <td align="center" width="50%" valign="top">
      <p><b>链接识别</b></p>
      <img src="https://free.picui.cn/free/2026/04/25/69ec69950ade5.png" alt="链接识别" width="100%">
    </td>
    <td align="center" width="50%" valign="top">
      <p><b>订阅列表</b></p>
      <img src="https://free.picui.cn/free/2026/04/25/69ec6994c9fe4.png" alt="订阅列表" width="100%">
    </td>
  </tr>
</table>

---

## 功能特色

### 📡 订阅管理
- **订阅/取关推主** — 在群聊或私聊中独立订阅与取消关注，各会话互不影响
- **批量关注** — 一次性订阅多个推主，支持 R18 和仅媒体选项
- **批量取关** — 一次性取关多个推主，支持批量操作
- **订阅列表** — 查看当前会话的所有订阅（按会话隔离）
- **推送开关** — 独立控制当前会话的推送状态
- **Dashboard 管理** — 在 AstrBot Dashboard 中集中查看各群聊订阅，并新增、修改、移除订阅或调整群级推送状态

### 🔄 定时推送
- **双数据源** — 默认保持 Nitter 行为，也可切换到 FxTwitter JSON API；FxTwitter 模式不会检测或访问 Nitter
- **自动轮询** — 定时检测已订阅推主的最新推文并推送
- **since_id 增量** — 基于 `since_id` 游标机制，仅推送新推文，避免重复
- **积压分轮补发** — 每个推主每轮默认最多推送 5 条，剩余内容按旧到新留待后续轮询，避免数据源恢复后集中刷屏
- **Nitter 镜像自动切换** — 当前镜像不可用时自动轮换到下一个可用镜像
- **集体转发模式** — 可选将一轮轮询内的多推主推文合并为一条转发消息
- **转帖控制** — 可配置轮询推送和 `/推特测试` 是否包含转帖；转帖会标明谁转发/引用了谁，并附带原帖正文与媒体
- **转帖去重** — 可选在轮询推送中对多个订阅推主转发的同一条原帖按会话去重
- **截图模式** — 可选将推文正文渲染为 X 官方深/浅色时间线风格截图
- **媒体资源独立发送** — 可关闭正文或截图之外的原图和视频发送；截图中的媒体预览仍会保留

### 🔗 链接识别
- **自动解析** — 聊天中出现 `twitter.com` / `x.com` 链接时自动解析推文内容，可在配置项选择开启或关闭

### 🌐 推文翻译
- **自动翻译** — 开启后推文正文自动翻译为目标语言，原文被替换
- **灵活 Provider** — 支持指定 LLM Provider，留空则自动选择
- **模型标注** — 翻译后推文末尾标注翻译所用模型名称

### 🛡️ 稳定性保障
- **实时订阅校验** — 推送时实时读取最新订阅数据，取关即时生效，避免重复推送
- **R18 / 媒体过滤** — 按会话独立配置，未开启 R18 的会话不接收敏感内容
- **逐条游标检查点** — 普通推送成功一条即保存一次游标；集体转发则在消息实际发送成功后再推进，减少重启造成的重复或漏推

---

## Dashboard 订阅管理

AstrBot `v4.24.2+` 可在 Dashboard 中直接打开插件的“Twitter 订阅管理”页面。页面会列出机器人已加入的群聊及现有其他会话订阅，并支持：

- 查看群名、群号、推主、推送状态、R18 和仅媒体设置
- 为群聊新增推主，修改或移除现有订阅
- 一次开启或关闭某个群聊的全部推送
- 调整插件全局轮询间隔

旧版 AstrBot 缺少 Plugin Pages API 时不会显示该页面，但插件仍可正常加载，聊天指令和自动推送功能不受影响。WebUI 中的轮询间隔仍是全局配置，对所有会话生效。

---

## 指令

| 指令 | 别名 | 说明 |
|------|------|------|
| `/推特关注 <用户名> [r18] [媒体]` | `/twitter_follow` | 订阅推主，可选开启 R18 和仅媒体 |
| `/推特批量关注 <用户1> <用户2> ... [r18] [媒体]` | `/twitter_batch_follow` | 批量订阅多个推主 |
| `/推特取关 <用户名>` | `/twitter_unfollow` | 取关推主（仅影响当前会话） |
| `/推特批量取关 <用户1> <用户2> ...` | `/twitter_batch_unfollow` | 批量取关多个推主（仅影响当前会话） |
| `/推特清空订阅` | `/twitter_clear_all` | 清空所有订阅（仅管理员） |
| `/推特列表` | `/twitter_list` | 查看当前会话的订阅列表 |
| `/推特推送 <开启\|关闭>` | `/twitter_push` | 开关当前会话的推送 |
| `/推特测试 <用户名>` | `/twitter_test` | 立即获取并推送指定推主的最新推文 |

---

## 配置项

> [!NOTE]
> 以下配置可在 AstrBot WebUI 的插件配置页面中设置。

### 基础配置

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `twitter_data_provider` | string | `nitter` | 数据源：`nitter` 保持原有 HTML 抓取；`fxtwitter` 使用 FxTwitter JSON API |
| `twitter_fxtwitter_api_base` | string | `https://api.fxtwitter.com` | FxTwitter API 基础地址，末尾斜杠会自动清理 |
| `twitter_nitter_url` | string | （空） | Nitter 镜像站地址，留空则使用内置列表自动切换（内置列表仅有1个且可能失效，强烈建议自定义） |
| `twitter_proxy` | string | （空） | 代理地址，如 `http://127.0.0.1:7890` |
| `twitter_pre_download_media` | bool | `false` | 配置代理后可预下载图片、头像和视频封面，媒体下载失败时保留文字并回退到原 URL |
| `twitter_poll_interval` | int | `5` | 推文轮询间隔（分钟），建议不低于 3 |
| `twitter_poll_max_tweets_per_user` | int | `5` | 每个推主每轮最多推送的推文数；积压内容按旧到新保留到后续轮询继续推送，最小值为 1 |

FxTwitter 部署示例：

```text
twitter_data_provider = fxtwitter
twitter_fxtwitter_api_base = https://api.fxtwitter.com
twitter_proxy = （空）
twitter_nitter_url = （空或保留；FxTwitter 模式不会使用）
twitter_poll_interval = 5
twitter_poll_max_tweets_per_user = 5
```

FxTwitter 时间线使用有限 cursor 分页并在本地按推文 ID 去重、筛选和排序；首次关注只记录最新 ID，不回放历史。自动轮询获取到超过单轮上限的内容时，会保存本轮最后成功处理的游标并在下一轮继续。API 当前返回的媒体 URL 可能仍属于 `pbs.twimg.com` / `video.twimg.com`，网络受限环境可配置 `twitter_proxy` 并开启 `twitter_pre_download_media`。

### 消息格式

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `twitter_use_node` | bool | `true` | 使用合并转发消息发送推文 |
| `twitter_no_text` | bool | `false` | 推文含媒体时不输出文字内容 |
| `twitter_text_render_mode` | string | `text` | 推文正文渲染方式：`text` 普通文字推送；`screenshot` 使用 AstrBot `html_render()` 渲染 X 暗色时间线风格截图 |
| `twitter_screenshot_theme` | string | `dark` | 截图模式主题：`dark` 黑色背景 / `light` 白色背景 |
| `twitter_send_media_separately` | bool | `true` | 是否在正文或截图之外单独发送原图和视频；关闭后截图中的媒体预览不受影响 |
| `twitter_image_quality` | string | `orig` | 推文图片质量：`large`（缩略图）/ `orig`（原图，默认） |
| `twitter_video_max_size_mb` | int | `256` | 视频直发大小上限；超过后改为发送说明和视频链接|
| `twitter_collective_forward` | bool | `false` | 集体转发模式（多推主推文合并为一条转发消息） |
| `twitter_collective_max_authors` | int | `5` | 集体转发时单条消息包含的最大推主数 |
| `twitter_include_tweet_link` | bool | `true` | 推送消息是否附带对应 X/Twitter 帖子链接，适用于轮询推送、`/推特测试` 和链接识别解析 |

### 内容过滤

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `twitter_include_retweets` | bool | `true` | 轮询推送和 `/推特测试` 是否推送转帖；关闭后测试指令会寻找最新非转贴推文 |
| `twitter_deduplicate_retweets` | bool | `false` | 轮询推送时对转帖去重；同一条原帖被多个订阅推主转发时 |
| `twitter_link_recognition_enabled` | bool | `true` | 推文链接识别全局开关，关闭后聊天中的 Twitter/X 链接不再自动解析 |

### 翻译配置

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `twitter_translate_enabled` | bool | `false` | 推文内容翻译开关 |
| `twitter_translate_target_lang` | string | `简体中文` | 翻译目标语言（如：简体中文、日语、英语） |
| `twitter_translate_provider_id` | string | （空） | 翻译使用的 LLM Provider ID，留空自动选择 |

> [!TIP]
> **LLM Provider 自动选择逻辑**：
> 1. 尝试使用配置中指定的 `Provider ID`
> 2. 回退到当前会话的 Provider
> 3. 回退到第一个可用的 Provider

---

## 安装

1. 将本目录放入 AstrBot 的插件目录
2. 安装依赖：
   ```bash
   pip install -r requirements.txt
   ```
3. 重启 AstrBot 或在 WebUI 中加载插件

## 依赖

- `httpx[http2]>=0.25.0`
- `beautifulsoup4>=4.12.0`

---

## 注意事项

> [!WARNING]
> - 默认数据源仍为 Nitter，以兼容旧配置；需要 FxTwitter 时请显式选择 `fxtwitter`
> - FxTwitter 是第三方公开 JSON API，并非 X/Twitter 官方 API；可用性、限流和字段可能随其服务更新变化
> - Nitter 镜像站可能随时失效，~~插件内置了多个镜像地址并支持自动切换~~
> - **强烈建议自行部署 Nitter** 以保证稳定性，项目地址：[https://github.com/zedeus/nitter](https://github.com/zedeus/nitter)
> - **Nitter本地部署教程**：https://mib7kzqsrf5.feishu.cn/wiki/O1ztwWl3GiBc4AknKvIcyaKsnFb?from=from_copylink
> - 翻译功能需至少配置一个可用的 LLM Provider

> [!CAUTION]
> **关于订阅隔离**：
> - 订阅数据按**会话（umo）** 隔离存储，私聊与群聊的订阅列表相互独立
> - 在私聊取关推主后，不会影响群聊的订阅状态，反之亦然
> - `/推特列表` 仅显示当前会话的订阅

---

## 参考项目

本插件在开发过程中参考了以下项目：

- [**nonebot-plugin-twitter**](https://github.com/nek0us/nonebot-plugin-twitter) — 参考了基于 Nitter 镜像站的推文抓取架构与推送机制设计
- [**astrbot_plugin_rsshub**](https://github.com/FlanChanXwO/astrbot_plugin_rsshub) — 参考了 AstrBot 插件框架下的订阅管理与 KV 存储模式
- [**astrbot_plugin_qq_group_daily_analysis**](https://github.com/SXP-Simon/astrbot_plugin_qq_group_daily_analysis) — 参考了 LLM Provider 选择逻辑（配置指定 → 会话 Provider → 第一个可用）以及翻译功能的 system_prompt 分离与重试机制

---

## 关于本项目

> [!IMPORTANT]
> 本项目代码由 ~~GLM-5.1~~ **codex** 辅助生成与迭代，可能存在遗留问题或未知的 Bug。如遇到任何异常，欢迎提交 [Issue](https://github.com/Ars1027/astrbot_plugin_twitter/issues) 反馈。

---

## 许可证

MIT License

欢迎提交 Issue 和 Pull Request 来改进这个插件！
