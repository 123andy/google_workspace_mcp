"""User content stays out of INFO logs.

Server logs routinely ship to aggregators whose access rules are broader than
the user's own data (operators, retention pipelines), so free-text the user
typed — search queries, find/replace text, subjects, titles — must not appear
at INFO. Operational metadata (who, which document, how much) is what INFO is
for; the full text may go to DEBUG, which production does not run at.

These tests pin the principle on the two historically worst offenders rather
than enumerating every tool: a regression elsewhere should be caught in review
by pattern-matching against these.
"""

import logging
import os
import sys
from unittest.mock import Mock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from gdocs.docs_tools import find_and_replace_doc, search_docs  # noqa: E402


def _unwrap(tool):
    fn = tool.fn if hasattr(tool, "fn") else tool
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


SECRET = "acquisition of ExampleCorp"


def _info_text(caplog) -> str:
    return " ".join(
        r.getMessage() for r in caplog.records if r.levelno >= logging.INFO
    )


@pytest.mark.asyncio
async def test_find_and_replace_logs_lengths_not_text(caplog):
    service = Mock()
    service.documents().batchUpdate().execute.return_value = {
        "replies": [{"replaceAllText": {"occurrencesChanged": 2}}]
    }

    with caplog.at_level(logging.DEBUG):
        await _unwrap(find_and_replace_doc)(
            service=service,
            user_google_email="user@example.com",
            document_id="doc-1",
            find_text=SECRET,
            replace_text=SECRET + " (final)",
        )

    assert SECRET not in _info_text(caplog)
    # The text is still available for debugging — at DEBUG, not silently gone.
    debug_text = " ".join(
        r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG
    )
    assert SECRET in debug_text


@pytest.mark.asyncio
async def test_search_docs_logs_query_length_not_query(caplog):
    service = Mock()
    service.files().list().execute.return_value = {"files": []}

    with caplog.at_level(logging.DEBUG):
        await _unwrap(search_docs)(
            service=service,
            user_google_email="user@example.com",
            query=SECRET,
        )

    assert SECRET not in _info_text(caplog)
