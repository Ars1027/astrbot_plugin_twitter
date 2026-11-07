# 诊断日志参考

[返回使用说明](../README.md)

本文记录日志字段、轮询进度和发送重试行为，供排查具体问题时参考。

默认关闭基础设置中的“详细诊断日志”（`twitter_detailed_diagnostics=false`）。AstrBot 使用 DEBUG 级别时，每位推主每次检查输出一条 `poll.author.summary`，无更新时不再展开请求、分页和空重试窗口；有新帖时仍记录帖子 ID、转帖标记、处理结果和游标提交。摘要中的 `since` 为检查开始时的游标，`new` 为本次新接纳数（尚未成功获取列表时为 `null`），`retry_items` 为待恢复条目数，`handled` 为本次已处理或已排队的条目数，不等于成功发送群消息数；`status` 区分无更新、检查结束、达到条数上限、失败、订阅已移除或中断。集体转发的最终送达仍应查看整轮末尾的刷新结果。

排查具体请求、截图或上传耗时时，临时开启“详细诊断日志”，并将 AstrBot 日志级别设为 DEBUG，保存配置并重载插件。单次复现后按 `[twitter:` 查找日志锚点，再按 `trace` 关联同一次操作；排查结束后关闭详细诊断。开关按插件实例生效，兼容旧版顶层同名配置。INFO 的整轮统计与订阅操作，以及 WARN/ERROR 异常日志不受此开关影响。日志不新增消息正文、Bot Token、完整媒体 URL 或分页游标；`target`、`media` 和 `cursor` 使用哈希标记，同值可关联。

常用锚点：开启详细诊断后，`timeline.page.start/result` 与 `timeline.cursor.repeated` 定位分页停滞；`fx.request.start/response/failed` 查看 HTTP 状态和耗时；`render.start/complete/fallback` 定位截图；`video.size` 查看大小探测和实际上限；`tg.batch.*`、`tg.upload.*`、`platform.send.*` 定位发送。默认 DEBUG 仍保留 `video.link.sent` 确认视频链接降级成功，`poll.tweet.result`、`receipts.save.complete`、`cursor.*` 区分已发送、排队、失败和保存进度；分页重复及发送失败等异常锚点也始终保留。`poll.author.summary` / `poll.author.end` 不能单独作为发送成功凭据；`cursor.*.result` 的 `accepted` 表示订阅服务是否接纳此次提交，`receipts.save.complete` 表示保存调用无异常结束，均不代表每次都新增了 KV 写入。

每轮会以 INFO 输出 `poll.round.start` 和 `poll.round.end`，整轮及其子调用共用 `trace`。结束日志的 `elapsed` 单位为秒，包含逐推主的固定等待、发送、限流退避及集体转发刷新，不包含两轮之间配置的轮询间隔。`authors` / `checked` / `unchecked` 分别表示本轮开始时的推主总数、已开始检查数和未检查数；`checks_failed` 表示检查返回失败或抛出异常的推主数，`delivery_failures` 仅计检查阶段即时返回失败的推文，集体发送最终结果需结合积压及 `collective.flush.result` 查看；`limit_authors` 表示因每轮条数上限留下后续条目的推主数。

`known_backlog` / `backlog_authors` 表示整轮结束时已知尚未提交的推文数及推主数：按推主和推文 ID 去重，合并本轮接纳条目、持久化重试窗口及旧回执，并在实际集体发送完成后按最终游标扣除已处理内容。它不是待发送群消息数，不包含数据源尚未返回的新帖；分页失败时不能据此判断上游没有积压。`status=stopped_early` 表示提前结束，`interrupted` 表示异常或取消中断，`completed` 仅表示本轮流程结束，不保证每条消息均发送成功。读取最终统计失败时积压字段为 `null`，不会误报为零。

达到每轮条数上限后，已接纳但未处理的条目及转帖元数据会与已完成游标一起保存。后续轮询优先恢复该窗口，插件重载或条目滑出时间线后仍可按 ID 获取详情；窗口处理完成前不接纳新批次。

平台明确返回 `retry_after`（兼容 Telegram SDK 秒数或 `timedelta`）或 HTTP 429 时，普通消息、视频、合并转发和 Telegram 相册会按机器人实例共享冷却。优先遵守平台等待时间；429 未提供有效 `Retry-After` 时按 2、4、8 秒退避，每次发送最多尝试 3 次，累计等待预算为 60 秒。需要等待更久或重试耗尽时暂缓本次发送，冷却状态供该实例后续发送共用，其他机器人实例可继续处理；冷却仅保存在内存中，插件重载后重置。限流不会触发拆图、合并消息降级或视频链接补发；主要内容未送达时保留游标与已有成功会话回执，附加媒体仍沿用最佳努力策略。适配器只返回 `False`、吞掉限流信息或 QQ 风控未提供明确限流字段时，无法自动识别等待时间。网络超时不按限流重试，以免重复发送结果不确定的请求。

`platform.rate_limited` 为 WARN，记录限流等待秒数及尝试次数；`platform.backoff.wait` / `platform.backoff.deferred` 为 DEBUG，分别表示等待冷却与暂缓发送。日志中的 `platform` 为机器人实例标识的哈希。

Telegram 普通消息按组件及文本分段分别执行限流重试，长文本每段不超过 4096 个 UTF-16 单位。后一段限流时仅重试该段，不在本次退避中重发成功的前段；原生相册仍以单次相册请求为重试单位。

失败日志中的 `stage`、`elapsed`、`bytes` 和 `causes` 可帮助定位问题。相册的 `prepare_images` 表示获取图片阶段，`send_photo` / `send_media_group` 表示文件已准备好、正在调用 Telegram；视频的 `platform_send` 包括适配器内部的下载和发送，不能仅凭该阶段区分二者。

`ConnectTimeout`、`WriteTimeout`、`ReadTimeout`、`PoolTimeout` 分别提示连接、上传写入、等待响应、等待连接池超时；底层异常未保留时仍需结合服务器网络测试判断。请在 AstrBot 所在容器或进程的网络环境中检查 Telegram API 和媒体源的可达性；`twitter_proxy` 配置不会自动应用到 Telegram SDK 上传。超时后服务端可能已收件，重测前先核对聊天记录。

订阅管理在 INFO 级别输出 `subscription.add.result`、`subscription.remove.result`、`subscription.update.result`、`subscription.session_status.result` 和 `subscription.clear.result`，覆盖调用订阅服务的聊天指令、批量操作和 WebUI。结果记录推主、会话哈希、成功或拒绝原因；新增区分 `created_author`（新建推主）和 `replaced`（覆盖已有会话选项），删除的 `author_removed` 表示是否移除了最后一个订阅关系及推主进度。清空记录删除前的推主数和关系数，群级开关记录影响数量。抛出异常或取消时输出 WARN `subscription.<操作>.failed`，只保留异常类型链，不记录异常正文。

首次新增推主会输出 `subscription.cursor.selected`（获取到的初始候选）与 `subscription.cursor.saved`（持久化后的最终游标）。`source=new` 表示新建，`existing` 表示复用现有推主，`concurrent_existing` 表示获取资料期间另一个入口已先创建推主，此时复用其游标；候选日志不代表保存成功。`strategy=initial_batch_max_id` 表示初始游标取时间线首批有效条目的最大原帖 ID，避免顶部的旧帖转发导致补推关注前的历史内容。一次订阅操作及其请求共享 `trace`，可将该游标与后续 `poll.author.summary` 对照。

## Telegram 上传写入超时

AstrBot v4.29.4 的 Telegram 适配器使用 python-telegram-bot 默认的 `media_write_timeout`（20 秒）。它约束网络写入等待，不等于整个视频必须在 20 秒内上传完成，不能直接按“带宽 × 20 秒”计算文件大小上限。实际发送还受代理、链路稳定性和所用适配器版本影响。
