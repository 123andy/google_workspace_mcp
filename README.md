<!-- mcp-name: io.github.taylorwilsdon/workspace-mcp -->

<div align="center">

# <span style="color:#cad8d9">Google Workspace MCP Server</span> <img src="https://github.com/user-attachments/assets/b89524e4-6e6e-49e6-ba77-00d6df0c6e5c" width="80" align="right" />

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![PyPI](https://img.shields.io/pypi/v/workspace-mcp.svg)](https://pypi.org/project/workspace-mcp/)
[![PyPI Downloads](https://static.pepy.tech/personalized-badge/workspace-mcp?period=total&units=NONE&left_color=GREY&right_color=BLUE&left_text=pypi+downloads)](https://pepy.tech/projects/workspace-mcp)
[![MCP Toplist](https://mcptoplist.com/badge/glama%2Ftaylorwilsdon%2Fgoogle_workspace_mcp.svg)](https://mcptoplist.com/server/glama%2Ftaylorwilsdon%2Fgoogle_workspace_mcp)
[![Website](https://img.shields.io/badge/Website-workspacemcp.com-green.svg)](https://workspacemcp.com/?utm_source=github.com&utm_medium=referral&utm_campaign=readme&utm_content=badge-website)

*Full natural language control over Google Calendar, Drive, Gmail, Docs, Sheets, Slides, Forms, Tasks, Contacts, and Chat through all MCP clients, AI assistants and developer tools.*
*Includes a full featured CLI & Code Mode for use with tools like Claude Code and Codex!*

**The fastest, most feature-complete Google Workspace MCP server** is in a class of it's own: it can do things that Google's own tooling and the built in integrations with Claude and ChatGPT can't come close to with multi-user support, rich fine-grained editing tools and the most extensive coverage of any Workspace AI integration in existence. 

By leveraging native OAuth 2.1, stateless deployment capability and external auth server & gateway passthrough auth support, it's also the only Workspace MCP you can host for your whole organization centrally & securely! Supports all free Google accounts & Google Workspace plans with expanded app options like Chat & Spaces.

</div>

<p align="center">
  <a href="https://workspacemcp.com/docs?utm_source=github.com&utm_medium=referral&utm_campaign=readme&utm_content=hero-docs">
    <img src="https://img.shields.io/badge/Read%20the%20Docs-0969DA?style=for-the-badge&logo=readthedocs&logoColor=white" alt="Read the Docs">
  </a><a href="https://workspacemcp.com/quick-start?utm_source=github.com&utm_medium=referral&utm_campaign=readme&utm_content=hero-quickstart">
    <img src="https://img.shields.io/badge/Quick%20Start-2EA44F?style=for-the-badge" alt="Quick Start Guide">
  </a>
</p>

<div align="center">
<a href="https://www.pulsemcp.com/servers/taylorwilsdon-google-workspace">
<img width="375" src="https://github.com/user-attachments/assets/0794ef1a-dc1c-447d-9661-9c704d7acc9d" align="center"/>
</a>
</div>

<div align="center">
<a href="https://workspacemcp.com/workspace-mcp-cloud?utm_source=github.com&utm_medium=referral&utm_campaign=readme&utm_content=hero-cloud"><img width="400" height="53" alt="workspace-cloud" src="https://github.com/user-attachments/assets/349a6bab-c83a-4254-be2e-4ac6a9e8b3e5" /></a>
</div>

---

**See it in action:**
<div align="center">
  <video width="400" src="https://github.com/user-attachments/assets/a342ebb4-1319-4060-a974-39d202329710"></video>
</div>

---

## What It Does

Workspace MCP connects AI assistants to all twelve major Google Workspace services - 120+ tools behind a single MCP server, with OAuth 2.1 multi-user auth, three progressive tool tiers, read-only mode, a full CLI, and stateless container deployment. It runs locally over stdio for legacy clients and remotely over streamable HTTP with full implementation of the latest MCP spec.

The README covers just enough to get you running, with extensive documentation on the website:

| Where to go | What you'll find |
|:---|:---|
| **[Quick&nbsp;Start](https://workspacemcp.com/quick-start?utm_source=github.com&utm_medium=referral&utm_campaign=readme&utm_content=nav-quickstart)** | Google Cloud setup, credentials, and client connection with screenshots |
| **[Full&nbsp;Documentation](https://workspacemcp.com/docs?utm_source=github.com&utm_medium=referral&utm_campaign=readme&utm_content=nav-docs)** | Every tool, parameter, and auth mode |
| **[Advanced&nbsp;Deployment](https://workspacemcp.com/docs/deployment?utm_source=github.com&utm_medium=referral&utm_campaign=readme&utm_content=nav-deployment)** | Reverse proxy & nginx config, origin validation, credential store backends (GCS/CMEK), [trusted-gateway identity](https://workspacemcp.com/docs/deployment/gateway-identity?utm_source=github.com&utm_medium=referral&utm_campaign=readme&utm_content=nav-gateway-identity), and the complete environment variable reference |
| **[Client&nbsp;Setup&nbsp;Guides](https://workspacemcp.com/guides?utm_source=github.com&utm_medium=referral&utm_campaign=readme&utm_content=nav-guides)** | Claude Desktop/web Connectors, ChatGPT Developer Mode, and more |
| **[FAQ&nbsp;&&nbsp;Troubleshooting](https://workspacemcp.com/welcome/faq?utm_source=github.com&utm_medium=referral&utm_campaign=readme&utm_content=nav-faq)** | Enabling Google APIs, OAuth errors, redirect URIs, Google Chat setup, client quirks |

## <span style="color:#adbcbc">Security & Compliance</span>

<table>
<tr>
<td valign="top" width="50%">

**For Security Teams**

By default, this server sends no data anywhere except Google's APIs, on behalf of the authenticated user, using your own OAuth client credentials. There is no usage reporting, analytics, license server, or SaaS dependency outside optional OTel support for your own usage.

- **Fully open source** — every line is auditable in this repo
- **Your OAuth client, your GCP project** — credentials never leave your environment & you control scopes
- **You control the network** — deploy behind your reverse proxy, in your VPC, on your own terms
- **Stateless mode** — zero disk writes for locked-down container environments
- **Sensitive path blocking** — local file reads default to the managed attachment directory, and `validate_file_path()` still blocks `.env*` files plus common home-directory credential stores such as `~/.ssh/` and `~/.aws/` even if `ALLOWED_FILE_DIRS` is broadened

Full dependency tree in `pyproject.toml`, pinned in `uv.lock`.

</td>
<td valign="top" width="50%">

**For Legal & Procurement**

This project is [MIT licensed](LICENSE) — 

- **Use commercially without restriction** — build products, sell services, deploy internally
- **Fork, embed, redistribute** — MIT requires only attribution
- **No CLA** — contributions remain under MIT
- **No built-in telemetry to disclose** — optional tracing is off unless you configure it
- **No network effects** — the server never contacts any endpoint you didn't configure
- **Standard dependency licenses** — MIT, Apache 2.0, and BSD throughout the dependency chain; no copyleft, no AGPL
</td>
</tr>
</table>

## Services

<table width="100%" align="center">
<tr>
<td align="center" width="25%">
<h3>📧</h3><a href="https://workspacemcp.com/gmail?utm_source=github.com&utm_medium=referral&utm_campaign=readme&utm_content=services-gmail"><b>Gmail</b></a><br>
<sub>15 tools - search, send, draft,<br>labels, filters, attachments</sub>
</td>
<td align="center" width="25%">
<h3>📁</h3><a href="https://workspacemcp.com/google-drive?utm_source=github.com&utm_medium=referral&utm_campaign=readme&utm_content=services-google-drive"><b>Drive</b></a><br>
<sub>16 tools - search, create, share,<br>import Office files</sub>
</td>
<td align="center" width="25%">
<h3>📅</h3><a href="https://workspacemcp.com/google-calendar?utm_source=github.com&utm_medium=referral&utm_campaign=readme&utm_content=services-google-calendar"><b>Calendar</b></a><br>
<sub>7 tools - events, sharing, free/busy,<br>Out of Office, Focus Time</sub>
</td>
<td align="center" width="25%">
<h3>📝</h3><a href="https://workspacemcp.com/google-docs?utm_source=github.com&utm_medium=referral&utm_campaign=readme&utm_content=services-google-docs"><b>Docs</b></a><br>
<sub>19 tools - edit, style, tables,<br>tabs, comments, export</sub>
</td>
</tr>
<tr>
<td align="center" width="25%">
<h3>📊</h3><a href="https://workspacemcp.com/google-sheets?utm_source=github.com&utm_medium=referral&utm_campaign=readme&utm_content=services-google-sheets"><b>Sheets</b></a><br>
<sub>14 tools - ranges, tables,<br>formatting, conditional rules</sub>
</td>
<td align="center" width="25%">
<h3>🖼️</h3><a href="https://workspacemcp.com/google-slides?utm_source=github.com&utm_medium=referral&utm_campaign=readme&utm_content=services-google-slides"><b>Slides</b></a><br>
<sub>7 tools - create, batch update,<br>slide images, comments</sub>
</td>
<td align="center" width="25%">
<h3>📋</h3><a href="https://workspacemcp.com/google-forms?utm_source=github.com&utm_medium=referral&utm_campaign=readme&utm_content=services-google-forms"><b>Forms</b></a><br>
<sub>6 tools - build forms, publish,<br>read responses</sub>
</td>
<td align="center" width="25%">
<h3>✅</h3><a href="https://workspacemcp.com/google-tasks?utm_source=github.com&utm_medium=referral&utm_campaign=readme&utm_content=services-google-tasks"><b>Tasks</b></a><br>
<sub>6 tools - tasks & lists<br>with hierarchy</sub>
</td>
</tr>
<tr>
<td align="center" width="25%">
<h3>👤</h3><a href="https://workspacemcp.com/google-contacts?utm_source=github.com&utm_medium=referral&utm_campaign=readme&utm_content=services-google-contacts"><b>Contacts</b></a><br>
<sub>8 tools - people, groups,<br>batch operations</sub>
</td>
<td align="center" width="25%">
<h3>💬</h3><a href="https://workspacemcp.com/google-chat?utm_source=github.com&utm_medium=referral&utm_campaign=readme&utm_content=services-google-chat"><b>Chat</b></a><br>
<sub>6 tools - spaces, messages,<br>search, reactions</sub>
</td>
<td align="center" width="25%">
<h3>🔍</h3><a href="https://workspacemcp.com/google-search?utm_source=github.com&utm_medium=referral&utm_campaign=readme&utm_content=services-google-search"><b>Custom Search</b></a><br>
<sub>2 tools - programmable<br>web search</sub>
</td>
<td align="center" width="25%">
<h3>⚡</h3><a href="https://workspacemcp.com/google-apps-script?utm_source=github.com&utm_medium=referral&utm_campaign=readme&utm_content=services-google-apps-script"><b>Apps Script</b></a><br>
<sub>15 tools - write, deploy,<br>run & debug scripts</sub>
</td>
</tr>
</table>

Each page lists every tool with its tier, parameters, required scopes, and example prompts. The [complete reference](https://workspacemcp.com/docs?utm_source=github.com&utm_medium=referral&utm_campaign=readme&utm_content=services-docs-all) covers all twelve in one place.

> 💬 **Google Chat** needs a one-time Chat app configuration and a Workspace account - see the [Chat setup FAQ](https://workspacemcp.com/welcome/faq?utm_source=github.com&utm_medium=referral&utm_campaign=readme&utm_content=services-chat-faq).

## Quick Start

> Set credentials → pick a launch command → connect your client. Full walkthrough with screenshots: **[workspacemcp.com/quick-start](https://workspacemcp.com/quick-start?utm_source=github.com&utm_medium=referral&utm_campaign=readme&utm_content=quickstart-hero)**

You'll need an OAuth client from [Google Cloud Console](https://console.cloud.google.com/) in a project with the Google APIs enabled for the services you plan to use. The docs have [one-click enable links for every API](https://workspacemcp.com/docs?utm_source=github.com&utm_medium=referral&utm_campaign=readme&utm_content=quickstart-enable-apis#authentication) plus a single `gcloud services enable` command that covers them all, and the [quick start guide](https://workspacemcp.com/quick-start?utm_source=github.com&utm_medium=referral&utm_campaign=readme&utm_content=quickstart-inline) walks through the whole setup in about five minutes.

> **Agentic OAuth clients:** this server only calls Google's REST APIs, so your OAuth client does not need Google's "Use this client for an AI-powered agent" setting. If your organization wants agent traffic labeled for admin controls, create a separate client with that setting enabled; it can't be changed on an existing client after November 12, 2026.

<table>
<tr>
<td valign="top" width="50%">

**Confidential Client**

```bash
# 1. Credentials
export GOOGLE_OAUTH_CLIENT_ID="..."
export GOOGLE_OAUTH_CLIENT_SECRET="..."

# 2. Launch - pick a tier
uvx workspace-mcp --tool-tier core       # essential tools
uvx workspace-mcp --tool-tier extended   # core + management ops
uvx workspace-mcp --tool-tier complete   # everything

# Or cherry-pick services
uvx workspace-mcp --tools gmail drive calendar
```

</td>
<td valign="top" width="50%">

**OAuth 2.1 (PKCE)**

```bash
# 1. Credentials - MCP clients connect with PKCE and no
#    secret, but Google still requires one server-side
export MCP_ENABLE_OAUTH21=true
export GOOGLE_OAUTH_CLIENT_ID="..."
export GOOGLE_OAUTH_CLIENT_SECRET="..."
#    Alternatively, point GOOGLE_CLIENT_SECRET_PATH at a client_secret.json
#    that contains the client id and secret (env vars take precedence).
export WORKSPACE_MCP_PORT=8000
export GOOGLE_OAUTH_REDIRECT_URI="http://localhost:${WORKSPACE_MCP_PORT}/oauth2callback"
export OAUTHLIB_INSECURE_TRANSPORT=1

# 2. Launch - OAuth 2.1 requires HTTP transport
uvx workspace-mcp --transport streamable-http --tool-tier core
```

</td>
</tr>
</table>

**Tool tiers** keep context windows lean: `core` is the essential set, `extended` adds management operations, `complete` loads everything. Combine with `--tools <service> ...`, `--read-only`, or per-service `--permissions`, and subtract individual tools with `--disabled-tools <name> ...` - details in the [server modes docs](https://workspacemcp.com/docs?utm_source=github.com&utm_medium=referral&utm_campaign=readme&utm_content=tiers-server-modes#server-modes).

**Per-request toolsets** let HTTP clients sharing one server see different tools, using the same headers as the GitHub MCP server. `X-MCP-Toolsets` takes comma-separated service names (the `--tools` names, e.g. `gmail,drive`) and `X-MCP-Tools` takes comma-separated tool names (e.g. `search_gmail_messages`); when both are sent the request gets the union. Names ignore case and whitespace, unknown names match nothing, and a blank or missing header leaves the tool list unchanged. Headers only narrow what the server already loaded: a tool removed by the tier, `--tools`, `--read-only`, `--permissions` or `--disabled-tools` stays unavailable, and calls to tools outside the request's selection fail as unknown tools.

## Connect Your Client

**Claude Desktop, web & mobile** - run the server in HTTP mode and add it as a **Connector** (Settings → Connectors → Add custom connector). This is the recommended path; the [Connector guide](https://workspacemcp.com/guides/claude-connectors?utm_source=github.com&utm_medium=referral&utm_campaign=readme&utm_content=clients-connectors) has step-by-step screenshots. Legacy stdio configuration remains available for clients without Connector support - see the [FAQ](https://workspacemcp.com/welcome/faq?utm_source=github.com&utm_medium=referral&utm_campaign=readme&utm_content=clients-connectors-faq).

**Claude Code**

```bash
# Start the server in HTTP mode, then:
claude mcp add --transport http workspace-mcp http://localhost:8000/mcp

# Optional: install the bundled skill for better Workspace tool routing
ln -s "$(pwd)/skills/managing-google-workspace" ~/.claude/skills/managing-google-workspace
```

**ChatGPT** - connect via Developer Mode with the [ChatGPT guide](https://workspacemcp.com/guides/chatgpt-developer-mode?utm_source=github.com&utm_medium=referral&utm_campaign=readme&utm_content=clients-chatgpt).

**VS Code, LM Studio, Open WebUI, and everything else** - any MCP client works over streamable HTTP (recommended) or stdio. Client-specific walkthroughs live in the [guides](https://workspacemcp.com/guides?utm_source=github.com&utm_medium=referral&utm_campaign=readme&utm_content=clients-guides) and [FAQ](https://workspacemcp.com/welcome/faq?utm_source=github.com&utm_medium=referral&utm_campaign=readme&utm_content=clients-guides-faq).

## CLI

`workspace-cli` lists and calls tools against a running server with encrypted, disk-backed OAuth token caching - authenticate once, script forever:

```bash
uv run workspace-cli list
uv run workspace-cli call search_gmail_messages query="is:unread" max_results=5
```

Install globally with `uv tool install .` from this repo. ⚠️ Don't use `uvx workspace-cli` - an abandoned PyPI package squats that name.

## Deployment & Advanced Configuration

Everything you need to run this in production lives in two places. The [documentation](https://workspacemcp.com/docs?utm_source=github.com&utm_medium=referral&utm_campaign=readme&utm_content=deploy-docs) covers auth modes and server configuration:

- **[OAuth 2.1 multi-user auth](https://workspacemcp.com/docs?utm_source=github.com&utm_medium=referral&utm_campaign=readme&utm_content=deploy-oauth21#authentication)** - bearer tokens, required for remote or shared HTTP endpoints
- **[Stateless container mode](https://workspacemcp.com/docs?utm_source=github.com&utm_medium=referral&utm_campaign=readme&utm_content=deploy-stateless#authentication)** - zero disk writes for locked-down deployments
- **[OAuth proxy storage backends](https://workspacemcp.com/docs?utm_source=github.com&utm_medium=referral&utm_campaign=readme&utm_content=deploy-proxy-storage#authentication)** - memory, disk, or Valkey/Redis for distributed setups
- **[External OAuth provider mode](https://workspacemcp.com/docs?utm_source=github.com&utm_medium=referral&utm_campaign=readme&utm_content=deploy-external-oauth#authentication)** - bring your own auth server, validate bearer tokens only
- **[Service accounts with domain-wide delegation](https://workspacemcp.com/docs?utm_source=github.com&utm_medium=referral&utm_campaign=readme&utm_content=deploy-service-accounts#authentication)** - per-request user impersonation with an optional domain allowlist
- **[Trusted-gateway identity](https://workspacemcp.com/docs/deployment/gateway-identity?utm_source=github.com&utm_medium=referral&utm_campaign=readme&utm_content=deploy-gateway-identity)** - proxy-verified per-user isolation with Pomerium, Cloudflare Access, oauth2-proxy, or any JWKS-verifiable gateway
- **[OpenTelemetry tracing](https://workspacemcp.com/docs?utm_source=github.com&utm_medium=referral&utm_campaign=readme&utm_content=deploy-otel#server-modes)** - optional, off unless you configure an OTLP endpoint
- **Docker** - `docker build -t workspace-mcp . && docker run -p 8000:8000 workspace-mcp`

The **[Advanced Deployment guide](https://workspacemcp.com/docs/deployment?utm_source=github.com&utm_medium=referral&utm_campaign=readme&utm_content=deploy-advanced)** covers self-hosting specifics: reverse proxy setup with `WORKSPACE_EXTERNAL_URL` (including the nginx `Origin: null` consent workaround, the `WORKSPACE_MCP_ALLOW_NULL_ORIGIN_CONSENT` escape hatch, and the `Referrer-Policy` pitfall), origin validation and VS Code webview allowlisting, credential store backends (local directory or GCS with CMEK enforcement), and the **[complete environment variable reference](https://workspacemcp.com/docs/deployment?utm_source=github.com&utm_medium=referral&utm_campaign=readme&utm_content=deploy-env-vars#environment-variables)**.

Production tuning is all environment variables; the names below link to their reference entries.

- **Google API requests:** [`WORKSPACE_MCP_GOOGLE_API_WORKERS`](https://workspacemcp.com/docs/deployment?utm_source=github.com&utm_medium=referral&utm_campaign=readme&utm_content=deploy-env-vars#WORKSPACE_MCP_GOOGLE_API_WORKERS), [`WORKSPACE_MCP_GOOGLE_API_TIMEOUT_SECONDS`](https://workspacemcp.com/docs/deployment?utm_source=github.com&utm_medium=referral&utm_campaign=readme&utm_content=deploy-env-vars#WORKSPACE_MCP_GOOGLE_API_TIMEOUT_SECONDS)
- **External OAuth token validation:** [`WORKSPACE_MCP_TOKEN_VALIDATION_WORKERS`](https://workspacemcp.com/docs/deployment?utm_source=github.com&utm_medium=referral&utm_campaign=readme&utm_content=deploy-env-vars#WORKSPACE_MCP_TOKEN_VALIDATION_WORKERS), [`WORKSPACE_MCP_TOKEN_VALIDATION_CACHE_TTL`](https://workspacemcp.com/docs/deployment?utm_source=github.com&utm_medium=referral&utm_campaign=readme&utm_content=deploy-env-vars#WORKSPACE_MCP_TOKEN_VALIDATION_CACHE_TTL)
- **Memory ceilings:** [`WORKSPACE_MCP_MAX_FILE_BYTES`](https://workspacemcp.com/docs/deployment?utm_source=github.com&utm_medium=referral&utm_campaign=readme&utm_content=deploy-env-vars#WORKSPACE_MCP_MAX_FILE_BYTES), [`WORKSPACE_MCP_STATELESS_INLINE_MAX_BYTES`](https://workspacemcp.com/docs/deployment?utm_source=github.com&utm_medium=referral&utm_campaign=readme&utm_content=deploy-env-vars#WORKSPACE_MCP_STATELESS_INLINE_MAX_BYTES), [`WORKSPACE_MCP_MAX_OFFICE_XML_BYTES`](https://workspacemcp.com/docs/deployment?utm_source=github.com&utm_medium=referral&utm_campaign=readme&utm_content=deploy-env-vars#WORKSPACE_MCP_MAX_OFFICE_XML_BYTES)
- **Text returned per call:** `get_drive_file_content` returns long content a page at a time by default, so a long file does not fill the model's context. `WORKSPACE_MCP_MAX_CONTENT_CHARS` sets the page size (default `40000`; `0` returns whole content by default). Each page ends with a note giving the `offset` to read on from. This is only the default: a client can always pass `full_content=true` to get the whole file in one call. Text being read page by page is kept in memory for ten minutes so later pages are not downloaded again, within `WORKSPACE_MCP_PAGED_CONTENT_CACHE_BYTES` per process (default `33554432`, 32 MiB; `0` keeps nothing).
- **Sessions and health probes:** [`WORKSPACE_MCP_SESSION_IDLE_TIMEOUT`](https://workspacemcp.com/docs/deployment?utm_source=github.com&utm_medium=referral&utm_campaign=readme&utm_content=deploy-env-vars#WORKSPACE_MCP_SESSION_IDLE_TIMEOUT), [`WORKSPACE_MCP_READINESS_TIMEOUT_SECONDS`](https://workspacemcp.com/docs/deployment?utm_source=github.com&utm_medium=referral&utm_campaign=readme&utm_content=deploy-env-vars#WORKSPACE_MCP_READINESS_TIMEOUT_SECONDS)
- **DNS-rebinding protection:** `WORKSPACE_MCP_ALLOWED_HOSTS`, a comma-separated list of hostnames without ports (for example `workspace-mcp,10.0.0.5`). When set, requests whose `Host` is not loopback, listed, or the host of `WORKSPACE_EXTERNAL_URL` or an allowed origin get `403`, so include every name clients and health probes use. Unset, any `Host` is accepted.
- **Hosted servers without access to the caller's disk:** [`WORKSPACE_MCP_DISABLE_LOCAL_FILES`](https://workspacemcp.com/docs/deployment?utm_source=github.com&utm_medium=referral&utm_campaign=readme&utm_content=deploy-env-vars#WORKSPACE_MCP_DISABLE_LOCAL_FILES)
  With local files disabled, `create_drive_file`, `update_drive_file` and the import tools offer `return_upload_url`: the caller PUTs the bytes to the returned URL. If the file sits in a cloud-mirrored folder (Drive for desktop), copy it to a local directory first, since mirrored paths can stall mid-upload.
- **OAuth 2.1 token refresh and lifetimes:** see [`.env.oauth21`](.env.oauth21)
- **OAuth 2.1 consent screen:** `WORKSPACE_MCP_OAUTH_PROXY_REQUIRE_CONSENT=remember` shows the OAuth proxy's consent screen once per browser for each MCP client instead of on every authorization; cross-site navigations are still prompted. The default, `always`, prompts every time. Any other value fails startup.

For downloads, horizontally scaled or scale-to-zero HTTP deployments (Cloud Run, Kubernetes) can stage attachments in Google Cloud Storage instead of instance-local disk, whose `/attachments/{id}` URL may reach a different instance. Install the `gcs` extra and set `WORKSPACE_MCP_FILES_GCS_BUCKET` to a bucket name; objects are written under `WORKSPACE_MCP_FILES_GCS_PREFIX` (default `attachments`) and handed out as V4 signed URLs valid for one hour, which the client fetches straight from GCS. This also lets stateless mode return download URLs instead of inline files. stdio ignores the setting, since its client reads the local file path directly. The runtime service account needs object read and write access on the bucket — `roles/storage.objectUser`, or `roles/storage.objectCreator` plus `roles/storage.objectViewer` — and, without an exported private key, `roles/iam.serviceAccountTokenCreator` on itself so URLs can be signed through IAM. Read access is not optional: a signed URL carries the signer's authority, so without `storage.objects.get` the upload and the signing both succeed while the client's download fails with `403`. Objects are not deleted by the server, so add a bucket lifecycle rule that removes them after a few hours.

## This fork's additions

These features are carried on this fork's deploy line and are not in upstream (some are open upstream PRs).

**Exact tool selection (`--only-tools`)**

```bash
uv run main.py --only-tools send_gmail_message manage_drive_access
```

**`--only-tools`** takes an exact (and possibly cross-service) tool allowlist and *also* derives the **minimal** OAuth scopes those tools need — the smallest possible consent screen for a purpose-built endpoint. Mutually exclusive with `--tools`, `--tool-tier`, `--permissions`, `--read-only`, and `--disabled-tools` (just omit a tool from the list rather than disabling it). See [docs/only-tools.md](docs/only-tools.md), including how it differs from the subtractive `--disabled-tools`.

**Strict block list**: with `WORKSPACE_MCP_STRICT_DISABLED_TOOLS=true`, a `--disabled-tools` entry that matches no registered tool stops the server at startup instead of only logging a warning. Names of tools retired in a recent release stay accepted, so one block list works on the current and the previous image.

**PostgreSQL OAuth proxy storage**: set `WORKSPACE_MCP_OAUTH_PROXY_STORAGE_BACKEND=postgres` and `WORKSPACE_MCP_OAUTH_PROXY_POSTGRES_DSN` (install `workspace-mcp[postgres]`). An explicit backend always wins; with none set, a set `..._VALKEY_HOST` implies Valkey, then a set `..._POSTGRES_DSN` implies Postgres. Records are Fernet-encrypted as for disk/Valkey. Optional: `..._POSTGRES_TABLE` (`workspace_mcp_kv`), `..._POSTGRES_SWEEP_INTERVAL_SECONDS` (900; Postgres does not evict expired rows itself, so the server sweeps them opportunistically), `..._POSTGRES_POOL_MIN`/`_MAX` (1/5).

**Signed download URLs** (opt-in, for hosted deployments): with `WORKSPACE_MCP_SIGNED_DOWNLOAD_URLS=true`, `get_gmail_attachment_content`, `get_drive_file_download_url`, `get_gmail_message_content(full=true)` and `download_chat_attachment` (uploaded files only; Drive-file attachments go through the Drive tools) return a short-lived encrypted link (`/attachments/signed/{token}`) that streams the bytes from Google on demand (Chat attachments only when Google declares their length), instead of writing the file to disk or inlining base64; when signing is on and no link can be issued, the tool returns an error instead of falling back to an unsigned link. The switch is off by default and needs `WORKSPACE_EXTERNAL_URL` as the links' public base plus key material (`GOOGLE_OAUTH_CLIENT_SECRET` or `FASTMCP_SERVER_AUTH_GOOGLE_JWT_SIGNING_KEY`) to derive the link key; with the switch on over streamable-http, the server refuses to start when either is missing. See the **[Advanced Deployment guide](https://workspacemcp.com/docs/deployment?utm_source=github.com&utm_medium=referral&utm_campaign=readme&utm_content=deploy-advanced)** for proxy, replica and logging setup.

**Short download links**: while signed download URLs are on, the tools hand out the claim-check form `{external_base}/dl/{handle}` (~22-char random handle) instead of the ~600-character `/attachments/signed/{token}` link. The full encrypted token is held in the shared KV store (`WORKSPACE_MCP_OAUTH_PROXY_*` backend, encrypted at rest; in-process store otherwise) for the link's remaining life, and `/dl/{handle}` serves it through exactly the same route logic, checks and headers as the long form — it never redirects to the long URL. An unknown or expired handle answers as a bad token does. If the handle cannot be stored the long link is returned instead, so a link is never dead. On by default; `WORKSPACE_MCP_SHORT_SIGNED_URLS=false` keeps the long form (e.g. multi-replica deployments without a shared backend). Carve `/dl/*` out of any authenticating proxy alongside `/attachments/signed/*`.

**Signed upload links** (opt-in): with `WORKSPACE_MCP_SIGNED_UPLOAD_URLS=true` (and signed download URLs on), `return_upload_url` hands out `{external_base}/ul/{handle}` instead of Google's resumable-session URL, and the server streams the PUT on to the session, so the caller never needs to reach `www.googleapis.com`. Files up to `WORKSPACE_MCP_SIGNED_UPLOAD_MAX_BYTES` (default 100 MiB); links close once the upload completes. Off by default; off, `/ul/` does not exist. Carve `/ul/*` out of any authenticating proxy and allow large bodies there. See [docs/signed-upload-relay.md](docs/signed-upload-relay.md).

**Domain-restricted Drive sharing**: `WORKSPACE_MCP_ALLOWED_SHARE_DOMAINS` (comma-separated) limits `manage_drive_access` and `manage_calendar` sharing to users, groups and domains in the list and makes `set_drive_file_permissions` reject link sharing; `WORKSPACE_MCP_SHARE_RESTRICTED_MESSAGE` appends operator routing text to rejections and to the tools' descriptions. Unset leaves behaviour unchanged. See [docs/share-domain-allowlist.md](docs/share-domain-allowlist.md).

**Gmail attachments**: attach Drive files with `drive_file_id`. A `/dl/` or `/attachments/signed/` link from this server cannot be passed back as an attachment URL (a bare `/attachments/{id}` link can). See [docs/gmail-attachment-gotchas.md](docs/gmail-attachment-gotchas.md).

**Short sign-in URLs**: the ~800-character Google authorization URL in an auth prompt is replaced by `{external_base}/auth/{handle}` (~60 chars), the same claim-check trick as `/dl`. The full URL is held Fernet-encrypted in the shared KV store under a random 128-bit handle with the OAuth state's TTL (10 min); `/auth/{handle}` 302-redirects to it. The stored value is validated as an `https://accounts.google.com` URL on both store and load, so the route cannot become an open redirect. Falls back to the full URL when no store is usable. `WORKSPACE_MCP_SHORT_AUTH_URLS=false` disables it.

## Security Best Practices

By default this server sends no data anywhere except Google's APIs, using your own OAuth client credentials - no usage reporting, analytics, license server, or SaaS dependency. MIT licensed with no CLA, no dual licensing, and no copyleft in the dependency chain. The full security posture - scope minimization, sensitive-path blocking, stateless mode - is documented at [workspacemcp.com](https://workspacemcp.com/privacy?utm_source=github.com&utm_medium=referral&utm_campaign=readme&utm_content=security-privacy).

A few things worth internalizing before you connect an LLM to your email:

- **Prompt injection is real.** Emails, docs, and events can contain hidden instructions. Only connect trusted data to an LLM, and be deliberate about which write tools you enable.
- **Never commit** `.env`, `client_secret.json`, or `.credentials/` to source control.
- **Local file reads are sandboxed** to the managed attachment directory. Broaden with `ALLOWED_FILE_DIRS` only if you trust the client and its data sources; `.env*`, `~/.ssh/`, `~/.aws/`, and similar paths are always blocked.
- **Production** deployments should use HTTPS and OAuth 2.1.

## Development

```bash
uv sync --group dev    # install deps
uv run ruff check .    # lint
uv run pytest          # test
```

Single-file service modules live in `g<service>/`, tools are registered with `@server.tool` decorators, and tiers are defined in `core/tool_tiers.yaml`. PRs welcome.

## License

MIT - see [`LICENSE`](LICENSE). The license is 21 lines and says what it means.

_Workspace MCP is an independent open-source project and is not affiliated with, endorsed by, or sponsored by Google LLC. Google and Google Workspace are trademarks of Google LLC._

---

Validations:
[![MCP Badge](https://lobehub.com/badge/mcp/taylorwilsdon-google_workspace_mcp)](https://lobehub.com/mcp/taylorwilsdon-google_workspace_mcp)
