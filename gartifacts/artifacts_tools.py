"""
Artifact tools (opt-in).

``register_artifact`` publishes a bundle folder the caller owns; ``get_artifact_policy``
tells a packaging skill which hosts and limits the server enforces. Neither is loaded
unless the artifacts service is selected or WORKSPACE_MCP_ARTIFACTS is set.
"""

import logging

from mcp.types import ToolAnnotations

from auth.service_decorator import require_google_service
from core.server import server
from core.utils import UserInputError, handle_http_errors
from gartifacts.artifacts_config import viewer_base_url
from gartifacts.artifacts_drive import ArtifactError
from gartifacts.artifacts_helpers import (
    format_policy,
    format_registration,
    register_bundle,
)
from gartifacts.artifacts_policy import PolicyError, load_policy, policy_version
from gartifacts.artifacts_registry import get_artifact_registry

logger = logging.getLogger(__name__)


@server.tool(
    title="Register Artifact",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
@handle_http_errors("register_artifact", service_type="drive")
@require_google_service("drive", "drive_read")
async def register_artifact(service, user_google_email: str, folder_id: str) -> str:
    """
    Publishes an artifact bundle so the artifact viewer will serve it.

    The folder must be owned by you and sit at Claude/Artifacts/<bundle> in your My
    Drive, with an index.html at its top level. The bundle's files are checked against
    the content policy and any problems are returned as warnings. Registering the same
    folder again is safe and returns the same link.

    Args:
        user_google_email (str): The user's Google email address. Required.
        folder_id (str): The ID of the bundle folder. Required.

    Returns:
        str: The viewer link and any policy warnings.
    """
    logger.info("[register_artifact] Invoked. Email: '%s'", user_google_email)
    try:
        policy = load_policy()
        result = await register_bundle(
            service,
            user_google_email,
            folder_id,
            get_artifact_registry(),
            policy,
            viewer_base_url(),
        )
    except (ArtifactError, PolicyError) as exc:
        raise UserInputError(str(exc)) from exc
    return format_registration(result, policy_version(policy))


@server.tool(
    title="Get Artifact Policy",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
async def get_artifact_policy() -> str:
    """
    Returns the content policy the artifact viewer enforces.

    Call this before building an artifact to learn which hosts it may load libraries,
    styles, fonts and images from, the size limits, what the sandbox forbids, and a
    short rules block to follow. The policy_version changes whenever the policy does.

    Returns:
        str: The policy as JSON.
    """
    try:
        return format_policy(load_policy())
    except PolicyError as exc:
        raise UserInputError(str(exc)) from exc
