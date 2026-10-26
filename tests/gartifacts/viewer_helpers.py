"""Shared setup for the viewer tests."""

import contextlib
from types import SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from gartifacts import artifacts_registry, artifacts_viewer as viewer
from gartifacts.artifacts_config import (
    APP_MARKER,
    CONTENT_MARKER,
    CONTENT_URL_ENV,
    ROUTE_MARKER_HEADER,
    VIEWER_URL_ENV,
)
from gartifacts.artifacts_registry import SqliteArtifactRegistry
from gartifacts.artifacts_tokens import TOKEN_KEY_ENV, mint_content_token
from tests.gartifacts.fake_drive import FakeDrive

OWNER = "owner@scientist.com"
APP = {ROUTE_MARKER_HEADER: APP_MARKER, "x-test-user": OWNER}
CONTENT = {ROUTE_MARKER_HEADER: CONTENT_MARKER}
FRAME = {**CONTENT, "sec-fetch-dest": "iframe", "sec-fetch-site": "same-site"}
CONTENT_ORIGIN = "https://content.example.com"
VIEWER_ORIGIN = "https://artifacts.example.com"


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv(TOKEN_KEY_ENV, "a-test-secret-of-sufficient-length")
    monkeypatch.setenv(CONTENT_URL_ENV, CONTENT_ORIGIN)
    monkeypatch.setenv(VIEWER_URL_ENV, VIEWER_ORIGIN)
    monkeypatch.delenv("WORKSPACE_MCP_ARTIFACTS_CSP_PROFILE", raising=False)
    viewer._folder_ok.clear()
    viewer._report_times.clear()
    viewer._report_by_key.clear()


@pytest.fixture
def world(env, monkeypatch):
    drive = FakeDrive()
    registry = SqliteArtifactRegistry(":memory:")
    monkeypatch.setattr(artifacts_registry, "_registry", registry)
    state = SimpleNamespace(drive=drive, registry=registry, consent=False, users={})

    async def principal(request):
        return request.headers.get("x-test-user")

    async def drive_service(email):
        if state.consent:
            raise viewer.NeedsConsent()
        return state.drive

    monkeypatch.setattr(viewer, "_principal", principal)
    monkeypatch.setattr(viewer, "_drive_service", drive_service)
    monkeypatch.setattr(
        viewer, "recycling", lambda service: contextlib.nullcontext(service)
    )
    monkeypatch.setattr(
        viewer,
        "begin_consent",
        lambda email, path: "https://accounts.example/auth?x=" + path,
    )
    state.app = Starlette(routes=viewer.viewer_routes())
    state.client = TestClient(state.app, follow_redirects=False)
    return state


def make_bundle(world, files=None):
    bundle = world.drive.bundle()
    world.drive.add("index.html", bundle, mime="text/html", content=b"<h1>hi</h1>")
    for name, content in (files or {}).items():
        world.drive.add(name, bundle, content=content)
    record = world.registry.register(bundle, OWNER, "q3 board deck")
    return bundle, record


def token_for(record, email=OWNER, **kwargs):
    return mint_content_token(record.hash, email, **kwargs)
