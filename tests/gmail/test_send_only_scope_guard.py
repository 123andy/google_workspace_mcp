"""send_gmail_message stays scope-minimal: it declares gmail.send ONLY.

Why this matters: in scope-minimal deployments (--only-tools), the requested
Google grant is derived from the union of the selected tools' declared scopes.
A read scope declared here would put mail-reading power on a send-only
endpoint's token. The convenience paths that DO read (reply derivation,
quoting, forwarding) must instead degrade with actionable guidance when the
grant cannot read — never silently change behavior, and never widen the grant.
"""

import os
import sys
from unittest.mock import Mock

import pytest
from googleapiclient.errors import HttpError

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from auth.scopes import (  # noqa: E402
    CHAT_WRITE_SCOPE,
    DRIVE_FILE_SCOPE,
    GMAIL_COMPOSE_SCOPE,
    GMAIL_SEND_SCOPE,
)
from core.utils import UserInputError  # noqa: E402
from gmail.gmail_tools import send_gmail_draft, send_gmail_message  # noqa: E402


def _unwrap(tool):
    """Unwrap FunctionTool + decorators to the original async function."""
    fn = tool.fn if hasattr(tool, "fn") else tool
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


def _required_scopes(tool) -> set:
    """Read the declared scopes the way main()'s --only-tools derivation does."""
    fn = getattr(tool, "fn", tool)
    return set(getattr(fn, "_required_google_scopes", []) or [])


class _FakeResp:
    """Minimal stand-in for an httplib2 Response (status + reason)."""

    def __init__(self, status: int):
        self.status = status
        self.reason = "Forbidden" if status == 403 else "Error"


def _http_error(status: int) -> HttpError:
    return HttpError(_FakeResp(status), b"{}")


class TestScopeDeclarations:
    def test_send_gmail_message_declares_send_only(self):
        """The send tool must not declare any read scope — the decorator list is
        both the consent driver (--only-tools) and a hard runtime requirement."""
        assert _required_scopes(send_gmail_message) == {GMAIL_SEND_SCOPE}

    def test_commit_toolset_scope_union_is_exactly_four(self):
        """A commit-only tool selection (send mail / send draft / post chat /
        share files) must derive exactly four scopes — none of them reads.
        This pins the scope-minimal property end to end: if any of these five
        tools grows a read-scope declaration, this fails before any deploy."""
        from gchat.chat_tools import send_message
        from gdrive.drive_tools import (
            manage_drive_access,
            set_drive_file_permissions,
        )

        union = set()
        for tool in (
            send_gmail_message,
            send_gmail_draft,
            send_message,
            manage_drive_access,
            set_drive_file_permissions,
        ):
            union |= _required_scopes(tool)

        assert union == {
            GMAIL_SEND_SCOPE,
            GMAIL_COMPOSE_SCOPE,
            CHAT_WRITE_SCOPE,
            DRIVE_FILE_SCOPE,
        }


@pytest.mark.asyncio
class TestSendOnlyDegradation:
    """On a send-only grant, read-dependent paths fail with guidance, loudly."""

    async def test_reply_derivation_403_gives_send_only_guidance(self):
        """thread_id triggers a thread fetch; a 403 (insufficient scope) must
        surface as guidance, not be swallowed into a silent unquoted send."""
        service = Mock()
        service.users().threads().get.side_effect = _http_error(403)

        with pytest.raises(UserInputError, match="send-only"):
            await _unwrap(send_gmail_message)(
                service=service,
                user_google_email="user@example.com",
                to="rcpt@example.com",
                subject="Hi",
                body="Hello",
                thread_id="t1",
                quote_original=True,
            )

    async def test_forward_403_gives_send_only_guidance(self):
        """Forwarding must read the original; a 403 becomes guidance that routes
        to the draft-then-send flow instead of a bare HttpError."""
        service = Mock()
        service.users().messages().get().execute.side_effect = _http_error(403)

        with pytest.raises(UserInputError, match="send-only"):
            await _unwrap(send_gmail_message)(
                service=service,
                user_google_email="user@example.com",
                to="rcpt@example.com",
                forward_message_id="m1",
            )

    async def test_forward_non_403_is_not_masked_as_scope_guidance(self):
        """A 404 (message not found) is a different problem — it must NOT be
        rewritten into send-only guidance."""
        service = Mock()
        service.users().messages().get().execute.side_effect = _http_error(404)

        with pytest.raises(HttpError):
            await _unwrap(send_gmail_message)(
                service=service,
                user_google_email="user@example.com",
                to="rcpt@example.com",
                forward_message_id="m1",
            )

    async def test_explicit_headers_skip_the_fetch_entirely(self):
        """A reply with to/in_reply_to/references supplied needs NO read — it
        must send successfully on a send-only grant (no thread fetch occurs)."""
        service = Mock()
        service.users().threads().get.side_effect = _http_error(403)
        service.users().messages().send().execute.return_value = {"id": "sent1"}
        # signature fetch is tolerant of failures; make it benignly empty
        service.users().settings().sendAs().list().execute.return_value = {"sendAs": []}

        result = await _unwrap(send_gmail_message)(
            service=service,
            user_google_email="user@example.com",
            to="rcpt@example.com",
            subject="Re: Hi",
            body="Hello",
            thread_id="t1",
            in_reply_to="<orig@example.com>",
            references="<orig@example.com>",
            include_signature=False,
        )

        assert "sent1" in result
        service.users().threads().get.assert_not_called()
