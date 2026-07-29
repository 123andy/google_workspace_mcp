"""
Tests for the share-domain allowlist (WORKSPACE_MCP_ALLOWED_SHARE_DOMAINS).

When the env var is set, the Drive sharing tools refuse targets outside the
listed domains: 'anyone' grants, external user/group emails, external domain
shares, link_sharing (other than "off"), escalation of existing external
permissions via 'update', and ownership transfer to external users. Revoking
permissions is always allowed. When unset, behavior is unchanged.

WORKSPACE_MCP_SHARE_RESTRICTED_MESSAGE optionally APPENDS operator guidance to
the default rejection text (never replaces it).

Enforcement reads the env at call time (validate_share_target); the docstring
note (share_restriction_doc_note / _with_share_restriction_note) is evaluated
at import/registration time.
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from gdrive.drive_helpers import (  # noqa: E402
    ALLOWED_SHARE_DOMAINS_ENV,
    SHARE_RESTRICTED_MESSAGE_ENV,
    get_allowed_share_domains,
    share_restriction_doc_note,
    share_restriction_message,
    validate_existing_permission_target,
    validate_share_target,
)
from gdrive.drive_tools import (  # noqa: E402
    _with_share_restriction_note,
    manage_drive_access,
    set_drive_file_permissions,
)


def _unwrap(tool):
    fn = tool.fn if hasattr(tool, "fn") else tool
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


manage_access_fn = _unwrap(manage_drive_access)
set_permissions_fn = _unwrap(set_drive_file_permissions)


@pytest.fixture
def restricted(monkeypatch):
    monkeypatch.setenv(ALLOWED_SHARE_DOMAINS_ENV, "scientist.com")
    monkeypatch.delenv(SHARE_RESTRICTED_MESSAGE_ENV, raising=False)


@pytest.fixture
def unrestricted(monkeypatch):
    monkeypatch.delenv(ALLOWED_SHARE_DOMAINS_ENV, raising=False)
    monkeypatch.delenv(SHARE_RESTRICTED_MESSAGE_ENV, raising=False)


def _make_service(**responses):
    """Mock Drive service; permissions().<method>().execute() return values
    come from *responses* (create=..., get=..., update=..., list=...)."""
    service = MagicMock()
    permissions = service.permissions.return_value
    for method, value in responses.items():
        getattr(permissions, method).return_value.execute.return_value = value
    return service


def _resolve_patch():
    return patch(
        "gdrive.drive_tools.resolve_drive_item",
        return_value=(
            "file123",
            {"name": "Test File", "webViewLink": "https://drive.google.com/x"},
        ),
    )


# ─── env parsing ────────────────────────────────────────────────────────────────


class TestGetAllowedShareDomains:
    def test_unset_returns_none(self, unrestricted):
        assert get_allowed_share_domains() is None

    def test_empty_returns_none(self, monkeypatch):
        monkeypatch.setenv(ALLOWED_SHARE_DOMAINS_ENV, "")
        assert get_allowed_share_domains() is None

    def test_only_commas_returns_none(self, monkeypatch):
        monkeypatch.setenv(ALLOWED_SHARE_DOMAINS_ENV, " , ,")
        assert get_allowed_share_domains() is None

    def test_normalizes_case_whitespace_and_at(self, monkeypatch):
        monkeypatch.setenv(ALLOWED_SHARE_DOMAINS_ENV, " Scientist.com , @Partner.ORG ")
        assert get_allowed_share_domains() == ["scientist.com", "partner.org"]


# ─── rejection message ──────────────────────────────────────────────────────────


class TestShareRestrictionMessage:
    def test_default_names_the_domains(self, unrestricted):
        msg = share_restriction_message(["scientist.com", "partner.org"])
        assert "scientist.com, partner.org" in msg

    def test_operator_message_appends_never_replaces(self, monkeypatch):
        monkeypatch.setenv(
            SHARE_RESTRICTED_MESSAGE_ENV,
            "For external sharing, use the full endpoint.",
        )
        msg = share_restriction_message(["scientist.com"])
        assert "restricted to these domains: scientist.com" in msg
        assert msg.endswith("For external sharing, use the full endpoint.")


# ─── target validation ──────────────────────────────────────────────────────────


class TestValidateShareTarget:
    def test_noop_when_unset(self, unrestricted):
        validate_share_target("anyone", None)
        validate_share_target("user", "evil@external.com")

    def test_internal_user_allowed(self, restricted):
        validate_share_target("user", "jane@scientist.com")

    def test_case_insensitive(self, restricted):
        validate_share_target("user", "Jane@SCIENTIST.com")

    def test_group_same_rules(self, restricted):
        validate_share_target("group", "team@scientist.com")
        with pytest.raises(ValueError, match="restricted to these domains"):
            validate_share_target("group", "team@external.com")

    def test_external_user_rejected(self, restricted):
        with pytest.raises(ValueError, match="Cannot share with 'bob@external.com'"):
            validate_share_target("user", "bob@external.com")

    def test_subdomain_not_covered(self, restricted):
        with pytest.raises(ValueError):
            validate_share_target("user", "jane@sub.scientist.com")

    def test_email_without_at_rejected(self, restricted):
        with pytest.raises(ValueError):
            validate_share_target("user", "scientist.com")

    def test_empty_identifier_rejected(self, restricted):
        with pytest.raises(ValueError):
            validate_share_target("user", None)

    def test_anyone_always_rejected(self, restricted):
        with pytest.raises(ValueError, match="'anyone' \\(public/link\\) access"):
            validate_share_target("anyone", None)

    def test_allowed_domain_share(self, restricted):
        validate_share_target("domain", "scientist.com")
        validate_share_target("domain", "@Scientist.COM")

    def test_external_domain_rejected(self, restricted):
        with pytest.raises(ValueError, match="Cannot share with domain"):
            validate_share_target("domain", "external.com")

    def test_multiple_domains(self, monkeypatch):
        monkeypatch.setenv(ALLOWED_SHARE_DOMAINS_ENV, "scientist.com,partner.org")
        validate_share_target("user", "a@partner.org")
        with pytest.raises(ValueError):
            validate_share_target("user", "a@other.org")

    def test_operator_message_in_rejection(self, restricted, monkeypatch):
        monkeypatch.setenv(SHARE_RESTRICTED_MESSAGE_ENV, "Use the full endpoint.")
        with pytest.raises(ValueError, match="Use the full endpoint."):
            validate_share_target("user", "bob@external.com")


class TestValidateExistingPermissionTarget:
    def test_internal_user_permission_ok(self, restricted):
        validate_existing_permission_target(
            {"type": "user", "emailAddress": "jane@scientist.com"}
        )

    def test_external_user_permission_rejected(self, restricted):
        with pytest.raises(ValueError):
            validate_existing_permission_target(
                {"type": "user", "emailAddress": "bob@external.com"}
            )

    def test_anyone_permission_rejected(self, restricted):
        with pytest.raises(ValueError):
            validate_existing_permission_target({"type": "anyone"})

    def test_domain_permission_checks_domain_field(self, restricted):
        validate_existing_permission_target(
            {"type": "domain", "domain": "scientist.com"}
        )
        with pytest.raises(ValueError):
            validate_existing_permission_target(
                {"type": "domain", "domain": "external.com"}
            )


# ─── docstring note ─────────────────────────────────────────────────────────────


class TestDocstringNote:
    def test_empty_when_unset(self, unrestricted):
        assert share_restriction_doc_note() == ""

    def test_names_domains_when_set(self, restricted):
        note = share_restriction_doc_note()
        assert "RESTRICTED" in note and "scientist.com" in note

    def test_includes_operator_message(self, restricted, monkeypatch):
        monkeypatch.setenv(SHARE_RESTRICTED_MESSAGE_ENV, "Use the full endpoint.")
        assert "Use the full endpoint." in share_restriction_doc_note()

    def test_decorator_appends_when_set(self, restricted):
        def dummy():
            """Original docs."""

        decorated = _with_share_restriction_note(dummy)
        assert decorated is dummy
        assert dummy.__doc__.startswith("Original docs.")
        assert "RESTRICTED" in dummy.__doc__

    def test_decorator_inserts_above_args_section(self, restricted):
        # FastMCP keeps only the text above "Args:" as the tool description,
        # so the note must land before that marker to be visible to clients.
        def dummy():
            pass

        # Module-level tool docstrings indent sections by 4 spaces; set
        # __doc__ directly so the formatter can't reindent the marker.
        dummy.__doc__ = "Summary line.\n\n    Args:\n        x: A thing.\n    "

        _with_share_restriction_note(dummy)
        assert dummy.__doc__.index("RESTRICTED") < dummy.__doc__.index("Args:")

    def test_decorator_noop_when_unset(self, unrestricted):
        def dummy():
            """Original docs."""

        _with_share_restriction_note(dummy)
        assert dummy.__doc__ == "Original docs."


# ─── manage_drive_access enforcement ────────────────────────────────────────────


@pytest.mark.asyncio
class TestManageDriveAccess:
    async def test_grant_internal_succeeds(self, restricted):
        service = _make_service(
            create={
                "type": "user",
                "role": "reader",
                "emailAddress": "jane@scientist.com",
            }
        )
        with _resolve_patch():
            result = await manage_access_fn(
                service=service,
                user_google_email="me@scientist.com",
                file_id="file123",
                action="grant",
                share_with="jane@scientist.com",
            )
        assert "Successfully shared" in result
        service.permissions.return_value.create.assert_called_once()

    async def test_grant_external_rejected_before_any_api_call(self, restricted):
        service = _make_service()
        with pytest.raises(ValueError, match="restricted to these domains"):
            await manage_access_fn(
                service=service,
                user_google_email="me@scientist.com",
                file_id="file123",
                action="grant",
                share_with="bob@external.com",
            )
        service.permissions.return_value.create.assert_not_called()

    async def test_grant_anyone_rejected(self, restricted):
        with pytest.raises(ValueError, match="public/link"):
            await manage_access_fn(
                service=_make_service(),
                user_google_email="me@scientist.com",
                file_id="file123",
                action="grant",
                share_type="anyone",
            )

    async def test_grant_external_domain_rejected(self, restricted):
        with pytest.raises(ValueError, match="Cannot share with domain"):
            await manage_access_fn(
                service=_make_service(),
                user_google_email="me@scientist.com",
                file_id="file123",
                action="grant",
                share_type="domain",
                share_with="external.com",
            )

    async def test_grant_external_allowed_when_unset(self, unrestricted):
        service = _make_service(
            create={
                "type": "user",
                "role": "reader",
                "emailAddress": "bob@external.com",
            }
        )
        with _resolve_patch():
            result = await manage_access_fn(
                service=service,
                user_google_email="me@scientist.com",
                file_id="file123",
                action="grant",
                share_with="bob@external.com",
            )
        assert "Successfully shared" in result

    async def test_grant_batch_skips_external_keeps_internal(self, restricted):
        service = _make_service(
            create={
                "type": "user",
                "role": "reader",
                "emailAddress": "jane@scientist.com",
            }
        )
        with _resolve_patch():
            result = await manage_access_fn(
                service=service,
                user_google_email="me@scientist.com",
                file_id="file123",
                action="grant_batch",
                recipients=[
                    {"email": "jane@scientist.com"},
                    {"email": "bob@external.com"},
                ],
            )
        assert "1 succeeded, 1 failed" in result
        assert "bob@external.com: Failed" in result
        assert "restricted to these domains" in result
        service.permissions.return_value.create.assert_called_once()

    async def test_update_external_permission_rejected(self, restricted):
        service = _make_service(
            get={"type": "user", "role": "reader", "emailAddress": "bob@external.com"}
        )
        with _resolve_patch(), pytest.raises(ValueError, match="restricted"):
            await manage_access_fn(
                service=service,
                user_google_email="me@scientist.com",
                file_id="file123",
                action="update",
                permission_id="perm1",
                role="writer",
            )
        service.permissions.return_value.update.assert_not_called()

    async def test_update_internal_permission_succeeds(self, restricted):
        service = _make_service(
            get={
                "type": "user",
                "role": "reader",
                "emailAddress": "jane@scientist.com",
            },
            update={
                "type": "user",
                "role": "writer",
                "emailAddress": "jane@scientist.com",
            },
        )
        with _resolve_patch():
            result = await manage_access_fn(
                service=service,
                user_google_email="me@scientist.com",
                file_id="file123",
                action="update",
                permission_id="perm1",
                role="writer",
            )
        assert "Successfully updated" in result
        # restriction active + explicit role → the permission is still fetched
        # to vet its target
        service.permissions.return_value.get.assert_called_once()

    async def test_update_skips_lookup_when_unset_and_role_given(self, unrestricted):
        service = _make_service(
            update={
                "type": "user",
                "role": "writer",
                "emailAddress": "bob@external.com",
            }
        )
        with _resolve_patch():
            await manage_access_fn(
                service=service,
                user_google_email="me@scientist.com",
                file_id="file123",
                action="update",
                permission_id="perm1",
                role="writer",
            )
        service.permissions.return_value.get.assert_not_called()

    async def test_revoke_always_allowed(self, restricted):
        service = _make_service()
        with _resolve_patch():
            result = await manage_access_fn(
                service=service,
                user_google_email="me@scientist.com",
                file_id="file123",
                action="revoke",
                permission_id="perm1",
            )
        assert "revoked" in result
        service.permissions.return_value.delete.assert_called_once()

    async def test_transfer_owner_external_rejected(self, restricted):
        service = _make_service()
        with pytest.raises(ValueError, match="restricted"):
            await manage_access_fn(
                service=service,
                user_google_email="me@scientist.com",
                file_id="file123",
                action="transfer_owner",
                new_owner_email="bob@external.com",
            )
        service.permissions.return_value.create.assert_not_called()

    async def test_transfer_owner_internal_succeeds(self, restricted):
        service = _make_service(create={"id": "perm2"})
        with patch(
            "gdrive.drive_tools.resolve_drive_item",
            return_value=(
                "file123",
                {"name": "Test File", "owners": [{"emailAddress": "me@scientist.com"}]},
            ),
        ):
            result = await manage_access_fn(
                service=service,
                user_google_email="me@scientist.com",
                file_id="file123",
                action="transfer_owner",
                new_owner_email="jane@scientist.com",
            )
        assert "jane@scientist.com" in result
        service.permissions.return_value.create.assert_called_once()


# ─── set_drive_file_permissions enforcement ─────────────────────────────────────


@pytest.mark.asyncio
class TestSetDriveFilePermissions:
    async def test_link_sharing_rejected_when_restricted(self, restricted):
        service = _make_service()
        with pytest.raises(ValueError, match="anyone with the link"):
            await set_permissions_fn(
                service=service,
                user_google_email="me@scientist.com",
                file_id="file123",
                link_sharing="reader",
            )
        service.permissions.return_value.update.assert_not_called()

    async def test_link_sharing_off_allowed(self, restricted):
        service = _make_service(list={"permissions": []})
        with _resolve_patch():
            result = await set_permissions_fn(
                service=service,
                user_google_email="me@scientist.com",
                file_id="file123",
                link_sharing="off",
            )
        assert "already off" in result

    async def test_tightening_flags_allowed(self, restricted):
        service = _make_service()
        with _resolve_patch():
            result = await set_permissions_fn(
                service=service,
                user_google_email="me@scientist.com",
                file_id="file123",
                writers_can_share=False,
            )
        assert "restricted to owner" in result

    async def test_link_sharing_allowed_when_unset(self, unrestricted):
        service = _make_service(
            list={"permissions": []},
            create={"id": "anyone1", "type": "anyone", "role": "reader"},
        )
        with _resolve_patch():
            result = await set_permissions_fn(
                service=service,
                user_google_email="me@scientist.com",
                file_id="file123",
                link_sharing="reader",
            )
        assert "Permission settings updated" in result
