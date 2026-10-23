import os
import stat

import pytest

from gartifacts import artifacts_registry
from gartifacts.artifacts_links import is_valid_hash
from gartifacts.artifacts_registry import SqliteArtifactRegistry


@pytest.fixture(params=["memory", "file"])
def registry(request, tmp_path):
    path = ":memory:" if request.param == "memory" else str(tmp_path / "a.sqlite")
    return SqliteArtifactRegistry(path)


def test_register_creates_record(registry):
    record = registry.register("folder1", "Owner@Scientist.com", "Q3 Board Deck")
    assert is_valid_hash(record.hash)
    assert record.folder_id == "folder1"
    assert record.owner_email == "owner@scientist.com"
    assert record.slug == "q3-board-deck"


def test_register_is_idempotent_and_refreshes(registry):
    first = registry.register("folder1", "a@scientist.com", "Old Title")
    second = registry.register("folder1", "b@scientist.com", "New Title")
    assert second.hash == first.hash
    assert second.owner_email == "b@scientist.com"
    assert second.slug == "new-title"
    assert second.registered_at == first.registered_at


def test_lookups(registry):
    record = registry.register("folder1", "a@scientist.com", "T")
    assert registry.get_by_hash(record.hash) == record
    assert registry.get_by_folder("folder1") == record
    assert registry.get_by_hash("zzzzzzzz") is None
    assert registry.get_by_folder("nope") is None


def test_refresh_updates_title_and_slug(registry):
    record = registry.register("folder1", "a@scientist.com", "Old")
    refreshed = registry.refresh(record.hash, "Brand New Name")
    assert refreshed.title == "Brand New Name"
    assert refreshed.slug == "brand-new-name"
    assert refreshed.hash == record.hash
    assert registry.refresh("zzzzzzzz", "x") is None


def test_hash_collision_is_redrawn(registry, monkeypatch):
    draws = iter(["aaaaaaaa", "aaaaaaaa", "bbbbbbbb"])
    monkeypatch.setattr(artifacts_registry, "new_hash", lambda: next(draws))
    one = registry.register("folder1", "a@scientist.com", "One")
    two = registry.register("folder2", "a@scientist.com", "Two")
    assert (one.hash, two.hash) == ("aaaaaaaa", "bbbbbbbb")


def test_exhausted_hash_attempts_raise(registry, monkeypatch):
    registry.register("folder1", "a@scientist.com", "One")
    taken = registry.get_by_folder("folder1").hash
    monkeypatch.setattr(artifacts_registry, "new_hash", lambda: taken)
    with pytest.raises(RuntimeError):
        registry.register("folder2", "a@scientist.com", "Two")


def test_database_file_is_private(tmp_path):
    path = tmp_path / "nested" / "a.sqlite"
    SqliteArtifactRegistry(str(path))
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_persists_across_instances(tmp_path):
    path = str(tmp_path / "a.sqlite")
    record = SqliteArtifactRegistry(path).register("f", "a@scientist.com", "T")
    assert SqliteArtifactRegistry(path).get_by_hash(record.hash) == record


def test_default_path_follows_env(monkeypatch, tmp_path):
    monkeypatch.delenv(artifacts_registry.REGISTRY_PATH_ENV, raising=False)
    monkeypatch.setenv("WORKSPACE_MCP_CREDENTIALS_DIR", str(tmp_path))
    assert artifacts_registry._default_registry_path() == str(
        tmp_path / "artifacts.sqlite"
    )
    monkeypatch.setenv(artifacts_registry.REGISTRY_PATH_ENV, "/x/y.sqlite")
    assert artifacts_registry._default_registry_path() == "/x/y.sqlite"
