"""Offline crawl → tag → match → delivery acceptance path."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from nestra.core.config import Settings
from nestra.core.models import ArticleText, DiscoveredItem, FetchResult, Tagset, TagsetEntry
from nestra.core.time import now_iso
from nestra.crawler.service import crawl_site
from nestra.notifier.dispatcher import Dispatcher
from nestra.notifier.matcher import Matcher
from nestra.storage.repositories.sites import get_site, import_yaml_sites
from nestra.tagger.chain import TaggerChain

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_full_offline_pipeline(db, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("E2E_API_KEY", "test-key")
    settings = Settings(
        politeness={"respect_robots": False, "delay_sec": 0},
        tagset_groups=[{"slug": "campus", "name": "Campus", "min_docs_for_build": 1}],
        sites=[
            {
                "slug": "site",
                "name": "Site",
                "base_url": "https://example.test",
                "tagset_group": "campus",
                "discovery_mode": "rss",
                "config": {"feed_url": "https://example.test/feed"},
                "extract": {
                    "min_content_length": 1,
                    "selectors": {"title": "h1", "content": "main"},
                },
            }
        ],
        tagger={
            "strategy": "llm_only",
            "llm": {
                "max_retries_per_model": 0,
                "providers": [
                    {
                        "name": "mock",
                        "type": "openai_compatible",
                        "base_url": "https://llm.test/v1",
                        "api_key_env": "E2E_API_KEY",
                        "models": ["model"],
                    }
                ],
            },
        },
    )
    import_yaml_sites(db, settings)
    stored = get_site(db, "site")
    assert stored is not None

    async def discover(_site, _fetcher):
        return [DiscoveredItem("https://example.test/article", title="Exam notice")]

    class Fetcher:
        async def fetch(self, url, **_kwargs):
            return FetchResult(
                url,
                url,
                200,
                "<h1>Exam notice</h1><main>Exam schedule and room details.</main>",
                "utf-8",
            )

        async def close(self):
            return None

    monkeypatch.setattr("nestra.crawler.service.discover_rss", discover)
    crawled = await crawl_site(settings, db, stored, fetcher=Fetcher())
    assert crawled.extracted == 1
    article_row = db.query_one("SELECT * FROM articles")
    assert article_row["status"] == "EXTRACTED"

    timestamp = now_iso()
    group_id = db.query_one("SELECT id FROM tagset_groups WHERE slug='campus'")[0]
    tag_id = db.execute(
        "INSERT INTO tags (group_id,slug,name,description,threshold,tagset_version,frozen_at) "
        "VALUES (?,'exam','Exam','Exam arrangements',0.3,'v1',?)",
        (group_id, timestamp),
    ).lastrowid
    db.execute(
        "UPDATE tagset_groups SET status='frozen',tagset_version='v1',frozen_at=? WHERE id=?",
        (timestamp, group_id),
    )
    tagset = Tagset(
        "campus",
        "v1",
        "llm",
        "checksum",
        (TagsetEntry("exam", "Exam", "Exam arrangements", (), 0.3),),
        datetime.now(UTC),
    )

    def llm_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"tags":[{"slug":"exam","confidence":0.9}]}'}}]
            },
        )

    llm = httpx.AsyncClient(transport=httpx.MockTransport(llm_handler))
    chain = TaggerChain(settings.tagger, db, client=llm)
    await chain.tag_article(
        article_row["id"],
        ArticleText(
            article_row["title"],
            article_row["content_text"],
            article_row["content_html"],
        ),
        tagset,
    )
    await llm.aclose()

    user_id = db.execute(
        "INSERT INTO users (username,password_hash,created_at,updated_at) VALUES ('owner','x',?,?)",
        (timestamp, timestamp),
    ).lastrowid
    subscription_id = db.execute(
        "INSERT INTO subscriptions (user_id,name,created_at,updated_at) VALUES (?,'Exam',?,?)",
        (user_id, timestamp, timestamp),
    ).lastrowid
    db.execute(
        "INSERT INTO subscription_tags (subscription_id,tag_id) VALUES (?,?)",
        (subscription_id, tag_id),
    )
    target_id = db.execute(
        "INSERT INTO notify_targets "
        "(user_id,name,apprise_url_enc,url_fingerprint,created_at) "
        "VALUES (?,'Target',X'01','tgram://…target',?)",
        (user_id, timestamp),
    ).lastrowid
    attachment = tmp_path / "schedule.pdf"
    attachment.write_bytes(b"%PDF attachment")
    db.execute(
        "INSERT INTO attachments "
        "(article_id,source_url,filename,size_bytes,local_path,status,created_at) "
        "VALUES (?,?,?,?,?,'downloaded',?)",
        (
            article_row["id"],
            "https://example.test/schedule.pdf",
            "schedule.pdf",
            attachment.stat().st_size,
            str(attachment),
            timestamp,
        ),
    )
    assert Matcher(db).match(article_row["id"])

    class NotifyClient:
        def __init__(self) -> None:
            self.calls = 0
            self.kwargs = {}

        async def notify(self, *_args, **kwargs):
            self.calls += 1
            self.kwargs = kwargs
            return True

    client = NotifyClient()
    outcomes = await Dispatcher(db, client).send_due()
    assert client.calls == 1 and outcomes[0].status == "sent"
    assert "Exam schedule and room details." in client.kwargs["body"]
    assert client.kwargs["attachments"] == [str(attachment)]
    delivery = db.query_one("SELECT status,target_id FROM deliveries")
    assert delivery["status"] == "sent" and delivery["target_id"] == target_id
    assert db.query_one("SELECT backend FROM article_tags")[0] == "llm:mock:model"
