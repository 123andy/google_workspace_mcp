# Granular tool selection: `--only-tools` and `--exclude-tools`

This server already ships three ways to decide which tools (and which OAuth
scopes) a running instance exposes:

- **`--tool-tier core|extended|complete`** — fixed, *cumulative* tiers. Each tier
  is a curated superset of the previous one across every service.
- **`--permissions service:level ...`** — *cumulative* permission **levels** per
  service (e.g. `gmail:readonly` ⊂ `gmail:organize` ⊂ `gmail:drafts` ⊂
  `gmail:send` ⊂ `gmail:full`). A level maps to a set of scopes, and any tool
  whose required scopes aren't all covered is dropped.
- **`--tools gmail drive ...`** — whole **services**. All or nothing per service.

These three are the right tool most of the time. But they share two structural
limits, and the two fork flags described here exist to fill exactly those gaps.

## The gap these flags fill

None of the three built-in selectors can do either of the following:

1. **Expose an arbitrary, disjoint subset of tools with a minimal grant.** Tiers
   and permission levels are cumulative ladders; `--tools` is whole services.
   There is no built-in way to say "expose *exactly* these four tools, drawn from
   three different services, and request *only* the scopes those four tools
   need." You always end up over-granting scopes and over-exposing tools.

2. **Take a permission level "minus a couple of dangerous tools."** Permission
   levels are defined by *scopes*, and several distinct tools share a single
   Google scope. If you want `drive:full` but without the file-sharing tools,
   you can't express that with `--permissions` alone, because the sharing tools
   ride on the same `drive.file` scope as the create/edit tools you want to keep.
   Dropping the scope would drop the tools you need; keeping the scope keeps the
   tools you don't.

`--only-tools` solves (1). `--exclude-tools` solves (2).

## `--only-tools` — exact allowlist + minimal scopes

```bash
--only-tools send_gmail_message manage_drive_access
```

`--only-tools` takes an explicit list of **tool names** and does two things:

1. **Allowlist the tools.** Only the named tools are registered; everything else
   is removed. The list can be an arbitrary, disjoint subset that crosses
   service boundaries.
2. **Derive the minimal scope union.** The server inspects exactly those tools'
   declared Google scopes (`_required_google_scopes`) and requests **only** that
   union plus the base identity scopes (`userinfo.email`, `openid`). It bypasses
   the service-granular scope maps entirely.

So `--only-tools` **narrows both layers at once**: the tool surface *and* the
OAuth grant. The token you mint can do nothing beyond what those specific tools
require. This is the tightest possible grant for a given set of tools.

`--only-tools` is **mutually exclusive** with `--tools`, `--tool-tier`,
`--permissions`, `--read-only`, **and `--exclude-tools`** — it is a self-contained
selector that picks both tools and scopes on its own, so combining it with another
selector is contradictory and rejected with an error. (Excluding one of its tools
would drop that tool from the surface while still requesting its scope, defeating
the minimal grant; just omit the tool from the `--only-tools` list instead.)

**When to use it:** you want a purpose-built endpoint that does a few specific
things and nothing else, with the smallest OAuth consent screen possible. An
agent that only sends email and shares Drive files, for example, should never be
granted read access to your whole mailbox.

## `--exclude-tools` — blocklist that composes, scopes untouched

```bash
--permissions drive:full --exclude-tools manage_drive_access set_drive_file_permissions
```

`--exclude-tools` takes a list of **tool names** and removes them from whatever
set the *other* selectors produced. Crucially:

- It **composes with** `--permissions`, `--tools`, `--tool-tier`, and the default
  (all-tools) mode. It is **not** mutually exclusive with them — you layer it on
  top. First the normal selector decides the tool/scope set; then
  `--exclude-tools` trims named tools out of that set. (The one exception is
  `--only-tools`, which already picks an exact list *and* its minimal scopes;
  combining the two is rejected — see above.)
- It does **not** change the requested OAuth scopes. The token keeps every scope
  the *remaining* tools need. This is the entire point: it lets you drop a tool
  whose scope is **shared** with tools you keep. The excluded tool is removed at
  the **tool layer**, not the scope layer — the underlying scope stays on the
  token because another, kept tool still needs it.

Unknown tool names are rejected with an error, same as `--only-tools`.

**When to use it:** you've selected a permission level or service set you're
happy with, except for one or two specific tools you want gone — and those tools
can't be removed by tightening scopes because they share a coarse scope with
tools you need. `--exclude-tools` removes them at the tool layer as a
defense-in-depth measure.

## The key distinction

This is the single most important thing to understand about the two flags:

- **`--only-tools` = scope-AND-tool narrowing.** It rebuilds the OAuth grant from
  scratch as the minimal union for exactly the listed tools. The token is as
  tight as the tools allow.
- **`--exclude-tools` = tool-ONLY narrowing.** It leaves the OAuth grant exactly
  as the other selectors set it and only removes tools from the registered
  surface. The token can still *technically* do what the excluded tool did
  (because a kept tool shares the scope); the excluded tool simply isn't exposed.

| Selector | Tool granularity | Scope effect | Composes with others |
|---|---|---|---|
| `--tool-tier` | cumulative tier | cumulative (per tier) | with `--tools` |
| `--permissions` | scope-driven (per level) | cumulative levels | with `--tool-tier` |
| `--tools` | whole service | whole service | with `--tool-tier` |
| **`--only-tools`** | **arbitrary tool subset** | **minimal union of those tools** | **no (exclusive)** |
| **`--exclude-tools`** | **arbitrary tool subset (removed)** | **none — scopes unchanged** | **yes, on top of any selector except `--only-tools`** |

## Worked real-world example: additive permission endpoints

This is exactly how the two flags are used in production. The goal is to run
**two MCP server instances together** whose tool sets are **disjoint**, so a
single agent wired to both sees no overlapping tools — yet each endpoint holds
only the scopes it actually needs. We split the workspace into a low-risk
"read/draft" **base** endpoint and a high-risk "commit" **delta** endpoint.

### The delta endpoint — the risky "commit" actions, tightly scoped

```bash
python main.py --only-tools \
  send_gmail_message send_message \
  manage_drive_access set_drive_file_permissions
```

This exposes **only** those four commit tools and, via `--only-tools`' minimal
scope derivation, requests **only**:

- `gmail.send` (for `send_gmail_message`)
- `chat.messages` (for `send_message`)
- `drive.file` (for `manage_drive_access` and `set_drive_file_permissions`)

No read scopes whatsoever. This endpoint physically cannot read a mailbox, list
Drive files, or browse Chat history — its token doesn't carry those scopes.

### The base endpoint — everything else, with two surgical exclusions

```bash
python main.py \
  --permissions \
    gmail:drafts chat:readonly drive:full calendar:full \
    docs:full sheets:full slides:full tasks:full contacts:full forms:full \
  --exclude-tools manage_drive_access set_drive_file_permissions
```

This endpoint provides the broad read/draft/edit surface. The `--exclude-tools`
clause is what keeps it from overlapping with the delta endpoint on Drive. Here's
**why it's needed** — and why Gmail and Chat *don't* need it:

- **Gmail and Chat separate cleanly by scope.** The base endpoint requests
  `gmail:drafts` (which stops below `gmail:send`) and `chat:readonly` (which
  stops below `chat.messages`). Because the base token lacks `gmail.send` and
  `chat.messages`, the permissions filter **automatically removes**
  `send_gmail_message` and `send_message` — their required scopes aren't covered.
  No exclusion needed; the scope boundary does the work.

- **Drive cannot separate by scope.** Both "create / edit a file"
  (`create_drive_file`, `update_drive_file`, ...) and "share a file"
  (`manage_drive_access`, `set_drive_file_permissions`) require the **same**
  `drive.file` scope. The base endpoint genuinely needs `drive:full` so it can
  create and edit documents — but `drive:full` includes `drive.file`, which
  **unavoidably** also satisfies the sharing tools. There is no permission level
  that gives "create/edit but not share," because Google models both with one
  scope.

  So we drop the two sharing tools at the **tool layer** with
  `--exclude-tools manage_drive_access set_drive_file_permissions`. The base
  endpoint's token still holds `drive.file` (it must, to create files), but the
  sharing tools are no longer exposed on this endpoint. Sharing is available
  **only** on the delta endpoint, preserving the disjoint split.

This is tool-layer, defense-in-depth enforcement: the base endpoint *could*
technically share a file at the API level because its token carries `drive.file`,
but no tool on that endpoint lets an agent do so. The capability is concentrated
on the delta endpoint, where it's deliberately and visibly granted.

The net result: an agent connected to both endpoints sees one unified,
non-overlapping tool set, while each endpoint's OAuth grant is as narrow as
Google's scope model permits.

## Scope vs tool layer — what to rely on

Prefer **scope-level** exclusion wherever Google's scopes let you express the
boundary, and fall back to **tool-level** exclusion only where a coarse scope
forces your hand:

- **First choice — narrow the scope.** Use `--only-tools` (which derives a
  minimal grant) or a tighter `--permissions` level. If the capability you want
  to deny has its own scope, deny that scope. A token that never received the
  scope cannot exercise the capability through *any* tool, present or future.
  This is real, enforced-at-Google security.

- **Fallback — exclude the tool.** Use `--exclude-tools` only when several tools
  share one coarse scope and you must keep that scope for the tools you want
  (the `drive.file` situation above). This is defense-in-depth at the tool layer:
  it removes the tool from the exposed surface, but the token still technically
  holds the scope. Treat it as "don't expose this," not "can't possibly do this."

In short: deny by scope when you can, deny by tool when you must.

## Restricting Drive sharing to trusted domains

Sometimes removing the sharing tools entirely (`--exclude-tools`) is more than
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

This composes naturally with the split-endpoint pattern above: a base endpoint
can expose domain-restricted sharing for everyday internal collaboration, while
unrestricted sharing stays concentrated on the deliberately-granted elevated
endpoint.
