import pytest

from gartifacts.artifacts_links import (
    DEFAULT_SLUG,
    HASH_ALPHABET,
    HASH_LENGTH,
    build_link_segment,
    is_valid_hash,
    new_hash,
    parse_link_segment,
    slugify,
    title_from_folder_name,
)


def test_new_hash_shape():
    for _ in range(200):
        value = new_hash()
        assert len(value) == HASH_LENGTH
        assert set(value) <= set(HASH_ALPHABET)
        assert is_valid_hash(value)


def test_alphabet_has_no_confusable_characters():
    assert not set("01ilo-") & set(HASH_ALPHABET)


def test_new_hash_redraws_blocklisted(monkeypatch):
    draws = iter("shit2222" + "abcd2345")
    monkeypatch.setattr(
        "gartifacts.artifacts_links.secrets.choice", lambda _alphabet: next(draws)
    )
    assert new_hash() == "abcd2345"


@pytest.mark.parametrize(
    "value", ["", "short", "k7f3q9x", "k7f3q9xm1", "K7F3Q9XM", "k7f3q9x0", "k7f3q9x-"]
)
def test_invalid_hashes(value):
    assert not is_valid_hash(value)


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Q3 Board Deck", "q3-board-deck"),
        ("Q3 Board Deck Final Version", "q3-board-deck"),
        ("  Café   Menu!  ", "cafe-menu"),
        ("日本語", DEFAULT_SLUG),
        ("", DEFAULT_SLUG),
        ("---", DEFAULT_SLUG),
        ("Supercalifragilistic Expialidocious Wonderful", "supercalifragilistic"),
        ("a" * 80, "a" * 30),
    ],
)
def test_slugify(title, expected):
    assert slugify(title) == expected
    assert len(slugify(title)) <= 30


def test_title_from_folder_name():
    assert title_from_folder_name("20261022-1815-q3-board-deck") == "q3 board deck"
    assert title_from_folder_name("my_report") == "my report"
    assert title_from_folder_name("") == ""


def test_link_segment_round_trip():
    segment = build_link_segment("k7f3q9xm", "q3-board-deck")
    assert segment == "k7f3q9xm-q3-board-deck"
    assert parse_link_segment(segment) == "k7f3q9xm"
    assert parse_link_segment("k7f3q9xm") == "k7f3q9xm"
    assert parse_link_segment("k7f3q9xm-anything-else") == "k7f3q9xm"
    assert parse_link_segment("not-a-hash") is None
    assert parse_link_segment("") is None
