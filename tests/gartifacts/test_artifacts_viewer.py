import logging
import os
import time
from types import SimpleNamespace

import pytest

from gartifacts import artifacts_viewer as viewer
from gartifacts.artifacts_config import (
    APP_MARKER,
    BRANDING_FILE_ENV,
    CONTENT_URL_ENV,
    ROUTE_MARKER_HEADER,
    VIEWER_URL_ENV,
)
from gartifacts.artifacts_drive import SHORTCUT_MIME
from gartifacts.artifacts_tokens import verify_content_token
from tests.gartifacts.fake_drive import ROOT_ID
from tests.gartifacts.viewer_helpers import (
    APP,
    CONTENT,
    CONTENT_ORIGIN,
    FRAME,
    OWNER,
    VIEWER_ORIGIN,
    make_bundle,
    token_for,
)


class TestViewerPage:
    def test_requires_the_route_marker(self, world):
        _, record = make_bundle(world)
        response = world.client.get(
            f"/a/{record.hash}-x/", headers={"x-test-user": OWNER}
        )
        assert response.status_code == 404

    def test_requires_identity(self, world):
        _, record = make_bundle(world)
        response = world.client.get(
            f"/a/{record.hash}-x/", headers={ROUTE_MARKER_HEADER: APP_MARKER}
        )
        assert response.status_code == 401

    @pytest.mark.parametrize("segment", ["zzzzzzzz-x", "not-a-hash", "k7f3q9xm"])
    def test_unknown_links_look_unavailable(self, world, segment):
        make_bundle(world)
        response = world.client.get(f"/a/{segment}/", headers=APP)
        assert response.status_code == 404
        assert "do not have access" in response.text

    def test_sends_new_users_to_consent_and_back(self, world):
        _, record = make_bundle(world)
        world.consent = True
        link = f"/a/{record.hash}-q3-board-deck/page2.html"
        response = world.client.get(link, headers=APP)
        assert response.status_code == 302
        assert response.headers["location"] == "https://accounts.example/auth?x=" + link

    def test_denied_when_drive_refuses(self, world):
        bundle, record = make_bundle(world)
        del world.drive.items[bundle]
        response = world.client.get(f"/a/{record.hash}-q3-board-deck/", headers=APP)
        assert response.status_code == 404

    def test_denied_when_folder_is_trashed(self, world):
        bundle, record = make_bundle(world)
        world.drive.items[bundle]["trashed"] = True
        response = world.client.get(f"/a/{record.hash}-q3-board-deck/", headers=APP)
        assert response.status_code == 404

    def test_serves_the_shell(self, world):
        _, record = make_bundle(world)
        response = world.client.get(f"/a/{record.hash}-q3-board-deck/", headers=APP)
        assert response.status_code == 200
        text = response.text
        assert "CAUTION" in text and "Never enter passwords or secrets here." in text
        assert "has not been reviewed" in text
        assert 'aria-label="Reload"' in text and 'aria-label="Save as PDF"' in text
        assert "<header class=hdr>" in text and "id=reload" in text and "id=pdf" in text
        assert 'sandbox="allow-scripts allow-modals"' in text
        assert "allow-same-origin" not in text
        assert f"{CONTENT_ORIGIN}/c/" in text
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["referrer-policy"] == "no-referrer"
        csp = response.headers["content-security-policy"]
        assert f"frame-src {CONTENT_ORIGIN}" in csp and "frame-ancestors 'none'" in csp
        token = text.split("/c/")[1].split("/")[0]
        claims = verify_content_token(token)
        assert (claims.artifact_hash, claims.email) == (record.hash, OWNER)

    def test_iframe_keeps_the_requested_path(self, world):
        _, record = make_bundle(world)
        response = world.client.get(
            f"/a/{record.hash}-q3-board-deck/sub/page%202.html", headers=APP
        )
        assert "/sub/page%202.html" in response.text

    def test_reload_keeps_the_current_page(self, world):
        _, record = make_bundle(world)
        response = world.client.get(
            f"/a/{record.hash}-q3-board-deck/sub/page2.html", headers=APP
        )
        assert f'href="/a/{record.hash}-q3-board-deck/sub/page2.html"' in response.text

    def test_title_is_escaped(self, world):
        bundle, record = make_bundle(world)
        world.drive.items[bundle]["name"] = "20261022-1815-<script>alert(1)</script>"
        response = world.client.get(f"/a/{record.hash}-q3-board-deck/", headers=APP)
        canonical = response.headers["location"]
        response = world.client.get(canonical, headers=APP)
        assert "<script>alert" not in response.text
        assert "&lt;script&gt;" in response.text

    def test_wrong_slug_redirects_to_canonical_after_access_check(self, world):
        _, record = make_bundle(world)
        response = world.client.get(
            f"/a/{record.hash}-wrong/page2.html?x=1", headers=APP
        )
        assert response.status_code == 302
        assert response.headers["location"] == (
            f"/a/{record.hash}-q3-board-deck/page2.html?x=1"
        )

    def test_slug_is_not_revealed_to_users_without_access(self, world):
        bundle, record = make_bundle(world)
        del world.drive.items[bundle]
        response = world.client.get(f"/a/{record.hash}-wrong/", headers=APP)
        assert response.status_code == 404
        assert "location" not in response.headers
        assert "q3" not in response.text

    def test_rename_follows_drive_and_old_links_redirect(self, world):
        bundle, record = make_bundle(world)
        world.drive.items[bundle]["name"] = "20261023-0900-annual-plan"
        response = world.client.get(f"/a/{record.hash}-q3-board-deck/", headers=APP)
        assert response.status_code == 302
        assert response.headers["location"] == f"/a/{record.hash}-annual-plan/"
        assert world.registry.get_by_hash(record.hash).title == "annual plan"

    def test_missing_slash_redirects(self, world):
        response = world.client.get("/a/k7f3q9xm-x", headers=APP)
        assert response.status_code == 308
        assert response.headers["location"] == "/a/k7f3q9xm-x/"

    def test_not_configured_without_content_url(self, world, monkeypatch):
        _, record = make_bundle(world)
        monkeypatch.delenv(CONTENT_URL_ENV)
        response = world.client.get(f"/a/{record.hash}-q3-board-deck/", headers=APP)
        assert response.status_code == 500


class TestContent:
    def test_requires_the_route_marker(self, world):
        _, record = make_bundle(world)
        response = world.client.get(
            f"/c/{token_for(record)}/", headers={"sec-fetch-dest": "iframe"}
        )
        assert response.status_code == 404

    @pytest.mark.parametrize("token", ["", "garbage", "x" * 100])
    def test_bad_token(self, world, token):
        response = world.client.get(f"/c/{token or 'x'}/index.html", headers=FRAME)
        assert response.status_code == 403
        assert "expired" in response.text

    def test_expired_token(self, world):
        _, record = make_bundle(world)
        token = token_for(record, ttl_seconds=60, now=1000.0)
        response = world.client.get(f"/c/{token}/index.html", headers=FRAME)
        assert response.status_code == 403

    def test_serves_index_by_default_with_security_headers(self, world):
        _, record = make_bundle(world)
        response = world.client.get(f"/c/{token_for(record)}/", headers=FRAME)
        assert response.status_code == 200
        assert response.content.startswith(b"<h1>hi</h1><script>")
        assert response.headers["content-type"] == "text/html; charset=utf-8"
        csp = response.headers["content-security-policy"]
        assert "sandbox allow-scripts allow-modals" in csp
        assert f"frame-ancestors {VIEWER_ORIGIN}" in csp
        assert "connect-src 'self' data: blob:" in csp
        assert "report-uri /csp-report" in csp
        assert response.headers["access-control-allow-origin"] == "*"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert "content-disposition" not in response.headers

    @pytest.mark.parametrize("dest", ["", "document", "frame", "embed", "object"])
    def test_unframed_navigation_goes_to_the_viewer(self, world, dest):
        _, record = make_bundle(world)
        headers = {**CONTENT, **({"sec-fetch-dest": dest} if dest else {})}
        response = world.client.get(
            f"/c/{token_for(record)}/page.html", headers=headers
        )
        assert response.status_code == 302
        assert response.headers["location"] == (
            f"{VIEWER_ORIGIN}/a/{record.hash}-q3-board-deck/page.html"
        )

    @pytest.mark.parametrize(
        "dest", ["script", "style", "image", "font", "empty", "worker"]
    )
    def test_subresources_are_allowed_from_the_sandbox(self, world, dest):
        _, record = make_bundle(world, {"a.js": b"1"})
        headers = {**CONTENT, "sec-fetch-dest": dest, "sec-fetch-site": "cross-site"}
        response = world.client.get(f"/c/{token_for(record)}/a.js", headers=headers)
        assert response.status_code == 200

    def test_link_navigation_inside_the_frame_is_allowed(self, world):
        _, record = make_bundle(world, {"page2.html": b"two"})
        headers = {
            **CONTENT,
            "sec-fetch-dest": "iframe",
            "sec-fetch-site": "cross-site",
        }
        response = world.client.get(
            f"/c/{token_for(record)}/page2.html", headers=headers
        )
        assert response.status_code == 200 and response.content.startswith(b"two")

    def test_unframed_without_viewer_url_is_refused(self, world, monkeypatch):
        _, record = make_bundle(world)
        monkeypatch.delenv(VIEWER_URL_ENV)
        response = world.client.get(f"/c/{token_for(record)}/", headers=CONTENT)
        assert response.status_code == 403

    @pytest.mark.parametrize(
        "name,expected,download",
        [
            ("app.css", "text/css; charset=utf-8", False),
            ("app.js", "text/javascript; charset=utf-8", False),
            ("data.json", "application/json; charset=utf-8", False),
            ("logo.svg", "image/svg+xml", False),
            ("pic.png", "image/png", False),
            ("font.woff2", "font/woff2", False),
            ("notes.xyz", "application/octet-stream", True),
            ("page.exe", "application/octet-stream", True),
        ],
    )
    def test_content_type_comes_from_the_extension(
        self, world, name, expected, download
    ):
        _, record = make_bundle(world, {name: b"x"})
        response = world.client.get(f"/c/{token_for(record)}/{name}", headers=FRAME)
        assert response.status_code == 200
        assert response.headers["content-type"] == expected
        assert ("content-disposition" in response.headers) == download

    def test_nested_paths(self, world):
        bundle, record = make_bundle(world)
        assets = world.drive.folder("assets", bundle)
        world.drive.add("app.css", assets, content=b"body{}")
        response = world.client.get(
            f"/c/{token_for(record)}/assets/app.css", headers=FRAME
        )
        assert response.content == b"body{}"

    @pytest.mark.parametrize(
        "path", ["missing.html", "assets", "%2e%2e/secret", "index.html/x"]
    )
    def test_missing_and_unsafe_paths(self, world, path):
        bundle, record = make_bundle(world)
        world.drive.folder("assets", bundle)
        world.drive.add("secret", ROOT_ID, content=b"top secret")
        response = world.client.get(f"/c/{token_for(record)}/{path}", headers=FRAME)
        assert response.status_code == 404
        assert b"top secret" not in response.content

    def test_duplicates_shortcuts_and_google_files_are_not_served(self, world):
        bundle, record = make_bundle(world)
        world.drive.add("dup.txt", bundle, content=b"a")
        world.drive.add("dup.txt", bundle, content=b"b")
        world.drive.add("link", bundle, mime=SHORTCUT_MIME)
        world.drive.add("doc", bundle, mime="application/vnd.google-apps.document")
        for name in ("dup.txt", "link", "doc"):
            response = world.client.get(f"/c/{token_for(record)}/{name}", headers=FRAME)
            assert response.status_code == 404, name

    def test_trashed_bundle_is_not_served(self, world):
        bundle, record = make_bundle(world)
        world.drive.items[bundle]["trashed"] = True
        response = world.client.get(f"/c/{token_for(record)}/", headers=FRAME)
        assert response.status_code == 404

    def test_expired_google_grant_asks_to_reload(self, world):
        _, record = make_bundle(world)
        world.consent = True
        response = world.client.get(f"/c/{token_for(record)}/", headers=FRAME)
        assert response.status_code == 401
        assert "Reload" in response.text

    def test_oversized_file(self, world):
        bundle, record = make_bundle(world)
        world.drive.add("big.bin", bundle, size=50_000_000)
        response = world.client.get(f"/c/{token_for(record)}/big.bin", headers=FRAME)
        assert response.status_code == 413

    def test_unknown_hash_in_token(self, world):
        other = SimpleNamespace(hash="zzzzzzzz")
        response = world.client.get(f"/c/{token_for(other)}/", headers=FRAME)
        assert response.status_code == 404

    def test_missing_slash_redirects(self, world):
        response = world.client.get("/c/sometoken", headers=CONTENT)
        assert response.status_code == 308
        assert response.headers["location"] == "/c/sometoken/"


class TestCspReport:
    def test_logs_scrubbed_report(self, world, caplog):
        body = {
            "csp-report": {
                "document-uri": "https://content.example.com/c/SECRETTOKEN/index.html",
                "violated-directive": "connect-src",
                "blocked-uri": "https://evil.example.com",
            }
        }
        with caplog.at_level(logging.INFO, logger="gartifacts.access"):
            response = world.client.post("/csp-report", json=body, headers=CONTENT)
        assert response.status_code == 204
        assert "SECRETTOKEN" not in caplog.text
        assert "csp_violation" in caplog.text and "<token>" in caplog.text

    def test_requires_marker_and_ignores_junk(self, world):
        assert world.client.post("/csp-report", json={}).status_code == 404
        assert (
            world.client.post(
                "/csp-report", content=b"not json", headers=CONTENT
            ).status_code
            == 204
        )
        assert (
            world.client.post(
                "/csp-report", content=b"x" * 20000, headers=CONTENT
            ).status_code
            == 204
        )

    def test_rate_limited(self, world, caplog):
        with caplog.at_level(logging.INFO, logger="gartifacts.access"):
            for _ in range(viewer._REPORT_PER_MINUTE + 20):
                world.client.post(
                    "/csp-report",
                    json={"csp-report": {"blocked-uri": "x"}},
                    headers=CONTENT,
                )
        assert caplog.text.count("csp_violation") == viewer._REPORT_PER_MINUTE


class TestPrincipal:
    @pytest.mark.asyncio
    async def test_needs_gateway_mode_and_header(self, monkeypatch):
        config = SimpleNamespace(
            trust_gateway_identity=False, gateway_identity_header="x-assert"
        )
        monkeypatch.setattr(viewer, "get_oauth_config", lambda: config)
        monkeypatch.setattr(
            viewer,
            "extract_email_from_assertion",
            lambda token: token + "@scientist.com",
        )
        request = SimpleNamespace(headers={"x-assert": "bob"})
        assert await viewer._principal(request) is None
        config.trust_gateway_identity = True
        assert await viewer._principal(request) == "bob@scientist.com"
        assert await viewer._principal(SimpleNamespace(headers={})) is None


def test_log_events_are_json_and_carry_no_token(world, caplog):
    _, record = make_bundle(world)
    with caplog.at_level(logging.INFO, logger="gartifacts.access"):
        world.client.get(f"/a/{record.hash}-q3-board-deck/", headers=APP)
        world.client.get(f"/c/{token_for(record)}/", headers=FRAME)
    assert (
        '"outcome": "allowed"' in caplog.text and '"outcome": "served"' in caplog.text
    )
    assert token_for(record)[:20] not in caplog.text


class TestShellFeatures:
    def test_first_visit_notice_is_a_modal_with_a_30_day_cookie(self, world):
        _, record = make_bundle(world)
        text = world.client.get(f"/a/{record.hash}-q3-board-deck/", headers=APP).text
        assert "id=ack" in text and "I understand" in text
        assert '"artifacts_notice"' in text and "Max-Age=2592000" in text
        assert "SameSite=Lax" in text and "Domain=" not in text

    def test_title_links_to_the_first_page(self, world):
        _, record = make_bundle(world)
        text = world.client.get(
            f"/a/{record.hash}-q3-board-deck/page2.html", headers=APP
        ).text
        assert f'<a class=title href="/a/{record.hash}-q3-board-deck/"' in text

    def test_script_runs_only_with_the_nonce(self, world):
        _, record = make_bundle(world)
        response = world.client.get(f"/a/{record.hash}-q3-board-deck/", headers=APP)
        csp = response.headers["content-security-policy"]
        nonce = csp.split("script-src 'nonce-")[1].split("'")[0]
        assert f'<script nonce="{nonce}">' in response.text
        assert "unsafe-eval" not in csp and "script-src 'unsafe-inline'" not in csp

    def test_shell_is_given_the_canonical_link_for_the_address_bar(self, world):
        _, record = make_bundle(world)
        response = world.client.get(f"/a/{record.hash}-old-slug/", headers=APP)
        assert response.status_code == 302
        shell = world.client.get(response.headers["location"], headers=APP)
        assert f'base="/a/{record.hash}-q3-board-deck"' in shell.text

    def test_tracker_is_added_to_html_only(self, world):
        _, record = make_bundle(
            world,
            {"a.css": b"body{}", "b.js": b"1", "c.html": b"<body>x</body>"},
        )
        token = token_for(record)
        css = world.client.get(f"/c/{token}/a.css", headers=FRAME)
        js = world.client.get(f"/c/{token}/b.js", headers=FRAME)
        page = world.client.get(f"/c/{token}/c.html", headers=FRAME)
        assert css.content == b"body{}" and js.content == b"1"
        assert page.content.startswith(b"<body>x<script>")
        assert page.content.endswith(b"</script></body>")
        assert VIEWER_ORIGIN.encode() in page.content

    def test_tracker_skipped_without_a_viewer_url(self, world, monkeypatch):
        _, record = make_bundle(world)
        monkeypatch.delenv(VIEWER_URL_ENV)
        response = world.client.get(f"/c/{token_for(record)}/", headers=FRAME)
        assert b"<script>" not in response.content

    def _link(self, world):
        _, record = make_bundle(world)
        return f"/a/{record.hash}-q3-board-deck/"

    def _branding(self, monkeypatch, tmp_path, text):
        path = tmp_path / "branding.yaml"
        path.write_text(text)
        monkeypatch.setenv(BRANDING_FILE_ENV, str(path))
        return path

    def test_defaults_without_a_branding_file(self, world):
        text = world.client.get(self._link(world), headers=APP).text
        assert "<span class=brand>Artifacts</span>" in text and "#1f3a5f" in text
        assert "class=logo" not in text and "rel=icon" not in text
        assert world.client.get("/a/_icon", headers=APP).status_code == 404

    def test_branding_file_sets_name_colour_wording_and_icon(
        self, world, monkeypatch, tmp_path
    ):
        (tmp_path / "logo.png").write_bytes(b"\x89PNG fake")
        self._branding(
            monkeypatch,
            tmp_path,
            "name: Acme Artifacts\ncolor: '#1F346F'\nicon: logo.png\n"
            "notice:\n  - 'Ask <a href=\"https://x.example\" target=_blank>help</a>.'\n"
            "about:\n  - Shared inside Acme only.\n  - 'Slack #support-ai'\n",
        )
        link = self._link(world)
        text = world.client.get(link, headers=APP).text
        assert "Acme Artifacts" in text and "#1F346F" in text
        assert '<a href="https://x.example" target=_blank>help</a>' in text
        assert "Shared inside Acme only." in text and "Slack #support-ai" in text
        assert "has not been reviewed" not in text
        assert "Never enter passwords or secrets here." in text
        assert "class=logo" in text and "rel=icon" in text
        icon = world.client.get("/a/_icon", headers=APP)
        assert (
            icon.content == b"\x89PNG fake"
            and icon.headers["content-type"] == "image/png"
        )
        assert "sandbox" in icon.headers["content-security-policy"]
        assert world.client.get("/a/_icon", headers=CONTENT).status_code == 404

    def test_config_html_cannot_run_script(self, world, monkeypatch, tmp_path):
        self._branding(
            monkeypatch, tmp_path, "notice:\n  - '<img src=x onerror=alert(1)>'\n"
        )
        response = world.client.get(self._link(world), headers=APP)
        csp = response.headers["content-security-policy"]
        assert "script-src 'nonce-" in csp and "unsafe-inline'" not in csp.split(";")[1]

    def test_notice_days_zero_turns_the_modal_off(self, world, monkeypatch, tmp_path):
        self._branding(monkeypatch, tmp_path, "notice_days: 0\n")
        text = world.client.get(self._link(world), headers=APP).text
        assert "id=ack" not in text and "I understand" not in text

    def test_notice_days_sets_the_cookie_lifetime(self, world, monkeypatch, tmp_path):
        self._branding(monkeypatch, tmp_path, "notice_days: 7\n")
        text = world.client.get(self._link(world), headers=APP).text
        assert "Max-Age=604800" in text

    @pytest.mark.parametrize(
        "text",
        [
            "name: [not, a, string]\ncolor: red;}</style>\nnotice_days: -3\n"
            "notice: 42\nabout: [1, 2]\n",
            "just a string",
            ": : bad yaml : [",
        ],
    )
    def test_bad_branding_falls_back_to_defaults(
        self, world, monkeypatch, tmp_path, text
    ):
        self._branding(monkeypatch, tmp_path, text)
        page = world.client.get(self._link(world), headers=APP)
        assert page.status_code == 200
        assert "<span class=brand>Artifacts</span>" in page.text
        assert "#1f3a5f" in page.text and "red;}" not in page.text

    def test_missing_branding_file_falls_back(self, world, monkeypatch, tmp_path):
        monkeypatch.setenv(BRANDING_FILE_ENV, str(tmp_path / "absent.yaml"))
        assert world.client.get(self._link(world), headers=APP).status_code == 200

    def test_branding_is_reread_when_the_file_changes(
        self, world, monkeypatch, tmp_path
    ):
        path = self._branding(monkeypatch, tmp_path, "name: One\n")
        link = self._link(world)
        assert (
            "<span class=brand>One</span>" in world.client.get(link, headers=APP).text
        )
        path.write_text("name: Two\n")
        os.utime(path, (time.time() + 5, time.time() + 5))
        assert (
            "<span class=brand>Two</span>" in world.client.get(link, headers=APP).text
        )

    def test_header_has_about_share_reload_and_pdf(self, world):
        text = world.client.get(self._link(world), headers=APP).text
        assert "id=about-open class=brandbtn" in text and "id=about role=dialog" in text
        assert "id=about-link" in text
        assert 'aria-label="Copy link"' in text and "id=share" in text
        assert "Link copied. People also need access to the Drive folder." in text

    def test_denied_page_offers_drive_access_request(self, world):
        bundle, record = make_bundle(world)
        del world.drive.items[bundle]
        denied = world.client.get(f"/a/{record.hash}-q3-board-deck/", headers=APP)
        assert denied.status_code == 404
        assert f"https://drive.google.com/drive/folders/{bundle}" in denied.text
        assert "Request access in Google Drive" in denied.text
        unknown = world.client.get("/a/zzzzzzzz-x/", headers=APP)
        assert unknown.status_code == 404 and "drive.google.com" not in unknown.text
