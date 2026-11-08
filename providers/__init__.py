"""数据源实现共用的标识和异常，不创建独立客户端或缓存。"""

DATA_PROVIDER_NITTER = "nitter"
DATA_PROVIDER_FXTWITTER = "fxtwitter"
DATA_PROVIDER_OPTIONS = (DATA_PROVIDER_NITTER, DATA_PROVIDER_FXTWITTER)


class TwitterTimelineError(RuntimeError):
    """时间线请求失败、解析失败或分页结果不完整。"""


# 保留原有异常别名，两个数据源的时间线失败使用同一类型。
FxTwitterTimelineError = TwitterTimelineError
