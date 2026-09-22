"""Signed download URLs: the token is the authorization, so these pin the security
properties — required claims, rejection of anything not minted here (including
tokens from the server's other key families), a TTL that never outlives the
credentials, mint and serve agreeing on "usable" with REAL google-auth
credentials, the route touching Google only after verification and only with the
token owner's credentials (recovered read-only, refreshed in memory), hardened
response headers, and bounded-memory Drive streaming.
"""

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import h11
import jwt
import pytest
import requests
from fastmcp.server.auth.jwt_issuer import derive_jwt_key
from google.oauth2.credentials import Credentials

import core.signed_downloads as sd

USER = "user@example.com"
SECRET = "client-secret-with-enough-entropy"
TOKEN_URI = "https://oauth2.googleapis.com/token"


@pytest.fixture(autouse=True)
def signing_material(monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", SECRET)
    monkeypatch.delenv("FASTMCP_SERVER_AUTH_GOOGLE_JWT_SIGNING_KEY", raising=False)
    monkeypatch.setenv("WORKSPACE_EXTERNAL_URL", "https://mcp.example.com/")
    monkeypatch.setattr(sd, "get_transport_mode", lambda: "streamable-http")
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


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)  # naive UTC, as google-auth


def _credentials(seconds_left=3600, refresh_token=None, token="ya29.access"):
    """A real google-auth object, so ``.valid`` applies REFRESH_THRESHOLD for real."""
    return Credentials(
        token=token,
        refresh_token=refresh_token,
        token_uri=TOKEN_URI,
        client_id="client-id",
        client_secret="client-secret",
        scopes=["https://www.googleapis.com/auth/gmail.readonly"],
        expiry=_now() + timedelta(seconds=seconds_left),
    )


class _ReadOnlyStores:
    """Session-store and credential-store doubles that record and REFUSE writes."""

    def __init__(self, session=None, persistent=None):
        self.session, self.persistent, self.writes = session, persistent, []
        self.session_lookups, self.persistent_lookups = [], []

    # OAuth21SessionStore surface used by the module
    def get_credentials(self, email):
        self.session_lookups.append(email)
        return self.session.get(email) if self.session else None

    def store_session(self, **kwargs):
        self.writes.append(("store_session", kwargs))
        raise AssertionError("the signed route must not write to the session store")

    # CredentialStore surface used by the module
    def get_credential(self, email):
        self.persistent_lookups.append(email)
        return self.persistent.get(email) if self.persistent else None

    def store_credential(self, email, credentials):
        self.writes.append(("store_credential", email))
        raise AssertionError("the signed route must not write to the credential store")


@pytest.fixture
def stores(monkeypatch):
    """Install the doubles behind the real lookup helper (not patching the helper)."""
    doubles = _ReadOnlyStores()
    monkeypatch.setattr(
        "auth.oauth21_session_store.get_oauth21_session_store", lambda: doubles
    )
    monkeypatch.setattr("auth.credential_store.get_credential_store", lambda: doubles)
    monkeypatch.setattr("auth.oauth_config.is_stateless_mode", lambda: False)
    return doubles


@pytest.fixture
def fetcher(monkeypatch):
    """Records the credentials the route hands to Google."""
    seen = {}

    async def fake(claims, credentials):
        seen["claims"], seen["token"] = claims, credentials.token
        return sd.DownloadResult(
            filename="f.bin", media_type="application/octet-stream", content=b"ok"
        )

    monkeypatch.setitem(sd._FETCHERS, "gmail", fake)
    return seen


@pytest.fixture
def token_endpoint(monkeypatch):
    """Google's token endpoint, faked at the HTTP transport (requests.Session)."""
    calls = []
    outcome = {
        "status": 200,
        "body": {"access_token": "ya29.refreshed", "expires_in": 3600},
    }

    def fake_request(self, method, url, **kwargs):
        calls.append((method, url, kwargs.get("data")))
        response = requests.Response()
        response.status_code = outcome["status"]
        response._content = json.dumps(outcome["body"]).encode()
        response.headers["content-type"] = "application/json"
        return response

    monkeypatch.setattr(requests.Session, "request", fake_request)
    return calls, outcome


class TestEnabledFlag:
    def test_off_by_default(self, monkeypatch):
        monkeypatch.delenv(sd.FLAG_ENV, raising=False)
        assert sd.enabled() is False
        assert sd.offer_url(USER, source="gmail", ref={}) is None

    def test_on_when_true(self, monkeypatch):
        monkeypatch.setenv(sd.FLAG_ENV, "true")
        assert sd.enabled() is True

    def test_off_on_stdio_even_when_set(self, monkeypatch):
        """The stdio callback server does not mount the route; a local server hands
        out file paths instead."""
        monkeypatch.setenv(sd.FLAG_ENV, "true")
        monkeypatch.setattr(sd, "get_transport_mode", lambda: "stdio")
        assert sd.enabled() is False

    @pytest.mark.asyncio
    async def test_route_is_inert_when_disabled(self, monkeypatch):
        monkeypatch.setenv(sd.FLAG_ENV, "true")
        token = _token(_mint())
        monkeypatch.delenv(sd.FLAG_ENV)
        monkeypatch.setattr(sd, "_recover_credentials", Mock())
        response = await sd.serve(token)
        assert response.status_code == 404
        sd._recover_credentials.assert_not_called()


class TestStartupLog:
    def test_flag_on_stdio_logs_once_that_it_is_ignored(self, monkeypatch, caplog):
        monkeypatch.setenv(sd.FLAG_ENV, "true")
        with caplog.at_level(logging.WARNING, logger=sd.__name__):
            sd.log_if_ignored("stdio")
        notes = [r for r in caplog.records if "ignored" in r.getMessage()]
        assert len(notes) == 1
        assert sd.FLAG_ENV in notes[0].getMessage() and "stdio" in notes[0].getMessage()

    @pytest.mark.parametrize(
        "flag, transport", [(None, "stdio"), ("true", "streamable-http")]
    )
    def test_otherwise_silent(self, monkeypatch, caplog, flag, transport):
        if flag is None:
            monkeypatch.delenv(sd.FLAG_ENV, raising=False)
        else:
            monkeypatch.setenv(sd.FLAG_ENV, flag)
        with caplog.at_level(logging.DEBUG, logger=sd.__name__):
            sd.log_if_ignored(transport)
        assert caplog.records == []


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

    def test_no_key_material_fails_closed(self, monkeypatch):
        monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_SECRET")
        sd._signing_key.cache_clear()
        from auth.oauth_config import get_oauth_config

        monkeypatch.setattr(get_oauth_config(), "client_secret", None)
        with pytest.raises(RuntimeError):
            _mint()
        assert sd.verify_token("x.y.z") is None


class TestSigningKey:
    """Which material signs (derived only — no key setting of its own), and that
    this route's key family is isolated from the server's other derived keys (the
    OAuth proxy's JWT key and storage key)."""

    OTHER_SALTS = ["fastmcp-jwt-signing-key", "fastmcp-storage-encryption-key"]

    def _claims(self):
        now = int(time.time())
        return {
            "src": "gmail",
            "sub": USER,
            "iat": now,
            "exp": now + 60,
            "mid": "m",
            "aid": "a",
        }

    def test_key_derives_from_the_client_secret_under_this_modules_salt(self):
        assert sd._signing_key() == derive_jwt_key(
            high_entropy_material=SECRET, salt=sd._KEY_SALT
        )

    def test_fastmcp_jwt_key_material_beats_the_client_secret(self, monkeypatch):
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

    @pytest.mark.parametrize("salt", OTHER_SALTS)
    def test_tokens_from_the_servers_other_key_families_are_rejected(self, salt):
        """Same client secret, the OAuth proxy's salts: a FastMCP access token or a
        storage key must never verify as a download link."""
        other_key = derive_jwt_key(high_entropy_material=SECRET, salt=salt)
        assert other_key != sd._signing_key()
        assert sd.verify_token(jwt.encode(self._claims(), other_key, "HS256")) is None

    @pytest.mark.parametrize("salt", OTHER_SALTS)
    def test_isolation_holds_with_the_fastmcp_jwt_material(self, monkeypatch, salt):
        material = "operator-supplied-jwt-signing-material"
        monkeypatch.setenv("FASTMCP_SERVER_AUTH_GOOGLE_JWT_SIGNING_KEY", material)
        sd._signing_key.cache_clear()
        other_key = derive_jwt_key(low_entropy_material=material, salt=salt)
        assert other_key != sd._signing_key()
        assert sd.verify_token(jwt.encode(self._claims(), other_key, "HS256")) is None
        assert sd.verify_token(_token(_mint())) is not None


class TestUsability:
    """One predicate for mint and serve, evaluated on real google-auth credentials."""

    NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    THRESHOLD = sd.REFRESH_THRESHOLD.total_seconds()  # 225 s in google-auth 2.x

    def _creds(self, seconds_left, **kw):
        creds = _credentials(**kw)
        creds.expiry = (self.NOW + timedelta(seconds=seconds_left)).replace(tzinfo=None)
        return creds

    def test_google_auth_threshold_is_what_this_module_assumes(self):
        creds = _credentials()
        creds.expiry = _now() + timedelta(seconds=self.THRESHOLD + 5)
        assert creds.valid is True
        creds.expiry = _now() + timedelta(seconds=self.THRESHOLD - 5)
        assert creds.valid is False  # still 220 s on the clock, already "expired"

    def test_non_refreshable_usable_until_threshold_not_expiry(self):
        creds = self._creds(3600)
        assert sd.usable_seconds(creds, now=self.NOW) == 3600 - self.THRESHOLD
        assert sd.usable_seconds(self._creds(200), now=self.NOW) < 0

    def test_refreshable_is_unbounded(self):
        creds = self._creds(-3600, refresh_token="1//r")  # long expired, refreshable
        assert sd.usable_seconds(creds, now=self.NOW) == float("inf")
        assert sd.clamp_ttl(creds, now=self.NOW) == sd.URL_TTL_SECONDS

    def test_refresh_token_alone_is_not_refreshable(self):
        creds = self._creds(200, refresh_token="1//r")
        creds._client_secret = None  # google-auth cannot refresh without it
        assert sd.usable_seconds(creds, now=self.NOW) < 0

    def test_no_access_token_and_no_refresh_is_unusable(self):
        assert sd.usable_seconds(self._creds(3600, token=None), now=self.NOW) == 0

    def test_unknown_expiry_uses_default_ttl(self):
        creds = self._creds(0)
        creds.expiry = None
        assert sd.clamp_ttl(creds, now=self.NOW) == sd.URL_TTL_SECONDS

    def test_far_expiry_capped_at_default(self):
        assert sd.clamp_ttl(self._creds(3600), now=self.NOW) == sd.URL_TTL_SECONDS

    def test_near_expiry_clamped_below_the_threshold_with_margin(self):
        # 300 s on the clock: usable for 75 s, URL gets 45 s.
        assert sd.clamp_ttl(self._creds(300), now=self.NOW) == 300 - 225 - 30

    @pytest.mark.parametrize("seconds", [-120, 0, 200, 255])
    def test_inside_threshold_or_margin_is_non_positive(self, seconds):
        assert sd.clamp_ttl(self._creds(seconds), now=self.NOW) <= 0

    def test_url_never_outlives_usability(self):
        for secs in (256, 300, 600, 3600):
            creds = self._creds(secs)
            assert sd.clamp_ttl(creds, now=self.NOW) < sd.usable_seconds(
                creds, now=self.NOW
            )

    def test_format_ttl(self):
        assert sd.format_ttl(45) == "45 seconds"
        assert sd.format_ttl(270) == "~4 minutes"
        assert sd.format_ttl(900) == "~15 minutes"


class TestCredentialRecovery:
    """Session store first, then the persistent store — the same order for the
    tool-side gate and the route, so what gets offered can be served."""

    def test_session_hit_skips_the_persistent_store(self, stores):
        stores.session = {USER: _credentials(token="ya29.session")}
        stores.persistent = {USER: _credentials(token="ya29.store")}
        assert sd._recover_credentials(USER).token == "ya29.session"
        assert stores.persistent_lookups == []

    def test_session_miss_falls_back_to_the_persistent_store(self, stores):
        stores.persistent = {USER: _credentials(token="ya29.store")}
        assert sd._recover_credentials(USER).token == "ya29.store"
        assert stores.session_lookups == [USER] and stores.persistent_lookups == [USER]

    def test_stateless_mode_never_consults_the_persistent_store(
        self, stores, monkeypatch
    ):
        monkeypatch.setattr("auth.oauth_config.is_stateless_mode", lambda: True)
        stores.persistent = {USER: _credentials(token="ya29.store")}
        assert sd._recover_credentials(USER) is None
        assert stores.persistent_lookups == []

    def test_store_errors_mean_not_recoverable(self, stores, monkeypatch):
        monkeypatch.setattr(stores, "get_credential", Mock(side_effect=OSError("disk")))
        assert sd._recover_credentials(USER) is None

    def test_offer_url_uses_the_persistent_store_too(self, stores, monkeypatch):
        """Legacy / trusted-gateway mode: the session store is empty after a restart
        and credentials live only in the credential store — the tool must still mint."""
        monkeypatch.setenv(sd.FLAG_ENV, "true")
        stores.persistent = {USER: _credentials()}
        url, ttl = sd.offer_url(USER, source="gmail", ref={"mid": "m", "aid": "a"})
        assert ttl == sd.URL_TTL_SECONDS and sd.verify_token(_token(url))["sub"] == USER
        assert stores.writes == []


class TestOfferUrl:
    """The tool-side gate: a URL is only issued when the route can serve it, and the
    TTL the caller shows is the real (clamped) one."""

    @pytest.fixture(autouse=True)
    def _on(self, monkeypatch):
        monkeypatch.setenv(sd.FLAG_ENV, "true")

    def test_mints_with_clamped_ttl(self, monkeypatch):
        creds = _credentials(seconds_left=600)  # no refresh token: clamp applies
        monkeypatch.setattr(sd, "_recover_credentials", lambda email: creds)
        url, ttl = sd.offer_url(USER, source="gmail", ref={"mid": "m", "aid": "a"})
        assert 600 - 225 - 30 - 2 <= ttl <= 600 - 225 - 30
        assert sd.verify_token(_token(url))["sub"] == USER

    def test_none_without_recoverable_credentials(self, monkeypatch):
        monkeypatch.setattr(sd, "_recover_credentials", lambda email: None)
        assert sd.offer_url(USER, source="gmail", ref={}) is None

    def test_none_when_token_inside_googles_refresh_threshold(self, monkeypatch):
        creds = _credentials(seconds_left=200)
        monkeypatch.setattr(sd, "_recover_credentials", lambda email: creds)
        assert sd.offer_url(USER, source="gmail", ref={}) is None

    def test_none_when_no_key_can_be_derived(self, monkeypatch):
        monkeypatch.setattr(sd, "_recover_credentials", lambda email: _credentials())
        monkeypatch.setattr(
            sd, "_signing_key", Mock(side_effect=RuntimeError("no key"))
        )
        assert sd.offer_url(USER, source="gmail", ref={}) is None


class TestMintAndServeAgree:
    """The reviewer's probe as a test: real credentials 200 s from expiry — inside
    google-auth's 225 s REFRESH_THRESHOLD, so ``.valid`` is already False."""

    @pytest.fixture(autouse=True)
    def _on(self, monkeypatch):
        monkeypatch.setenv(sd.FLAG_ENV, "true")

    def _offer(self):
        return sd.offer_url(USER, source="gmail", ref={"mid": "m1", "aid": "a1"})

    @pytest.mark.asyncio
    async def test_no_refresh_token_neither_mints_nor_serves(self, stores, fetcher):
        stores.session = {USER: _credentials(seconds_left=200)}
        assert stores.session[USER].valid is False
        assert self._offer() is None
        response = await sd.serve(_token(_mint()))  # a link minted by force
        assert response.status_code == 401 and "token" not in fetcher

    @pytest.mark.asyncio
    async def test_refresh_token_mints_full_ttl_and_route_refreshes_in_memory(
        self, stores, fetcher, token_endpoint
    ):
        calls, _ = token_endpoint
        creds = _credentials(seconds_left=200, refresh_token="1//refresh")
        stores.session = {USER: creds}
        url, ttl = self._offer()
        assert ttl == sd.URL_TTL_SECONDS

        response = await sd.serve(_token(url))

        assert response.status_code == 200 and response.body == b"ok"
        assert fetcher["token"] == "ya29.refreshed"
        assert [(m, u) for m, u, _ in calls] == [("POST", TOKEN_URI)]
        assert b"grant_type=refresh_token" in calls[0][2]
        assert stores.writes == []  # refreshed credentials never reach storage

    @pytest.mark.asyncio
    async def test_refresh_failure_is_401_and_nothing_is_fetched(
        self, stores, fetcher, token_endpoint
    ):
        _, outcome = token_endpoint
        outcome.update(status=400, body={"error": "invalid_grant"})
        stores.session = {USER: _credentials(seconds_left=200, refresh_token="1//r")}
        url, _ = self._offer()
        response = await sd.serve(_token(url))
        assert response.status_code == 401 and "token" not in fetcher
        assert stores.writes == []

    @pytest.mark.asyncio
    async def test_still_valid_token_is_used_without_a_refresh(
        self, stores, fetcher, token_endpoint
    ):
        calls, _ = token_endpoint
        stores.session = {USER: _credentials(seconds_left=300, refresh_token="1//r")}
        url, ttl = self._offer()
        assert (await sd.serve(_token(url))).status_code == 200
        assert fetcher["token"] == "ya29.access" and calls == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "seconds_left", [3600, 300, 256, 254, 240, 224, 200, 60, 0]
    )
    async def test_offer_and_route_agree_at_every_point_of_the_token_life(
        self, stores, fetcher, seconds_left
    ):
        """Never mint what the route would refuse. Only inside the 30 s safety
        margin above google-auth's threshold may the tool decline a URL the route
        would still have served — that gap is the margin's job."""
        stores.session = {USER: _credentials(seconds_left=seconds_left)}
        offered = self._offer()
        status = (await sd.serve(_token(_mint()))).status_code
        if offered is not None:
            assert status == 200, (seconds_left, status)
        threshold = sd.REFRESH_THRESHOLD.total_seconds()
        if not threshold < seconds_left <= threshold + sd._EXPIRY_MARGIN_SECONDS + 1:
            assert (offered is not None) == (status == 200), (seconds_left, status)


class TestServe:
    @pytest.fixture
    def collaborators(self, monkeypatch):
        monkeypatch.setenv(sd.FLAG_ENV, "true")
        seen = {}

        async def fetcher(claims, credentials):
            seen["claims"], seen["credentials"] = claims, credentials
            return sd.DownloadResult(
                filename='rep"ort\r\n.pdf',
                media_type="application/pdf",
                content=b"%PDF-1.3",
            )

        creds = _credentials()

        def recover(email):
            seen.setdefault("emails", []).append(email)
            return creds if email == USER else None

        monkeypatch.setitem(sd._FETCHERS, "gmail", fetcher)
        monkeypatch.setattr(sd, "_recover_credentials", recover)
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
    async def test_control_characters_never_reach_the_ascii_filename(
        self, collaborators, monkeypatch
    ):
        """Sender-chosen names: h11 (uvicorn's HTTP/1.1 layer) refuses a header
        value containing NUL, which would turn one download into a dropped
        connection instead of a 200."""

        async def fetcher(claims, credentials):
            return sd.DownloadResult(
                filename="bad\x00name\x7f\ttab\x01.txt",
                media_type="text/plain",
                content=b"x",
            )

        monkeypatch.setitem(sd._FETCHERS, "gmail", fetcher)
        response = await sd.serve(_token(_mint()))
        disposition = response.headers["content-disposition"]
        assert 'filename="badnametab.txt"' in disposition
        assert "filename*=UTF-8''bad%00name%7F%09tab%01.txt" in disposition
        # h11 accepts exactly what the route emits; the raw name it would not.
        h11.Response(
            status_code=200,
            headers=[
                (k.encode(), v.encode("latin-1")) for k, v in response.headers.items()
            ],
        )
        with pytest.raises(h11.LocalProtocolError):
            h11.Response(
                status_code=200,
                headers=[
                    (b"content-disposition", b'attachment; filename="bad\x00name.txt"')
                ],
            )

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
    async def test_owner_without_credentials_is_401_and_nothing_is_fetched(
        self, collaborators
    ):
        response = await sd.serve(_token(_mint(user_email="other@example.com")))
        assert response.status_code == 401
        assert collaborators["emails"] == ["other@example.com"]
        assert "claims" not in collaborators

    @pytest.mark.asyncio
    async def test_unusable_credentials_are_401(self, collaborators):
        creds = collaborators["creds"]  # no refresh token
        creds.expiry = _now() + timedelta(seconds=100)
        assert creds.valid is False
        assert (await sd.serve(_token(_mint()))).status_code == 401
        assert "claims" not in collaborators

    @pytest.mark.asyncio
    async def test_fetch_failure_is_502(self, collaborators, monkeypatch):
        async def failing(claims, credentials):
            raise sd.SignedDownloadError("boom")

        monkeypatch.setitem(sd._FETCHERS, "gmail", failing)
        assert (await sd.serve(_token(_mint()))).status_code == 502

    @pytest.mark.asyncio
    async def test_every_response_is_nosniff_and_uncacheable(
        self, collaborators, monkeypatch
    ):
        """Sender-typed bytes on a public capability URL: success and every error."""
        responses = {
            "ok": await sd.serve(_token(_mint())),
            "403": await sd.serve("garbage"),
            "401": await sd.serve(_token(_mint(user_email="other@example.com"))),
        }

        async def failing(claims, credentials):
            raise sd.SignedDownloadError("boom")

        monkeypatch.setitem(sd._FETCHERS, "gmail", failing)
        responses["502"] = await sd.serve(_token(_mint()))
        monkeypatch.delenv(sd.FLAG_ENV)
        responses["404"] = await sd.serve(_token(_mint()))

        for name, response in responses.items():
            assert response.headers["x-content-type-options"] == "nosniff", name
            assert response.headers["cache-control"] == "no-store", name

    @pytest.mark.asyncio
    async def test_streamed_responses_carry_the_same_headers(
        self, collaborators, monkeypatch
    ):
        async def body():
            yield b"part"

        async def streaming(claims, credentials):
            return sd.DownloadResult(
                filename="v.mov", media_type="video/quicktime", stream=body()
            )

        monkeypatch.setitem(sd._FETCHERS, "gmail", streaming)
        response = await sd.serve(_token(_mint()))
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["cache-control"] == "no-store"
        assert 'filename="v.mov"' in response.headers["content-disposition"]


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
