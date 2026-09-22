"""Signed download URLs: the token is the authorization, so these pin the security
properties — required claims, rejection of anything not minted here, a TTL that
never outlives the credential, the route touching Google only after verification
and only with the token owner's credentials, and bounded-memory Drive streaming.
"""

import time
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import jwt
import pytest

import core.signed_downloads as sd

USER = "user@example.com"


@pytest.fixture(autouse=True)
def signing_material(monkeypatch):
    monkeypatch.setenv(
        "GOOGLE_OAUTH_CLIENT_SECRET", "client-secret-with-enough-entropy"
    )
    monkeypatch.delenv("FASTMCP_SERVER_AUTH_GOOGLE_JWT_SIGNING_KEY", raising=False)
    monkeypatch.setenv("WORKSPACE_EXTERNAL_URL", "https://mcp.example.com/")
    sd._signing_key.cache_clear()
    yield
    sd._signing_key.cache_clear()


def _mint(**overrides):
    kwargs = dict(
        source="gmail", user_email=USER, ref={"mid": "m1", "aid": "a1"}, ttl_seconds=60
    )
    kwargs.update(overrides)
    return sd.mint_url(**kwargs)


def _token(url: str) -> str:
    return url.rsplit("/", 1)[1]


class TestEnabledFlag:
    def test_off_by_default(self, monkeypatch):
        monkeypatch.delenv("WORKSPACE_MCP_SIGNED_ATTACHMENT_URLS", raising=False)
        assert sd.enabled() is False
        assert sd.offer_url(USER, source="gmail", ref={}) is None

    def test_on_when_true(self, monkeypatch):
        monkeypatch.setenv("WORKSPACE_MCP_SIGNED_ATTACHMENT_URLS", "true")
        assert sd.enabled() is True


class TestToken:
    def test_round_trip_carries_ref_owner_and_names(self):
        url = _mint(
            source="drive",
            ref={"fid": "F1", "emt": "application/pdf"},
            filename="Report.pdf",
            mime_type="application/pdf",
        )
        assert url.startswith("https://mcp.example.com/attachments/signed/")
        claims = sd.verify_token(_token(url))
        assert claims["src"] == "drive" and claims["sub"] == USER
        assert claims["fid"] == "F1" and claims["emt"] == "application/pdf"
        assert claims["fn"] == "Report.pdf" and claims["mt"] == "application/pdf"
        assert claims["exp"] - claims["iat"] == 60

    def test_tampered_token_rejected(self):
        token = _token(_mint())
        header, payload, sig = token.split(".")
        assert sd.verify_token(f"{header}.{payload}x.{sig}") is None
        assert sd.verify_token(token + "x") is None
        assert sd.verify_token("not.a.jwt") is None

    def test_wrong_key_rejected(self, monkeypatch):
        token = _token(_mint())
        monkeypatch.setenv(
            "GOOGLE_OAUTH_CLIENT_SECRET", "a-different-client-secret-value"
        )
        sd._signing_key.cache_clear()
        assert sd.verify_token(token) is None

    def test_expired_token_rejected(self):
        assert sd.verify_token(_token(_mint(ttl_seconds=-1))) is None

    @pytest.mark.parametrize("missing", ["exp", "sub", "iat"])
    def test_correctly_signed_token_missing_a_required_claim_rejected(self, missing):
        claims = {
            "src": "gmail",
            "sub": USER,
            "iat": int(time.time()),
            "exp": int(time.time()) + 60,
        }
        del claims[missing]
        token = jwt.encode(claims, sd._signing_key(), algorithm="HS256")
        assert sd.verify_token(token) is None

    def test_ref_cannot_override_reserved_claims(self):
        with pytest.raises(ValueError, match="reserved"):
            _mint(ref={"fid": "F", "sub": "attacker@example.com"})

    def test_key_override_takes_precedence_over_client_secret(self, monkeypatch):
        monkeypatch.setenv(
            "FASTMCP_SERVER_AUTH_GOOGLE_JWT_SIGNING_KEY", "override-material"
        )
        sd._signing_key.cache_clear()
        token = _token(_mint())
        assert sd.verify_token(token) is not None
        monkeypatch.delenv("FASTMCP_SERVER_AUTH_GOOGLE_JWT_SIGNING_KEY")
        sd._signing_key.cache_clear()
        # Key now derived from the client secret: the override-signed token must fail.
        assert sd.verify_token(token) is None

    def test_no_key_material_fails_closed(self, monkeypatch):
        monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_SECRET")
        sd._signing_key.cache_clear()
        from auth.oauth_config import get_oauth_config

        monkeypatch.setattr(get_oauth_config(), "client_secret", None)
        with pytest.raises(RuntimeError):
            _mint()
        assert sd.verify_token("x.y.z") is None


class TestTtl:
    NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)

    def _expiry(self, seconds):
        return (self.NOW + timedelta(seconds=seconds)).replace(tzinfo=None)  # naive UTC

    def test_unknown_expiry_uses_default(self):
        assert sd.clamp_ttl(None) == sd.URL_TTL_SECONDS

    def test_far_expiry_capped_at_default(self):
        assert sd.clamp_ttl(self._expiry(3600), now=self.NOW) == sd.URL_TTL_SECONDS

    def test_near_expiry_clamped_with_margin(self):
        assert sd.clamp_ttl(self._expiry(300), now=self.NOW) == 270

    @pytest.mark.parametrize("seconds", [-120, 0, 20])
    def test_expired_or_inside_margin_is_non_positive(self, seconds):
        assert sd.clamp_ttl(self._expiry(seconds), now=self.NOW) <= 0

    def test_url_never_outlives_credential(self):
        for secs in (60, 120, 600, 3600):
            assert sd.clamp_ttl(self._expiry(secs), now=self.NOW) <= secs

    def test_format_ttl(self):
        assert sd.format_ttl(45) == "45 seconds"
        assert sd.format_ttl(270) == "~4 minutes"
        assert sd.format_ttl(900) == "~15 minutes"


class TestOfferUrl:
    """The tool-side gate: a URL is only issued when the route can serve it, and the
    TTL the caller shows is the real (clamped) one."""

    def _creds(self, expiry):
        return Mock(expiry=expiry)

    def test_mints_with_clamped_ttl(self, monkeypatch):
        monkeypatch.setenv("WORKSPACE_MCP_SIGNED_ATTACHMENT_URLS", "true")
        expiry = datetime.utcnow() + timedelta(seconds=300)
        monkeypatch.setattr(
            sd, "_session_credentials", lambda email: self._creds(expiry)
        )

        url, ttl = sd.offer_url(USER, source="gmail", ref={"mid": "m", "aid": "a"})

        assert 265 <= ttl <= 270  # 300 s left minus the 30 s margin
        assert sd.verify_token(_token(url))["sub"] == USER

    def test_none_without_recoverable_credentials(self, monkeypatch):
        monkeypatch.setenv("WORKSPACE_MCP_SIGNED_ATTACHMENT_URLS", "true")
        monkeypatch.setattr(sd, "_session_credentials", lambda email: None)
        assert sd.offer_url(USER, source="gmail", ref={}) is None

    def test_none_when_token_too_near_expiry(self, monkeypatch):
        monkeypatch.setenv("WORKSPACE_MCP_SIGNED_ATTACHMENT_URLS", "true")
        soon = datetime.utcnow() + timedelta(seconds=10)
        monkeypatch.setattr(sd, "_session_credentials", lambda email: self._creds(soon))
        assert sd.offer_url(USER, source="gmail", ref={}) is None

    def test_none_when_no_key_can_be_derived(self, monkeypatch):
        monkeypatch.setenv("WORKSPACE_MCP_SIGNED_ATTACHMENT_URLS", "true")
        monkeypatch.setattr(sd, "_session_credentials", lambda email: self._creds(None))
        monkeypatch.setattr(
            sd, "_signing_key", Mock(side_effect=RuntimeError("no key"))
        )
        assert sd.offer_url(USER, source="gmail", ref={}) is None


class TestServe:
    @pytest.fixture
    def collaborators(self, monkeypatch):
        seen = {}

        async def fetcher(claims, credentials):
            seen["claims"], seen["credentials"] = claims, credentials
            return sd.DownloadResult(
                filename='rep"ort\r\n.pdf',
                media_type="application/pdf",
                content=b"%PDF-1.3",
            )

        creds = Mock(valid=True)

        def session_credentials(email):
            seen.setdefault("emails", []).append(email)
            return creds if email == USER else None

        monkeypatch.setitem(sd._FETCHERS, "gmail", fetcher)
        monkeypatch.setattr(sd, "_session_credentials", session_credentials)
        seen["creds"] = creds
        return seen

    @pytest.mark.asyncio
    async def test_streams_with_the_token_owners_credentials(self, collaborators):
        response = await sd.serve(_token(_mint()))

        assert response.status_code == 200
        assert response.body == b"%PDF-1.3"
        assert collaborators["emails"] == [USER]
        assert collaborators["credentials"] is collaborators["creds"]
        assert collaborators["claims"]["mid"] == "m1"
        disposition = response.headers["content-disposition"]
        assert "\r" not in disposition and "\n" not in disposition
        assert 'filename="report.pdf"' in disposition
        assert "filename*=UTF-8''rep%22ort%0D%0A.pdf" in disposition

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", ["garbage", "a.b.c", ""])
    async def test_invalid_token_is_403_before_any_lookup(self, collaborators, bad):
        response = await sd.serve(bad)
        assert response.status_code == 403
        assert "emails" not in collaborators and "claims" not in collaborators

    @pytest.mark.asyncio
    async def test_expired_token_is_403(self, collaborators):
        assert (await sd.serve(_token(_mint(ttl_seconds=-5)))).status_code == 403
        assert "claims" not in collaborators

    @pytest.mark.asyncio
    async def test_unknown_source_is_403(self, collaborators):
        assert (await sd.serve(_token(_mint(source="ftp")))).status_code == 403

    @pytest.mark.asyncio
    async def test_owner_without_session_is_401_and_nothing_is_fetched(
        self, collaborators
    ):
        response = await sd.serve(_token(_mint(user_email="other@example.com")))
        assert response.status_code == 401
        assert collaborators["emails"] == ["other@example.com"]
        assert "claims" not in collaborators

    @pytest.mark.asyncio
    async def test_expired_credentials_are_401(self, collaborators):
        collaborators["creds"].valid = False
        assert (await sd.serve(_token(_mint()))).status_code == 401
        assert "claims" not in collaborators

    @pytest.mark.asyncio
    async def test_fetch_failure_is_502(self, collaborators, monkeypatch):
        async def failing(claims, credentials):
            raise sd.SignedDownloadError("boom")

        monkeypatch.setitem(sd._FETCHERS, "gmail", failing)
        assert (await sd.serve(_token(_mint()))).status_code == 502


class _FakeDownloader:
    """MediaIoBaseDownload stand-in: appends one chunk per next_chunk, never seeks."""

    def __init__(self, fh, request, chunksize):
        self._fh, self._payload, self._pos, self._cs = fh, request.payload, 0, chunksize

    def next_chunk(self):
        nxt = self._payload[self._pos : self._pos + self._cs]
        self._fh.write(nxt)
        self._pos += len(nxt)
        return "status", self._pos >= len(self._payload)


class _FakeFiles:
    def __init__(self, calls, payload):
        self.calls, self.payload = calls, payload

    # Mirrors the real client's signature so a kwarg the API rejects fails here too.
    def get_media(self, fileId, supportsAllDrives=False):
        self.calls.append(("get_media", fileId, supportsAllDrives))
        return self

    def export_media(self, fileId, mimeType):
        self.calls.append(("export_media", fileId, mimeType))
        return self


class TestDriveFetcher:
    PAYLOAD = bytes(range(256)) * 200  # 51,200 bytes
    CHUNK = 8192

    @pytest.fixture
    def drive(self, monkeypatch):
        import gdrive.drive_tools as drive_tools

        calls = []
        files = _FakeFiles(calls, self.PAYLOAD)
        monkeypatch.setattr(sd, "build", lambda *a, **k: Mock(files=lambda: files))
        monkeypatch.setattr(sd, "MediaIoBaseDownload", _FakeDownloader)
        monkeypatch.setattr(drive_tools, "DOWNLOAD_CHUNK_SIZE", self.CHUNK)
        return calls

    @pytest.mark.asyncio
    async def test_streams_bounded_chunks_that_reassemble_exactly(self, drive):
        result = await sd._fetch_drive(
            {"fid": "F", "fn": "v.mov", "mt": "video/quicktime"}, Mock()
        )

        assert result.content is None and result.stream is not None
        chunks = [c async for c in result.stream]
        assert b"".join(chunks) == self.PAYLOAD
        assert len(chunks) > 1 and max(map(len, chunks)) <= self.CHUNK
        assert result.filename == "v.mov" and result.media_type == "video/quicktime"

    @pytest.mark.asyncio
    async def test_get_media_supports_shared_drives(self, drive):
        """Without supportsAllDrives=True Drive 404s on shared-drive files, so a
        minted URL would 502 on every fetch while the non-signed path works."""
        result = await sd._fetch_drive({"fid": "SHARED"}, Mock())
        async for _ in result.stream:
            pass
        assert drive == [("get_media", "SHARED", True)]

    @pytest.mark.asyncio
    async def test_export_uses_export_media(self, drive):
        result = await sd._fetch_drive({"fid": "DOC", "emt": "application/pdf"}, Mock())
        async for _ in result.stream:
            pass
        assert drive == [("export_media", "DOC", "application/pdf")]
        assert result.media_type == "application/pdf"

    @pytest.mark.asyncio
    async def test_missing_fid_and_first_chunk_failure_raise(self, monkeypatch):
        with pytest.raises(sd.SignedDownloadError):
            await sd._fetch_drive({}, Mock())
        monkeypatch.setattr(sd, "build", lambda *a, **k: Mock())
        monkeypatch.setattr(
            sd,
            "MediaIoBaseDownload",
            Mock(return_value=Mock(next_chunk=Mock(side_effect=OSError("403")))),
        )
        with pytest.raises(sd.SignedDownloadError):
            await sd._fetch_drive({"fid": "F"}, Mock())


class TestGmailFetchers:
    @pytest.mark.asyncio
    async def test_attachment_bytes_and_signed_in_names(self, monkeypatch):
        gmail = Mock()
        gmail.users().messages().attachments().get().execute.return_value = {
            "data": "aGVsbG8"
        }
        monkeypatch.setattr(sd, "build", lambda *a, **k: gmail)

        result = await sd._fetch_gmail_attachment(
            {"mid": "m", "aid": "a", "fn": "hi.txt", "mt": "text/plain"}, Mock()
        )

        assert result.content == b"hello"
        assert (result.filename, result.media_type) == ("hi.txt", "text/plain")
        get = gmail.users().messages().attachments().get
        assert get.call_args.kwargs == {"userId": "me", "messageId": "m", "id": "a"}

    @pytest.mark.asyncio
    async def test_attachment_errors_become_download_errors(self, monkeypatch):
        gmail = Mock()
        gmail.users().messages().attachments().get().execute.return_value = {"data": ""}
        monkeypatch.setattr(sd, "build", lambda *a, **k: gmail)
        with pytest.raises(sd.SignedDownloadError):
            await sd._fetch_gmail_attachment({"mid": "m", "aid": "a"}, Mock())
        with pytest.raises(sd.SignedDownloadError):
            await sd._fetch_gmail_attachment({"mid": "m"}, Mock())

    @pytest.mark.asyncio
    async def test_message_export_eml_round_trip(self, monkeypatch):
        import base64

        raw = b"From: a@example.com\r\nSubject: hi\r\n\r\nbody\r\n"
        gmail = Mock()
        gmail.users().messages().get().execute.return_value = {
            "raw": base64.urlsafe_b64encode(raw).decode().rstrip("=")
        }
        monkeypatch.setattr(sd, "build", lambda *a, **k: gmail)

        result = await sd._fetch_gmail_message(
            {"mid": "m1", "fmt": "raw", "fn": "hi.eml"}, Mock()
        )

        assert result.content == raw
        assert (result.filename, result.media_type) == ("hi.eml", "message/rfc822")

    @pytest.mark.asyncio
    async def test_message_export_rejects_bad_claims(self):
        with pytest.raises(sd.SignedDownloadError):
            await sd._fetch_gmail_message({"fmt": "raw"}, Mock())
        with pytest.raises(sd.SignedDownloadError):
            await sd._fetch_gmail_message({"mid": "m", "fmt": "pdf"}, Mock())
