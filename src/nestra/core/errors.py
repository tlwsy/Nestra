"""异常层次。

分层原则：每个域一个基类，调度器据此决定重试策略。
`Retryable` / `Fatal` 是横切标记，调度器只看这两个 mixin，
不需要认识具体的域异常。
"""

from __future__ import annotations


class NestraError(Exception):
    """所有自定义异常的根。"""


class Retryable(NestraError):
    """瞬时故障，退避后重试有意义（网络超时、429、5xx）。"""

    def __init__(self, message: str, *, retry_after_sec: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_sec = retry_after_sec


class Fatal(NestraError):
    """确定性故障，重试无意义（404、选择器不匹配、配置错误）。"""


# ── 配置 ──────────────────────────────────────────────────────────


class ConfigError(Fatal):
    """配置加载或校验失败。启动期抛出，拒绝启动。"""

    def __init__(self, message: str, *, path: str | None = None) -> None:
        self.path = path
        super().__init__(f"{path}: {message}" if path else message)


class ConfigValidationError(ConfigError):
    """聚合多条校验错误，一次性报给用户。

    逐条抛出会让用户改一个错重启一次，体验很差。
    """

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        body = "\n".join(f"  - {e}" for e in errors)
        super().__init__(f"配置校验失败（{len(errors)} 项）:\n{body}")


# ── 存储 ──────────────────────────────────────────────────────────


class StorageError(NestraError):
    """数据库访问失败。"""


class MigrationError(StorageError, Fatal):
    """迁移应用失败。启动期抛出。"""


# ── 抓取与提取 ────────────────────────────────────────────────────


class CrawlError(NestraError):
    """抓取域基类。"""


class FetchTimeout(CrawlError, Retryable):
    pass


class FetchFailed(CrawlError, Retryable):
    """连接中断或可重试 HTTP 状态在耗尽重试后仍失败。"""


class RateLimited(CrawlError, Retryable):
    pass


class HttpStatusError(CrawlError, Fatal):
    """不可重试的 HTTP 错误。"""


class ResponseTooLarge(CrawlError, Fatal):
    """响应超过抓取上限。"""


class NotFound(CrawlError, Fatal):
    pass


class RobotsDenied(CrawlError, Fatal):
    pass


class ExtractError(NestraError):
    """提取域基类。"""


class ContentTooShort(ExtractError, Fatal):
    """正文短于阈值，通常意味着选择器失效或需要 JS 渲染。"""


class SelectorMiss(ExtractError, Fatal):
    pass


class ContentRejected(ExtractError, Fatal):
    """页面明确是权限/错误提示，不应当重试或进入标签流水线。"""


# ── 打标 ──────────────────────────────────────────────────────────


class TaggerError(NestraError):
    """打标域基类。"""


class ProviderUnavailable(TaggerError, Retryable):
    """单个 provider 不可用，链上还有下一个可试。"""


class QuotaExhausted(TaggerError, Fatal):
    """额度耗尽。对该 provider 是 fatal，链继续。"""


class InvalidTaggerOutput(TaggerError, Retryable):
    """LLM 输出不合结构或含未知标签。有限次重试。"""


class TagsetNotReady(TaggerError, Fatal):
    """标签集未冻结或 checksum 不匹配。"""


class AllBackendsFailed(TaggerError, Retryable):
    """整条链加本地兜底全失败。文章留在 EXTRACTED 等下轮。"""


# ── 推送 ──────────────────────────────────────────────────────────


class NotifyError(NestraError):
    """推送域基类。"""


class TargetRejected(NotifyError, Fatal):
    """渠道拒绝（token 失效、URL 非法）。"""


class NotifyTransient(NotifyError, Retryable):
    pass


# ── 安全 ──────────────────────────────────────────────────────────


class SecurityError(NestraError):
    """安全域基类。"""


class SsrfBlocked(SecurityError, Fatal):
    """目标地址指向内网或元数据服务。"""

    def __init__(self, url: str, reason: str) -> None:
        self.url = url
        self.reason = reason
        super().__init__(f"拒绝访问 {url}: {reason}")


class DecryptionFailed(SecurityError, Fatal):
    """密钥不匹配。通常是 NESTRA_SECRET_KEY 变了。"""
