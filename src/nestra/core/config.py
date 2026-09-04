"""配置模型、加载与交叉校验。

三条硬规则：

1. YAML 只放行为配置，机密一律走环境变量。模型里存的是**变量名**。
2. 校验失败拒绝启动，且一次报全部错误（`ConfigValidationError`）。
   不用默认值兜混淆错误。
3. 站点配置的运行期真值在 DB。YAML 的 `sites[]` 仅在库中无该 slug 时导入，
   之后以 DB 为准 —— 向导写库，YAML 写文件，两者会打架，必须定一个赢家。
"""

from __future__ import annotations

import ipaddress
import os
import re
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from .errors import ConfigError, ConfigValidationError

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
Slug = Annotated[str, Field(pattern=_SLUG_RE.pattern)]


class _Base(BaseModel):
    """拒绝未知字段。拼错的键会被静默忽略是配置系统最坑的失败模式。"""

    model_config = ConfigDict(extra="forbid")


# ── app / runtime / web / storage ─────────────────────────────────


class AppConfig(_Base):
    timezone: str = "Asia/Shanghai"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["json", "console"] = "json"

    @field_validator("timezone")
    @classmethod
    def _tz_exists(cls, v: str) -> str:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"未知时区 {v!r}") from exc
        return v


class RuntimeConfig(_Base):
    web_workers: int = Field(1, ge=1, le=8)
    crawl_concurrency: int = Field(4, ge=1, le=64)
    playwright_concurrency: int = Field(1, ge=1, le=1)
    sqlite_cache_mb: int = Field(32, ge=4, le=512)


class WebConfig(_Base):
    host: str = "127.0.0.1"
    port: int = Field(8080, ge=1, le=65535)
    base_url: str = "http://localhost:8080"
    cookie_secure: bool = True
    session_days: int = Field(14, ge=1, le=365)
    trusted_proxies: list[str] = Field(default_factory=list)

    @field_validator("base_url")
    @classmethod
    def _strip_slash(cls, v: str) -> str:
        try:
            parsed = urlsplit(v)
            _ = parsed.port
        except ValueError as exc:
            raise ValueError("base_url 非法") from exc
        if (
            not 1 <= len(v) <= 4096
            or parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
            or any(ord(char) <= 32 or ord(char) == 127 for char in v)
        ):
            raise ValueError("base_url 必须是无凭据、路径、查询参数或片段的 http(s) 源站")
        return v.rstrip("/")

    @field_validator("trusted_proxies")
    @classmethod
    def _valid_trusted_proxies(cls, values: list[str]) -> list[str]:
        try:
            for value in values:
                _ = ipaddress.ip_network(value, strict=False)
        except ValueError as exc:
            raise ValueError("trusted_proxies 必须是合法 IP 或 CIDR") from exc
        return values


class StorageConfig(_Base):
    db_path: Path = Path("data/db/nestra.db")
    attachment_dir: Path = Path("data/attachments")


# ── 标签集分组 ────────────────────────────────────────────────────


class TagsetGroupConfig(_Base):
    slug: Slug
    name: str
    description: str = ""
    build_mode: Literal["llm", "embedding"] = "llm"
    min_docs_for_build: int = Field(200, ge=1)
    require_manual_review: bool = False


# ── 站点 ──────────────────────────────────────────────────────────


class RssDiscovery(_Base):
    feed_url: str
    content_from_feed: Literal["auto", "always", "never"] = "auto"


class SitemapDiscovery(_Base):
    sitemap_url: str
    url_allow_pattern: str | None = None
    url_pattern: str | None = None
    lastmod_within_days: int | None = Field(None, ge=1)


class PaginationConfig(_Base):
    mode: Literal["none", "next_link", "url_template", "query_param"] = "none"
    template: str | None = None
    param: str | None = None
    next_selector: str | None = None
    order: Literal["asc", "desc_index"] = "asc"
    max_page: int | None = Field(None, ge=1)
    max_pages: int = Field(1, ge=1, le=500)

    @model_validator(mode="after")
    def _mode_needs_its_field(self) -> PaginationConfig:
        required = {
            "url_template": ("template", self.template),
            "query_param": ("param", self.param),
            "next_link": ("next_selector", self.next_selector),
        }
        if self.mode in required:
            field, value = required[self.mode]
            if not value:
                raise ValueError(f"pagination.mode={self.mode} 需要 pagination.{field}")
        if self.mode == "url_template" and self.template and "{page}" not in self.template:
            raise ValueError("pagination.template 必须含 {page} 占位符")
        if self.order == "desc_index" and self.mode != "url_template":
            raise ValueError("order=desc_index 仅适用于 mode=url_template")
        if self.order == "desc_index" and self.max_page is None:
            raise ValueError("order=desc_index 需要 pagination.max_page")
        return self


class HtmlListDiscovery(_Base):
    list_urls: list[str] = Field(min_length=1)
    item_selector: str
    url_allow_pattern: str | None = None
    fields: dict[str, str] = Field(default_factory=dict)
    date_format: str | None = None
    pagination: PaginationConfig = Field(default_factory=PaginationConfig)


class JsonApiDiscovery(_Base):
    endpoint: str
    items_path: str = "data"
    field_map: dict[str, str] = Field(min_length=1)
    pagination: PaginationConfig = Field(default_factory=PaginationConfig)
    max_pages: int | None = Field(None, ge=1, le=500)

    @model_validator(mode="after")
    def _endpoint_pagination(self) -> JsonApiDiscovery:
        if self.max_pages is not None:
            self.pagination.max_pages = self.max_pages
        if "{page}" in self.endpoint and self.pagination.mode == "none":
            self.pagination.mode = "url_template"
            self.pagination.template = self.endpoint
        return self


class ExtractConfig(_Base):
    min_content_length: int = Field(200, ge=0)
    selectors: dict[str, str] = Field(default_factory=dict)
    strip_selectors: list[str] = Field(default_factory=list)
    reject_title_patterns: list[str] = Field(default_factory=list)
    reject_content_patterns: list[str] = Field(default_factory=list)


class CanonicalRule(_Base):
    match: str
    extract_params: list[str] = Field(default_factory=list)
    rewrite: str


class UrlCanonicalConfig(_Base):
    rules: list[CanonicalRule] = Field(default_factory=list)
    strip_params: list[str] = Field(default_factory=list)


class SiteAttachmentConfig(_Base):
    enabled: bool = True
    link_patterns: list[str] = Field(default_factory=list)
    anchor_text_patterns: list[str] = Field(default_factory=list)
    inline_image_patterns: list[str] = Field(default_factory=list)
    send_referer: bool = False


class RenderConfig(_Base):
    wait_until: Literal["commit", "domcontentloaded", "load", "networkidle"] = "domcontentloaded"
    wait_selector: str | None = None
    timeout_ms: int = Field(15000, ge=1000, le=120000)


class SitePolitenessConfig(_Base):
    """站点级限速覆盖。未设的项回落到全局 politeness。"""

    max_concurrency: int | None = Field(None, ge=1, le=32)
    delay_sec: float | None = Field(None, ge=0)
    conditional_requests: bool = True


class SiteConfig(_Base):
    slug: Slug
    name: str
    base_url: str
    tagset_group: str
    enabled: bool = True
    crawl_interval_sec: int = Field(1800, ge=60)
    render_js: bool = False
    render: RenderConfig | None = None
    discovery_mode: Literal["rss", "sitemap", "html_list", "json_api"]
    config: dict[str, Any]
    extract: ExtractConfig = Field(default_factory=ExtractConfig)
    url_canonical: UrlCanonicalConfig = Field(default_factory=UrlCanonicalConfig)
    attachments: SiteAttachmentConfig = Field(default_factory=SiteAttachmentConfig)
    politeness: SitePolitenessConfig = Field(default_factory=SitePolitenessConfig)

    @field_validator("base_url")
    @classmethod
    def _valid_base_url(cls, value: str) -> str:
        try:
            parsed = urlsplit(value)
            _ = parsed.port
        except ValueError as exc:
            raise ValueError("site base_url 非法") from exc
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
            or any(ord(char) <= 32 or ord(char) == 127 for char in value)
        ):
            raise ValueError("site base_url 必须是无凭据、路径、查询参数或片段的 http(s) 源站")
        return value.rstrip("/")

    @model_validator(mode="after")
    def _parse_discovery(self) -> SiteConfig:
        """按 discovery_mode 用对应子模型校验 config，并把结果缓存下来。

        dict[str, Any] 挡不住任何错误，这一步才是真正的校验。
        """
        models: dict[str, type[_Base]] = {
            "rss": RssDiscovery,
            "sitemap": SitemapDiscovery,
            "html_list": HtmlListDiscovery,
            "json_api": JsonApiDiscovery,
        }
        object.__setattr__(self, "_discovery", models[self.discovery_mode](**self.config))
        return self

    @property
    def discovery(self) -> _Base:
        """已校验的发现配置。"""
        return self._discovery  # type: ignore[attr-defined]


# ── 抓取通用 ──────────────────────────────────────────────────────


class RetryConfig(_Base):
    max_attempts: int = Field(3, ge=1, le=10)
    backoff_base_sec: float = Field(5, ge=0)
    retry_on: list[int | str] = Field(
        default_factory=lambda: [429, 500, 502, 503, 504, "timeout", "connreset"]
    )


class PolitenessConfig(_Base):
    respect_robots: bool = True
    user_agent: str = "Nestra/1.0"
    delay_sec: float = Field(2, ge=0)
    max_concurrency: int = Field(4, ge=1, le=64)
    timeout_sec: float = Field(20, ge=1)
    retry: RetryConfig = Field(default_factory=RetryConfig)


class AttachmentsConfig(_Base):
    enabled: bool = True
    allow_mime: list[str] = Field(
        default_factory=lambda: [
            "image/*",
            "application/pdf",
            "application/zip",
            "application/vnd.openxmlformats-officedocument.*",
            "application/x-ole-storage",
        ]
    )
    max_size_mb: int = Field(20, ge=1, le=100)
    max_per_article: int = Field(10, ge=1)
    total_quota_gb: float = Field(5, gt=0)


# ── 打标 ──────────────────────────────────────────────────────────


class ProviderConfig(_Base):
    name: Slug
    type: Literal["openai_compatible", "gemini", "anthropic"]
    base_url: str | None = None
    api_key_env: str = ""
    api_key_value: str | None = Field(default=None, exclude=True, repr=False)
    models: list[str] = Field(min_length=1)
    max_input_chars: int = Field(8000, ge=500)

    @model_validator(mode="after")
    def _openai_needs_base_url(self) -> ProviderConfig:
        if self.type == "openai_compatible" and not self.base_url:
            raise ValueError("type=openai_compatible 需要 base_url")
        if self.base_url:
            try:
                parsed = urlsplit(self.base_url)
                _ = parsed.port
            except ValueError as exc:
                raise ValueError("provider base_url 非法") from exc
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
                or any(ord(char) <= 32 or ord(char) == 127 for char in self.base_url)
            ):
                raise ValueError("provider base_url 必须是无凭据、查询参数或片段的 https URL")
            if self.type == "openai_compatible" and not parsed.path:
                self.base_url += "/v1"
        return self

    @property
    def api_key(self) -> str | None:
        """运行期读取。值不进模型，避免被日志或配置导出带出去。"""
        return self.api_key_value or os.environ.get(self.api_key_env) or None


class CircuitBreakerConfig(_Base):
    failure_threshold: int = Field(5, ge=1)
    cooldown_sec: int = Field(600, ge=1)
    half_open_probe: bool = True


class LlmConfig(_Base):
    request_timeout_sec: float = Field(30, ge=1)
    max_retries_per_model: int = Field(2, ge=0, le=5)
    backoff_base_sec: float = Field(2, ge=0)
    providers: list[ProviderConfig] = Field(default_factory=list)
    circuit_breaker: CircuitBreakerConfig = Field(default_factory=CircuitBreakerConfig)

    @field_validator("providers")
    @classmethod
    def _unique_names(cls, v: list[ProviderConfig]) -> list[ProviderConfig]:
        seen = [p.name for p in v]
        dupes = {n for n in seen if seen.count(n) > 1}
        if dupes:
            raise ValueError(f"provider 名重复: {sorted(dupes)}")
        return v


class LocalTaggerConfig(_Base):
    enabled: bool = False  # 默认关闭：ONNX 是可选依赖，且吃内存
    model_path: Path = Path("data/models/bge-small-zh-v1.5-int8.onnx")
    tokenizer_path: Path = Path("data/models/bge-small-zh-v1.5-tokenizer.json")
    idle_unload_after_sec: int = Field(900, ge=0)
    intra_op_num_threads: int = Field(1, ge=1, le=4)
    top_k: int = Field(5, ge=1, le=20)


class AutoCurateConfig(_Base):
    min_cluster_docs: int = Field(5, ge=1)
    max_tags: int = Field(40, ge=1)


class TagsetBuildConfig(_Base):
    auto_curate: AutoCurateConfig = Field(default_factory=AutoCurateConfig)
    batch_size: int = Field(40, ge=1)


class TaggerConfig(_Base):
    strategy: Literal["llm_chain_with_local_fallback", "llm_only", "local_only"] = (
        "llm_chain_with_local_fallback"
    )
    tagset_dir: Path = Path("data/models/tagsets")
    max_tags_per_article: int = Field(5, ge=1, le=20)
    min_confidence_to_store: float = Field(0.3, ge=0, le=1)
    tagset: TagsetBuildConfig = Field(default_factory=TagsetBuildConfig)
    llm: LlmConfig = Field(default_factory=LlmConfig)
    local: LocalTaggerConfig = Field(default_factory=LocalTaggerConfig)


# ── 推送 / 调度 / 保留 / 告警 / 向导 ──────────────────────────────


class NotifyRetryConfig(_Base):
    max_attempts: int = Field(5, ge=1, le=20)
    backoff_base_sec: float = Field(30, ge=0)


class NotifyConfig(_Base):
    body_format: Literal["markdown", "text", "html"] = "markdown"
    include_full_content: bool = True
    max_body_chars: int = Field(8000, ge=100)
    attachment_mode: Literal["apprise", "link", "both"] = "both"
    attachment_inline_max_mb: int = Field(10, ge=1)
    signed_link_ttl_hours: int = Field(72, ge=1)
    dedupe_window_days: int = Field(7, ge=0)
    retry: NotifyRetryConfig = Field(default_factory=NotifyRetryConfig)
    target_auto_disable_after_failures: int = Field(10, ge=1)


class ScheduleConfig(_Base):
    crawl_default_interval_sec: int = Field(1800, ge=60)
    tag_interval_sec: int = Field(300, ge=10)
    dispatch_interval_sec: int = Field(120, ge=10)
    retry_delivery_interval_sec: int = Field(600, ge=10)
    housekeeping_cron: str = "0 4 * * *"


class RetentionConfig(_Base):
    article_days: int = Field(180, ge=1)
    attachment_days: int = Field(30, ge=1)
    audit_days: int = Field(90, ge=1)
    session_cleanup: bool = True


class AlertsConfig(_Base):
    enabled: bool = True
    on_all_providers_down: bool = True
    on_disk_usage_pct: int = Field(85, ge=1, le=100)
    on_site_consecutive_failures: int = Field(5, ge=1)


class ProbeConfig(_Base):
    max_pages: int = Field(40, ge=1, le=500)
    max_duration_sec: int = Field(120, ge=5)
    max_bytes_per_page: int = Field(3_145_728, ge=1024)
    sample_articles: int = Field(6, ge=1, le=50)
    delay_sec: float = Field(1, ge=0)


class DryrunConfig(_Base):
    sample_size: int = Field(10, ge=1, le=100)


class PickerConfig(_Base):
    load_external_assets: bool = False


class OnboardingConfig(_Base):
    probe: ProbeConfig = Field(default_factory=ProbeConfig)
    dryrun: DryrunConfig = Field(default_factory=DryrunConfig)
    picker: PickerConfig = Field(default_factory=PickerConfig)


# ── 根配置 ────────────────────────────────────────────────────────


class Settings(BaseSettings):
    """根配置。

    环境变量以 `NESTRA__` 前缀覆盖，双下划线表示层级：
    `NESTRA__WEB__PORT=9000`。
    """

    model_config = SettingsConfigDict(
        env_prefix="NESTRA__",
        env_nested_delimiter="__",
        extra="forbid",
        case_sensitive=False,
    )

    app: AppConfig = Field(default_factory=AppConfig)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    web: WebConfig = Field(default_factory=WebConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    tagset_groups: list[TagsetGroupConfig] = Field(default_factory=list)
    sites: list[SiteConfig] = Field(default_factory=list)
    politeness: PolitenessConfig = Field(default_factory=PolitenessConfig)
    attachments: AttachmentsConfig = Field(default_factory=AttachmentsConfig)
    tagger: TaggerConfig = Field(default_factory=TaggerConfig)
    notify: NotifyConfig = Field(default_factory=NotifyConfig)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    retention: RetentionConfig = Field(default_factory=RetentionConfig)
    alerts: AlertsConfig = Field(default_factory=AlertsConfig)
    onboarding: OnboardingConfig = Field(default_factory=OnboardingConfig)

    # 机密只从环境变量读，不出现在 YAML
    secret_key: str = Field("", alias="NESTRA_SECRET_KEY", repr=False, exclude=True)
    admin_password: str = Field("", alias="NESTRA_ADMIN_PASSWORD", repr=False, exclude=True)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """环境变量必须覆盖 YAML。

        `load_settings` 把 YAML 作为 init kwargs 传入；Pydantic 默认让 init 高于 env，
        与文档约定相反。显式交换顺序，保留 dotenv/file secrets 为低优先级来源。
        """
        del settings_cls
        return env_settings, init_settings, dotenv_settings, file_secret_settings

    def tagset_path(self, group_slug: str) -> Path:
        """某组标签集文件路径。布局：`{tagset_dir}/{group}/tags.json`。"""
        return self.tagger.tagset_dir / group_slug / "tags.json"

    def group(self, slug: str) -> TagsetGroupConfig | None:
        return next((g for g in self.tagset_groups if g.slug == slug), None)


# ── 加载与交叉校验 ────────────────────────────────────────────────


def _collect_warnings(s: Settings) -> list[str]:
    """不阻止启动，但需要用户知道的问题。"""
    warns: list[str] = []

    for p in s.tagger.llm.providers:
        if not p.api_key:
            warns.append(
                f"provider {p.name!r} 的环境变量 {p.api_key_env} 未设置，该 provider 不可用"
            )
    usable = [p for p in s.tagger.llm.providers if p.api_key]
    if (
        s.tagger.strategy == "llm_chain_with_local_fallback"
        and not usable
        and not s.tagger.local.enabled
    ):
        warns.append(
            "YAML 中没有可用打标后端；可在 Web 配置 provider，未配置时文章将停在 EXTRACTED"
        )

    if s.tagger.local.enabled and not s.tagger.local.model_path.exists():
        warns.append(
            f"local.enabled=true 但模型文件不存在: {s.tagger.local.model_path}，本地兜底不可用"
        )

    if s.web.cookie_secure and s.web.base_url.startswith("http://"):
        warns.append(
            "cookie_secure=true 但 base_url 是 http://，浏览器不会回传 Cookie，登录会静默失败"
        )

    if s.attachments.max_size_mb > s.notify.attachment_inline_max_mb:
        warns.append(
            f"attachments.max_size_mb({s.attachments.max_size_mb}) > "
            f"notify.attachment_inline_max_mb({s.notify.attachment_inline_max_mb})，"
            "超限附件将走签名链接模式"
        )

    for g in s.tagset_groups:
        if g.build_mode == "embedding" and not s.tagger.local.enabled:
            warns.append(f"分组 {g.slug!r} build_mode=embedding 需要 local.enabled=true 才能生成")

    if s.web.trusted_proxies == [] and not s.web.base_url.startswith("http://localhost"):
        warns.append("trusted_proxies 为空：若前置反代，限流会对所有请求看到同一个 IP")

    return warns


def _collect_errors(s: Settings) -> list[str]:
    """必须拒绝启动的问题。"""
    errs: list[str] = []

    if not s.secret_key:
        errs.append(
            "NESTRA_SECRET_KEY 未设置。生成：openssl rand -base64 32 "
            "（不自动生成：每次重启换密钥会让已存的推送目标全部无法解密）"
        )
    elif len(s.secret_key) < 32:
        errs.append(f"NESTRA_SECRET_KEY 过短（{len(s.secret_key)} 字符），至少 32")

    if s.runtime.web_workers != 1:
        errs.append("runtime.web_workers 必须为 1：调度器、限流与探测任务是单进程状态")
    if s.web.base_url.startswith("https://") and not s.web.cookie_secure:
        errs.append("HTTPS base_url 必须启用 cookie_secure")

    if not s.tagset_groups:
        errs.append("tagset_groups 至少需要一个分组")

    slugs = [g.slug for g in s.tagset_groups]
    dupes = {x for x in slugs if slugs.count(x) > 1}
    if dupes:
        errs.append(f"tagset_groups slug 重复: {sorted(dupes)}")

    site_slugs = [x.slug for x in s.sites]
    site_dupes = {x for x in site_slugs if site_slugs.count(x) > 1}
    if site_dupes:
        errs.append(f"sites slug 重复: {sorted(site_dupes)}")

    known = set(slugs)
    for site in s.sites:
        if site.tagset_group not in known:
            errs.append(
                f"站点 {site.slug!r} 的 tagset_group={site.tagset_group!r} "
                f"不存在于 tagset_groups（可用: {sorted(known)}）"
            )

    # 显式单后端策略必须可用；默认降级策略允许先抓取、稍后补密钥。
    usable = [p for p in s.tagger.llm.providers if p.api_key]
    if s.tagger.strategy == "local_only" and not s.tagger.local.enabled:
        errs.append("strategy=local_only 但 local.enabled=false")
    if s.tagger.strategy == "llm_only" and not usable:
        errs.append("strategy=llm_only 但没有可用的 provider")

    # 正则必须能编译，否则运行期才炸
    for site in s.sites:
        patterns: list[tuple[str, str]] = []
        cfg = site.discovery
        for attr in ("url_allow_pattern", "url_pattern"):
            val = getattr(cfg, attr, None)
            if val:
                patterns.append((attr, val))
        patterns += [
            (f"attachments.link_patterns[{i}]", p)
            for i, p in enumerate(site.attachments.link_patterns)
        ]
        patterns += [
            (f"attachments.inline_image_patterns[{i}]", p)
            for i, p in enumerate(site.attachments.inline_image_patterns)
        ]
        patterns += [
            (f"attachments.anchor_text_patterns[{i}]", p)
            for i, p in enumerate(site.attachments.anchor_text_patterns)
        ]
        patterns += [
            (f"url_canonical.rules[{i}].match", r.match)
            for i, r in enumerate(site.url_canonical.rules)
        ]
        patterns += [
            (f"extract.reject_title_patterns[{i}]", pattern)
            for i, pattern in enumerate(site.extract.reject_title_patterns)
        ]
        patterns += [
            (f"extract.reject_content_patterns[{i}]", pattern)
            for i, pattern in enumerate(site.extract.reject_content_patterns)
        ]
        if pattern := site.extract.selectors.get("published_at_regex"):
            patterns.append(("extract.selectors.published_at_regex", pattern))
        for name, pat in patterns:
            try:
                re.compile(pat)
            except re.error as exc:
                errs.append(f"站点 {site.slug!r} 的 {name} 不是合法正则: {exc}")

    for site in s.sites:
        if site.render_js and not _playwright_available():
            errs.append(
                f"站点 {site.slug!r} render_js=true 但未安装 Playwright。"
                "请用 NESTRA_IMAGE_TARGET=runtime-render（或 runtime-full）重建镜像，"
                "或改为 render_js=false"
            )

    if s.schedule.housekeeping_cron.count(" ") != 4:
        errs.append(f"housekeeping_cron 需为 5 段 cron 表达式: {s.schedule.housekeeping_cron!r}")

    return errs


def _playwright_available() -> bool:
    from importlib.util import find_spec

    return find_spec("playwright") is not None


def load_yaml(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ConfigError(
            "配置文件不存在（可从 config/config.example.yaml 复制）",
            path=str(path),
        ) from exc
    except (OSError, UnicodeError) as exc:
        raise ConfigError(f"配置文件无法读取: {exc}", path=str(path)) from exc

    try:
        raw = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"YAML 解析失败: {exc}", path=str(path)) from exc
    if not isinstance(raw, dict):
        raise ConfigError("配置根节点必须是映射", path=str(path))
    return raw


def load_settings(
    config_path: Path | str = "config/config.yaml",
    *,
    strict: bool = True,
) -> tuple[Settings, list[str]]:
    """加载配置并做交叉校验。

    返回 `(settings, warnings)`。`strict=False` 时跳过错误检查，
    供 `nestra config check` 在缺密钥的环境下也能校验结构。
    """
    from pydantic import ValidationError

    raw = load_yaml(Path(config_path))

    # 字段 alias 让 pydantic-settings 能读取无双下划线的环境变量，但同一 alias
    # 也会被模型输入接受。必须在构造模型前拒绝，防止机密被写入可分享的 YAML。
    forbidden_secrets = {
        "NESTRA_SECRET_KEY",
        "NESTRA_ADMIN_PASSWORD",
        "secret_key",
        "admin_password",
    }
    leaked = sorted(forbidden_secrets.intersection(raw))
    if leaked:
        raise ConfigValidationError(
            [f"机密字段 {key!r} 不允许出现在 YAML，必须通过环境变量提供" for key in leaked]
        )

    try:
        settings = Settings(**raw)
    except ValidationError as exc:
        msgs = [f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in exc.errors()]
        raise ConfigValidationError(msgs) from exc

    if strict and (errors := _collect_errors(settings)):
        raise ConfigValidationError(errors)

    return settings, _collect_warnings(settings)
