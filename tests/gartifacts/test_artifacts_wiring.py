"""The artifacts service is opt-in: with no switch set, nothing changes."""

import pytest
import yaml

import main
from auth.permissions import (
    SERVICE_PERMISSION_LEVELS,
    get_scopes_for_permission,
    parse_permissions_arg,
)
from auth.scopes import (
    DRIVE_READONLY_SCOPE,
    TOOL_READONLY_SCOPES_MAP,
    TOOL_SCOPES_MAP,
    get_scopes_for_tools,
)
from gartifacts import artifacts_config

ORIGINAL_SERVICES = [
    "gmail",
    "drive",
    "calendar",
    "docs",
    "sheets",
    "chat",
    "forms",
    "slides",
    "tasks",
    "contacts",
    "search",
    "appscript",
]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in (
        artifacts_config.ARTIFACTS_ENV,
        artifacts_config.VIEWER_ENV,
        artifacts_config.VIEWER_URL_ENV,
    ):
        monkeypatch.delenv(name, raising=False)


def test_default_services_are_unchanged_without_the_switch():
    assert main.default_services(main.SERVICE_MODULES) == ORIGINAL_SERVICES


def test_switch_adds_the_service(monkeypatch):
    monkeypatch.setenv(artifacts_config.ARTIFACTS_ENV, "true")
    assert main.default_services(main.SERVICE_MODULES) == ORIGINAL_SERVICES + [
        "artifacts"
    ]


def test_service_is_selectable_by_name():
    assert "artifacts" in main.VALID_SERVICES
    assert main.SERVICE_MODULES["artifacts"] == "gartifacts.artifacts_tools"
    assert main.OPT_IN_SERVICES == {"artifacts"}


def test_tier_suggestions_exclude_the_service_by_default():
    assert "artifacts" not in main.default_services(["drive", "artifacts"])


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "maybe"])
def test_falsy_values_do_not_enable(monkeypatch, value):
    monkeypatch.setenv(artifacts_config.ARTIFACTS_ENV, value)
    assert not artifacts_config.artifacts_enabled()
    assert main.default_services(main.SERVICE_MODULES) == ORIGINAL_SERVICES


def test_viewer_switch_is_separate(monkeypatch):
    monkeypatch.setenv(artifacts_config.VIEWER_ENV, "1")
    assert artifacts_config.viewer_enabled()
    assert not artifacts_config.artifacts_enabled()


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("https://artifacts.example.com", "https://artifacts.example.com"),
        ("https://artifacts.example.com/", "https://artifacts.example.com"),
        ("http://localhost:8000", "http://localhost:8000"),
        ("", None),
        ("artifacts.example.com", None),
        ("ftp://artifacts.example.com", None),
        ("https://artifacts.example.com/a/b", None),
        ("https://artifacts.example.com?x=1", None),
    ],
)
def test_viewer_base_url(monkeypatch, raw, expected):
    monkeypatch.setenv(artifacts_config.VIEWER_URL_ENV, raw)
    assert artifacts_config.viewer_base_url() == expected


def test_artifacts_needs_only_drive_read_scope():
    assert TOOL_SCOPES_MAP["artifacts"] == [DRIVE_READONLY_SCOPE]
    assert TOOL_READONLY_SCOPES_MAP["artifacts"] == [DRIVE_READONLY_SCOPE]
    scopes = get_scopes_for_tools(["artifacts"])
    assert DRIVE_READONLY_SCOPE in scopes
    assert not any("drive.file" in s or s.endswith("/auth/drive") for s in scopes)


def test_artifacts_permission_level():
    assert [name for name, _ in SERVICE_PERMISSION_LEVELS["artifacts"]] == ["readonly"]
    assert get_scopes_for_permission("artifacts", "readonly") == [DRIVE_READONLY_SCOPE]
    assert parse_permissions_arg(["artifacts:readonly"]) == {"artifacts": "readonly"}
    with pytest.raises(ValueError):
        parse_permissions_arg(["artifacts:full"])


def test_tool_tiers_list_the_tools():
    with open("core/tool_tiers.yaml", "r", encoding="utf-8") as handle:
        tiers = yaml.safe_load(handle)
    assert tiers["artifacts"]["core"] == ["register_artifact", "get_artifact_policy"]
