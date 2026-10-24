# Artifact hosting (optional)

Lets people publish small client-side web pages ("artifacts") to Google Drive and share a link that renders them for signed-in colleagues. **Everything here is off unless you switch it on**: with none of the variables below set, the server loads the same services and serves the same routes as before.

## What it adds

- **`register_artifact`** (tool): publishes a bundle folder you own at `Claude/Artifacts/<bundle>` in your My Drive, after checking the folder's ownership and location and scanning its files against the content policy. Returns the viewer link and any warnings. Needs only the Drive read-only scope.
- **`get_artifact_policy`** (tool): returns the content policy (allowed hosts per resource type, size limits, what the sandbox forbids, a rules block for the model that builds the artifact).
- **Viewer routes** (when the viewer is on): `/a/<hash>-<slug>/<path>` shows the sandboxed page behind sign-in; `/c/<token>/<path>` serves the files on a separate host; `/csp-report` receives policy violation reports.

A *bundle* is a Drive folder with an `index.html` at its top level and other pages and assets beside or below it, referenced by relative paths. The owner registers it; only registered bundles are served, and a path is resolved by walking down from the registered folder, so nothing outside it can be addressed.

## Switches

| Variable | Effect |
| --- | --- |
| `WORKSPACE_MCP_ARTIFACTS=true` | Adds the `artifacts` service to the default service set. It can also be chosen with `--tools artifacts` or `--permissions artifacts:readonly`, which request only the Drive read-only scope. |
| `WORKSPACE_MCP_ARTIFACTS_VIEWER=true` | Mounts the viewer, content and report routes and allows `Origin: null` on content reads only. |
| `WORKSPACE_MCP_ARTIFACTS_VIEWER_URL` | Public base URL of the viewer (links returned to users; allowed to frame the content). |
| `WORKSPACE_MCP_ARTIFACTS_CONTENT_URL` | Public base URL of the content host. |
| `WORKSPACE_MCP_ARTIFACTS_BRAND_NAME` | Name in the viewer's header bar (default `Artifacts`). |
| `WORKSPACE_MCP_ARTIFACTS_BRAND_COLOR` | Header bar colour as `#rrggbb`; text is white (default dark blue). |
| `WORKSPACE_MCP_ARTIFACTS_ICON_PATH` | PNG, SVG or ICO file (up to 256 KB) used as the favicon and the header logo; none by default. |
| `WORKSPACE_MCP_ARTIFACTS_TOKEN_KEY` | Secret (at least 16 characters) used to encrypt content tokens. |
| `WORKSPACE_MCP_ARTIFACTS_TOKEN_TTL_SECONDS` | Token lifetime, 60 to 86400 (default 3600). |
| `WORKSPACE_MCP_ARTIFACTS_REGISTRY_PATH` | SQLite file for the registry (default: `artifacts.sqlite` in the credentials directory). |
| `WORKSPACE_MCP_ARTIFACTS_CSP_PROFILE` | JSON file replacing the default host lists (`script`, `style`, `font`, `img`, `connect`), `allow_unsafe_eval` and `limits`. Hosts where anyone can publish content (for example `*.github.io`) are refused. |

## Deployment shape

The viewer expects a gateway that signs users in and passes a signed identity assertion (see *Trusted-gateway identity*): set `TRUST_GATEWAY_IDENTITY=true` and its JWKS and audience settings. Users grant Drive read access once through the server's normal OAuth callback.

Two public hostnames point at the same server. The gateway adds one request header to each, `x-artifacts-route: app` on the viewer host and `x-artifacts-route: content` on the content host, because the `Host` header is usually rewritten on the way in. Requests for `/a/…` are accepted only with `app` and `/c/…` and `/csp-report` only with `content`. Route the content host publicly (a sandboxed frame cannot send cookies), strip the `Cookie` request header there, and expose nothing else on it.

## Security notes

- Artifact pages run in `<iframe sandbox="allow-scripts allow-modals">` with no `allow-same-origin`, on a host that holds no session, under a Content-Security-Policy that also sends `sandbox allow-scripts`, so a page opened directly is sandboxed too. A content URL opened as a top-level page is redirected to the viewer.
- The policy limits where scripts, styles, fonts and images may load from and blocks other network access. It cannot stop script from navigating its own frame, so the shell shows a permanent "user-generated content, never enter a password" banner, and artifacts must not hold secrets.
- Content tokens are encrypted and expire; a leaked token reads one bundle as one user until it expires.
- Drive remains the only permission system: the viewer reads as the signed-in user, so sharing a bundle folder in Drive is what grants access.
