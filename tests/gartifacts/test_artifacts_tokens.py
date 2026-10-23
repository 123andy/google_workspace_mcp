import json
import time

import pytest

from gartifacts import artifacts_tokens as tokens

SECRET = "a-test-secret-of-sufficient-length"


def test_round_trip():
    token = tokens.mint_content_token(
        "k7f3q9xm", "User@Scientist.com", ttl_seconds=60, now=1000.0, secret=SECRET
    )
    claims = tokens.verify_content_token(token, now=1030.0, secret=SECRET)
    assert claims.artifact_hash == "k7f3q9xm"
    assert claims.email == "user@scientist.com"
    assert claims.expires_at == 1060.0


def test_token_is_url_safe_and_hides_claims():
    token = tokens.mint_content_token("k7f3q9xm", "user@scientist.com", secret=SECRET)
    assert "=" not in token and "/" not in token and "+" not in token
    assert "scientist" not in token and "k7f3q9xm" not in token


def test_expired_token_rejected():
    token = tokens.mint_content_token(
        "k7f3q9xm", "u@scientist.com", ttl_seconds=60, now=1000.0, secret=SECRET
    )
    assert tokens.verify_content_token(token, now=1060.0, secret=SECRET) is None
    assert tokens.verify_content_token(token, now=1059.9, secret=SECRET) is not None


def test_wrong_key_rejected():
    token = tokens.mint_content_token("k7f3q9xm", "u@scientist.com", secret=SECRET)
    assert tokens.verify_content_token(token, secret="another-secret-entirely") is None


@pytest.mark.parametrize("bad", ["", "abc", "x" * 5000, "gAAAA" + "A" * 80])
def test_garbage_rejected(bad):
    assert tokens.verify_content_token(bad, secret=SECRET) is None


def test_tampered_token_rejected():
    token = tokens.mint_content_token("k7f3q9xm", "u@scientist.com", secret=SECRET)
    flipped = token[:-3] + ("A" if token[-3] != "A" else "B") + token[-2:]
    assert tokens.verify_content_token(flipped, secret=SECRET) is None


def test_other_purpose_rejected():
    fernet = tokens._fernet(SECRET)
    payload = {
        "v": 1,
        "p": "something-else",
        "h": "k7f3q9xm",
        "e": "u@x.com",
        "x": time.time() + 60,
    }
    foreign = fernet.encrypt(json.dumps(payload).encode()).decode().rstrip("=")
    assert tokens.verify_content_token(foreign, secret=SECRET) is None


def test_missing_key_raises(monkeypatch):
    monkeypatch.delenv(tokens.TOKEN_KEY_ENV, raising=False)
    with pytest.raises(tokens.TokenConfigError):
        tokens.mint_content_token("k7f3q9xm", "u@scientist.com")
    monkeypatch.setenv(tokens.TOKEN_KEY_ENV, "short")
    with pytest.raises(tokens.TokenConfigError):
        tokens.mint_content_token("k7f3q9xm", "u@scientist.com")


def test_ttl_config(monkeypatch):
    monkeypatch.delenv(tokens.TOKEN_TTL_ENV, raising=False)
    assert tokens.token_ttl_seconds() == 3600
    monkeypatch.setenv(tokens.TOKEN_TTL_ENV, "5")
    assert tokens.token_ttl_seconds() == tokens.MIN_TOKEN_TTL_SECONDS
    monkeypatch.setenv(tokens.TOKEN_TTL_ENV, "9999999")
    assert tokens.token_ttl_seconds() == tokens.MAX_TOKEN_TTL_SECONDS
    monkeypatch.setenv(tokens.TOKEN_TTL_ENV, "oops")
    assert tokens.token_ttl_seconds() == 3600
    monkeypatch.setenv(tokens.TOKEN_TTL_ENV, "1800")
    assert tokens.token_ttl_seconds() == 1800
