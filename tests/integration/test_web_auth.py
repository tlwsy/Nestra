"""M5 Web authentication and tenant-boundary integration checks."""

# ruff: noqa: E501

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import yaml

from nestra.core.crypto import hash_password, verify_password
from nestra.core.time import now_iso
from nestra.web.app import create_app
from nestra.web.security import RateLimiter, new_totp_secret

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
PASSWORD = "correct horse battery staple"


def config(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(
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
    return path


async def login(client: httpx.AsyncClient, username: str, password: str = PASSWORD) -> str:
    assert (await client.get("/login")).status_code == 200
    csrf = client.cookies["nestra_csrf"]
    response = await client.post(
        "/login?format=json",
        json={"username": username, "password": password},
        headers={"x-csrf-token": csrf},
    )
    assert response.status_code == 200, response.text
    return client.cookies["nestra_csrf"]


@pytest.fixture
async def web(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("NESTRA_ADMIN_PASSWORD", PASSWORD)
    app = create_app(config(tmp_path), strict_config=False)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("198.51.100.10", 1234)),
            base_url="http://testserver",
        ) as client,
    ):
        yield app, client


async def test_setup_is_signed_one_time_and_there_is_no_registration(
    tmp_path: Path,
) -> None:
    app = create_app(config(tmp_path), strict_config=False)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client,
    ):
        assert (await client.get("/setup?token=bad")).status_code == 404
        token = app.state.setup_token
        redirect = await client.get("/", follow_redirects=False)
        assert token and redirect.status_code == 303
        assert redirect.headers["location"] == f"/setup?token={token}"
        assert (await client.get("/setup", params={"token": token})).status_code == 200
        csrf = client.cookies["nestra_csrf"]
        response = await client.post(
            "/setup?format=json",
            json={"token": token, "username": "owner", "password": PASSWORD},
            headers={"x-csrf-token": csrf},
        )
        assert response.status_code == 201
        assert (await client.get("/setup", params={"token": token})).status_code == 404
        assert (await client.post("/register", json={})).status_code == 404


async def test_dashboard_redirects_anonymous_browser_to_login(web) -> None:
    _app, client = web
    response = await client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


async def test_login_rehashes_password_when_parameters_change(
    web, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, client = web
    before = app.state.db.query_one("SELECT password_hash FROM users WHERE username='admin'")[0]
    monkeypatch.setattr("nestra.web.api.auth.password_needs_rehash", lambda _value: True)
    await login(client, "admin")
    after = app.state.db.query_one("SELECT password_hash FROM users WHERE username='admin'")[0]
    assert after != before and verify_password(PASSWORD, after)


async def test_session_is_hashed_revocable_and_csrf_is_required(web) -> None:
    app, client = web
    csrf = await login(client, "admin")
    token = client.cookies["nestra_session"]
    row = app.state.db.query_one("SELECT * FROM sessions")
    assert row["token_hash"] != token and len(row["token_hash"]) == 64
    assert (await client.post("/logout")).status_code == 403
    assert (
        await client.post("/logout?format=json", headers={"x-csrf-token": csrf})
    ).status_code == 200
    assert app.state.db.query_one("SELECT revoked_at FROM sessions")[0]
    assert (await client.get("/?format=json")).status_code == 401


async def test_user_queries_and_attachment_download_are_isolated_and_html_is_cleaned(
    web, tmp_path: Path
) -> None:
    app, _admin_client = web
    timestamp = now_iso()
    with app.state.db.transaction() as conn:
        for username in ("alice", "bob"):
            conn.execute(
                "INSERT INTO users (username,password_hash,role,created_at,updated_at) VALUES (?,?,'user',?,?)",
                (username, hash_password(PASSWORD), timestamp, timestamp),
            )
        users = {
            row["username"]: row["id"] for row in conn.execute("SELECT id,username FROM users")
        }
        group_id = conn.execute("SELECT id FROM tagset_groups WHERE slug='test'").fetchone()[0]
        site_id = conn.execute(
            "INSERT INTO sites (slug,name,base_url,discovery_mode,tagset_group_id,config_json,created_at,updated_at) "
            "VALUES ('site','Site','https://example.test','rss',?,'{}',?,?)",
            (group_id, timestamp, timestamp),
        ).lastrowid
        article_id = conn.execute(
            "INSERT INTO articles (site_id,url,url_hash,title,content_html,status,discovered_at) "
            "VALUES (?,?,?,?,?,'NOTIFIED',?)",
            (
                site_id,
                "https://example.test/a",
                "a" * 64,
                "Unsafe",
                '<p onclick="steal()">ok</p><script>alert(1)</script><img src=x onerror=steal()>',
                timestamp,
            ),
        ).lastrowid
        subscription_id = conn.execute(
            "INSERT INTO subscriptions (user_id,name,created_at,updated_at) VALUES (?,'Alice only',?,?)",
            (users["alice"], timestamp, timestamp),
        ).lastrowid
        target_id = conn.execute(
            "INSERT INTO notify_targets (user_id,name,apprise_url_enc,created_at) VALUES (?,'A',?,?)",
            (users["alice"], app.state.crypto.encrypt("json://secret"), timestamp),
        ).lastrowid
        conn.execute(
            "INSERT INTO deliveries (subscription_id,article_id,target_id,status,created_at,sent_at) "
            "VALUES (?,?,?,'sent',?,?)",
            (subscription_id, article_id, target_id, timestamp, timestamp),
        )
        attachment_dir = Path(app.state.settings.storage.attachment_dir)
        attachment_dir.mkdir(parents=True, exist_ok=True)
        (attachment_dir / "safe.txt").write_text("private", encoding="utf-8")
        attachment_id = conn.execute(
            "INSERT INTO attachments (article_id,source_url,filename,local_path,status,created_at) "
            "VALUES (?,'https://example.test/file','safe.txt','safe.txt','downloaded',?)",
            (article_id, timestamp),
        ).lastrowid

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as alice:
        csrf = await login(alice, "alice")
        blocked = await alice.post(
            "/targets?format=json",
            json={"name": "internal", "apprise_url": "json://127.0.0.1/hook"},
            headers={"x-csrf-token": csrf},
        )
        assert blocked.status_code == 400
        local = await alice.post(
            "/targets?format=json",
            json={"name": "file", "apprise_url": "file:///tmp/nestra-alert"},
            headers={"x-csrf-token": csrf},
        )
        assert local.status_code == 400
        malformed = await alice.post(
            "/targets?format=json",
            json={"name": "bad port", "apprise_url": "https://example.com:not-a-port/hook"},
            headers={"x-csrf-token": csrf},
        )
        assert malformed.status_code == 400
        allowed = await alice.post(
            "/targets?format=json",
            json={"name": "telegram", "apprise_url": "tgram://token/channel"},
            headers={"x-csrf-token": csrf},
        )
        assert allowed.status_code == 200
        body = (await alice.get(f"/articles/{article_id}")).text
        assert "<script" not in body and "onclick" not in body and "onerror" not in body
        assert (await alice.get(f"/attachments/{attachment_id}")).text == "private"

    token = app.state.crypto.sign_payload(
        {"attachment_id": attachment_id, "user_id": users["alice"]},
        ttl_sec=60,
        purpose="link",
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as anonymous:
        assert (
            await anonymous.get(f"/shared/attachments/{attachment_id}", params={"token": token})
        ).text == "private"
        assert (
            await anonymous.get(f"/shared/attachments/{attachment_id + 1}", params={"token": token})
        ).status_code == 404

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as bob:
        csrf = await login(bob, "bob")
        assert (await bob.get(f"/articles/{article_id}?format=json")).status_code == 404
        assert (await bob.get(f"/attachments/{attachment_id}")).status_code == 404
        assert (
            await bob.delete(f"/subscriptions/{subscription_id}", headers={"x-csrf-token": csrf})
        ).status_code == 404


async def test_subscription_cannot_cross_tagset_groups(web) -> None:
    app, _client = web
    timestamp = now_iso()
    with app.state.db.transaction() as conn:
        user_id = conn.execute(
            "INSERT INTO users (username,password_hash,role,created_at,updated_at) "
            "VALUES ('group-user',?,'user',?,?)",
            (hash_password(PASSWORD), timestamp, timestamp),
        ).lastrowid
        first_group = conn.execute("SELECT id FROM tagset_groups WHERE slug='test'").fetchone()[0]
        conn.execute(
            "UPDATE tagset_groups SET status='frozen',tagset_version='v1',frozen_at=? WHERE id=?",
            (timestamp, first_group),
        )
        second_group = conn.execute(
            "INSERT INTO tagset_groups (slug,name,status,tagset_version,frozen_at,created_at) "
            "VALUES ('other','Other','frozen','v1',?,?)",
            (timestamp, timestamp),
        ).lastrowid
        tag_ids = []
        for group_id, slug in ((first_group, "first"), (second_group, "second")):
            tag_ids.append(
                conn.execute(
                    "INSERT INTO tags (group_id,slug,name,tagset_version,frozen_at) "
                    "VALUES (?,?,?,'v1',?)",
                    (group_id, slug, slug.title(), timestamp),
                ).lastrowid
            )
        stale_tag_id = conn.execute(
            "INSERT INTO tags (group_id,slug,name,tagset_version,frozen_at) "
            "VALUES (?,'stale','Stale','old',?)",
            (first_group, timestamp),
        ).lastrowid
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        csrf = await login(client, "group-user")
        response = await client.post(
            "/subscriptions?format=json",
            json={"name": "cross-group", "tag_ids": tag_ids},
            headers={"x-csrf-token": csrf},
        )
        stale_response = await client.post(
            "/subscriptions?format=json",
            json={"name": "stale", "tag_ids": [stale_tag_id]},
            headers={"x-csrf-token": csrf},
        )
    assert response.status_code == 400
    assert stale_response.status_code == 400
    assert (
        app.state.db.query_one("SELECT COUNT(*) FROM subscriptions WHERE user_id=?", (user_id,))[0]
        == 0
    )


async def test_site_confirmation_requires_matching_completed_dryrun(
    web, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, client = web
    csrf = await login(client, "admin")

    async def fake_preview(*_args, **_kwargs):
        from nestra.onboarding.dryrun import DryRunReport

        return DryRunReport((), discovered=2, succeeded=2, failed=0, duration_ms=1)

    monkeypatch.setattr("nestra.web.api.admin.preview_site", fake_preview)
    candidate = {
        "slug": "previewed",
        "name": "Previewed",
        "base_url": "https://example.test",
        "tagset_group": "test",
        "discovery_mode": "html_list",
        "config": {
            "list_urls": ["https://example.test/list"],
            "item_selector": "article a",
        },
        "attachments": {"link_patterns": [r"download\\.jsp"]},
    }
    response = await client.post(
        "/admin/sites/dryrun?format=json",
        json=candidate,
        headers={"x-csrf-token": csrf},
    )
    assert response.status_code == 200, response.text
    task = response.json()
    result = await client.get(f"/admin/sites/probe/{task['task_id']}")
    assert result.json()["status"] == "done"
    assert (
        await client.post(
            "/admin/sites/confirm?format=json",
            json={**candidate, "name": "altered", **task},
            headers={"x-csrf-token": csrf},
        )
    ).status_code == 409
    confirmed = await client.post(
        "/admin/sites/confirm?format=json",
        json={**candidate, "enabled": True, **task},
        headers={"x-csrf-token": csrf},
    )
    assert confirmed.status_code == 200
    stored = app.state.db.query_one("SELECT enabled,config_json FROM sites WHERE slug='previewed'")
    assert stored["enabled"] == 0
    assert r"download\\.jsp" in json.loads(stored["config_json"])["attachments"]["link_patterns"]


async def test_manual_crawl_records_completion(web, monkeypatch: pytest.MonkeyPatch) -> None:
    from nestra.crawler.service import CrawlStats

    app, client = web
    csrf = await login(client, "admin")
    created = await client.post(
        "/admin/sites?format=json",
        json={
            "slug": "manual",
            "name": "Manual",
            "base_url": "https://example.test",
            "tagset_group": "test",
            "enabled": True,
            "discovery_mode": "rss",
            "config": {"feed_url": "https://example.test/feed"},
        },
        headers={"x-csrf-token": csrf},
    )

    async def fake_crawl(*_args, **_kwargs):
        return CrawlStats(discovered=2, extracted=1)

    monkeypatch.setattr("nestra.web.api.admin.crawl_site", fake_crawl)
    response = await client.post(
        f"/admin/sites/{created.json()['id']}/crawl?format=json",
        json={},
        headers={"x-csrf-token": csrf},
    )

    assert response.json() == {"queued": True, "status": "queued", "kind": "crawl"}
    assert app.state.crawl_tasks[created.json()["id"]]["status"] == "done"
    assert app.state.crawl_tasks[created.json()["id"]]["result"]["extracted"] == 1


async def test_web_backfill_is_bounded_and_does_not_change_site_config(
    web, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nestra.crawler.service import CrawlStats
    from nestra.storage.repositories.sites import get_site

    app, client = web
    csrf = await login(client, "admin")
    created = await client.post(
        "/admin/sites?format=json",
        json={
            "slug": "history",
            "name": "History",
            "base_url": "https://example.test",
            "tagset_group": "test",
            "enabled": True,
            "discovery_mode": "html_list",
            "config": {
                "list_urls": ["https://example.test/news"],
                "item_selector": "article",
                "pagination": {
                    "mode": "url_template",
                    "template": "https://example.test/news/{page}",
                    "order": "desc_index",
                    "max_page": 78,
                    "max_pages": 1,
                },
            },
        },
        headers={"x-csrf-token": csrf},
    )
    page = await client.get("/admin/sites")
    assert "Historical backfill" in page.text
    assert 'name="pages" min="1" max="78"' in page.text
    assert "/articles?site=history" in page.text
    seen = {}

    async def fake_crawl(_settings, _db, stored):
        seen["pages"] = stored.config.discovery.pagination.max_pages
        seen["conditional"] = stored.config.politeness.conditional_requests
        return CrawlStats(discovered=30, extracted=15)

    monkeypatch.setattr("nestra.web.api.admin.crawl_site", fake_crawl)
    site_id = created.json()["id"]
    rejected = await client.post(
        f"/admin/sites/{site_id}/crawl?format=json",
        json={"pages": 79},
        headers={"x-csrf-token": csrf},
    )
    assert rejected.status_code == 400
    response = await client.post(
        f"/admin/sites/{site_id}/crawl?format=json",
        json={"pages": 78},
        headers={"x-csrf-token": csrf},
    )

    assert response.json() == {"queued": True, "status": "queued", "kind": "backfill"}
    assert seen == {"pages": 78, "conditional": False}
    assert app.state.crawl_tasks[site_id]["pages"] == 78
    assert get_site(app.state.db, "history").config.discovery.pagination.max_pages == 1
    audit_row = app.state.db.query_one(
        "SELECT action,detail FROM audit_log WHERE target_type='site' AND target_id=? "
        "ORDER BY id DESC LIMIT 1",
        (site_id,),
    )
    assert tuple(audit_row) == ("admin.site_backfill", "pages=78")


async def test_admin_can_view_undelivered_crawled_articles(web) -> None:
    app, client = web
    await login(client, "admin")
    timestamp = now_iso()
    group_id = app.state.db.query_one("SELECT id FROM tagset_groups WHERE slug='test'")[0]
    site_id = app.state.db.execute(
        "INSERT INTO sites (slug,name,base_url,discovery_mode,tagset_group_id,config_json,"
        "created_at,updated_at) VALUES ('raw','Raw','https://example.test','rss',?,'{}',?,?)",
        (group_id, timestamp, timestamp),
    ).lastrowid
    article_id = app.state.db.execute(
        "INSERT INTO articles (site_id,url,url_hash,title,content_html,status,discovered_at) "
        "VALUES (?,?,?,'Raw result','<p onclick=bad()>safe</p><script>bad()</script>',"
        "'EXTRACTED',?)",
        (site_id, "https://example.test/raw", "raw-hash", timestamp),
    ).lastrowid

    listing = await client.get("/articles")
    detail = await client.get(f"/articles/{article_id}")

    assert "Raw result" in listing.text and "EXTRACTED" in listing.text
    assert "safe" in detail.text and "<script" not in detail.text and "onclick" not in detail.text

    app.state.db.execute(
        "INSERT INTO users (username,password_hash,role,created_at,updated_at) "
        "VALUES ('viewer',?,'user',?,?)",
        (hash_password(PASSWORD), timestamp, timestamp),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as viewer:
        await login(viewer, "viewer")
        assert "Raw result" not in (await viewer.get("/articles")).text
        assert (await viewer.get(f"/articles/{article_id}")).status_code == 404


async def test_admin_created_user_must_change_temporary_password(web) -> None:
    app, client = web
    csrf = await login(client, "admin")
    temporary = "temporary-password-123"
    created = await client.post(
        "/admin/users?format=json",
        json={"username": "temporary", "password": temporary},
        headers={"x-csrf-token": csrf},
    )
    assert created.status_code == 200
    assert (
        app.state.db.query_one("SELECT must_change_password FROM users WHERE username='temporary'")[
            0
        ]
        == 1
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as newcomer:
        await newcomer.get("/login")
        login_csrf = newcomer.cookies["nestra_csrf"]
        response = await newcomer.post(
            "/login?format=json",
            json={"username": "temporary", "password": temporary},
            headers={"x-csrf-token": login_csrf},
        )
        assert response.json()["must_change_password"] is True
        csrf = newcomer.cookies["nestra_csrf"]
        assert (await newcomer.get("/")).status_code == 403
        assert (await newcomer.get("/settings")).status_code == 200
        changed = await newcomer.post(
            "/settings/password?format=json",
            json={"old_password": temporary, "new_password": "new-secure-password-456"},
            headers={"x-csrf-token": csrf},
        )
        assert changed.status_code == 200
    assert (
        app.state.db.query_one("SELECT must_change_password FROM users WHERE username='temporary'")[
            0
        ]
        == 0
    )


async def test_admin_can_trigger_and_poll_tagset_build(
    web, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from types import SimpleNamespace

    _app, client = web
    csrf = await login(client, "admin")
    report = tmp_path / "report.md"
    report.write_text("ok", encoding="utf-8")

    async def fake_bootstrap(*_args, **_kwargs):
        return SimpleNamespace(frozen=True, report_path=report, tagset_path=tmp_path / "tags.json")

    monkeypatch.setattr("nestra.web.api.admin.bootstrap_tagset", fake_bootstrap)
    response = await client.post(
        "/admin/tagset/build?format=json",
        json={"group": "test"},
        headers={"x-csrf-token": csrf},
    )
    assert response.status_code == 200, response.text
    result = await client.get(f"/admin/tagset/build/{response.json()['task_id']}")
    assert result.json()["status"] == "done"


async def test_target_secret_is_encrypted_and_only_fingerprint_is_returned(web) -> None:
    app, client = web
    csrf = await login(client, "admin")
    url = "tgram://super-secret-token/channel"
    response = await client.post(
        "/targets?format=json",
        json={"name": "private", "apprise_url": url},
        headers={"x-csrf-token": csrf},
    )
    assert response.status_code == 200 and "super-secret" not in response.text
    row = app.state.db.query_one("SELECT apprise_url_enc,url_fingerprint FROM notify_targets")
    assert url.encode() not in bytes(row["apprise_url_enc"])
    assert row["url_fingerprint"].startswith("tgram://…")


async def test_target_test_persists_failure_and_success(
    web, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, client = web
    csrf = await login(client, "admin")
    created = await client.post(
        "/targets?format=json",
        json={"name": "test", "apprise_url": "tgram://token/channel"},
        headers={"x-csrf-token": csrf},
    )
    target_id = created.json()["id"]

    async def fail(*_args, **_kwargs):
        raise RuntimeError("injected failure")

    monkeypatch.setattr("nestra.web.api.user.AppriseClient.notify", fail)
    failed = await client.post(
        f"/targets/{target_id}/test?format=json",
        json={},
        headers={"x-csrf-token": csrf},
    )
    assert failed.status_code == 502
    row = app.state.db.query_one(
        "SELECT consecutive_failures,last_error FROM notify_targets WHERE id=?", (target_id,)
    )
    assert row["consecutive_failures"] == 1 and "RuntimeError" in row["last_error"]

    async def succeed(*_args, **_kwargs):
        return None

    monkeypatch.setattr("nestra.web.api.user.AppriseClient.notify", succeed)
    succeeded = await client.post(
        f"/targets/{target_id}/test?format=json",
        json={},
        headers={"x-csrf-token": csrf},
    )
    assert succeeded.status_code == 200
    row = app.state.db.query_one(
        "SELECT consecutive_failures,last_ok_at,last_error FROM notify_targets WHERE id=?",
        (target_id,),
    )
    assert row["consecutive_failures"] == 0 and row["last_ok_at"] and row["last_error"] is None


async def test_rate_limiter_bounds_attacker_controlled_identity_keys() -> None:
    limiter = RateLimiter()
    for index in range(10_100):
        limiter.check("login-account", f"address:user-{index}", 10, 300)
    assert len(limiter._events) == 10_000


async def test_login_rate_limit_ignores_untrusted_forwarded_headers(web) -> None:
    _app, client = web
    await client.get("/login")
    csrf = client.cookies["nestra_csrf"]
    for attempt in range(10):
        response = await client.post(
            "/login",
            json={"username": f"missing-{attempt}", "password": "wrong"},
            headers={"x-csrf-token": csrf, "x-forwarded-for": f"203.0.113.{attempt}"},
        )
        assert response.status_code == 401
    response = await client.post(
        "/login",
        json={"username": "missing", "password": "wrong"},
        headers={"x-csrf-token": csrf, "x-forwarded-for": "203.0.113.250"},
    )
    assert response.status_code == 429


async def test_invalid_totp_attempts_lock_account(web, monkeypatch: pytest.MonkeyPatch) -> None:
    app, client = web
    monkeypatch.setattr("nestra.web.api.auth.verify_totp", lambda *_: False)
    secret = new_totp_secret()
    app.state.db.execute(
        "UPDATE users SET totp_secret=? WHERE username='admin'",
        (app.state.crypto.encrypt(secret),),
    )
    await client.get("/login")
    csrf = client.cookies["nestra_csrf"]
    for _ in range(5):
        response = await client.post(
            "/login",
            json={"username": "admin", "password": PASSWORD, "totp": "000000"},
            headers={"x-csrf-token": csrf},
        )
        assert response.status_code == 401
    row = app.state.db.query_one(
        "SELECT failed_logins,locked_until FROM users WHERE username='admin'"
    )
    assert row["failed_logins"] == 5 and row["locked_until"]


async def test_chunked_request_body_is_bounded(web) -> None:
    _app, client = web

    async def oversized():
        yield b"x" * (1024 * 1024)
        yield b"x" * (1024 * 1024 + 1)

    response = await client.post(
        "/login",
        content=oversized(),
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 413


async def test_security_headers_and_hsts_only_on_https(web) -> None:
    app, _client = web
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        plain = await client.get("/healthz")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://testserver"
    ) as client:
        tls = await client.get("/healthz")
    assert plain.json()["status"] == "ok"
    assert "strict-transport-security" not in plain.headers
    assert tls.headers["strict-transport-security"] == "max-age=31536000"
    assert tls.headers["x-content-type-options"] == "nosniff"
    assert "default-src 'self'" in tls.headers["content-security-policy"]
    assert "version" not in tls.text and "schema" not in tls.text


async def test_account_locks_after_five_failures_and_audits_them(web) -> None:
    app, client = web
    await client.get("/login")
    csrf = client.cookies["nestra_csrf"]
    for _ in range(5):
        assert (
            await client.post(
                "/login",
                json={"username": "admin", "password": "definitely wrong"},
                headers={"x-csrf-token": csrf},
            )
        ).status_code == 401
    user = app.state.db.query_one(
        "SELECT failed_logins,locked_until FROM users WHERE username='admin'"
    )
    assert user["failed_logins"] == 5 and user["locked_until"]
    assert (
        app.state.db.query_one("SELECT COUNT(*) FROM audit_log WHERE action='auth.login_failed'")[0]
        == 5
    )
    valid = await client.post(
        "/login",
        json={"username": "admin", "password": PASSWORD},
        headers={"x-csrf-token": csrf},
    )
    assert valid.status_code == 200
    user = app.state.db.query_one(
        "SELECT failed_logins,locked_until FROM users WHERE username='admin'"
    )
    assert user["failed_logins"] == 0 and user["locked_until"] is None
