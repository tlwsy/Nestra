from __future__ import annotations

import asyncio
import io
import sys
import zipfile
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from nestra.core.crypto import Crypto
from nestra.core.errors import NotifyTransient, TargetRejected
from nestra.notifier.apprise_client import AppriseClient
from nestra.notifier.attachment import (
    AttachmentTooLarge,
    filename_from_content_disposition,
    mime_allowed,
    read_limited,
    sniff_mime,
)
from nestra.notifier.dispatcher import Dispatcher
from nestra.notifier.matcher import Matcher, simhash_distance
from nestra.notifier.message import MessageAttachment, render_message, truncate_unicode
from nestra.storage.db import Database

pytestmark = pytest.mark.unit
NOW = "2026-01-01T00:00:00+00:00"
FIXED = datetime(2026, 1, 1, 23, 30, tzinfo=UTC)


def test_simhash_digit_only_values_remain_hexadecimal() -> None:
    assert simhash_distance("10", "20") == 2


def _seed(db: Database) -> dict[str, int]:
    with db.transaction() as conn:
        user = conn.execute(
            "INSERT INTO users (username,password_hash,created_at,updated_at) VALUES (?,?,?,?)",
            ("owner", "hash", NOW, NOW),
        ).lastrowid
        group = conn.execute(
            "INSERT INTO tagset_groups (slug,name,created_at) VALUES (?,?,?)", ("g", "组", NOW)
        ).lastrowid
        other_group = conn.execute(
            "INSERT INTO tagset_groups (slug,name,created_at) VALUES (?,?,?)", ("g2", "组2", NOW)
        ).lastrowid
        site = conn.execute(
            "INSERT INTO sites (slug,name,base_url,discovery_mode,tagset_group_id,config_json,"
            "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            ("site", "站点", "https://example.test", "rss", group, "{}", NOW, NOW),
        ).lastrowid
        tags = []
        for gid, slug in ((group, "a"), (group, "b"), (other_group, "other")):
            tags.append(
                conn.execute(
                    "INSERT INTO tags (group_id,slug,name,tagset_version,frozen_at) "
                    "VALUES (?,?,?,?,?)",
                    (gid, slug, slug.upper(), "v1", NOW),
                ).lastrowid
            )
        target = conn.execute(
            "INSERT INTO notify_targets (user_id,name,apprise_url_enc,url_fingerprint,created_at) "
            "VALUES (?,?,?,?,?)",
            (user, "tg", b"encrypted", "tgram://…abc", NOW),
        ).lastrowid
    return {
        "user": user,
        "group": group,
        "site": site,
        "a": tags[0],
        "b": tags[1],
        "other": tags[2],
        "target": target,
    }


def _article(db: Database, seeded: dict[str, int], suffix: str, *, simhash: str = "0") -> int:
    with db.transaction() as conn:
        article = conn.execute(
            "INSERT INTO articles (site_id,url,url_hash,title,content_text,simhash,status,"
            "discovered_at,published_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                seeded["site"],
                f"https://example.test/{suffix}",
                suffix,
                f"标题{suffix}",
                "完整正文" * 100,
                simhash,
                "TAGGED",
                NOW,
                NOW,
            ),
        ).lastrowid
        conn.execute(
            "INSERT INTO article_tags (article_id,tag_id,confidence,backend,created_at) "
            "VALUES (?,?,?,?,?)",
            (article, seeded["a"], 0.8, "local:test", NOW),
        )
        conn.execute(
            "INSERT INTO article_tags (article_id,tag_id,confidence,backend,created_at) "
            "VALUES (?,?,?,?,?)",
            (article, seeded["b"], 0.4, "local:test", NOW),
        )
    return article


def _subscription(
    db: Database,
    seeded: dict[str, int],
    tags: tuple[int, ...],
    *,
    mode: str = "any",
    confidence: float = 0.5,
    quiet: str | None = None,
    site_filter: str | None = None,
) -> int:
    with db.transaction() as conn:
        sub = conn.execute(
            "INSERT INTO subscriptions (user_id,name,match_mode,min_confidence,quiet_hours,"
            "site_filter,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (seeded["user"], str(tags), mode, confidence, quiet, site_filter, NOW, NOW),
        ).lastrowid
        for tag in tags:
            conn.execute(
                "INSERT INTO subscription_tags (subscription_id,tag_id) VALUES (?,?)", (sub, tag)
            )
    return sub


def test_matching_obeys_any_all_group_site_confidence_and_cross_midnight(db: Database) -> None:
    seeded = _seed(db)
    article = _article(db, seeded, "one")
    hit = _subscription(db, seeded, (seeded["a"], seeded["other"]), quiet="23:00-07:00")
    _subscription(db, seeded, (seeded["a"], seeded["other"]), mode="all")
    _subscription(db, seeded, (seeded["a"], seeded["b"]), mode="all", confidence=0.5)
    all_hit = _subscription(db, seeded, (seeded["a"], seeded["b"]), mode="all", confidence=0.3)
    _subscription(db, seeded, (seeded["a"],), site_filter="[999]")

    matched = Matcher(db).match(article, at=FIXED)

    assert [(item.subscription_id, item.target_id) for item in matched] == [
        (hit, seeded["target"]),
        (all_hit, seeded["target"]),
    ]
    assert matched[0].scheduled_at == datetime(2026, 1, 2, 7, tzinfo=UTC)
    assert matched[1].scheduled_at == FIXED
    assert (
        db.query_one("SELECT status FROM articles WHERE id=?", (article,))["status"] == "NOTIFIED"
    )


def test_delivery_creation_is_deduplicated_and_simhash_duplicates_are_skipped(db: Database) -> None:
    seeded = _seed(db)
    sub = _subscription(db, seeded, (seeded["a"],))
    first = _article(db, seeded, "first", simhash="f0")
    Matcher(db).match(first, at=datetime(2026, 1, 1, tzinfo=UTC))
    second = _article(db, seeded, "second", simhash="f1")

    matcher = Matcher(db)
    assert matcher.match(second, at=datetime(2026, 1, 2, tzinfo=UTC)) == []
    assert matcher.match(second, at=datetime(2026, 1, 2, tzinfo=UTC)) == []
    rows = db.query("SELECT * FROM deliveries WHERE subscription_id=? ORDER BY id", (sub,))
    assert len(rows) == 2
    assert rows[1]["status"] == "skipped"
    assert rows[1]["last_error"] == f"duplicate_of:{first}"


def test_chinese_content_disposition_mime_sniff_and_limits() -> None:
    header = (
        "attachment; filename=legacy.docx; "
        "filename*=UTF-8''%E9%99%84%E4%BB%B61%20%E7%94%B3%E6%8A%A5%E8%A1%A8.docx"
    )
    assert filename_from_content_disposition(header) == "附件1 申报表.docx"
    archive_data = io.BytesIO()
    with zipfile.ZipFile(archive_data, "w") as archive:
        archive.writestr("word/document.xml", "<doc/>")
    mime = sniff_mime(archive_data.getvalue())
    assert mime.endswith("wordprocessingml.document")
    assert mime_allowed(mime, ["application/vnd.openxmlformats-officedocument.*"])
    with pytest.raises(AttachmentTooLarge):
        read_limited([b"123", b"456"], 5)


def test_unicode_truncation_and_channel_limit() -> None:
    assert truncate_unicode("中文内容" * 20, 18).endswith("…（全文见原文链接）")
    message = render_message(
        title="标题",
        site_name="站点",
        url="https://example.test/a",
        published_at=datetime(2026, 1, 1, tzinfo=UTC),
        tags=[("通知", 0.88)],
        content="汉字" * 5000,
        summary="这是 AI 生成的摘要。",
        attachments=[MessageAttachment("课程表.pdf", url="https://example.test/file?token=signed")],
        channel="tgram://…abc",
        max_body_chars=8000,
    )
    assert len(message.body) <= 4096
    assert "token=signed" in message.body
    assert "AI 总结" in message.body and "这是 AI 生成的摘要。" in message.body
    assert message.body.endswith("…（全文见原文链接）")
    message.body.encode("utf-8")


async def test_apprise_client_decrypts_caller_supplied_target(
    crypto: Crypto, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: dict[str, object] = {}

    class FakeApprise:
        def add(self, target: str) -> bool:
            calls["target"] = target
            return True

        def notify(self, **kwargs) -> bool:
            calls.update(kwargs)
            return True

    monkeypatch.setitem(sys.modules, "apprise", SimpleNamespace(Apprise=FakeApprise))
    await AppriseClient(crypto).notify(
        crypto.encrypt("tgram://token/channel"),
        body="正文",
        title="标题",
        body_format="markdown",
    )
    assert calls["target"] == "tgram://token/channel"
    assert calls["body"] == "正文"
    with pytest.raises(TargetRejected, match="自定义网络目标"):
        await AppriseClient(crypto).notify(
            crypto.encrypt("https://example.test/webhook"), body="x", title="x"
        )


class _FailThenPass:
    def __init__(self, failures: list[BaseException]) -> None:
        self.failures = failures
        self.calls = 0

    async def notify(self, *_args, **_kwargs) -> None:
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)


def _pending_delivery(db: Database, crypto: Crypto) -> int:
    seeded = _seed(db)
    encrypted = crypto.encrypt("json://localhost")
    db.execute(
        "UPDATE notify_targets SET apprise_url_enc=? WHERE id=?", (encrypted, seeded["target"])
    )
    sub = _subscription(db, seeded, (seeded["a"],))
    article = _article(db, seeded, "dispatch")
    Matcher(db).match(article, at=datetime(2026, 1, 1, tzinfo=UTC))
    return db.query_one("SELECT id FROM deliveries WHERE subscription_id=?", (sub,))["id"]


async def test_pending_attachment_does_not_emit_unusable_signed_link(
    db: Database, crypto: Crypto
) -> None:
    delivery = _pending_delivery(db, crypto)
    article_id = db.query_one("SELECT article_id FROM deliveries WHERE id=?", (delivery,))[0]
    db.execute(
        "INSERT INTO attachments(article_id,source_url,filename,status,created_at) "
        "VALUES (?,?,?,'pending',?)",
        (article_id, "https://example.test/pending.pdf", "pending.pdf", NOW),
    )

    class Capture:
        body = ""

        async def notify(self, _target, **kwargs) -> None:
            self.body = kwargs["body"]

    client = Capture()
    await Dispatcher(
        db,
        client,
        crypto=crypto,
        base_url="https://nestra.example",
        attachment_mode="link",
    ).send(delivery, at=datetime(2026, 1, 1, tzinfo=UTC))
    assert "pending.pdf: https://example.test/pending.pdf" in client.body
    assert "/shared/attachments/" not in client.body


async def test_dispatch_retries_then_succeeds(db: Database, crypto: Crypto) -> None:
    delivery = _pending_delivery(db, crypto)
    client = _FailThenPass([NotifyTransient("temporary")])
    dispatcher = Dispatcher(db, client, backoff_base_sec=30)
    first_at = datetime(2026, 1, 1, tzinfo=UTC)

    first = await dispatcher.send(delivery, at=first_at)
    assert first is not None and first.status == "pending" and first.attempts == 1
    assert first.next_attempt_at == datetime(2026, 1, 1, 0, 0, 30, tzinfo=UTC)
    assert await dispatcher.send(delivery, at=datetime(2026, 1, 1, 0, 0, 20, tzinfo=UTC))
    sent = await dispatcher.send(delivery, at=datetime(2026, 1, 1, 0, 0, 30, tzinfo=UTC))
    assert sent is not None and sent.status == "sent" and sent.attempts == 1
    assert client.calls == 2


async def test_terminal_transient_failure_promotes_newest_skipped_duplicate(
    db: Database,
) -> None:
    seeded = _seed(db)
    subscription = _subscription(db, seeded, (seeded["a"],))
    first_article = _article(db, seeded, "first", simhash="1")
    second_article = _article(db, seeded, "second", simhash="1")
    matcher = Matcher(db)
    matcher.match(first_article, at=FIXED)
    matcher.match(second_article, at=FIXED)
    first = db.query_one(
        "SELECT id FROM deliveries WHERE subscription_id=? AND article_id=?",
        (subscription, first_article),
    )[0]

    outcome = await Dispatcher(
        db, _FailThenPass([NotifyTransient("exhausted")]), max_attempts=1
    ).send(first, at=FIXED)
    assert outcome is not None and outcome.status == "failed"
    assert (
        db.query_one(
            "SELECT status FROM deliveries WHERE subscription_id=? AND article_id=?",
            (subscription, second_article),
        )[0]
        == "pending"
    )


async def test_dispatch_claim_prevents_concurrent_duplicate_send(
    db: Database, crypto: Crypto
) -> None:
    delivery = _pending_delivery(db, crypto)
    started = asyncio.Event()
    release = asyncio.Event()

    class SlowClient:
        calls = 0

        async def notify(self, *_args, **_kwargs) -> None:
            self.calls += 1
            started.set()
            await release.wait()

    client = SlowClient()
    first = asyncio.create_task(Dispatcher(db, client).send(delivery, at=FIXED))
    await started.wait()
    assert await Dispatcher(db, client).send(delivery, at=FIXED) is None
    release.set()
    assert (await first).status == "sent"
    assert client.calls == 1


async def test_dispatch_fatal_failure_does_not_retry(db: Database, crypto: Crypto) -> None:
    delivery = _pending_delivery(db, crypto)
    client = _FailThenPass([TargetRejected("bad target")])
    outcome = await Dispatcher(db, client).send(delivery, at=datetime(2026, 1, 1, tzinfo=UTC))
    assert outcome is not None and outcome.status == "failed" and outcome.attempts == 1
    assert db.query_one("SELECT next_attempt_at FROM deliveries WHERE id=?", (delivery,))[0] is None
