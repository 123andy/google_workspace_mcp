"""
Drive access for artifact bundles.

Everything here takes an authorised Drive v3 ``service`` and runs the blocking calls
in a thread. Ownership and location are proven by walking *up* from a folder (only the
owner's token can do that); serving resolves paths by walking *down* from a registered
folder, so a file outside the bundle can never be addressed.
"""

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from gartifacts.artifacts_policy import ArtifactPolicy
from gartifacts.artifacts_scan import TEXT_EXTENSIONS, scan_text

FOLDER_MIME = "application/vnd.google-apps.folder"
SHORTCUT_MIME = "application/vnd.google-apps.shortcut"
GOOGLE_APPS_PREFIX = "application/vnd.google-apps."

ROOT_PATH: Tuple[str, ...] = ("Claude", "Artifacts")
ENTRY_FILE = "index.html"
MAX_PATH_SEGMENTS = 12
MAX_SEGMENT_CHARS = 255
MAX_SCAN_DEPTH = 6

_FILE_FIELDS = "id, name, mimeType, size, modifiedTime, md5Checksum, trashed"
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class ArtifactError(Exception):
    """A problem a user can act on; the message is safe to show them."""


class NotABundleError(ArtifactError):
    """The folder cannot be registered as an artifact bundle."""


class NotFoundError(ArtifactError):
    """No servable file at that path."""


class AmbiguousNameError(ArtifactError):
    """Drive allows duplicate names; the path matches more than one item."""


class NotServableError(ArtifactError):
    """The item exists but is a shortcut or Google-native file, which is not served."""


def _quote(value: str) -> str:
    """Escape a value for use inside single quotes in a Drive query."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


async def _get(service: Any, file_id: str, fields: str) -> Dict[str, Any]:
    return await asyncio.to_thread(
        service.files()
        .get(fileId=file_id, fields=fields, supportsAllDrives=True)
        .execute
    )


async def get_folder(service: Any, folder_id: str) -> Dict[str, Any]:
    """Fetch the metadata the viewer needs about a registered folder."""
    return await _get(service, folder_id, "id, name, mimeType, trashed")


async def list_children(
    service: Any, folder_id: str, name: Optional[str] = None
) -> List[Dict[str, Any]]:
    """List non-trashed children of a folder, optionally those with an exact name."""
    query = f"'{_quote(folder_id)}' in parents and trashed = false"
    if name is not None:
        query = f"name = '{_quote(name)}' and {query}"
    items: List[Dict[str, Any]] = []
    page_token: Optional[str] = None
    while True:
        response = await asyncio.to_thread(
            service.files()
            .list(
                q=query,
                fields=f"nextPageToken, files({_FILE_FIELDS})",
                pageSize=1000,
                pageToken=page_token,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute
        )
        items.extend(
            item for item in response.get("files", []) if not item.get("trashed")
        )
        page_token = response.get("nextPageToken")
        if not page_token:
            return items


def _owned_by(meta: Dict[str, Any], owner_email: str) -> bool:
    owners = [o.get("emailAddress", "").lower() for o in meta.get("owners", [])]
    return owner_email.strip().lower() in owners


async def verify_owned_bundle(
    service: Any, folder_id: str, owner_email: str, root_path: Sequence[str] = ROOT_PATH
) -> str:
    """Prove ``folder_id`` is a bundle owned by ``owner_email`` under the root path.

    The chain must be ``<bundle> -> Artifacts -> Claude -> My Drive root``, every
    folder owned by the caller and not trashed, each with a single parent. Returns the
    bundle's folder name. Raises NotABundleError with a reason otherwise.
    """
    fields = "id, name, mimeType, trashed, parents, owners(emailAddress)"
    try:
        bundle = await _get(service, folder_id, fields)
    except Exception as exc:  # noqa: BLE001 - a missing or inaccessible folder
        raise NotABundleError(
            "That folder could not be read with your account. Check the ID."
        ) from exc
    if bundle.get("mimeType") != FOLDER_MIME:
        raise NotABundleError("That ID is not a folder.")
    if bundle.get("trashed"):
        raise NotABundleError("That folder is in the trash.")
    if not _owned_by(bundle, owner_email):
        raise NotABundleError("Only the owner of a folder can publish it.")

    current = bundle
    for expected in reversed(tuple(root_path)):
        parents = current.get("parents") or []
        if len(parents) != 1:
            raise NotABundleError(
                f"The folder must sit directly in a '{expected}' folder in My Drive."
            )
        try:
            parent = await _get(service, parents[0], fields)
        except Exception as exc:  # noqa: BLE001
            raise NotABundleError(
                f"The folder's parent could not be read; expected '{expected}'."
            ) from exc
        if (
            parent.get("mimeType") != FOLDER_MIME
            or parent.get("name") != expected
            or parent.get("trashed")
            or not _owned_by(parent, owner_email)
        ):
            raise NotABundleError(
                f"The folder must be inside {'/'.join(root_path)} in your My Drive."
            )
        current = parent

    top = current.get("parents") or []
    root = await _get(service, "root", "id")
    if len(top) != 1 or top[0] != root.get("id"):
        raise NotABundleError(
            f"{'/'.join(root_path)} must be at the top level of your My Drive."
        )
    return bundle.get("name", "")


def normalize_path(path: str) -> List[str]:
    """Split a request path into safe segments; empty or trailing slash means index."""
    cleaned = (path or "").lstrip("/")
    if not cleaned or cleaned.endswith("/"):
        cleaned += ENTRY_FILE
    segments = cleaned.split("/")
    if len(segments) > MAX_PATH_SEGMENTS:
        raise NotFoundError("Path too deep.")
    for segment in segments:
        if (
            not segment
            or segment in (".", "..")
            or "\\" in segment
            or _CONTROL.search(segment)
            or len(segment) > MAX_SEGMENT_CHARS
        ):
            raise NotFoundError("Invalid path.")
    return segments


async def resolve_path(service: Any, folder_id: str, path: str) -> Dict[str, Any]:
    """Resolve ``path`` by walking down from the registered folder.

    Duplicate names, shortcuts and Google-native files are refused; a folder is never
    returned as a file.
    """
    segments = normalize_path(path)
    current = folder_id
    for index, segment in enumerate(segments):
        matches = await list_children(service, current, name=segment)
        if not matches:
            raise NotFoundError("Not found.")
        if len(matches) > 1:
            raise AmbiguousNameError(f"More than one item is named '{segment}'.")
        item = matches[0]
        mime = item.get("mimeType", "")
        last = index == len(segments) - 1
        if mime == SHORTCUT_MIME or (
            mime.startswith(GOOGLE_APPS_PREFIX) and mime != FOLDER_MIME
        ):
            raise NotServableError("That item is a shortcut or Google file.")
        if last:
            if mime == FOLDER_MIME:
                raise NotFoundError("Not found.")
            return item
        if mime != FOLDER_MIME:
            raise NotFoundError("Not found.")
        current = item["id"]
    raise NotFoundError("Not found.")


@dataclass
class BundleReport:
    """What registration found in a bundle."""

    file_count: int = 0
    total_bytes: int = 0
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


async def _download_text(service: Any, file_id: str) -> str:
    data = await asyncio.to_thread(service.files().get_media(fileId=file_id).execute)
    if isinstance(data, str):
        return data
    return bytes(data).decode("utf-8", errors="replace")


async def scan_bundle(
    service: Any, folder_id: str, policy: ArtifactPolicy
) -> BundleReport:
    """Walk a bundle, enforce its limits and check text files against the policy."""
    report = BundleReport()
    limits = policy.limits
    queue: List[Tuple[str, str, int]] = [(folder_id, "", 0)]
    root_names: List[str] = []
    while queue:
        current, prefix, depth = queue.pop(0)
        children = await list_children(service, current)
        names = [c.get("name", "") for c in children]
        for duplicate in sorted({n for n in names if names.count(n) > 1}):
            report.errors.append(
                f"{prefix}{duplicate}: more than one item has this name"
            )
        if depth == 0:
            root_names = names
        for child in children:
            name = child.get("name", "")
            mime = child.get("mimeType", "")
            if mime == FOLDER_MIME:
                if depth + 1 > MAX_SCAN_DEPTH:
                    report.errors.append(
                        f"{prefix}{name}: folders are nested too deeply"
                    )
                else:
                    queue.append((child["id"], f"{prefix}{name}/", depth + 1))
                continue
            if mime == SHORTCUT_MIME or mime.startswith(GOOGLE_APPS_PREFIX):
                report.warnings.append(
                    f"{prefix}{name}: shortcuts and Google files are not served"
                )
                continue
            size = int(child.get("size") or 0)
            report.file_count += 1
            report.total_bytes += size
            if size > limits["max_file_bytes"]:
                report.errors.append(f"{prefix}{name}: larger than the per-file limit")
                continue
            if report.file_count > limits["max_bundle_files"]:
                report.errors.append("The bundle has too many files")
                return report
            if report.total_bytes > limits["max_bundle_bytes"]:
                report.errors.append("The bundle is larger than the total size limit")
                return report
            if name.lower().endswith(TEXT_EXTENSIONS):
                text = await _download_text(service, child["id"])
                report.warnings.extend(scan_text(f"{prefix}{name}", text, policy))
    if ENTRY_FILE not in root_names:
        report.errors.append(f"The bundle has no {ENTRY_FILE} at its top level")
    return report
