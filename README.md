<div align="center">


<img src="https://github.com/Ars1027/astrbot_plugin_twitter/blob/master/logo.png" width="256" alt="icon">

# Twitter 推文转发插件

[![AstrBot](https://img.shields.io/badge/AstrBot-Plugin-ff69b4?style=for-the-badge)](https://github.com/AstrBotDevs/AstrBot)
[![License](https://img.shields.io/badge/License-MIT-green.svg?style=for-the-badge)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.10+-blue.svg?style=for-the-badge&color=76bad9)](https://www.python.org/)

_✨ 基于 Nitter 镜像站的 Twitter 推文转发插件，支持多会话独立订阅、定时推送、链接识别、合并转发与推文翻译。 ✨_

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
- **订阅列表** — 查看当前会话的所有订阅（按会话隔离）
- **推送开关** — 独立控制当前会话的推送状态

### 🔄 定时推送
- **自动轮询** — 定时检测已订阅推主的最新推文并推送
- **since_id 增量** — 基于 `since_id` 游标机制，仅推送新推文，避免重复
- **Nitter 镜像自动切换** — 当前镜像不可用时自动轮换到下一个可用镜像
- **集体转发模式** — 可选将一轮轮询内的多推主推文合并为一条转发消息

### 🔗 链接识别
- **自动解析** — 聊天中出现 `twitter.com` / `x.com` 链接时自动解析推文内容
- **群聊独立开关** — 各群聊可独立开启/关闭链接识别

### 🌐 推文翻译
- **自动翻译** — 开启后推文正文自动翻译为目标语言，原文被替换
- **灵活 Provider** — 支持指定 LLM Provider，留空则自动选择
- **模型标注** — 翻译后推文末尾标注翻译所用模型名称

### 🛡️ 稳定性保障
- **实时订阅校验** — 推送时实时读取最新订阅数据，取关即时生效，避免重复推送
- **R18 / 媒体过滤** — 按会话独立配置，未开启 R18 的会话不接收敏感内容

---

## 指令

| 指令 | 别名 | 说明 |
|------|------|------|
| `/推特关注 <用户名> [r18] [媒体]` | `/twitter_follow` | 订阅推主，可选开启 R18 和仅媒体 |
| `/推特批量关注 <用户1> <用户2> ... [r18] [媒体]` | `/twitter_batch_follow` | 批量订阅多个推主 |
| `/推特取关 <用户名>` | `/twitter_unfollow` | 取关推主（仅影响当前会话） |
| `/推特清空订阅` | `/twitter_clear_all` | 清空所有订阅（仅管理员） |
| `/推特列表` | `/twitter_list` | 查看当前会话的订阅列表 |
| `/推特推送 <开启\|关闭>` | `/twitter_push` | 开关当前会话的推送 |
| `/推特链接识别 <开启\|关闭>` | `/twitter_link` | 开关链接识别（仅群聊） |
| `/推特测试 <用户名>` | `/twitter_test` | 立即获取并推送指定推主的最新推文 |

---

## 配置项

> [!NOTE]
> 以下配置可在 AstrBot WebUI 的插件配置页面中设置。

### 基础配置

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `plugin_enabled` | bool | `true` | 插件总开关 |
| `twitter_nitter_url` | string | （空） | Nitter 镜像站地址，留空则使用内置列表自动切换 |
| `twitter_proxy` | string | （空） | 代理地址，如 `http://127.0.0.1:7890` |
| `twitter_poll_interval` | int | `5` | 推文轮询间隔（分钟），建议不低于 3 |

### 消息格式

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `twitter_use_node` | bool | `true` | 使用合并转发消息发送推文 |
| `twitter_no_text` | bool | `false` | 推文含媒体时不输出文字内容 |
| `twitter_collective_forward` | bool | `false` | 集体转发模式（多推主推文合并为一条转发消息） |
| `twitter_collective_max_authors` | int | `5` | 集体转发时单条消息包含的最大推主数 |

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
> 3. 回退到主 LLM Provider
> 4. 回退到第一个可用的 Provider

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
> - 本插件通过爬取 Nitter 镜像站获取推文数据，**不依赖 Twitter API**
> - Nitter 镜像站可能随时失效，插件内置了多个镜像地址并支持自动切换
> - **强烈建议自行部署 Nitter** 以保证稳定性，项目地址：[https://github.com/zedeus/nitter](https://github.com/zedeus/nitter)
> - 合并转发消息功能依赖平台支持，部分平台可能无法正常显示
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
- [**astrbot_plugin_qq_group_daily_analysis**](https://github.com/SXP-Simon/astrbot_plugin_qq_group_daily_analysis) — 参考了 LLM Provider 选择逻辑（配置指定 → 会话 Provider → 主 Provider → 第一个可用）以及翻译功能的 system_prompt 分离与重试机制

---

## 关于本项目

> [!IMPORTANT]
> 本项目代码由 **GLM-5.1** 辅助生成与迭代，可能存在遗留问题或未知的 Bug。如遇到任何异常，欢迎提交 [Issue](https://github.com/Ars1027/astrbot_plugin_twitter/issues) 反馈。

---

## 许可证

MIT License

欢迎提交 Issue 和 Pull Request 来改进这个插件！
