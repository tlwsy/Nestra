from __future__ import annotations

import json
from pathlib import Path

import pytest

from nestra.core.errors import TagsetNotReady
from nestra.core.models import ArticleText, Tagset, TagsetEntry
from nestra.tagger.prompt import parse_response, truncate_text
from nestra.tagger.tagset import load_groups, load_tagset, write_frozen

pytestmark = pytest.mark.unit


def _document(group: str = "campus") -> dict:
    return {
        "group": group,
        "tagset_version": "v1",
        "build_mode": "llm",
        "tags": [
            {
                "id": 1,
                "slug": "course",
                "name": "选课",
                "description": "选课与退课",
                "keywords": ["选课"],
                "threshold": 0.5,
                "centroid": None,
            },
            {
                "id": 2,
                "slug": "exam",
                "name": "考试",
                "description": "考试安排",
                "threshold": 0.4,
                "centroid": None,
            },
        ],
    }


def test_load_frozen_group_and_filter_hallucinations(tmp_path: Path) -> None:
    path = tmp_path / "campus" / "tags.json"
    write_frozen(path, _document())
    tagset = load_tagset(path, group="campus")

    assignments = parse_response(
        json.dumps(
            {
                "tags": [
                    {"slug": "invented", "confidence": 0.99},
                    {"slug": "course", "confidence": 0.8},
                    {"slug": "exam", "confidence": 0.2},
                ]
            }
        ),
        tagset,
        backend="llm:p:m",
        top_k=5,
        min_confidence=0.3,
    )

    assert tagset.slugs == frozenset({"course", "exam"})
    assert [(item.tag_slug, item.confidence) for item in assignments] == [("course", 0.8)]


def test_checksum_tamper_is_rejected_and_names_group(tmp_path: Path) -> None:
    path = tmp_path / "campus" / "tags.json"
    write_frozen(path, _document())
    raw = json.loads(path.read_text())
    raw["tags"][0]["name"] = "被篡改"
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(TagsetNotReady, match=r"campus.*checksum|checksum.*campus"):
        load_groups(tmp_path, ["campus"])


def test_prompt_parser_is_tolerant_but_structure_is_checked(tmp_path: Path) -> None:
    path = tmp_path / "campus" / "tags.json"
    write_frozen(path, _document())
    tagset = load_tagset(path)
    result = parse_response(
        'result: ```json\n{"tags":[{"slug":"exam","confidence":0.7}]}\n``` done',
        tagset,
        backend="llm:p:m",
        top_k=1,
    )
    assert result[0].tag_slug == "exam"


def test_truncate_keeps_beginning_and_end() -> None:
    text = "0123456789"
    assert truncate_text(text, 5) == "01289"


def test_local_tagset_without_centroids_stays_disabled() -> None:
    tagset = Tagset("g", "v", "llm", "sum", (TagsetEntry("x", "X"),))
    article = ArticleText("t", "body", "<p>body</p>")
    assert not tagset.has_centroids
    assert article.title == "t"
