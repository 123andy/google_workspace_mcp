"""A small in-memory Drive v3 service for artifact tests."""

import itertools
import re
from typing import Any, Dict, List, Optional

from gartifacts.artifacts_drive import FOLDER_MIME

ROOT_ID = "ROOT"
_NAME_Q = re.compile(r"^name = '((?:[^'\\]|\\.)*)' and (.*)$")
_PARENT_Q = re.compile(r"^'((?:[^'\\]|\\.)*)' in parents and trashed = false$")


def _unquote(value: str) -> str:
    return value.replace("\\'", "'").replace("\\\\", "\\")


class _Request:
    def __init__(self, producer):
        self._producer = producer

    def execute(self):
        return self._producer()


class _Files:
    def __init__(self, drive: "FakeDrive"):
        self._drive = drive

    def get(self, fileId: str, fields: str = "", supportsAllDrives: bool = True):
        def produce() -> Dict[str, Any]:
            if fileId == "root":
                return {"id": ROOT_ID}
            item = self._drive.items.get(fileId)
            if item is None:
                raise KeyError(fileId)
            return {k: v for k, v in item.items() if k != "content"}

        return _Request(produce)

    def get_media(self, fileId: str):
        return _Request(lambda: self._drive.items[fileId]["content"])

    def list(self, q: str, pageToken: Optional[str] = None, pageSize: int = 100, **_):
        self._drive.list_calls.append(q)

        def produce() -> Dict[str, Any]:
            name = None
            named = _NAME_Q.match(q)
            rest = q
            if named:
                name, rest = _unquote(named.group(1)), named.group(2)
            parent = _PARENT_Q.match(rest)
            assert parent, f"unsupported query: {q}"
            parent_id = _unquote(parent.group(1))
            matches = [
                {k: v for k, v in item.items() if k != "content"}
                for item in self._drive.items.values()
                if parent_id in item.get("parents", [])
                and (name is None or item["name"] == name)
                and not item.get("trashed")
            ]
            start = int(pageToken or 0)
            page = matches[start : start + self._drive.page_size]
            response: Dict[str, Any] = {"files": page}
            if start + self._drive.page_size < len(matches):
                response["nextPageToken"] = str(start + self._drive.page_size)
            return response

        return _Request(produce)


class FakeDrive:
    """Minimal files().get / list / get_media."""

    def __init__(self, page_size: int = 1000):
        self.items: Dict[str, Dict[str, Any]] = {}
        self.list_calls: List[str] = []
        self.page_size = page_size
        self._ids = itertools.count(1)

    def files(self) -> _Files:
        return _Files(self)

    def add(
        self,
        name: str,
        parent: Optional[str] = ROOT_ID,
        mime: str = "text/plain",
        owner: str = "owner@scientist.com",
        content: bytes = b"",
        trashed: bool = False,
        parents: Optional[List[str]] = None,
        size: Optional[int] = None,
    ) -> str:
        file_id = f"id{next(self._ids)}"
        self.items[file_id] = {
            "id": file_id,
            "name": name,
            "mimeType": mime,
            "parents": parents if parents is not None else ([parent] if parent else []),
            "owners": [{"emailAddress": owner}],
            "trashed": trashed,
            "size": str(len(content) if size is None else size),
            "modifiedTime": "2026-10-22T00:00:00Z",
            "content": content,
        }
        return file_id

    def folder(self, name: str, parent: Optional[str] = ROOT_ID, **kwargs) -> str:
        return self.add(name, parent, mime=FOLDER_MIME, **kwargs)

    def bundle(self, owner: str = "owner@scientist.com") -> str:
        """Create Claude/Artifacts/<bundle> under the root and return the bundle ID."""
        claude = self.folder("Claude", ROOT_ID, owner=owner)
        artifacts = self.folder("Artifacts", claude, owner=owner)
        return self.folder("20261022-1815-q3-board-deck", artifacts, owner=owner)
