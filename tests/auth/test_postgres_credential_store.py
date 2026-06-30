"""Tests for PostgresCredentialStore.

The unit tests use an in-memory fake connection that emulates the small slice of
the psycopg API the store uses, so they run without a live Postgres. A real-DB
integration test is included but skipped unless TEST_POSTGRES_DSN is set.
"""

import json
import os

import pytest
from cryptography.fernet import Fernet
from google.oauth2.credentials import Credentials

from auth.credential_store import (
    PostgresCredentialStore,
    get_credential_store,
    set_credential_store,
)


# --------------------------------------------------------------------------- #
# Fake psycopg connection (just enough of the API the store touches)
# --------------------------------------------------------------------------- #
class _FakeCursor:
    def __init__(self, store):
        self._store = store
        self._result = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        s = " ".join(sql.split())  # normalize whitespace
        if s.startswith("CREATE TABLE"):
            return
        if s.startswith("INSERT INTO"):
            self._store[params[0]] = bytes(params[1])
        elif s.startswith("SELECT cred_blob"):
            email = params[0]
            self._result = [(self._store[email],)] if email in self._store else []
        elif s.startswith("DELETE FROM"):
            self._store.pop(params[0], None)
        elif s.startswith("SELECT user_email"):
            self._result = [(e,) for e in sorted(self._store)]
        else:  # pragma: no cover - guards against an untested query
            raise AssertionError(f"unexpected SQL: {s}")

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return list(self._result)


class _FakeConn:
    def __init__(self, store):
        self._store = store

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return _FakeCursor(self._store)


def _fake_connect_factory(store):
    def _connect(_dsn):
        return _FakeConn(store)

    return _connect


@pytest.fixture
def key():
    return Fernet.generate_key().decode()


@pytest.fixture
def store(key):
    backing = {}
    return PostgresCredentialStore(
        dsn="postgresql://fake",
        encryption_key=key,
        connect=_fake_connect_factory(backing),
    )


def _sample_credentials():
    return Credentials(
        token="access-abc",
        refresh_token="refresh-xyz",
        token_uri="https://oauth2.googleapis.com/token",
        client_id="cid",
        client_secret="csecret",
        scopes=["https://www.googleapis.com/auth/gmail.readonly"],
    )


# --------------------------------------------------------------------------- #
# Unit tests
# --------------------------------------------------------------------------- #
def test_store_then_get_round_trip(store):
    creds = _sample_credentials()
    assert store.store_credential("alice@example.com", creds) is True

    loaded = store.get_credential("alice@example.com")
    assert loaded is not None
    assert loaded.token == "access-abc"
    assert loaded.refresh_token == "refresh-xyz"
    assert loaded.client_id == "cid"
    assert loaded.scopes == ["https://www.googleapis.com/auth/gmail.readonly"]


def test_get_missing_returns_none(store):
    assert store.get_credential("nobody@example.com") is None


def test_upsert_overwrites(store):
    store.store_credential("bob@example.com", _sample_credentials())
    updated = _sample_credentials()
    updated.token = "access-new"
    store.store_credential("bob@example.com", updated)
    assert store.get_credential("bob@example.com").token == "access-new"


def test_delete(store):
    store.store_credential("carol@example.com", _sample_credentials())
    assert store.delete_credential("carol@example.com") is True
    assert store.get_credential("carol@example.com") is None
    # deleting a non-existent user is still a success (matches LocalDirectory behavior)
    assert store.delete_credential("carol@example.com") is True


def test_list_users(store):
    store.store_credential("z@example.com", _sample_credentials())
    store.store_credential("a@example.com", _sample_credentials())
    assert store.list_users() == ["a@example.com", "z@example.com"]


def test_blob_is_encrypted_at_rest(key):
    backing = {}
    store = PostgresCredentialStore(
        dsn="postgresql://fake",
        encryption_key=key,
        connect=_fake_connect_factory(backing),
    )
    store.store_credential("dave@example.com", _sample_credentials())
    raw = bytes(backing["dave@example.com"])
    # the refresh token must not appear in plaintext anywhere in the stored blob
    assert b"refresh-xyz" not in raw
    # and it must be decryptable with the same key
    assert (
        json.loads(Fernet(key.encode()).decrypt(raw))["refresh_token"] == "refresh-xyz"
    )


def test_wrong_key_cannot_decrypt(key):
    backing = {}
    writer = PostgresCredentialStore(
        dsn="postgresql://fake",
        encryption_key=key,
        connect=_fake_connect_factory(backing),
    )
    writer.store_credential("eve@example.com", _sample_credentials())

    other_key = Fernet.generate_key().decode()
    reader = PostgresCredentialStore(
        dsn="postgresql://fake",
        encryption_key=other_key,
        connect=_fake_connect_factory(backing),
    )
    # decryption fails -> None (logged), not a crash
    assert reader.get_credential("eve@example.com") is None


def test_requires_dsn(key, monkeypatch):
    monkeypatch.delenv("WORKSPACE_MCP_CREDENTIAL_POSTGRES_DSN", raising=False)
    with pytest.raises(ValueError, match="requires a DSN"):
        PostgresCredentialStore(encryption_key=key)


def test_requires_encryption_key(monkeypatch):
    monkeypatch.delenv("WORKSPACE_MCP_CREDENTIAL_ENCRYPTION_KEY", raising=False)
    with pytest.raises(ValueError, match="requires an encryption key"):
        PostgresCredentialStore(dsn="postgresql://fake")


def test_invalid_table_name_rejected(key):
    with pytest.raises(ValueError, match="Invalid credential table name"):
        PostgresCredentialStore(
            dsn="postgresql://fake", table="creds; DROP TABLE x", encryption_key=key
        )


def test_backend_selector_returns_postgres(monkeypatch, key):
    monkeypatch.setenv("WORKSPACE_MCP_CREDENTIAL_STORE_BACKEND", "postgres")
    monkeypatch.setenv("WORKSPACE_MCP_CREDENTIAL_POSTGRES_DSN", "postgresql://fake")
    monkeypatch.setenv("WORKSPACE_MCP_CREDENTIAL_ENCRYPTION_KEY", key)
    set_credential_store(None)  # reset the module-global singleton
    try:
        store = get_credential_store()
        assert isinstance(store, PostgresCredentialStore)
    finally:
        set_credential_store(None)


# --------------------------------------------------------------------------- #
# Optional integration test against a real Postgres (set TEST_POSTGRES_DSN)
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(
    not os.getenv("TEST_POSTGRES_DSN"),
    reason="set TEST_POSTGRES_DSN to run the real-Postgres integration test",
)
def test_real_postgres_round_trip(key):
    store = PostgresCredentialStore(
        dsn=os.environ["TEST_POSTGRES_DSN"],
        table="credentials_test",
        encryption_key=key,
    )
    try:
        store.store_credential("real@example.com", _sample_credentials())
        loaded = store.get_credential("real@example.com")
        assert loaded.refresh_token == "refresh-xyz"
        assert "real@example.com" in store.list_users()
    finally:
        store.delete_credential("real@example.com")
