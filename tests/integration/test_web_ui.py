"""Browser form coverage for the user-owned M5 UI."""

# ruff: noqa: E501

from __future__ import annotations

import json
import re
import time
from pathlib import Path

import httpx
import pytest
import yaml

from nestra.core.config import ProviderConfig
from nestra.core.time import now_iso
from nestra.web.api.admin import _site_data
from nestra.web.app import create_app
from nestra.web.security import totp_code

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
PASSWORD = "correct horse battery staple"


@pytest.fixture
async def web_ui(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("NESTRA_ADMIN_PASSWORD", PASSWORD)
    config = tmp_path / "config.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "app": {"log_format": "console"},
                "web": {"base_url": "http://localhost:8080", "cookie_secure": False},
                "storage": {
                    "db_path": str(tmp_path / "nestra.db"),
                    "attachment_dir": str(tmp_path / "attachments"),
                },
                "tagset_groups": [{"slug": "test", "name": "Test"}],
                "tagger": {"local": {"enabled": True}},
            }
        ),
        encoding="utf-8",
    )
    app = create_app(config, strict_config=False)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client,
    ):
        await client.get("/login")
        csrf = client.cookies["nestra_csrf"]
        response = await client.post(
            "/login?format=json",
            json={"username": "admin", "password": PASSWORD},
            headers={"x-csrf-token": csrf},
        )
        assert response.status_code == 200
        yield app, client, client.cookies["nestra_csrf"]


async def test_settings_browser_flow_enables_and_disables_totp(web_ui) -> None:
    app, client, csrf = web_ui
    settings = await client.get("/settings")
    assert "Set up 2FA" in settings.text

    setup = await client.post("/settings/totp/start", data={"_csrf": csrf})
    assert setup.status_code == 200
    secret = re.search(r"<strong>Secret:</strong> <code>([^<]+)", setup.text)
    token = re.search(r'name="setup_token" value="([^"]+)', setup.text)
    assert secret and token

    enabled = await client.post(
        "/settings/totp/enable",
        data={
            "_csrf": csrf,
            "setup_token": token.group(1),
            "code": totp_code(secret.group(1)),
        },
    )
    assert enabled.status_code == 200
    assert "Save these one-time recovery codes now" in enabled.text
    assert app.state.db.query_one("SELECT totp_secret FROM users WHERE username='admin'")[0]
    assert (await client.post("/settings/totp/start", data={"_csrf": csrf})).status_code == 409

    disabled = await client.post(
        "/settings/totp/disable",
        data={"_csrf": csrf, "password": PASSWORD, "code": totp_code(secret.group(1))},
    )
    assert disabled.status_code == 303
    assert app.state.db.query_one("SELECT totp_secret FROM users WHERE username='admin'")[0] is None


async def test_admin_user_form_shows_one_time_password(web_ui) -> None:
    app, client, csrf = web_ui
    response = await client.post(
        "/admin/users",
        data={"_csrf": csrf, "username": "reader", "role": "user"},
    )
    assert response.status_code == 200
    assert "Copy this password now" in response.text
    reader = app.state.db.query_one(
        "SELECT id,must_change_password FROM users WHERE username='reader'"
    )
    assert reader["must_change_password"] == 1
    app.state.db.execute(
        "UPDATE users SET totp_secret=? WHERE id=?",
        (app.state.crypto.encrypt("JBSWY3DPEHPK3PXP"), reader["id"]),
    )
    assert "Reset 2FA" in (await client.get("/admin/users")).text
    reset = await client.post(
        f"/admin/users/{reader['id']}/totp/reset",
        data={"_csrf": csrf},
    )
    assert reset.status_code == 303
    assert (
        app.state.db.query_one("SELECT totp_secret FROM users WHERE id=?", (reader["id"],))[0]
        is None
    )


async def test_admin_management_pages_render(web_ui) -> None:
    _app, client, _csrf = web_ui
    for path, text in (
        ("/admin/users", "Create user"),
        ("/admin/sites", "Add a site"),
        ("/admin/sites/new", "Start probe"),
        ("/admin/tagset", "Tagsets"),
        ("/admin/providers", "Add provider"),
    ):
        response = await client.get(path)
        assert response.status_code == 200 and text in response.text


async def test_settings_controls_language_and_advanced_menu(web_ui) -> None:
    _app, client, csrf = web_ui
    assert 'href="/admin/system"' not in (await client.get("/admin/users")).text
    language = await client.post("/settings/language", data={"_csrf": csrf, "locale": "zh"})
    assert language.status_code == 303
    assert "界面语言" in (await client.get("/settings")).text
    advanced = await client.post("/settings/advanced", data={"_csrf": csrf, "enabled": "1"})
    assert advanced.status_code == 303
    assert 'href="/admin/system"' in (await client.get("/admin/users")).text
    assert (await client.get("/admin/system")).status_code == 200
    for path, text in (
        ("/", "仪表盘"),
        ("/subscriptions", "订阅"),
        ("/targets", "通知目标"),
        ("/articles", "文章"),
        ("/admin/users", "用户"),
        ("/admin/sites", "站点"),
        ("/admin/tagset", "标签集"),
        ("/admin/providers", "模型提供商"),
    ):
        response = await client.get(path)
        assert response.status_code == 200 and text in response.text


async def test_web_provider_overrides_same_named_configuration(web_ui) -> None:
    app, client, csrf = web_ui
    app.state.settings.tagger.llm.providers.append(
        ProviderConfig(
            name="deepseek",
            type="openai_compatible",
            base_url="https://api.deepseek.com/v1",
            api_key_env="NESTRA_TEST_DEEPSEEK_API_KEY",
            models=["deepseek-chat"],
        )
    )

    configured = await client.get("/admin/providers?format=json")
    assert configured.status_code == 200
    assert configured.json()[0]["source"] == "configuration"

    created = await client.post(
        "/admin/providers?format=json",
        json={
            "name": "deepseek",
            "type": "openai_compatible",
            "base_url": "https://api.deepseek.com/v1",
            "models": "deepseek-chat",
            "api_key": "web-secret",
        },
        headers={"x-csrf-token": csrf},
    )
    assert created.status_code == 200

    listed = (await client.get("/admin/providers?format=json")).json()
    assert len(listed) == 1
    assert listed[0]["name"] == "deepseek" and listed[0]["source"] == "web"

    from nestra.scheduler.jobs import build_dependencies

    dependencies = build_dependencies(app.state.settings, app.state.db)
    try:
        providers = dependencies.tagger._providers()
        assert len(providers) == 1
        assert providers[0].name == "deepseek" and providers[0].api_key == "web-secret"
    finally:
        await dependencies.aclose()


async def test_admin_can_choose_and_toggle_summary_ai(web_ui) -> None:
    app, client, csrf = web_ui
    created = await client.post(
        "/admin/providers?format=json",
        json={
            "name": "summary-ai",
            "type": "openai_compatible",
            "base_url": "https://api.example.test/v1",
            "models": "summary-model",
            "api_key": "summary-secret",
        },
        headers={"x-csrf-token": csrf},
    )
    assert created.status_code == 200

    enabled = await client.post(
        "/admin/providers/summarization?format=json",
        json={"enabled": True, "backend": "summary-ai|summary-model"},
        headers={"x-csrf-token": csrf},
    )
    assert enabled.status_code == 200
    row = app.state.db.query_one(
        "SELECT enabled,provider,model FROM ai_summary_settings WHERE id=1"
    )
    assert tuple(row) == (1, "summary-ai", "summary-model")
    page = await client.get("/admin/providers")
    assert "Summarize new articles" in page.text
    assert 'value="summary-ai|summary-model" selected' in page.text

    disabled = await client.post(
        "/admin/providers/summarization?format=json",
        json={"enabled": False, "backend": "summary-ai|summary-model"},
        headers={"x-csrf-token": csrf},
    )
    assert disabled.status_code == 200
    assert app.state.db.query_one("SELECT enabled FROM ai_summary_settings WHERE id=1")[0] == 0


async def test_admin_can_manage_encrypted_web_provider(web_ui) -> None:
    app, client, csrf = web_ui
    key = "web-provider-secret"
    response = await client.post(
        "/admin/providers?format=json",
        json={
            "name": "web-provider",
            "type": "openai_compatible",
            "base_url": "https://api.example.test/v1",
            "models": "model-a, model-b",
            "api_key": key,
            "max_input_chars": 8000,
        },
        headers={"x-csrf-token": csrf},
    )
    assert response.status_code == 200 and key not in response.text
    provider_id = response.json()["id"]
    row = app.state.db.query_one("SELECT api_key_enc FROM llm_providers WHERE id=?", (provider_id,))
    assert row and key.encode() not in bytes(row["api_key_enc"])
    assert key not in (await client.get(f"/admin/providers/{provider_id}/edit")).text

    for provider_type, model in (("gemini", "gemini-2.5-flash"), ("anthropic", "claude-sonnet-4-5")):
        updated = await client.post(
            f"/admin/providers/{provider_id}?format=json",
            json={
                "name": "web-provider",
                "type": provider_type,
                "base_url": "",
                "models": model,
                "api_key": "",
                "max_input_chars": 12000,
            },
            headers={"x-csrf-token": csrf},
        )
        assert updated.status_code == 200
        row = app.state.db.query_one(
            "SELECT type,base_url,models_json,api_key_enc FROM llm_providers WHERE id=?",
            (provider_id,),
        )
        assert row["type"] == provider_type and row["base_url"] is None
        assert json.loads(row["models_json"]) == [model]
        assert app.state.crypto.decrypt(bytes(row["api_key_enc"])) == key

    deleted = await client.delete(
        f"/admin/providers/{provider_id}?format=json", headers={"x-csrf-token": csrf}
    )
    assert deleted.status_code == 200 and deleted.json() == {"ok": True}
    assert app.state.db.query_one("SELECT id FROM llm_providers WHERE id=?", (provider_id,)) is None


async def test_selector_editor_overrides_candidate() -> None:
    site = _site_data(
        {
            "candidate": {
                "slug": "example",
                "name": "Example",
                "base_url": "https://example.test",
                "tagset_group": "research",
                "enabled": False,
                "render_js": False,
                "discovery_mode": "html_list",
                "config": {
                    "list_urls": ["https://example.test/list"],
                    "item_selector": "li.old",
                    "fields": {"url": "a@href"},
                },
            },
            "item_selector": "article.notice",
            "link_selector": "h2 a@href",
            "title_selector": "h2 a",
            "published_at_selector": "time",
            "content_selector": "main.article",
            "crawl_interval_minutes": "45",
        }
    )
    assert site.crawl_interval_sec == 2700
    assert site.discovery.item_selector == "article.notice"
    assert site.discovery.fields == {
        "url": "h2 a@href",
        "title": "h2 a",
        "published_at": "time",
    }
    assert site.extract.selectors["content"] == "main.article"


async def test_admin_can_create_tagset_group(web_ui) -> None:
    app, client, csrf = web_ui
    response = await client.post(
        "/admin/tagset/groups?format=json",
        json={"slug": "research", "name": "Research", "build_mode": "llm"},
        headers={"x-csrf-token": csrf},
    )
    assert response.status_code == 200
    assert (
        app.state.db.query_one("SELECT status FROM tagset_groups WHERE slug='research'")[0]
        == "draft"
    )


async def test_picker_applies_selected_probe_candidate(web_ui) -> None:
    app, client, _csrf = web_ui
    user_id = app.state.db.query_one("SELECT id FROM users WHERE username='admin'")[0]
    candidate = {
        "slug": "example",
        "name": "Example",
        "base_url": "https://example.test/",
        "enabled": False,
        "render_js": False,
        "discovery_mode": "html_list",
        "config": {"list_urls": ["https://example.test/list"]},
    }
    selectors = [
        {
            "item_selector": "li.notice",
            "link_selector": "a.title@href",
            "title_selector": "a.title",
            "published_at_selector": "time",
            "confidence": 0.9,
            "matches": 12,
            "samples": ["One", "Two"],
        }
    ]
    app.state.probe_tasks["probe"] = {
        "status": "done",
        "created_at": time.time(),
        "user_id": user_id,
        "result": {
            "findings": [
                {"key": "item_selector", "candidates": selectors},
                {"key": "config_candidate", "value": candidate},
            ]
        },
    }
    response = await client.get("/admin/sites/picker?task_id=probe&selector=0")
    assert response.status_code == 200
    assert "li.notice" in response.text and "a.title@href" in response.text
    assert 'action="/admin/sites/dryrun"' in response.text


async def test_picker_escapes_preview_content(web_ui) -> None:
    app, client, _csrf = web_ui
    user_id = app.state.db.query_one("SELECT id FROM users WHERE username='admin'")[0]
    app.state.probe_tasks["preview"] = {
        "status": "done",
        "kind": "dryrun",
        "created_at": time.time(),
        "user_id": user_id,
        "result": {
            "discovered": 1,
            "succeeded": 1,
            "failed": 0,
            "duration_ms": 1,
            "items": [{"title": "<script>alert(1)</script>", "summary": "safe"}],
        },
    }
    response = await client.get("/admin/sites/picker?task_id=preview")
    assert response.status_code == 200
    assert "<script>alert(1)</script>" not in response.text
    assert "sandbox" in response.text


async def test_picker_shows_actionable_failure(web_ui) -> None:
    app, client, _csrf = web_ui
    user_id = app.state.db.query_one("SELECT id FROM users WHERE username='admin'")[0]
    app.state.probe_tasks["failed"] = {
        "status": "failed",
        "error": "ProbeError: HTTP 403 for https://example.test/",
        "created_at": time.time(),
        "user_id": user_id,
    }

    response = await client.get("/admin/sites/picker?task_id=failed")

    assert "ProbeError: HTTP 403" in response.text
    assert "publicly reachable" in response.text
    assert "Refresh" not in response.headers


async def test_site_page_shows_running_crawl_and_deduplicates_submit(web_ui) -> None:
    app, client, csrf = web_ui
    timestamp = now_iso()
    group_id = app.state.db.query_one("SELECT id FROM tagset_groups WHERE slug='test'")[0]
    site_id = app.state.db.execute(
        "INSERT INTO sites (slug,name,base_url,discovery_mode,tagset_group_id,config_json,"
        "enabled,created_at,updated_at) VALUES ('running','Running','https://example.test',"
        "'rss',?,'{}',1,?,?)",
        (group_id, timestamp, timestamp),
    ).lastrowid
    app.state.crawl_tasks[site_id] = {"status": "running", "created_at": timestamp}

    page = await client.get("/admin/sites")
    duplicate = await client.post(
        f"/admin/sites/{site_id}/crawl", data={"_csrf": csrf}, follow_redirects=False
    )

    assert page.headers["Refresh"] == "2; url=/admin/sites"
    assert "Crawling" in page.text and "disabled" in page.text
    assert duplicate.status_code == 303
    assert len(app.state.crawl_tasks) == 1


async def test_site_page_shows_and_updates_crawl_frequency(web_ui) -> None:
    app, client, csrf = web_ui
    timestamp = now_iso()
    group_id = app.state.db.query_one("SELECT id FROM tagset_groups WHERE slug='test'")[0]
    site_id = app.state.db.execute(
        "INSERT INTO sites (slug,name,base_url,discovery_mode,tagset_group_id,config_json,"
        "crawl_interval_sec,created_at,updated_at) VALUES ('timed','Timed','https://example.test',"
        "'rss',?,'{}',1800,?,?)",
        (group_id, timestamp, timestamp),
    ).lastrowid

    page = await client.get("/admin/sites")
    assert "Crawl frequency: Every 30 minutes" in page.text
    assert f'action="/admin/sites/{site_id}/crawl-interval"' in page.text

    response = await client.post(
        f"/admin/sites/{site_id}/crawl-interval",
        data={"_csrf": csrf, "crawl_interval_minutes": "45"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert app.state.db.query_one(
        "SELECT crawl_interval_sec FROM sites WHERE id=?", (site_id,)
    )[0] == 2700


async def test_user_forms_manage_resources_without_rendering_target_secrets(web_ui) -> None:
    app, client, csrf = web_ui
    timestamp = now_iso()
    group_id = app.state.db.query_one("SELECT id FROM tagset_groups WHERE slug='test'")[0]
    with app.state.db.transaction() as conn:
        site_ids = [
            conn.execute(
                "INSERT INTO sites (slug,name,base_url,discovery_mode,tagset_group_id,config_json,created_at,updated_at) VALUES (?,?,?,?,?,'{}',?,?)",
                (slug, name, f"https://{slug}.test", "rss", group_id, timestamp, timestamp),
            ).lastrowid
            for slug, name in (("one", "Site One"), ("two", "Site Two"))
        ]
        conn.execute(
            "UPDATE tagset_groups SET status='frozen',tagset_version='v1',frozen_at=? WHERE id=?",
            (timestamp, group_id),
        )
        tag_ids = [
            conn.execute(
                "INSERT INTO tags (group_id,slug,name,tagset_version,frozen_at) VALUES (?,?,?,?,?)",
                (group_id, slug, name, "v1", timestamp),
            ).lastrowid
            for slug, name in (("alpha", "Alpha"), ("beta", "Beta"))
        ]

    subscriptions_page = await client.get("/subscriptions")
    assert 'action="/subscriptions"' in subscriptions_page.text
    assert all(
        name in subscriptions_page.text for name in ("Tags", "Sites", "Notification targets")
    )

    secret = "tgram://super-secret-token/channel"
    response = await client.post(
        "/targets",
        data={"_csrf": csrf, "name": "Phone", "apprise_url": secret, "enabled": "1"},
    )
    assert response.status_code == 303
    target = app.state.db.query_one("SELECT * FROM notify_targets")
    target_id = target["id"]
    assert secret not in (await client.get("/targets")).text

    response = await client.post(
        "/subscriptions",
        data={
            "_csrf": csrf,
            "name": "Daily reading",
            "match_mode": "all",
            "min_confidence": "0.65",
            "quiet_hours": "23:00-07:00",
            "site_ids": [str(value) for value in site_ids],
            "tag_ids": [str(value) for value in tag_ids],
            "target_ids": [str(target_id)],
            "include_attachments": "1",
            "enabled": "1",
        },
    )
    assert response.status_code == 303
    subscription = app.state.db.query_one("SELECT * FROM subscriptions")
    subscription_id = subscription["id"]
    assert subscription["site_filter"] == f"[{site_ids[0]}, {site_ids[1]}]"
    assert (
        app.state.db.query_one(
            "SELECT COUNT(*) FROM subscription_tags WHERE subscription_id=?", (subscription_id,)
        )[0]
        == 2
    )

    encrypted = bytes(target["apprise_url_enc"])
    response = await client.post(
        f"/targets/{target_id}",
        data={"_csrf": csrf, "name": "Renamed phone", "apprise_url": "", "enabled": "1"},
    )
    assert response.status_code == 303
    changed = app.state.db.query_one(
        "SELECT name,apprise_url_enc FROM notify_targets WHERE id=?", (target_id,)
    )
    assert changed["name"] == "Renamed phone" and bytes(changed["apprise_url_enc"]) == encrypted

    response = await client.post(
        f"/subscriptions/{subscription_id}",
        data={
            "_csrf": csrf,
            "name": "Updated reading",
            "match_mode": "any",
            "min_confidence": "0.5",
            "quiet_hours": "",
            "site_ids": str(site_ids[0]),
            "tag_ids": str(tag_ids[0]),
            "target_ids": str(target_id),
            "include_attachments": "0",
            "enabled": "0",
        },
    )
    assert response.status_code == 303
    updated = app.state.db.query_one("SELECT * FROM subscriptions WHERE id=?", (subscription_id,))
    assert (
        updated["name"] == "Updated reading"
        and not updated["include_attachments"]
        and not updated["enabled"]
    )

    article_id = app.state.db.execute(
        "INSERT INTO articles (site_id,url,url_hash,title,status,discovered_at,published_at) VALUES (?,?,?,?,?,?,?)",
        (
            site_ids[0],
            "https://one.test/article",
            "a" * 64,
            "Linked article",
            "NOTIFIED",
            timestamp,
            timestamp,
        ),
    ).lastrowid
    app.state.db.execute(
        "INSERT INTO deliveries (subscription_id,article_id,target_id,status,created_at,sent_at) VALUES (?,?,?,'sent',?,?)",
        (subscription_id, article_id, target_id, timestamp, timestamp),
    )
    articles = await client.get("/articles")
    assert articles.status_code == 200 and f'href="/articles/{article_id}"' in articles.text

    assert (await client.post(f"/targets/{target_id}/delete", data={})).status_code == 403
    assert (
        await client.post(f"/targets/{target_id}/delete", data={"_csrf": csrf})
    ).status_code == 303
    deleted = await client.delete(
        f"/subscriptions/{subscription_id}", headers={"x-csrf-token": csrf}
    )
    assert deleted.status_code == 200 and deleted.json() == {"ok": True}
