# 诊断日志参考

[返回使用说明](../README.md)

本文说明插件默认输出的轮询摘要与失败日志，供排查具体问题时参考。

## 轮询摘要

每轮轮询结束时以 INFO 输出 `poll.round.end`，字段含义如下：

| 字段 | 说明 |
| --- | --- |
| `status` | `completed` 本轮流程正常结束；`stopped_early` 提前结束；`interrupted` 异常或取消中断 |
| `elapsed` | 整轮耗时（秒），包含逐推主固定等待、发送与集体转发刷新，不含两轮之间的轮询间隔 |
| `authors` | 本轮开始时的推主总数 |
| `checked` | 已开始检查的推主数 |
| `checks_failed` | 检查返回失败或抛出异常的推主数 |
| `delivery_failures` | 检查阶段即时返回失败的推文数 |

`completed` 仅表示本轮流程结束，不保证每条消息均发送成功；集体转发的最终结果需结合积压与 `collective.flush.result` 查看。

## 失败日志

发送失败、详情获取失败和订阅操作异常会输出 WARN 或 ERROR 日志，包含 `stage`、`elapsed`、`causes` 等字段：

- `stage` 标识失败阶段，例如 `prepare_images`、`send_photo`、`send_media_group`、`platform_send`。
- `causes` 保留 SDK 包装前的异常类型链，用于区分 `ConnectTimeout`、`WriteTimeout`、`ReadTimeout` 和 `PoolTimeout`。
- 日志不输出消息正文、Bot Token 或完整媒体 URL；`target` 与 `media` 使用哈希标记，同值可相互关联。

视频发送失败时会改发链接，并以 `video.link.sent` 确认降级成功。

## Telegram 上传写入超时

AstrBot v4.29.4 的 Telegram 适配器使用 python-telegram-bot 默认的 `media_write_timeout`（20 秒）。它约束网络写入等待，不等于整个视频必须在上传 20 秒内完成，不能直接按“带宽 × 20 秒”计算文件大小上限。实际发送还受代理、链路稳定性和所用适配器版本影响。
