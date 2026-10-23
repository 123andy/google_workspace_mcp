"""Postgres registry tests. They need a database: set ARTIFACTS_TEST_POSTGRES_DSN."""

import os
import threading
import uuid

import pytest

from gartifacts import artifacts_registry
from gartifacts.artifacts_links import is_valid_hash
from gartifacts.artifacts_registry_postgres import PostgresArtifactRegistry

DSN = os.getenv("ARTIFACTS_TEST_POSTGRES_DSN")
needs_db = pytest.mark.skipif(not DSN, reason="ARTIFACTS_TEST_POSTGRES_DSN not set")


@pytest.fixture
def registry():
    table = "artifacts_test_" + uuid.uuid4().hex[:8]
    reg = PostgresArtifactRegistry(dsn=DSN, table=table)
    yield reg
    with reg._connect(DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(f"DROP TABLE IF EXISTS {table}")


def test_requires_a_dsn(monkeypatch):
    monkeypatch.delenv("WORKSPACE_MCP_ARTIFACTS_REGISTRY_POSTGRES_DSN", raising=False)
    monkeypatch.delenv("WORKSPACE_MCP_CREDENTIAL_POSTGRES_DSN", raising=False)
    with pytest.raises(ValueError, match="DSN"):
        PostgresArtifactRegistry()


def test_falls_back_to_the_credential_dsn(monkeypatch):
    monkeypatch.delenv("WORKSPACE_MCP_ARTIFACTS_REGISTRY_POSTGRES_DSN", raising=False)
    monkeypatch.setenv("WORKSPACE_MCP_CREDENTIAL_POSTGRES_DSN", "postgresql://x/y")
    assert PostgresArtifactRegistry().dsn == "postgresql://x/y"


@pytest.mark.parametrize("name", ["", "1x", "a-b", "a;drop", "a b"])
def test_table_name_is_validated(name):
    with pytest.raises(ValueError, match="table name"):
        PostgresArtifactRegistry(dsn="postgresql://x/y", table=name or "1")


def test_backend_selection(monkeypatch):
    monkeypatch.setattr(artifacts_registry, "_registry", None)
    monkeypatch.setenv("WORKSPACE_MCP_ARTIFACTS_REGISTRY_BACKEND", "bogus")
    with pytest.raises(ValueError, match="Unknown"):
        artifacts_registry.get_artifact_registry()
    monkeypatch.setenv("WORKSPACE_MCP_ARTIFACTS_REGISTRY_BACKEND", "postgres")
    monkeypatch.setenv(
        "WORKSPACE_MCP_ARTIFACTS_REGISTRY_POSTGRES_DSN", "postgresql://x/y"
    )
    assert isinstance(
        artifacts_registry.get_artifact_registry(), PostgresArtifactRegistry
    )
    artifacts_registry.set_artifact_registry(None)


@needs_db
def test_register_lookup_refresh(registry):
    record = registry.register("folder1", "Owner@Scientist.com", "Q3 Board Deck")
    assert is_valid_hash(record.hash)
    assert (record.owner_email, record.slug) == ("owner@scientist.com", "q3-board-deck")
    assert registry.get_by_hash(record.hash) == record
    assert registry.get_by_folder("folder1") == record
    assert registry.get_by_hash("zzzzzzzz") is None
    again = registry.register("folder1", "b@scientist.com", "New Title")
    assert again.hash == record.hash and again.owner_email == "b@scientist.com"
    assert again.registered_at == record.registered_at
    refreshed = registry.refresh(record.hash, "Brand New")
    assert refreshed.slug == "brand-new"
    assert registry.refresh("zzzzzzzz", "x") is None


@needs_db
def test_hash_collision_is_redrawn(registry, monkeypatch):
    from gartifacts import artifacts_registry_postgres as pg

    draws = iter(["aaaaaaaa", "aaaaaaaa", "bbbbbbbb"])
    monkeypatch.setattr(pg, "new_hash", lambda: next(draws))
    one = registry.register("folder1", "a@scientist.com", "One")
    two = registry.register("folder2", "a@scientist.com", "Two")
    assert (one.hash, two.hash) == ("aaaaaaaa", "bbbbbbbb")


@needs_db
def test_exhausted_attempts_raise(registry, monkeypatch):
    from gartifacts import artifacts_registry_postgres as pg

    registry.register("folder1", "a@scientist.com", "One")
    taken = registry.get_by_folder("folder1").hash
    monkeypatch.setattr(pg, "new_hash", lambda: taken)
    with pytest.raises(RuntimeError):
        registry.register("folder2", "a@scientist.com", "Two")


@needs_db
def test_concurrent_registration_of_one_folder_yields_one_record(registry):
    results = []

    def go():
        results.append(registry.register("same-folder", "a@scientist.com", "T").hash)

    threads = [threading.Thread(target=go) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(set(results)) == 1 and len(results) == 8


@needs_db
def test_persists_across_instances(registry):
    record = registry.register("f", "a@scientist.com", "T")
    other = PostgresArtifactRegistry(dsn=DSN, table=registry.table)
    assert other.get_by_hash(record.hash) == record
