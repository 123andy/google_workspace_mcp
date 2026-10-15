# Signed upload links (`/ul/{handle}`)

`create_drive_file`, `update_drive_file` and the `import_to_google_*` tools can
answer `return_upload_url=true` with an upload URL instead of taking the file
inline. By default that URL is Google's own resumable-session URL on
`www.googleapis.com`, so the caller must be able to reach Google. A sandboxed
client whose network is limited to this server cannot.

With signed upload links on, the tools open the same Google session but hand
out `{WORKSPACE_EXTERNAL_URL}/ul/{handle}`. The caller PUTs the file there and
this server streams the bytes on to the session. The caller only ever talks to
this server, and Google's session URL never leaves it.

## Settings

| Variable | Default | Meaning |
| --- | --- | --- |
| `WORKSPACE_MCP_SIGNED_UPLOAD_URLS` | `false` | Turn the feature on. Needs `WORKSPACE_MCP_SIGNED_DOWNLOAD_URLS=true` (its external-URL and key checks); the server refuses to start otherwise. Ignored (with a warning) over stdio, except that the tool descriptions, read at import, still mention the upload URL. |
| `WORKSPACE_MCP_SIGNED_UPLOAD_MAX_BYTES` | `104857600` (100 MiB) | Largest file one link accepts. Invalid values fail startup. |

Off, nothing changes: `/ul/` is not mounted (it answers like any unknown
path), and every tool result, description and error text is what it was.

`return_upload_url` is still offered only where local file access is disabled
(`WORKSPACE_MCP_DISABLE_LOCAL_FILES=true` or stateless mode).

## What the caller does

```bash
curl --fail-with-body -w '\nHTTP %{http_code}\n' \
  -H 'Content-Type: application/pdf' --upload-file report.pdf \
  'https://mcp.example.com/ul/<handle>'
```

- `Content-Length` is required (curl sends it for a regular file). Chunked
  bodies get `411`.
- Success is `200`/`201` with Google's JSON file metadata, including `id`.
- Resumable uploads pass through: a PUT with `Content-Range: bytes */<total>`
  and `Content-Length: 0` asks how much arrived (`308`, with `Range` if any
  bytes are committed), and the rest is sent with `Content-Range`.
- `410` means the link is closed (completed, refused by Google, or used up):
  call the tool again. After a completed upload, the status query above still
  returns Google's completion reply (kept with the link), so a caller that
  missed it can read the file id.

## Limits per link

| Limit | Value |
| --- | --- |
| Time to first use | 15 minutes |
| Expiry after use | 15 minutes after the last request, never more than 2 hours after minting (a body still arriving then is cut off) |
| Requests | 50 |
| Bytes in total | twice the size cap |
| Concurrency | one request at a time per server instance (`409` for a second) |

## Status codes

| Code | When |
| --- | --- |
| `200`/`201` | Google completed the upload; body is its JSON. The link closes. |
| `308` | Google has part of the file; `Range` says how much. |
| `400` | Malformed `Content-Length` or `Content-Range`, or the body did not arrive as declared: cut short, more than declared, no data for 60 s, still arriving at the 2-hour limit, or a server restart mid-upload (resume with a status query). |
| `401` | The owner's credentials are no longer recoverable on this server. |
| `403` | Unknown, malformed or expired handle (one generic body for all). |
| `405` | Method other than PUT or POST (POST is sent on as PUT). |
| `409` | Another request on this link is in progress on this server instance. |
| `410` | Link closed: upload completed, Google answered 4xx, or 50 requests used. |
| `411` | No `Content-Length`, or a chunked body. |
| `413` | Over the size cap (by `Content-Length` or `Content-Range`), or over the link's total. |
| `502` | Google answered 5xx or an unexpected status, or the connection to Google failed. |
| `503` | Google answered 408 or 429 (retry later; the link stays open). |
| `504` | Google did not answer in time. |

`401`, `403`, `409`, `411`, `413`, the header `400`s and the `410` for an
already closed link are decided before anything is read from the body or sent
to Google. A `410` caused by Google's 4xx comes after Google has seen the
request.

## Security model

- **The handle is the capability.** 128 random bits (`secrets.token_urlsafe`),
  as for `/dl/{handle}`. The record behind it (session URL, owner, MIME type,
  counters) is stored in the shared KV store (`WORKSPACE_MCP_OAUTH_PROXY_*`
  backend), Fernet-encrypted under its own salt; without a shared backend it is
  in-process (single instance only).
- **The relay can only reach Google's upload endpoint.** The session URL is
  validated when stored and again when loaded: `https`, host exactly
  `www.googleapis.com`, default port, no userinfo, fragment, backslash,
  whitespace or non-ASCII, path under `/upload/drive/v3/files`, an
  `upload_id`. Nothing in the request chooses the destination, and redirects
  are not followed.
- **Headers are allowlisted both ways.** Upstream gets only `Content-Length`,
  `Content-Range` and the MIME type fixed when the link was minted; the
  caller's `Authorization`, cookies and forwarding headers are dropped, and no
  credential is added (the session URL authorizes itself). Back to the caller go
  only `Content-Type` on success and a well-formed `Range` on `308`; never
  `Location` or Google's upload-ID header.
- **Revocation parity with `/dl/`.** Each request checks that the owner's
  credentials are still recoverable and usable here. This catches deleted or
  unrefreshable credentials, not a revocation made at Google.
- **No false success.** The body is streamed to Google as it arrives and the
  answer goes back only after Google's. If either side drops mid-body, the
  caller gets an error, never a `200`.
- **Logging.** The handle, the session URL and file bytes are never logged.
  The uvicorn access log redacts `/ul/<handle>` (see `core/log_formatter.py`).
  The entrypoints keep the `httpx` loggers at WARNING; at INFO they would log
  the session URL.
- **Residual risk.** A leaked live handle lets someone write into that one
  file until the link closes or expires: the same exposure as a leaked Google
  session URL today, but bounded to 2 hours and closable.

## Running it behind a proxy

The link must reach this server without an interactive login, and with large
bodies allowed through. For a Pomerium + nginx-ingress deployment:

1. **Pomerium:** a public route for the prefix `/ul/` on the server's host, placed
   above the host's catch-all MCP route (the first match wins), as a plain
   reverse proxy, with no `mcp:` block, and `allow_public_unauthenticated_access: true`.
   This is the same shape as the `/dl/` carve-out.
2. **nginx ingress:** a path-scoped Ingress for `/ul/` on that host (annotations
   do not inherit from another Ingress for the same host) with
   `proxy-body-size` above the cap plus headroom (e.g. `128m` for 100 MiB),
   and long `proxy-read-timeout`/`proxy-send-timeout`. Leave request buffering
   on (the default): nginx then takes the whole body from a slow client before
   passing it on at LAN speed, at the cost of spooling it to the controller's
   disk.
3. **Timeouts:** Pomerium's global `timeout_read` (default 30 s) bounds how
   long a request body may take to arrive. Without nginx buffering in front,
   a slow upload can be cut at that point; this has not been measured on a
   live gateway. Callers can also stay under it by sending the file in
   `Content-Range` chunks (non-final chunks multiples of 256 KiB).
4. **Edge logs:** proxy access logs record the path, and the handle is in the
   path. Decide retention and access accordingly, or mask `/ul/` paths there.

Rate limits at the edge reduce load from guessed handles but are not a
security control; the handle is.
