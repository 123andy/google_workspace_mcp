# Restricting Drive sharing to trusted domains

Sometimes removing the sharing tools entirely (`--disabled-tools`) is more than
you want: an endpoint may legitimately need to share files *inside* the
organization while never granting access to outsiders or minting public links.
The same coarse-scope problem applies — Google offers no OAuth scope that
separates internal from external sharing — so this, too, is tool-layer
enforcement, configured with two environment variables:

- **`WORKSPACE_MCP_ALLOWED_SHARE_DOMAINS`** — comma-separated domain allowlist
  (e.g. `example.com,example.org`). When set:
  - `manage_drive_access` only grants to users/groups whose email is in an
    allowed domain, and only to allowed domains for domain-wide shares;
    `anyone` grants are rejected. `update` re-validates the *existing*
    permission's target (so an external permission can't be escalated),
    `transfer_owner` checks the new owner, and `revoke` is always allowed.
  - `set_drive_file_permissions` rejects `link_sharing` values other than
    `off` (they mint an "anyone with the link" permission); the
    restriction-tightening flags still work.
  - Both tools advertise the restriction in their registered description, so
    agents can route external-sharing requests elsewhere *before* a failed
    call.

  Matching is case-insensitive and exact — `example.com` does not cover
  `sub.example.com`; list subdomains explicitly. Unset (the default) leaves
  behavior completely unchanged. Note the limits of address-level enforcement:
  a group address in an allowed domain may still contain external members.

- **`WORKSPACE_MCP_SHARE_RESTRICTED_MESSAGE`** — optional operator text
  **appended** to (never replacing) the default rejection message and the
  tools' description note. Use it for deployment-specific routing, e.g.
  "For external sharing, use the manage_drive_access tool on the elevated
  endpoint."

This composes naturally with a split-endpoint deployment (see [only-tools.md](only-tools.md)): a base endpoint
can expose domain-restricted sharing for everyday internal collaboration, while
unrestricted sharing stays concentrated on the deliberately-granted elevated
endpoint.
