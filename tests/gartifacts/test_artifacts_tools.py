import inspect
import json

import pytest

from core.utils import UserInputError
from gartifacts import artifacts_registry, artifacts_tools
from gartifacts.artifacts_drive import NotABundleError
from gartifacts.artifacts_helpers import (
    format_registration,
    register_bundle,
    viewer_link,
)
from gartifacts.artifacts_policy import ArtifactPolicy, policy_version
from gartifacts.artifacts_registry import SqliteArtifactRegistry
from tests.gartifacts.fake_drive import FakeDrive

OWNER = "owner@scientist.com"


@pytest.fixture
def registry():
    return SqliteArtifactRegistry(":memory:")


def make_bundle(drive, html=b"<h1>hi</h1>"):
    bundle = drive.bundle()
    drive.add("index.html", bundle, mime="text/html", content=html)
    return bundle


@pytest.mark.asyncio
async def test_register_bundle_happy_path(registry):
    drive = FakeDrive()
    bundle = make_bundle(drive)
    result = await register_bundle(
        drive,
        OWNER,
        bundle,
        registry,
        ArtifactPolicy(),
        "https://artifacts.example.com",
    )
    assert result.record.title == "q3 board deck"
    assert result.record.owner_email == OWNER
    assert result.viewer_url == (
        f"https://artifacts.example.com/a/{result.record.hash}-q3-board-deck/"
    )
    assert registry.get_by_folder(bundle) == result.record


@pytest.mark.asyncio
async def test_register_is_idempotent(registry):
    drive = FakeDrive()
    bundle = make_bundle(drive)
    first = await register_bundle(
        drive, OWNER, bundle, registry, ArtifactPolicy(), None
    )
    second = await register_bundle(
        drive, OWNER, bundle, registry, ArtifactPolicy(), None
    )
    assert first.record.hash == second.record.hash
    assert second.viewer_url is None


@pytest.mark.asyncio
async def test_register_refuses_other_owners_and_bad_bundles(registry):
    drive = FakeDrive()
    bundle = make_bundle(drive)
    with pytest.raises(NotABundleError):
        await register_bundle(
            drive, "intruder@scientist.com", bundle, registry, ArtifactPolicy(), None
        )
    assert registry.get_by_folder(bundle) is None

    empty = FakeDrive()
    no_index = empty.bundle()
    with pytest.raises(NotABundleError, match="no index.html"):
        await register_bundle(empty, OWNER, no_index, registry, ArtifactPolicy(), None)
    assert registry.get_by_folder(no_index) is None


@pytest.mark.asyncio
async def test_warnings_do_not_block_registration(registry):
    drive = FakeDrive()
    bundle = make_bundle(
        drive, b'<script src="https://evil.example.com/x.js"></script>'
    )
    result = await register_bundle(
        drive, OWNER, bundle, registry, ArtifactPolicy(), None
    )
    text = format_registration(result, policy_version(ArtifactPolicy()))
    assert "evil.example.com" in text
    assert "Warnings (checked against policy" in text


def test_format_registration_without_warnings_or_url():
    from gartifacts.artifacts_drive import BundleReport
    from gartifacts.artifacts_helpers import Registration
    from gartifacts.artifacts_registry import ArtifactRecord

    record = ArtifactRecord("k7f3q9xm", "f", OWNER, "T", "t", 1.0, 1.0)
    text = format_registration(Registration(record, BundleReport(1, 5), None), "abc")
    assert "Link path: /a/k7f3q9xm-t/" in text
    assert "No policy warnings (checked against policy abc)." in text
    assert viewer_link(None, record) is None


@pytest.mark.asyncio
async def test_tool_registers_and_reports(monkeypatch, registry):
    drive = FakeDrive()
    bundle = make_bundle(drive)
    monkeypatch.setattr(artifacts_registry, "_registry", registry)
    monkeypatch.setenv("WORKSPACE_MCP_ARTIFACTS_VIEWER_URL", "https://v.example.com")
    tool = inspect.unwrap(artifacts_tools.register_artifact)
    text = await tool(drive, OWNER, bundle)
    assert "https://v.example.com/a/" in text


@pytest.mark.asyncio
async def test_tool_turns_problems_into_user_errors(monkeypatch, registry):
    monkeypatch.setattr(artifacts_registry, "_registry", registry)
    tool = inspect.unwrap(artifacts_tools.register_artifact)
    with pytest.raises(UserInputError):
        await tool(FakeDrive(), OWNER, "missing")


@pytest.mark.asyncio
async def test_policy_tool_returns_json(monkeypatch):
    monkeypatch.delenv("WORKSPACE_MCP_ARTIFACTS_CSP_PROFILE", raising=False)
    described = json.loads(await artifacts_tools.get_artifact_policy())
    assert described["policy_version"] == policy_version(ArtifactPolicy())
    assert "cdn.jsdelivr.net" in described["hosts"]["script"]
    assert described["hosts"]["connect"] == []


@pytest.mark.asyncio
async def test_policy_tool_reports_a_bad_profile(monkeypatch, tmp_path):
    bad = tmp_path / "p.json"
    bad.write_text("[]")
    monkeypatch.setenv("WORKSPACE_MCP_ARTIFACTS_CSP_PROFILE", str(bad))
    with pytest.raises(UserInputError):
        await artifacts_tools.get_artifact_policy()
