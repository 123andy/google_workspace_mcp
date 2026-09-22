"""Builders for realistic Gmail ``users().messages().get()`` payloads.

Two properties of real Gmail responses are easy to lose when a message is
mocked by hand, and both matter to the attachment code paths:

1. ``format=full`` returns a MIME *tree*, not a flat list. A message with an
   inline image and a PDF arrives as ``multipart/mixed`` → ``multipart/related``
   → ``multipart/alternative``, with the image beside the alternative branch and
   the PDF beside the related branch.
2. ``body.attachmentId`` is per-fetch. Gmail hands out a different attachment ID
   for the same part on a later ``messages().get()`` call, so an ID a client
   captured earlier is stale by the time the server re-reads the message.

A mock that models a message as one flat part with one stable ID satisfies both
"find it by ID" and "there is exactly one attachment, so take it", which lets
ID-only resolution pass tests it should not.

Typical use::

    message = build_message(
        parts=[
            inline_image("logo.png", content_id="logo@example.com"),
            document("report.pdf", content=b"%PDF-1.7 ..."),
        ]
    )
    service = mock_gmail_service(message, generation=2)  # IDs already rotated
    stale_id = message.attachment_id(1, generation=1)

The builders use only the standard library and do not honour the ``fields``
mask: every call returns the whole tree. Tests that need Gmail's partial
projection (a part below the mask's depth) should keep building that response
themselves.
"""

from __future__ import annotations

import base64
import hashlib
import mimetypes
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple
from unittest.mock import Mock

__all__ = [
    "Part",
    "GmailMessage",
    "build_message",
    "document",
    "inline_image",
    "mock_gmail_service",
    "unnamed_part",
]

_DEFAULT_CONTENT = b"fixture attachment bytes"


def _b64(data: bytes) -> str:
    """Encode as Gmail does: URL-safe base64, unpadded stripping left alone."""
    return base64.urlsafe_b64encode(data).decode("ascii")


@dataclass(frozen=True)
class Part:
    """One non-text leaf of a fixture message.

    ``declared_size`` overrides the size advertised in the message metadata
    while ``attachments().get()`` still returns ``content``; Gmail's declared
    size is its own number, and the size-capping code trusts it before
    downloading anything.
    """

    filename: str
    mime_type: str
    content: bytes = _DEFAULT_CONTENT
    content_id: Optional[str] = None
    inline: bool = False
    declared_size: Optional[int] = None

    @property
    def size(self) -> int:
        """Size as the message metadata advertises it."""
        return (
            self.declared_size if self.declared_size is not None else len(self.content)
        )


def inline_image(
    filename: str,
    *,
    content_id: str,
    content: bytes = _DEFAULT_CONTENT,
    mime_type: str = "image/png",
) -> Part:
    """An image referenced from the HTML body by ``cid:`` (RFC 2392)."""
    return Part(
        filename=filename,
        mime_type=mime_type,
        content=content,
        content_id=content_id,
        inline=True,
    )


def document(
    filename: str,
    *,
    content: bytes = _DEFAULT_CONTENT,
    mime_type: Optional[str] = None,
    declared_size: Optional[int] = None,
) -> Part:
    """A named attachment, MIME type guessed from the extension when omitted."""
    if mime_type is None:
        mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    return Part(
        filename=filename,
        mime_type=mime_type,
        content=content,
        declared_size=declared_size,
    )


def unnamed_part(
    *,
    content: bytes = _DEFAULT_CONTENT,
    mime_type: str = "application/octet-stream",
    declared_size: Optional[int] = None,
) -> Part:
    """A downloadable part Gmail reports with an empty ``filename``.

    Such a part carries an ``attachmentId`` but is skipped by attachment
    *listings*, so its ordinal and its position in the tree differ.
    """
    return Part(
        filename="",
        mime_type=mime_type,
        content=content,
        declared_size=declared_size,
    )


def _header(name: str, value: str) -> Dict[str, str]:
    return {"name": name, "value": value}


@dataclass
class GmailMessage:
    """A synthetic Gmail message whose attachment IDs can be regenerated.

    ``generation`` selects which set of attachment IDs a rendering uses;
    generation 1 is what a client saw first, generation 2 what the server gets
    when it re-reads the same message.
    """

    parts: Sequence[Part]
    message_id: str = "msg-1"
    thread_id: str = "thread-1"
    subject: str = "Quarterly report"
    sender: str = "Sender <sender@example.com>"
    recipient: str = "user@example.com"
    date: str = "Tue, 01 Sep 2026 09:00:00 +0000"
    text_body: str = "See the attached files."
    html_body: Optional[str] = "<p>See the attached files.</p>"
    _ids: Dict[str, Tuple[int, int]] = field(default_factory=dict, repr=False)

    # -- identifiers ---------------------------------------------------

    def attachment_id(self, index: int, generation: int = 1) -> str:
        """Opaque, deterministic attachment ID for ``parts[index]``."""
        digest = hashlib.sha256(
            f"{self.message_id}|{index}|{generation}".encode()
        ).digest()
        attachment_id = "ANGjdJ" + _b64(digest).rstrip("=")[:52]
        self._ids[attachment_id] = (index, generation)
        return attachment_id

    def attachment_ordinal(self, index: int) -> int:
        """Position of ``parts[index]`` in an attachment listing.

        Attachment listings skip parts without a filename, so this is the
        number a caller would pass as ``attachment_index`` — not the index
        into :attr:`parts`.
        """
        if not self.parts[index].filename:
            raise ValueError(f"parts[{index}] has no filename, so it is not listed")
        # Listing order follows the tree, where the related branch (the inline
        # parts) precedes the parts hanging off multipart/mixed.
        listed = [
            i for i, part in enumerate(self.parts) if part.filename and part.inline
        ]
        listed += [
            i for i, part in enumerate(self.parts) if part.filename and not part.inline
        ]
        return listed.index(index)

    # -- rendering -----------------------------------------------------

    def payload(self, generation: int = 1) -> Dict[str, Any]:
        """The ``payload`` of a ``format=full`` message."""
        inline = [(i, p) for i, p in enumerate(self.parts) if p.inline]
        attached = [(i, p) for i, p in enumerate(self.parts) if not p.inline]

        node = self._body_branch()
        if inline:
            node = self._container(
                "multipart/related",
                [node] + [self._leaf(i, p, generation) for i, p in inline],
            )
        if attached:
            node = self._container(
                "multipart/mixed",
                [node] + [self._leaf(i, p, generation) for i, p in attached],
            )

        # The root carries the message headers; its container Content-Type is
        # rebuilt there, so drop the generic one to avoid a duplicate header.
        node["headers"] = self._headers(node["mimeType"]) + [
            header
            for header in node.get("headers", [])
            if header["name"] != "Content-Type"
        ]
        _assign_part_ids(node)
        return node

    def message(self, generation: int = 1) -> Dict[str, Any]:
        """The full ``messages().get()`` response."""
        payload = self.payload(generation)
        raw_bytes = sum(part.size for part in self.parts) + len(self.text_body)
        return {
            "id": self.message_id,
            "threadId": self.thread_id,
            "labelIds": ["INBOX"],
            "snippet": self.text_body[:100],
            "historyId": "1234567",
            "internalDate": "1756717200000",
            "sizeEstimate": int(raw_bytes * 4 / 3) + 512,
            "payload": payload,
        }

    # -- attachments().get() -------------------------------------------

    def attachment_response(self, index: int, generation: int = 1) -> Dict[str, Any]:
        """The ``attachments().get()`` response for ``parts[index]``."""
        part = self.parts[index]
        return {
            "attachmentId": self.attachment_id(index, generation),
            "size": len(part.content),
            "data": _b64(part.content),
        }

    def attachment_response_for_id(self, attachment_id: str) -> Dict[str, Any]:
        """Serve any ID this message has handed out, current or stale.

        Gmail keeps honouring an earlier fetch's attachment ID for a while,
        which is why a stale ID can download the right bytes and still fail to
        resolve a filename.
        """
        try:
            index, generation = self._ids[attachment_id]
        except KeyError:
            raise KeyError(
                f"{attachment_id!r} was never issued by this fixture; call "
                "attachment_id()/payload() for the generation under test"
            ) from None
        return self.attachment_response(index, generation)

    # -- internals -----------------------------------------------------

    def _headers(self, content_type: str) -> List[Dict[str, str]]:
        return [
            _header("Delivered-To", self.recipient),
            _header("From", self.sender),
            _header("To", self.recipient),
            _header("Subject", self.subject),
            _header("Date", self.date),
            _header("Message-ID", f"<{self.message_id}@mail.example.com>"),
            _header("MIME-Version", "1.0"),
            _header("Content-Type", f'{content_type}; boundary="b_{self.message_id}"'),
        ]

    def _container(self, mime_type: str, parts: List[Dict[str, Any]]) -> Dict[str, Any]:
        subtype = mime_type.split("/")[-1]
        return {
            "mimeType": mime_type,
            "filename": "",
            "headers": [
                _header("Content-Type", f'{mime_type}; boundary="b_{subtype}"')
            ],
            "body": {"size": 0},
            "parts": parts,
        }

    def _text_leaf(self, mime_type: str, text: str) -> Dict[str, Any]:
        data = text.encode()
        return {
            "mimeType": mime_type,
            "filename": "",
            "headers": [
                _header("Content-Type", f'{mime_type}; charset="UTF-8"'),
                _header("Content-Transfer-Encoding", "base64"),
            ],
            "body": {"size": len(data), "data": _b64(data)},
        }

    def _body_branch(self) -> Dict[str, Any]:
        text = self._text_leaf("text/plain", self.text_body)
        if self.html_body is None:
            return text
        return self._container(
            "multipart/alternative",
            [text, self._text_leaf("text/html", self.html_body)],
        )

    def _leaf(self, index: int, part: Part, generation: int) -> Dict[str, Any]:
        disposition = "inline" if part.inline else "attachment"
        headers = [
            _header("Content-Type", f'{part.mime_type}; name="{part.filename}"'),
            _header(
                "Content-Disposition", f'{disposition}; filename="{part.filename}"'
            ),
            _header("Content-Transfer-Encoding", "base64"),
        ]
        if part.content_id:
            headers.append(_header("Content-ID", f"<{part.content_id}>"))
            headers.append(_header("X-Attachment-Id", part.content_id))
        return {
            "mimeType": part.mime_type,
            "filename": part.filename,
            "headers": headers,
            "body": {
                "attachmentId": self.attachment_id(index, generation),
                "size": part.size,
            },
        }


def _assign_part_ids(node: Dict[str, Any], prefix: str = "") -> None:
    """Number parts the way Gmail does: "0", "1", "1.0", ... with "" at the root."""
    node["partId"] = prefix
    for position, child in enumerate(node.get("parts") or []):
        _assign_part_ids(child, f"{prefix}.{position}" if prefix else str(position))


def build_message(parts: Sequence[Part], **kwargs: Any) -> GmailMessage:
    """Build a :class:`GmailMessage`; see it for the keyword arguments."""
    return GmailMessage(parts=parts, **kwargs)


def mock_gmail_service(
    message: GmailMessage,
    *,
    generation: int = 1,
    rotate_ids: bool = False,
) -> Mock:
    """Mock a google-api Gmail service that serves ``message``.

    ``generation`` is the generation the first ``messages().get()`` serves; with
    ``rotate_ids`` each later call serves the next one, reproducing Gmail's
    per-fetch attachment IDs. ``attachments().get()`` serves any ID the message
    has issued, so a stale ID still downloads.

    The ``.get`` mocks are stable, so tests can assert on
    ``service.users().messages().get`` (or ``...attachments().get``) call args
    and call counts.
    """
    service = Mock()
    fetches = {"count": 0}

    def _message_execute() -> Dict[str, Any]:
        offset = fetches["count"] if rotate_ids else 0
        fetches["count"] += 1
        return message.message(generation + offset)

    messages_request = Mock()
    messages_request.execute = Mock(side_effect=_message_execute)
    messages_get = Mock(side_effect=lambda **kwargs: messages_request)

    def _attachment_execute() -> Dict[str, Any]:
        requested = attachments_get.call_args.kwargs["id"]
        return message.attachment_response_for_id(requested)

    attachments_request = Mock()
    attachments_request.execute = Mock(side_effect=_attachment_execute)
    attachments_get = Mock(side_effect=lambda **kwargs: attachments_request)

    service.users().messages().get = messages_get
    service.users().messages().attachments().get = attachments_get
    return service
