"""Tests for the per-source download fetchers (Design A).

Focus on the Drive streaming path: bytes must reassemble exactly, and memory must
stay bounded to one chunk (the whole file never sits in RAM).
"""

import pytest

import core.signed_download as sd
from core.signed_download import DownloadResult, get_fetcher


class TestRegistry:
    def test_known_and_unknown_sources(self):
        assert get_fetcher("gmail") is not None
        assert get_fetcher("drive") is not None
        assert get_fetcher("nope") is None


class TestDownloadResult:
    def test_buffered_vs_stream_are_distinguishable(self):
        buffered = DownloadResult(filename="a", media_type="text/plain", content=b"hi")
        assert buffered.stream is None and buffered.content == b"hi"


class _FakeDownloader:
    """Mimics MediaIoBaseDownload: append one chunk to fh per next_chunk, no seek."""

    def __init__(self, payload, fh, chunksize):
        self._payload = payload
        self._fh = fh
        self._pos = 0
        self._cs = chunksize

    def next_chunk(self):
        nxt = self._payload[self._pos : self._pos + self._cs]
        self._fh.write(nxt)
        self._pos += len(nxt)
        return "status", self._pos >= len(self._payload)


class _FakeFiles:
    def get_media(self, fileId):
        return "REQ"

    def export_media(self, fileId, mimeType):
        return "REQ"


class _FakeDrive:
    def files(self):
        return _FakeFiles()


@pytest.fixture
def patched_drive(monkeypatch):
    payload = bytes(range(256)) * 200  # 51200 bytes
    chunksize = 8192
    monkeypatch.setattr(sd, "_DRIVE_CHUNK_SIZE", chunksize)
    monkeypatch.setattr(sd, "build", lambda *a, **k: _FakeDrive())
    monkeypatch.setattr(
        sd,
        "MediaIoBaseDownload",
        lambda fh, request, chunksize=chunksize: _FakeDownloader(
            payload, fh, chunksize
        ),
    )
    return payload, chunksize


class TestDriveStreaming:
    @pytest.mark.asyncio
    async def test_streams_in_bounded_chunks_and_reassembles(self, patched_drive):
        payload, chunksize = patched_drive
        result = await sd._fetch_drive(
            {"fid": "F", "fn": "v.mov", "mt": "video/quicktime"}, None
        )

        assert result.content is None
        assert result.stream is not None
        assert result.filename == "v.mov"
        assert result.media_type == "video/quicktime"

        chunks = [c async for c in result.stream]
        assert b"".join(chunks) == payload  # byte-perfect
        assert max(len(c) for c in chunks) <= chunksize  # never the whole file at once
        assert len(chunks) > 1  # actually chunked

    @pytest.mark.asyncio
    async def test_missing_fid_raises(self):
        with pytest.raises(sd.SignedDownloadError):
            await sd._fetch_drive({"fn": "x"}, None)
