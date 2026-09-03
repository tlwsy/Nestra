"""core.config 的加载、环境覆盖与交叉校验测试。

重点在“启动即失败”：配置错误必须在 load_settings 时暴露，
而不是等爬取或打标跑到一半才炸。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from nestra.core.config import Settings, SiteConfig, load_settings
from nestra.core.errors import ConfigError, ConfigValidationError

pytestmark = pytest.mark.unit


def _minimal() -> dict[str, Any]:
    """能通过全部校验的最小配置。各测试在此基础上改坏一处。"""
    return {
        "tagset_groups": [{"slug": "campus", "name": "校园通知"}],
        "sites": [
            {
                "slug": "ujs-jwc",
                "name": "江苏大学教务处",
                "base_url": "https://jwc.ujs.edu.cn/",
                "tagset_group": "campus",
                "discovery_mode": "html_list",
                "config": {
                    "list_urls": ["https://jwc.ujs.edu.cn/index/tzgg.htm"],
                    "item_selector": 'li[id^="line_"]',
                    "fields": {"url": "a.title.tt1@href", "title": "a.title.tt1@title"},
                },
            }
        ],
        "tagger": {
            "llm": {
                "providers": [
                    {
                        "name": "primary",
                        "type": "openai_compatible",
                        "base_url": "https://api.example.test/v1",
                        "api_key_env": "TEST_LLM_API_KEY",
                        "models": ["m-small"],
                    }
                ]
            }
        },
    }


@pytest.fixture
def write_config(tmp_path: Path):
    def _write(data: dict[str, Any]) -> Path:
        path = tmp_path / "config.yaml"
        path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
        return path

    return _write


@pytest.fixture(autouse=True)
def _provider_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_LLM_API_KEY", "sk-test-value")


# ── 加载 ──────────────────────────────────────────────────────


def test_loads_minimal_config(write_config) -> None:
    settings, warnings = load_settings(write_config(_minimal()))
    assert settings.sites[0].slug == "ujs-jwc"
    assert settings.group("campus") is not None
    assert isinstance(warnings, list)


def test_missing_file_raises_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="不存在"):
        load_settings(tmp_path / "nope.yaml")


def test_malformed_yaml_raises_config_error(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text("sites: [unclosed\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="YAML"):
        load_settings(path)


def test_non_mapping_root_rejected(tmp_path: Path) -> None:
    path = tmp_path / "list.yaml"
    path.write_text("- a\n- b\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="映射"):
        load_settings(path)


def test_config_path_that_is_directory_has_clear_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="无法读取"):
        load_settings(tmp_path)


def test_non_utf8_config_has_clear_error(tmp_path: Path) -> None:
    path = tmp_path / "binary.yaml"
    path.write_bytes(b"\xff\xfe\x00")
    with pytest.raises(ConfigError, match="无法读取"):
        load_settings(path)


def test_unknown_key_rejected(write_config) -> None:
    """extra=forbid：拼错的键必须报错，不能被静默忽略。"""
    data = _minimal()
    data["sites"][0]["typo_field"] = 1
    with pytest.raises(ConfigValidationError):
        load_settings(write_config(data))


# ── 环境变量覆盖 ──────────────────────────────────────────────


def test_env_override_nested_value(write_config, monkeypatch: pytest.MonkeyPatch) -> None:
    data = _minimal()
    data["web"] = {"port": 8080}
    data["app"] = {"log_level": "INFO"}
    monkeypatch.setenv("NESTRA__WEB__PORT", "9123")
    monkeypatch.setenv("NESTRA__APP__LOG_LEVEL", "DEBUG")
    settings, _ = load_settings(write_config(data))
    assert settings.web.port == 9123
    assert settings.app.log_level == "DEBUG"


def test_secrets_come_from_env_not_yaml(write_config) -> None:
    settings, _ = load_settings(write_config(_minimal()))
    # conftest 设置了 NESTRA_SECRET_KEY
    assert settings.secret_key
    assert settings.tagger.llm.providers[0].api_key == "sk-test-value"
    assert settings.secret_key not in repr(settings)
    assert "secret_key" not in settings.model_dump()
    assert "admin_password" not in settings.model_dump()


@pytest.mark.parametrize(
    "key",
    ["NESTRA_SECRET_KEY", "NESTRA_ADMIN_PASSWORD", "secret_key", "admin_password"],
)
def test_yaml_cannot_contain_secret_fields(write_config, key: str) -> None:
    data = _minimal()
    data[key] = "must-not-be-in-yaml"
    with pytest.raises(ConfigValidationError, match="不允许出现在 YAML"):
        load_settings(write_config(data), strict=False)


def test_provider_key_absent_when_env_unset(write_config, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEST_LLM_API_KEY", raising=False)
    data = _minimal()
    data["tagger"]["local"] = {"enabled": True}
    settings, _ = load_settings(write_config(data))
    assert settings.tagger.llm.providers[0].api_key is None


# ── 交叉校验 ──────────────────────────────────────────────────


def test_site_referencing_unknown_group_rejected(write_config) -> None:
    data = _minimal()
    data["sites"][0]["tagset_group"] = "ghost"
    with pytest.raises(ConfigValidationError, match="tagset_group"):
        load_settings(write_config(data))


def test_default_strategy_can_start_without_a_tagger(
    write_config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TEST_LLM_API_KEY", raising=False)
    _settings, warnings = load_settings(write_config(_minimal()))
    assert any("停在 EXTRACTED" in warning for warning in warnings)


def test_local_only_requires_local_enabled(write_config) -> None:
    data = _minimal()
    data["tagger"]["strategy"] = "local_only"
    with pytest.raises(ConfigValidationError, match="local_only"):
        load_settings(write_config(data))


def test_invalid_regex_rejected(write_config) -> None:
    data = _minimal()
    data["sites"][0]["attachments"] = {"link_patterns": ["download(.jsp"]}
    with pytest.raises(ConfigValidationError, match="正则"):
        load_settings(write_config(data))


def test_invalid_extraction_regex_rejected(write_config) -> None:
    data = _minimal()
    data["sites"][0]["extract"] = {"selectors": {"published_at_regex": "发布时间：(unclosed"}}
    with pytest.raises(ConfigValidationError, match="published_at_regex"):
        load_settings(write_config(data))


def test_render_js_without_playwright_rejected(write_config) -> None:
    data = _minimal()
    data["sites"][0]["render_js"] = True
    with pytest.raises(ConfigValidationError, match="Playwright"):
        load_settings(write_config(data))


def test_attachment_memory_limit_is_bounded(write_config) -> None:
    data = _minimal()
    data["attachments"] = {"max_size_mb": 101}
    with pytest.raises(ConfigValidationError, match="less than or equal to 100"):
        load_settings(write_config(data))


def test_bad_cron_rejected(write_config) -> None:
    data = _minimal()
    data["schedule"] = {"housekeeping_cron": "0 3 *"}
    with pytest.raises(ConfigValidationError, match="cron"):
        load_settings(write_config(data))


def test_non_strict_skips_error_checks(write_config, monkeypatch: pytest.MonkeyPatch) -> None:
    """`config check` 要能在缺密钥的机器上校验结构。"""
    monkeypatch.delenv("TEST_LLM_API_KEY", raising=False)
    settings, _ = load_settings(write_config(_minimal()), strict=False)
    assert settings.sites[0].slug == "ujs-jwc"


# ── 字段级校验 ────────────────────────────────────────────────


def test_pagination_mode_requires_its_field() -> None:
    """config 是 dict[str, Any]，真正的校验发生在按 mode 选子模型时。"""
    with pytest.raises(Exception, match="next_selector"):
        Settings(
            tagset_groups=[{"slug": "g", "name": "G"}],
            sites=[
                {
                    "slug": "s",
                    "name": "S",
                    "base_url": "https://e.test/",
                    "tagset_group": "g",
                    "discovery_mode": "html_list",
                    "config": {
                        "list_urls": ["https://e.test/l"],
                        "item_selector": "li",
                        "pagination": {"mode": "next_link"},
                    },
                }
            ],
        )


def test_desc_index_order_requires_url_template() -> None:
    """目标站反向分页依赖 url_template，组合错了必须报错。"""
    with pytest.raises(Exception, match="desc_index"):
        Settings(
            sites=[
                {
                    "slug": "s",
                    "name": "S",
                    "base_url": "https://e.test/",
                    "tagset_group": "g",
                    "discovery_mode": "html_list",
                    "config": {
                        "list_urls": ["https://e.test/l"],
                        "item_selector": "li",
                        "pagination": {
                            "mode": "next_link",
                            "next_selector": "a.next",
                            "order": "desc_index",
                        },
                    },
                }
            ],
        )


def test_multiple_web_workers_rejected_for_single_process_state(write_config) -> None:
    data = _minimal()
    data["runtime"] = {"web_workers": 2}
    with pytest.raises(ConfigValidationError, match="web_workers 必须为 1"):
        load_settings(write_config(data))


def test_duplicate_provider_names_rejected() -> None:
    with pytest.raises(Exception, match="名重复"):
        Settings(
            tagger={
                "llm": {
                    "providers": [
                        {
                            "name": "dup",
                            "type": "openai_compatible",
                            "base_url": "https://a.test/v1",
                            "api_key_env": "A",
                            "models": ["m"],
                        },
                        {
                            "name": "dup",
                            "type": "openai_compatible",
                            "base_url": "https://b.test/v1",
                            "api_key_env": "B",
                            "models": ["m"],
                        },
                    ]
                }
            }
        )


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.test",
        "https://user:pass@example.test",
        "https://example.test:bad",
        "https://example.test/news",
    ],
)
def test_site_base_url_is_plain_http(url: str) -> None:
    with pytest.raises(Exception, match="base_url"):
        SiteConfig(
            slug="site",
            name="Site",
            base_url=url,
            tagset_group="group",
            discovery_mode="rss",
            config={"feed_url": "https://example.test/feed"},
        )


@pytest.mark.parametrize(
    "url",
    [
        "http://api.example/v1",
        "https://user:pass@api.example/v1",
        "https://api.example/v1?q=1",
        "https://api.example:bad/v1",
    ],
)
def test_provider_base_url_is_plain_https(url: str) -> None:
    with pytest.raises(Exception, match="base_url"):
        Settings(
            tagger={
                "llm": {
                    "providers": [
                        {
                            "name": "provider",
                            "type": "openai_compatible",
                            "base_url": url,
                            "api_key_env": "TEST_LLM_API_KEY",
                            "models": ["model"],
                        }
                    ]
                }
            }
        )


def test_https_base_url_requires_secure_cookie(write_config) -> None:
    data = _minimal()
    data["web"] = {"base_url": "https://nestra.example", "cookie_secure": False}
    with pytest.raises(ConfigValidationError, match="cookie_secure"):
        load_settings(write_config(data))


def test_web_base_url_trailing_slash_normalized() -> None:
    assert Settings(web={"base_url": "https://n.example/"}).web.base_url == "https://n.example"


@pytest.mark.parametrize(
    "url",
    [
        "n.example",
        "https://user:pass@n.example",
        "https://n.example/app",
        "https://n.example?q=1",
        "https://n.example:bad",
    ],
)
def test_web_base_url_requires_plain_origin(url: str) -> None:
    with pytest.raises(Exception, match="http"):
        Settings(web={"base_url": url})


def test_invalid_trusted_proxy_rejected() -> None:
    with pytest.raises(Exception, match="trusted_proxies"):
        Settings(web={"trusted_proxies": ["not-a-network"]})


def test_invalid_timezone_rejected() -> None:
    with pytest.raises(Exception, match="时区"):
        Settings(app={"timezone": "Mars/Olympus"})


def test_repository_example_config_is_valid_and_uses_probed_site(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """模板本身是发布契约，不能只测试人工构造的最小配置。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    path = Path(__file__).parents[2] / "config/config.example.yaml"
    settings, _ = load_settings(path)
    assert [site.slug for site in settings.sites] == ["ujs-jwc"]
    assert settings.sites[0].discovery_mode == "html_list"
    assert settings.sites[0].render_js is False
    assert settings.tagger.local.enabled is False
