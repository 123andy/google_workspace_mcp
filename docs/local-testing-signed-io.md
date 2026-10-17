# Testing signed links locally: Chat downloads and the `/ul/` upload relay

The unit and socket tests (`tests/core/test_upload_relay*.py`,
`tests/core/test_signed_download_chat*.py`, `tests/test_signed_io_flags_off.py`) cover the code
against fakes. This page is the manual check against real Google, optionally behind a local
authenticating proxy. Every step that mints a link needs a real Google sign-in.

## 1. Run the server with the settings on

Any way you normally run this server works; the settings are:

```bash
WORKSPACE_MCP_SIGNED_DOWNLOAD_URLS=true
WORKSPACE_MCP_SIGNED_UPLOAD_URLS=true
WORKSPACE_MCP_DISABLE_LOCAL_FILES=true          # return_upload_url is offered only then (or in stateless mode)
WORKSPACE_EXTERNAL_URL=<the URL clients reach this server at>
# optional: WORKSPACE_MCP_SIGNED_UPLOAD_MAX_BYTES=10485760   (to test 413 with a small file)
```

plus your usual OAuth client settings, then `workspace-mcp --transport streamable-http ...`. The
startup log must say `WORKSPACE_MCP_SIGNED_UPLOAD_URLS is on: upload links use <base>/ul/*`. If it
says the setting requires `WORKSPACE_MCP_SIGNED_DOWNLOAD_URLS=true`, the download setting is off or
misspelled (the retired `WORKSPACE_MCP_SIGNED_ATTACHMENT_URLS` is no longer read).

Below, `BASE` is `WORKSPACE_EXTERNAL_URL`, and `K=-k` if it uses a self-signed certificate.

## 2. Behind a local Pomerium (optional)

To see what the proxy does with uploads (public carve-out, slow bodies), add a route above the
host's catch-all MCP route, with no `mcp:` block, and restart Pomerium (it does not hot-reload a
bind-mounted config):

```yaml
- from: https://<server host>:<port>
  prefix: /ul/
  to: http://<backend>:8000
  name: <server>-ul
  allow_public_unauthenticated_access: true
```

Point `WORKSPACE_EXTERNAL_URL` at the Pomerium host. Without a link:

```bash
curl $K -s -o /dev/null -w '%{http_code}\n' -X PUT -H 'Content-Length: 0' $BASE/ul/AAAAAAAAAAAAAAAAAAAAAA   # 403: public, unknown handle
curl $K -s -o /dev/null -w '%{http_code}\n' -X POST $BASE/mcp                                          # 401: MCP still gated
```

A sign-in redirect instead of `403` means the carve-out is missing or below the catch-all.

## 3. Uploads

```bash
mkdir -p /tmp/ul && cd /tmp/ul
for n in 2 20 50 101; do dd if=/dev/urandom of=${n}m.bin bs=1048576 count=$n; done
```

1. Mint: call `create_drive_file(file_name="ul-2m.bin", mime_type="application/octet-stream",
   return_upload_url=true)`. The result shows `$BASE/ul/<handle>` and no `googleapis.com`.
   `UL=<that URL>`.
2. PUT it (curl sends no cookies or auth by default):
   ```bash
   curl $K --fail-with-body -w '\nHTTP %{http_code} %{time_total}s\n' \
     -H 'Content-Type: application/octet-stream' --upload-file 2m.bin "$UL"
   ```
   Expect `200`/`201` and the file's JSON (with `id`); the same PUT again gives `410` (body: the
   upload completed, do not upload again), and a status
   query (`-X PUT -H 'Content-Length: 0' -H "Content-Range: bytes */$(wc -c < 2m.bin)"`) replays
   the JSON.
3. Read it back: `get_drive_file_download_url(file_id=<id>)` returns a `/dl/` link;
   `curl $K --fail -o back.bin "<link>" && cmp 2m.bin back.bin && echo SAME`.
4. Repeat with `20m.bin` and `50m.bin`, watching the server's memory during the 50 MiB PUT.
5. `101m.bin` on a fresh link (default cap): `413`, nothing sent to Google.
6. Resumable, 4 MiB chunks on a fresh link (non-final chunks must be multiples of 256 KiB):
   ```bash
   f=20m.bin; total=$(wc -c < $f); chunk=$((4*1024*1024)); off=0
   while [ $off -lt $total ]; do
     end=$((off+chunk-1)); [ $end -ge $total ] && end=$((total-1))
     tail -c +$((off+1)) $f | head -c $((end-off+1)) > part.bin
     curl $K -s -D - -o body.txt --upload-file part.bin \
       -H "Content-Range: bytes $off-$end/$total" "$UL" | grep -iE '^HTTP|^range'
     off=$((end+1))
   done; cat body.txt
   ```
   `308` with `Range` per chunk, then `200`/`201`.
7. Interrupt and resume: start `curl $K --limit-rate 1m --upload-file 20m.bin "$UL"` on a fresh link,
   stop it after a few seconds, then ask how much arrived:
   ```bash
   curl $K -s -D - -o /dev/null -X PUT -H 'Content-Length: 0' \
     -H "Content-Range: bytes */$(wc -c < 20m.bin)" "$UL" | grep -iE '^HTTP|^range'
   ```
   and continue the chunk loop from the byte after the `Range` end.
8. The other tools: `import_to_google_doc(..., source_format="docx", return_upload_url=true)` with a
   real `.docx`, and `update_drive_file(file_id=..., mime_type=..., return_upload_url=true)`.
9. Refusals on a fresh link (they do not use it up): GET → `405`; a changed handle → `403`;
   `-H 'Transfer-Encoding: chunked'` → `411`; `-X PUT -H 'Content-Length: 0'` with no
   `Content-Range` → `400`.

### Slow uploads through the proxy

```bash
curl $K -w '\nHTTP %{http_code} after %{time_total}s, sent %{size_upload} bytes\n' \
  --limit-rate 300k -H 'Content-Type: application/octet-stream' --upload-file 20m.bin "$UL"
```

About 70 s of upload. Pomerium's `timeout_read` (default 30 s) may cut it when nothing buffers the
body in front of Pomerium; record status, time and bytes. The chunk loop with `--limit-rate 300k`
keeps each request near 14 s.

## 4. Chat download link

Upload a file into a Chat space from disk (keep the original), find the message with
`get_messages`, call `download_chat_attachment(message_id=..., attachment_index=0)`, then
`curl $K --fail -o chat.bin "<the /dl/ link>" && cmp <original> chat.bin`.

## 5. Facts only a live run settles

| Question | Where it shows |
| --- | --- |
| Does Chat media come with `Content-Length`? | The Chat download works; a `502` with `declared no Content-Length` in the server log means no. |
| Does the Chat media call redirect? | `502` with `Chat media download failed: HTTP 30x` in the log. |
| `Content-Encoding` on Chat media despite `Accept-Encoding: identity`? | `502` with `has a Content-Encoding` in the log. |
| Is `attachmentDataRef.resourceName` stable, and what characters does it use? | A Chat link fetched ~14 minutes after minting still works; Google's APIs Explorer (`chat.spaces.messages.get`) shows the name; read it twice and compare. |
| Is the Drive-attachment field `driveDataRef.driveFileId`? | With signing on, `get_messages` prints `with Drive file ID <id>` for a Drive attachment. |
| Does a `308` omit `Range` when nothing is committed? | The status query on a fresh link. |
| `200` or `201` on completion, for create and for update? | Uploads, steps 2 and 8. |
| Does Google keep the minted MIME type when the caller sends another `Content-Type`? | Step 2 with `-H 'Content-Type: text/plain'`. |
| Does the proxy cut slow bodies? | The throttled upload. |
