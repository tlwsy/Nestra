"""流水线领域类型的轻量行为测试。"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from nestra.core.models import (
    ArticleStatus,
    ArticleText,
    AttachmentRef,
    DeliveryStatus,
    ProbeFinding,
    ProbeReport,
    Tagset,
    TagsetEntry,
)

pytestmark = pytest.mark.unit


def test_article_terminal_states() -> None:
    assert ArticleStatus.NOTIFIED.is_terminal
    assert ArticleStatus.SKIPPED.is_terminal
    # FAILED 可重试，不是终态
    assert not ArticleStatus.FAILED.is_terminal
    assert not ArticleStatus.TAGGED.is_terminal


def test_status_values_match_database_schema() -> None:
    assert str(ArticleStatus.DISCOVERED) == "DISCOVERED"
    assert str(DeliveryStatus.PENDING) == "pending"


def test_pipeline_values_are_immutable() -> None:
    attachment = AttachmentRef("https://e.test/file", filename="通知.docx")
    with pytest.raises(FrozenInstanceError):
        attachment.filename = "changed.docx"  # type: ignore[misc]


def test_article_word_count_uses_character_count_for_cjk() -> None:
    article = ArticleText(title="通知", content_text="选课通知 ABC", content_html="<p>x</p>")
    assert article.word_count == len("选课通知 ABC")


def test_article_attachments_default_is_immutable_tuple() -> None:
    article = ArticleText(title="t", content_text="body", content_html="<p>body</p>")
    assert article.attachments == ()


def test_tagset_slug_whitelist_and_lookup() -> None:
    entries = (
        TagsetEntry(slug="course", name="选课"),
        TagsetEntry(slug="exam", name="考试"),
    )
    tagset = Tagset("campus", "v1", "llm", "sha256", entries)
    assert tagset.slugs == frozenset({"course", "exam"})
    assert tagset.get("exam") == entries[1]
    assert tagset.get("hallucinated") is None


def test_tagset_requires_all_centroids_for_local_fallback() -> None:
    full = Tagset(
        "campus",
        "v1",
        "embedding",
        "sum",
        (
            TagsetEntry("course", "选课", centroid=(0.1, 0.2)),
            TagsetEntry("exam", "考试", centroid=(0.3, 0.4)),
        ),
    )
    partial = Tagset(
        "campus",
        "v1",
        "llm",
        "sum",
        (
            TagsetEntry("course", "选课", centroid=(0.1, 0.2)),
            TagsetEntry("exam", "考试"),
        ),
    )
    empty = Tagset("campus", "v1", "llm", "sum", ())
    assert full.has_centroids
    assert not partial.has_centroids
    assert not empty.has_centroids


def test_probe_review_only_requires_low_or_failed() -> None:
    high = ProbeFinding("mode", "html_list", "high")
    medium = ProbeFinding("selector", "li", "medium")
    low = ProbeFinding("pagination", None, "low")
    failed = ProbeFinding("content", None, "failed")
    assert not high.needs_review
    assert not medium.needs_review
    assert low.needs_review
    assert failed.needs_review


def test_probe_report_lookup_and_review_list() -> None:
    report = ProbeReport("https://e.test")
    high = ProbeFinding("mode", "html_list", "high")
    low = ProbeFinding("selector", None, "low")
    report.add(high)
    report.add(low)
    assert report.get("mode") is high
    assert report.get("missing") is None
    assert report.review_required == [low]
