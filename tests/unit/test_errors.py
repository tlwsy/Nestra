"""领域异常的横切分类与诊断信息测试。"""

from __future__ import annotations

import pytest

from nestra.core.errors import (
    ConfigError,
    ConfigValidationError,
    CrawlError,
    DecryptionFailed,
    Fatal,
    FetchTimeout,
    MigrationError,
    NestraError,
    ProviderUnavailable,
    RateLimited,
    Retryable,
    SecurityError,
    SsrfBlocked,
    StorageError,
    TaggerError,
    TagsetNotReady,
)

pytestmark = pytest.mark.unit


def test_retryable_domain_error_has_both_axes() -> None:
    error = FetchTimeout("timeout", retry_after_sec=12.5)
    assert isinstance(error, NestraError)
    assert isinstance(error, CrawlError)
    assert isinstance(error, Retryable)
    assert not isinstance(error, Fatal)
    assert error.retry_after_sec == 12.5


def test_provider_error_is_retryable_tagger_error() -> None:
    error = ProviderUnavailable("down", retry_after_sec=30)
    assert isinstance(error, TaggerError)
    assert isinstance(error, Retryable)
    assert error.retry_after_sec == 30


def test_fatal_domain_error_has_both_axes() -> None:
    error = TagsetNotReady("checksum mismatch")
    assert isinstance(error, TaggerError)
    assert isinstance(error, Fatal)
    assert not isinstance(error, Retryable)


def test_migration_error_is_storage_and_fatal() -> None:
    error = MigrationError("bad sql")
    assert isinstance(error, StorageError)
    assert isinstance(error, Fatal)


def test_rate_limit_carries_retry_after() -> None:
    error = RateLimited("429", retry_after_sec=60)
    assert error.retry_after_sec == 60


def test_config_error_includes_path() -> None:
    error = ConfigError("YAML 解析失败", path="/etc/nestra.yaml")
    assert str(error) == "/etc/nestra.yaml: YAML 解析失败"
    assert error.path == "/etc/nestra.yaml"


def test_config_validation_aggregates_all_errors() -> None:
    error = ConfigValidationError(["缺密钥", "未知标签组"])
    assert error.errors == ["缺密钥", "未知标签组"]
    assert "2 项" in str(error)
    assert "缺密钥" in str(error)
    assert "未知标签组" in str(error)


def test_ssrf_error_keeps_machine_readable_fields() -> None:
    error = SsrfBlocked("http://169.254.169.254/", "元数据地址")
    assert isinstance(error, SecurityError)
    assert isinstance(error, Fatal)
    assert error.url == "http://169.254.169.254/"
    assert error.reason == "元数据地址"


def test_security_errors_share_root() -> None:
    assert isinstance(DecryptionFailed("wrong key"), SecurityError)
