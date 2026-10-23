"""The viewer routes behind the server's real Origin middleware."""

import pytest
from starlette.testclient import TestClient

from core.server import OriginValidationMiddleware
from tests.gartifacts.viewer_helpers import (
    APP,
    CONTENT,
    FRAME,
    make_bundle,
    token_for,
)


@pytest.fixture
def client(world, monkeypatch):
    monkeypatch.setenv("WORKSPACE_MCP_ARTIFACTS_VIEWER", "true")
    wrapped = OriginValidationMiddleware(world.app)
    return TestClient(wrapped, follow_redirects=False)


def test_sandboxed_frame_reads_content_with_null_origin(world, client):
    _, record = make_bundle(world, {"app.js": b"1"})
    headers = {**FRAME, "origin": "null", "sec-fetch-dest": "script"}
    response = client.get(f"/c/{token_for(record)}/app.js", headers=headers)
    assert response.status_code == 200


def test_null_origin_is_still_refused_elsewhere(world, client):
    _, record = make_bundle(world)
    assert (
        client.get(
            f"/a/{record.hash}-x/", headers={**APP, "origin": "null"}
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/csp-report", json={}, headers={**CONTENT, "origin": "null"}
        ).status_code
        == 403
    )


def test_content_origin_exemption_needs_the_viewer_switch(world, monkeypatch):
    monkeypatch.delenv("WORKSPACE_MCP_ARTIFACTS_VIEWER", raising=False)
    _, record = make_bundle(world)
    plain = TestClient(OriginValidationMiddleware(world.app), follow_redirects=False)
    response = plain.get(
        f"/c/{token_for(record)}/", headers={**FRAME, "origin": "null"}
    )
    assert response.status_code == 403
