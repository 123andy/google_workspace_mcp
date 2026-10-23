"""Business logic behind the artifact tools."""

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Optional

from gartifacts.artifacts_drive import (
    BundleReport,
    NotABundleError,
    scan_bundle,
    verify_owned_bundle,
)
from gartifacts.artifacts_links import build_link_segment, title_from_folder_name
from gartifacts.artifacts_policy import ArtifactPolicy, describe_policy
from gartifacts.artifacts_registry import ArtifactRecord, ArtifactRegistry


@dataclass(frozen=True)
class Registration:
    """The outcome of registering a bundle."""

    record: ArtifactRecord
    report: BundleReport
    viewer_url: Optional[str]


def viewer_link(base_url: Optional[str], record: ArtifactRecord) -> Optional[str]:
    """Return the canonical viewer URL for a bundle, or None without a base URL."""
    if not base_url:
        return None
    return f"{base_url}/a/{build_link_segment(record.hash, record.slug)}/"


async def register_bundle(
    service: Any,
    owner_email: str,
    folder_id: str,
    registry: ArtifactRegistry,
    policy: ArtifactPolicy,
    base_url: Optional[str],
) -> Registration:
    """Verify ownership and contents, then register the bundle (idempotent)."""
    folder_name = await verify_owned_bundle(service, folder_id, owner_email)
    report = await scan_bundle(service, folder_id, policy)
    if report.errors:
        raise NotABundleError(
            "The bundle cannot be published yet:\n- " + "\n- ".join(report.errors)
        )
    title = title_from_folder_name(folder_name)
    record = await asyncio.to_thread(registry.register, folder_id, owner_email, title)
    return Registration(record, report, viewer_link(base_url, record))


def format_registration(result: Registration, policy_version: str) -> str:
    """Human-readable result for the register tool."""
    lines = [
        f"Registered '{result.record.title}' "
        f"({result.report.file_count} files, {result.report.total_bytes} bytes).",
    ]
    if result.viewer_url:
        lines.append(f"Viewer link: {result.viewer_url}")
    else:
        lines.append(
            f"Link path: /a/{build_link_segment(result.record.hash, result.record.slug)}/ "
            "(no viewer URL is configured on this server)."
        )
    if result.report.warnings:
        lines.append("")
        lines.append(f"Warnings (checked against policy {policy_version}):")
        lines.extend(f"- {warning}" for warning in result.report.warnings)
    else:
        lines.append(f"No policy warnings (checked against policy {policy_version}).")
    return "\n".join(lines)


def format_policy(policy: ArtifactPolicy) -> str:
    """JSON description of the policy for the policy tool."""
    return json.dumps(describe_policy(policy), indent=2)
