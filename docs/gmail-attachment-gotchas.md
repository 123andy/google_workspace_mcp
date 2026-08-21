# Gmail attachment gotchas that look like bugs

**Gmail's draft list shows no paperclip for drafts created through the API**, even when the
attachment is really there. Verified server-side on a draft created with `draft_gmail_message`:
a genuine `application/pdf` MIME part, byte size matching the Drive original exactly, and
`in:drafts has:attachment` matching it. Opening the draft shows the attachment normally — only
the *list* view omits the icon. It reads as data loss and has prompted more than one "the
attachment is missing" report.

**Do not attach this server's own download URLs.** `get_drive_file_download_url` and
`get_gmail_attachment_content` mint `/dl/{handle}` or `/attachments/signed/{token}` links for a
*client* to fetch. Passing one back as an `attachments` entry asks the server to fetch itself over
HTTP, which the SSRF guard blocks on any deploy whose external base URI is localhost or a private
address — i.e. every containerised one. Use `drive_file_id` for a Drive file, or
`get_gmail_attachment_content(return_base64=True)` plus `content` for a Gmail attachment. (The
error message says this too, so a wrong pick self-corrects.)
