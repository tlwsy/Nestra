"""Browser form coverage for the user-owned M5 UI."""

# ruff: noqa: E501

from __future__ import annotations

import re
import time
from pathlib import Path

import httpx
import pytest
import yaml

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
    ):
        response = await client.get(path)
        assert response.status_code == 200 and text in response.text


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
        }
    )
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
