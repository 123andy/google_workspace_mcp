"""Tests for the send_draft tool (compose-then-send split)."""

from unittest.mock import Mock

import pytest

from gmail.gmail_tools import send_draft


def _unwrap(tool):
    """Unwrap FunctionTool + decorators to the original async function."""
    fn = tool.fn if hasattr(tool, "fn") else tool
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


@pytest.mark.asyncio
async def test_send_draft_sends_by_id():
    service = Mock()
    send_request = Mock()
    send_request.execute.return_value = {"id": "msg-123", "threadId": "thr-456"}
    service.users().drafts().send.return_value = send_request

    result = await _unwrap(send_draft)(
        service=service,
        user_google_email="user@example.com",
        draft_id="draft-abc",
    )

    # Calls drafts().send with the draft id, not messages().send.
    service.users().drafts().send.assert_called_with(
        userId="me", body={"id": "draft-abc"}
    )
    assert "draft-abc" in result
    assert "msg-123" in result
    assert "thr-456" in result
