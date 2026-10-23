"""
Link helpers for artifact bundles.

A bundle's public link is ``<hash>-<slug>``. The hash is the registry key; the slug is
decoration derived from the title and is never used for lookup, so renaming a bundle
cannot break a link and a look-alike slug cannot point anywhere else.
"""

import re
import secrets
import unicodedata
from typing import Optional

# Lowercase letters and digits without the confusable 0, 1, i, l, o (and no hyphen,
# which separates the hash from the slug).
HASH_ALPHABET = "23456789abcdefghjkmnpqrstuvwxyz"
HASH_LENGTH = 8

SLUG_MAX_WORDS = 3
SLUG_MAX_CHARS = 30
DEFAULT_SLUG = "artifact"

# Candidate hashes containing any of these are redrawn.
_HASH_BLOCKLIST = (
    "anus",
    "cock",
    "cunt",
    "dick",
    "fuck",
    "nazi",
    "piss",
    "porn",
    "rape",
    "shit",
    "slut",
    "tits",
)

_HASH_PATTERN = re.compile(rf"^[{HASH_ALPHABET}]{{{HASH_LENGTH}}}$")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_TIMESTAMP_PREFIX = re.compile(r"^\d{8}-\d{4}-")


def new_hash() -> str:
    """Return a random registry hash that avoids the blocklist."""
    while True:
        candidate = "".join(secrets.choice(HASH_ALPHABET) for _ in range(HASH_LENGTH))
        if not any(word in candidate for word in _HASH_BLOCKLIST):
            return candidate


def is_valid_hash(value: str) -> bool:
    """True when ``value`` has the exact shape of a registry hash."""
    return bool(_HASH_PATTERN.match(value or ""))


def slugify(title: str) -> str:
    """ASCII-fold a title into at most three lowercase words of 30 characters."""
    folded = unicodedata.normalize("NFKD", title or "")
    ascii_text = folded.encode("ascii", "ignore").decode("ascii").lower()
    words = [w for w in _NON_ALNUM.split(ascii_text) if w][:SLUG_MAX_WORDS]
    slug = "-".join(words)
    if len(slug) > SLUG_MAX_CHARS:
        kept = []
        for word in words:
            candidate = "-".join(kept + [word])
            if len(candidate) > SLUG_MAX_CHARS:
                break
            kept.append(word)
        slug = "-".join(kept) if kept else words[0][:SLUG_MAX_CHARS]
    return slug or DEFAULT_SLUG


def title_from_folder_name(folder_name: str) -> str:
    """Turn ``YYYYMMDD-HHMM-q3-board-deck`` into ``q3 board deck``."""
    stripped = _TIMESTAMP_PREFIX.sub("", folder_name or "").strip()
    words = [w for w in re.split(r"[-_\s]+", stripped) if w]
    return " ".join(words) or (folder_name or "").strip()


def build_link_segment(artifact_hash: str, slug: str) -> str:
    """Return the ``<hash>-<slug>`` path segment."""
    return f"{artifact_hash}-{slug or DEFAULT_SLUG}"


def parse_link_segment(segment: str) -> Optional[str]:
    """Return the hash from a ``<hash>[-anything]`` segment, or None if malformed."""
    candidate = (segment or "").split("-", 1)[0]
    return candidate if is_valid_hash(candidate) else None
