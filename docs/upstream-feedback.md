# Working with upstream: what we owe them, and what we've learned

This fork tracks [`taylorwilsdon/google_workspace_mcp`](https://github.com/taylorwilsdon/google_workspace_mcp).
This document lives on the personal fork on purpose: upstream PRs are developed, tested and opened
from here, so the record of that relationship belongs here too. The company fork
(`scientist-hq/google_workspace_mcp`) is a deploy line only — ideally upstream `main` plus the
approved changes from this fork, cherry-picked — and carries no upstream-relationship material.
Its deploy line `sci-prod` is upstream plus ~40 patches, several of which exist only there.
That arrangement has a maintenance cost, and this document is where we record what that cost
actually looks like in practice — so the next person to pick up a refresh, or to decide whether
to spend an evening on an upstream PR, starts from evidence instead of from instinct.

It covers three things: **patterns we've observed in how upstream absorbs work**, **feedback we
owe them that hasn't been sent**, and **the current state of every PR we've opened there**.

Everything below is marked **verified** (we ran it or read the code/commit) or **inferred**
(reasoned, not executed). Counts are as of **2026-09-20** (status entries updated 2026-09-21), against upstream `main` at `d80ddd9`
(v1.27.1) and our `sci-prod` at `3d982ba` (based on v1.24.1). Re-check them rather than trusting
them; `scripts/scan_param_naming.py` exists so at least one of them is a single command.

---

## 1. An unproposed downstream patch has a shelf life

The single most useful thing we've learned. Upstream has now **independently reimplemented three
of our patches**. Each time, the work we were carrying privately became redundant — and each time
we found out during a refresh rather than in advance.

| Ours | Upstream's version | Outcome |
| --- | --- | --- |
| `--exclude-tools` (blocklist, scopes untouched) | `--disabled-tools` + `WORKSPACE_MCP_DISABLED_TOOLS` | **Same semantics.** Ours is now dead weight; base can switch to the upstream flag. |
| `get_gmail_message_full` (untruncated export) | absorbed into `get_gmail_message_content(full=True)` | The standalone tool name no longer exists on this line. We had already folded it. |
| hardcoded `token_expiry_threshold_seconds=300` | `get_oauth_proxy_expiry_kwargs()` + `WORKSPACE_MCP_OAUTH_PROXY_TOKEN_EXPIRY_THRESHOLD_SECONDS` (upstream #1061) | Conflicts loudly, but is easy to resolve *wrongly* — see the warning below. |

**Verified:** `core/tool_registry.py` on `upstream/main` carries `--disabled-tools`, while
`sci-prod` still references `--exclude-tools` in `core/tool_registry.py` (3) and `main.py` (5),
8 in non-test Python; `get_gmail_message_full` is absent upstream and
`get_gmail_message_content` declares `full: Annotated[bool, ...]`; `sci-prod:core/server.py:568`
hardcodes `token_expiry_threshold_seconds=300` where upstream reads an env var.

⚠ **The third one needs care — but not because it hides.** Being precise here matters: an earlier
draft of this document claimed this patch vanishes on merge *with no conflict marker*. That was
wrong, and it would have sent the next refresher hunting for the wrong failure mode.

Git **does** raise a conflict. Both sides edited the same kwarg slot in the `GoogleProvider(...)`
call, so three-way merge cannot resolve it. **Verified** via
`git merge-tree --write-tree origin/sci-prod upstream/main`, reading `core/server.py` out of the
resulting tree:

```
584: <<<<<<< origin/sci-prod
585:     # Refresh the upstream Google token ~5 min before expiry rather
...
590:     token_expiry_threshold_seconds=300,
591: =======
592:     **expiry_kwargs,
593: >>>>>>> upstream/main
```

The danger is in **how that conflict gets resolved.** Taking upstream's side looks obviously
right — it is the generalised, configurable form of our hardcode. But
`get_oauth_proxy_expiry_kwargs()` returns `{}` when
`WORKSPACE_MCP_OAUTH_PROXY_TOKEN_EXPIRY_THRESHOLD_SECONDS` is unset (**verified**,
`auth/oauth_proxy_config.py:42–74`), so resolving that way and shipping drops our 300-second
proactive refresh to FastMCP's default, with no further signal.

**The remedy is therefore a config change, not a code check:** set
`WORKSPACE_MCP_OAUTH_PROXY_TOKEN_EXPIRY_THRESHOLD_SECONDS=300` in the k3 chart **in the same
change that takes upstream's side**, then drop our patch. Upstream caps that value at 300
(`MAX_OAUTH_TOKEN_EXPIRY_THRESHOLD_SECONDS`), so our setting sits exactly at the ceiling and
survives the move intact. §5's ledger row for #888 makes the same point from the other side: the
conflict there *is* upstream generalising our hardcode, and it is a genuine improvement — once
the env var is set.

**The lesson.** Carrying a patch privately is a bet that upstream won't build the same thing. We
have lost that bet three times. The exposure is proportional to how long a patch stays unproposed
and how obviously useful it is — so the patches most worth upstreaming are exactly the ones most
likely to be reinvented. Current largest exposure: **PR #888's four non-test modules**
(`attachment_cred_cache`, `attachment_signing`, `download_handles`, `signed_download`; ~1,900
lines including tests), open since 2026-06-26.

---

## 2. The bottleneck is maintainer attention, not code quality

**Verified**, via `gh api repos/taylorwilsdon/google_workspace_mcp/pulls/<n>/reviews`:

```
#887    123andy, coderabbitai[bot]
#888    123andy, coderabbitai[bot]
#1035   coderabbitai[bot]
#1036   coderabbitai[bot]
```

`taylorwilsdon` has posted **zero reviews on all four of our open PRs**. Every review round we
have answered on them came from a bot. GitHub reports `mergeable_state` as `blocked` for #887 and
#1035 and `dirty` for #888 and #1036 (**verified** 2026-09-20). `blocked` reads like rejection
but means *awaiting review*; `dirty` means a merge conflict, which the §5 ledger records
per-PR.

This reframes what "stuck" means here. #887 merges into today's `main` with **no conflicts at
all** (`git merge-tree --write-tree` exits 0) and every check green. It is not stuck on anything
we control.

**What actually gets merged.** Our merged PRs were small and self-contained:

| PR | Merged | Shape |
| --- | --- | --- |
| #1047 batch labels | 3 days | one behaviour fix, tests |
| #1037 log hygiene | 7 days | mechanical, wide but shallow |
| #939 message export | ~9 days | one tool |
| #891 gateway identity | ~4 weeks | larger, but a self-contained module |

The stuck ones are large (#888, ~1,900 lines), design-contested (#887 overlaps #806), or both.

**Inferred, from that pattern:** the highest-value action on a stalled PR here is *prose, not
code* — one specific, answerable question that converts an open-ended review into a one-word
decision. A solo maintainer facing 1,000 lines with no question attached has nothing cheap to do;
"do you want this as one PR or split in two?" costs him ten seconds. This is the thing we had
never tried before 2026-09-20.

---

## 3. Don't close a PR on a maintainer commit without re-checking that it stuck

The #890 story, **verified commit by commit**:

| Date | Commit | Where it lived | Effect on `set_publish_settings` |
| --- | --- | --- | --- |
| 2026-06-25 | `16ea991` (`ameet-rajababa`) | PR #883's branch | test assertion → bare `"publishState"`, matching `main` at v1.22.0 |
| 2026-06-26 | `f2783d2` (ours, PR #890) | our branch | `"publishState"` → `"publishState.isPublished,publishState.isAcceptingResponses"` |
| 2026-07-07 | `e2f53ba` (Taylor, *"fix failing test"*) | **`main`** | applies the **same** granular mask directly |
| 2026-07-07 | we close #890 | — | correct at that moment — `main` carried the fix |
| 2026-07-09 | `a472faa` (Taylor, *"fix publishState"*) | **PR #883's branch, not `main`** | code → bare `"publishState"` |
| **2026-07-22** | **`d23d163` — the #883 merge** | **`main`** | **the revert reaches `main`, inside someone else's PR** |

**Verified topologically, not by date order.** `git cat-file -p d23d163` shows parents
`3f19c7d a472faa` — so `a472faa` is the *head of the contributor's branch*, and
`git log --first-parent upstream/main` contains no `a472faa` at all. `main` at `3f19c7d` still
reads `"publishState.isPublished,publishState.isAcceptingResponses"`
(`gforms/forms_tools.py:289`), so the granular mask was live on `main` from 2026-07-07 to
2026-07-22. `upstream/main` today carries the bare `"updateMask": "publishState"`
(`gforms/forms_tools.py:291`).

Note the causality, which is the opposite of what it looks like: **#883 was opened 2026-06-25,
before our fix and before Taylor's** — its test change came *first*, written against a `main`
that still had the bare mask. #883 did not "fix a test the revert had broken"; it was the vehicle
that carried the revert onto `main`.

**Two separate lessons, and it matters not to conflate them.**

*Procedural:* closing on the strength of a maintainer commit, without a watch on whether it
survived, meant the reversal went unnoticed for two months. And the sharp part is *where* it
came from — not a commit on `main`, not a revert with our name on it, but a merge of an unrelated
contributor's PR whose branch happened to carry it. A watch on `main`'s direct commits, or on our
own PR's thread, would have caught neither. Only re-reading the file would have.

*Substantive:* we were **wrong on the merits anyway.** Both `publishState` children are
non-optional with defaults and are always populated, so a parent-level mask is functionally
identical to the granular one. There is no live bug. And **our own tree carries the bare mask
too** — `sci-prod:gforms/forms_tools.py:289` reads `"updateMask": "publishState"`, so despite
`f2783d2` existing in our history, a later upstream merge brought the bare form back. The patch
is dead here. Nothing to clean up, nothing to re-propose.

---

## 4. Open feedback items

| Item | Status | Where |
| --- | --- | --- |
| **Sibling tools reject each other's arguments** — three patterns needing different remedies: singular/batch variants with different signatures (`get_gmail_message_content` vs its `_batch`), one concept under two names (`page_size`/`max_results`, `query`/`q`), and confusable-but-distinct names (the Drive `mime_type`/`source_format`/`export_format` family) | **Filed 2026-09-21 as upstream issue #1152**, narrowed to the singular/batch mismatch actually hit in use (§7) plus the two aliasable pairs; the Drive format family was left out of the filing. | upstream #1152 + `scripts/scan_param_naming.py` |
| **Nested `message/rfc822` parts are unreadable** — a wrapped email's body cannot be read | **Fix proposed 2026-09-21 as upstream PR #1151**, verified against live Gmail. See §7. | upstream #1151 |
| **Stale `uv.lock` on `main`** — lock pins `workspace-mcp 1.27.0`, `pyproject.toml` says `1.27.1`, so `uv run` re-locks on any contributor's machine | **Raised** 2026-09-20, as an aside on PR #1035. Not separately filed. | PR #1035 comment |
| **`ruff format --check` fails on two fork-only files** | **Fixed on `sci-prod`** by fork PR #9 (2026-09-20), which reformatted both files in its own `style:` commit. Upstream CI never saw them. | fork PR #9 |

**Verified** 2026-09-20, `uv run ruff format --check`:

```
Would reformat: core/signed_download.py
Would reformat: tests/gmail/test_drive_attachments.py
2 files would be reformatted
```

Both are files that exist only on this fork, which is exactly why upstream CI has never flagged
them — and why a refresh will surface them as if they were new breakage when they are not.

### The parameter-naming item, in brief

**The lead finding is the one hit in real use** (recorded 2026-08-22, **re-verified** on `main`
`d80ddd9`, v1.27.1): the singular and batch variants of one operation take different arguments.

```
get_gmail_message_content         [message_id,  body_format, full]
get_gmail_messages_content_batch  [message_ids, format, body_format]
```

`format` is accepted by the batch and **rejected** by the singular —
`Unexpected keyword argument [type=unexpected_keyword_argument, input_value='full']` — and the
word "full" now means two things: a *detail level* on the batch (`Literal["full", "metadata"]`, so
`format="raw"` fails with `Input should be 'full' or 'metadata'`) and *untruncated export* on the
singular (`full=True`). Both error texts were reproduced against the same annotations. The thread
pair has drifted the same way (`include_analysis` on the singular only). The script finds two more
differing pairs, both **defensible and excluded**: `batch_modify_gmail_message_labels` adds
`verify` because Gmail's batch endpoint returns no per-message result (that is our own #1047), and
`manage_contacts_batch` takes lists of records by design.

The three patterns need **opposite remedies**, which is why the issue keeps them apart — conflating
them was an earlier draft's mistake, caught in review: (1) sibling signatures → accept the
sibling's arguments or name the right one in the rejection; (2) one concept, two names → alias;
(3) distinct concepts with confusable names → guidance, and **never** aliasing.

What follows is category 2, plus the record of why category 3 must not be aliased.

**Verified.** 117 `@server.tool` functions on `main` expose 348 distinct parameter names. Three
concepts are spelled more than one way; pagination (`page_token`, 11 tools) is already consistent,
which is the useful control — this is drift in a few places, not an absent convention.

| Concept | Spellings | Split |
| --- | --- | --- |
| result cap | `page_size` / `max_results` | 14 tools / 7 |
| free-text query | `query` / `q` | 7 / 1 (`search_custom`) |
| body or content | `content` / `body` / `text` | 6 / 2 / 2 — *observed, not proposed for aliasing* |
| **pagination cursor** | **`page_token`** | **11 — already consistent (the control)** |

⚠ **`mime_type` / `source_format` / `format` is NOT an instance**, and an earlier draft wrongly
listed it. Verified in the source, they are three different things: `format` on
`get_gmail_messages_content_batch` is Gmail's message *detail level*
(`Literal["full", "metadata"]`); `source_format` is an extension key (`"md"`, `"docx"`, …) that
`_resolve_import_media` turns into `.md` and looks up in an allowlist; `mime_type` is a MIME
string. `update_drive_file` declaring **both** `mime_type` and `source_format` is evidence they
are distinct, not evidence of drift. Aliasing them would make
`create_drive_file(source_format="md")` silently ask Drive for mimeType `md` instead of raising a
clear schema error — a bug introduced in the name of a fix. Recorded so it isn't re-derived.

It is a **defect, not a style complaint**, because every tool's schema is closed. Upstream states
the mechanism itself, in `core/camel_case_middleware.py`:

> Every tool in this server declares snake_case parameters with `additionalProperties: false`, so
> callers that mirror the Google API field names (`calendarId`, `timeMin`, `maxResults`, ...) fail
> schema validation before the request ever reaches Google.

The same sentence holds when the name is mirrored from a **sibling tool** rather than from Google.

**Verified that v1.25.0's `CamelCaseArgumentsMiddleware` does not cover it.** Its rename requires
`snake_key != key`, so an argument that is *already* snake_case is never renamed. `max_results`
sent to a `page_size` tool passes through untouched and is rejected by the schema. (Nor does the
camelCase path rescue it: `maxResults` normalises to `max_results`, which that tool also doesn't
declare.)

This is the same failure class as upstream **#918** — closed and fixed — but a different cause:
#918 was tool-schema vs Google's API; this is tool vs tool. **No prior art found**
(`gh search issues` across several phrasings), so it is a new report rather than a comment.

The proposed fix is deliberately cheap: extend the *existing* middleware with a bidirectional
alias map. The middleware's current guard — rename only a key that isn't already a declared
parameter, onto one that is — makes a bidirectional map safe with no further logic. No tool
signatures change, no schemas change, nothing breaks.

**Excluded on purpose:** the `*_id` family (`document_id`, `spreadsheet_id`, `file_id`, …). Those
are genuinely distinct resource types; one name per type is correct. Noted here so it isn't
re-raised as an oversight.

---

## 5. PR ledger

Every PR we have opened upstream, with its standing as of 2026-09-20. Kept here so this document
stands alone.

| PR | State | Merges clean? | We depend on it? | Call |
| --- | --- | --- | --- | --- |
| **#887** `--only-tools` | open | **yes, zero conflicts** | **load-bearing** — delta's 4-scope minimal grant | Push to land. No code work. Needs a direction call vs #806. |
| **#888** signed streaming URLs | open | 2 hunks / 2 files | **load-bearing** | Push to land. ~1 hr. One conflict is upstream generalising our hardcode — a genuine improvement. |
| **#1035** draft lifecycle | open | *(merged current 2026-09-20)* | yes — delta's `send_gmail_draft` | Push to land. Best odds of the four. |
| **#1036** remote `file_path` | open | 6 hunks / 1 file | partial | Rebase and leave. Strongest external validation, weakest maintainer signal. |
| #1047 batch labels | merged | — | upstream | Intact. |
| #1037 log hygiene | merged | — | upstream | Intact, ~30 call sites survive. |
| #939 `get_gmail_message_full` | merged | — | absorbed | Folded into `get_gmail_message_content(full=True)`. |
| #891 trusted-gateway identity | merged | — | **we run it in prod** | Intact. |
| #949 calendar reminders | closed by us | n/a | no | **Revive.** Gap still open and now *asymmetric* — `manage_event` takes `reminders` and `use_default_reminders`, but `get_events` has no reminder handling at all (verified: zero matches in its body) and `list_calendars` does not surface `defaultReminders`. So an agent can set a reminder it cannot read back. Easier sell than the original pitch. |
| #943 compose-then-send | closed by us | n/a | superseded | Correctly closed; #1035 supersets it. |
| #890 forms `updateMask` | closed by us | n/a | **no — dead in our tree** | Stay closed. Our premise was wrong. See §3. |
| #855 OAuth branding | closed | n/a | no | Correctly closed; the maintainer re-created it as #868 and merged that. |

**Pattern worth noticing in the closed rows:** three of the four were closed *by us*, and one of
those (#949) was closed as "not ready" while upstream went on to merge two changes of exactly its
shape (`include_attachments`, `single_events`, both now on `get_events` — verified). **Inferred
from that:** self-closing is cheap to do and expensive to undo — the work is written, and the gap
it addressed usually outlives the decision to withdraw it.

---

## 6. Refreshing the numbers

- **Parameter naming:** `python3 scripts/scan_param_naming.py upstream/main` — prints the concept
  table with current counts, then every singular/batch sibling pair whose argument lists differ.
  Takes any git ref.
- **Review state:** `gh api repos/taylorwilsdon/google_workspace_mcp/pulls/<n>/reviews --jq '[.[].user.login]|unique'`
- **Mergeability without touching a worktree:** `git merge-tree --write-tree <pr-ref> upstream/main`
  — exit 0 means clean.
- **Our divergence:** `git log $(git merge-base origin/sci-prod upstream/main)..origin/sci-prod --oneline`

## 7. Field findings from real use — and why this doc exists

Most of what we know about this server's rough edges came from *using* it, not from reading it:
one end-to-end session composing a real email with an attachment against a deployed
streamable-http instance (2026-08-21), and a mail-triage session the next day. Those findings
were written into **an untracked notes file in one personal clone**. Nothing was wrong with the
notes. But an untracked file in one checkout is invisible to every search of the work repos and
of GitHub — on 2026-09-20 a direct request to "find the parameter-naming problem we recorded"
turned up nothing across both, and the finding was re-derived from scratch before the original
was located. **That is the reason this document exists: a finding that is not in a tracked,
shared place will be paid for twice.**

Generalised here on purpose — the source notes carry personal mailbox detail that does not belong
in a company repo. Each status below was **re-verified on 2026-09-21**, not copied.

| Finding (2026-08-21/22) | Status now | Verified how |
| --- | --- | --- |
| `batch_modify_gmail_message_labels` reported success for IDs Gmail silently ignored — a wrong or stale ID produced a confident, false "updated" | **Fixed upstream: #1047, merged 2026-08-24.** | `_verify_batch_label_changes` is on `upstream/main` |
| No way to update or delete a draft | **Proposed: #1035 (open).** | PR state |
| A local `path` was advertised on remote transports where it cannot work | **Proposed: #1036 (open).** | PR state |
| The agent-facing `attachments` description never mentioned `drive_file_id`, the only option that works for a Drive-held file on a remote transport — it cost four failed attempts to discover, via an error message | **Fixed on the personal line only** (`7a94f67`). **Not on `sci-prod`**, so production still ships the old description. `drive_file_id` itself comes from upstream PR #873 (another contributor's, still open), which the fork adopted. | `git merge-base --is-ancestor 7a94f67 origin/sci-prod` → no |
| `get_drive_file_download_url` minted a URL that the attachment fetcher then refused (the server's own signed route, rejected by its own SSRF guard) | **Same commit, same state:** fixed on the personal line, **not promoted to `sci-prod`**. Fork-only code (the signed-URL work behind #888), so there is nothing to report upstream. | as above |
| Import tools appeared to HTML-escape `file_name` | **Closed — not a server bug.** No escaping exists in the import path; the caller had passed an already-escaped string. | audited in the source notes; not re-tested |
| Gmail's draft *list* shows no paperclip for API-created drafts, which reads as a lost attachment | **Not fixable server-side** (Gmail UI). Documented as a gotcha on the personal line's README; **not on `sci-prod`'s**. | `grep -ci paperclip` on each README |
| Singular/batch Gmail read tools take different arguments | **Filed as upstream #1152** (2026-09-21) — the lead of the naming issue (§4). | re-verified on `main`, errors reproduced |

⚠ **Two of those rows are the same unpromoted commit.** `7a94f67` was validated live on the
personal stack a month ago and never reached the deploy line. It is part of a wider set —
`git log origin/sci-prod..andy-prod` on the personal clone — and is worth promoting with the next
`sci-prod` change rather than rediscovering from a user report.

### Nested `message/rfc822` parts were unreadable — fix proposed upstream (#1151)

**Was unreported upstream** (the only related item was #389, an unrelated `.eml` export request); now upstream PR #1151.
**As observed in use (from the source notes, not re-run):** the attachment walker recurses into
a wrapped message and lists the inner message's attachments as if they belonged to the wrapper;
the body extractor does not descend at all, and the wrapped message is never offered as an
attachment either — so its headers and body are unreachable from both directions.
**Verified on `main` (v1.27.1):** the only `message/rfc822` mention under `gmail/` is on the
export path, so nothing in body extraction handles a wrapped message.

The affected class is wider than the case that surfaced it: Google Groups moderation notices,
messages forwarded **as an attachment** (the Outlook default, so common in corporate mail),
bounces and DSNs, and ARF abuse reports are all wrapped messages.

**Outcome (2026-09-21): upstream PR #1151.** A read-only renderer appends the wrapped message's
headers and body after the wrapper's body in the three Gmail read tools, marked as unverified, and
labels files that belong to the wrapped message. It walks the JSON payload already in hand: no new
dependency, no extra API call. **Verified against live Gmail** on a mailing-list moderation notice:
the wrapped headers and body render, nested files are labelled while a wrapper's own are not, and a
nested file downloads using the wrapper's message ID.

Two things learned on the way. `get_gmail_message_content(body_format="raw")` is only a partial
escape hatch — the MIME source is truncated at 20,000 characters and the wrapped body stays
transfer-encoded. And a bounce's `message/delivery-status` part, though present in the raw MIME, is
**not exposed as body data in `format=full`** (observed live), so rendering bounce status needs a
different approach; that was cut from #1151 and is an open follow-up. Parsing `format=raw` with the
standard library would reach it, at the cost of downloading the whole message, attachments included.

### Outcomes, 2026-09-20/21

- **Upstream #1035 updated**: preserve-on-update for draft addressing and threading,
  `clear_fields`, an in-place header patch for addressing-only updates, and `action="list"`.
  **Live-tested against Gmail** — which found four things no mocked test could: an MCP client that
  cannot send an empty string for an optional parameter (hence `clear_fields`), `Received` headers
  accumulating on re-uploaded drafts, a Gmail tab silently overwriting a later API update on
  autosave, and an error message recommending an argument form that client cannot send.
- **Upstream #1150 opened** (2026-09-21): a `thread_id` Gmail rejects (typically the token from
  a Gmail web URL) made `draft_gmail_message` create an orphan draft and report success. Kept
  small and separate on purpose — see §2 on what gets reviewed.
- **Upstream #1151 opened** (2026-09-21): wrapped emails, above. **Upstream #1152 filed**: the
  singular/batch argument mismatch.
- **Fork PR #9 merged into `sci-prod`** (2026-09-20): signed downloads of shared-drive files,
  which 404'd in production. Merging is not shipping; the image and deploy-tag bumps follow.

## Related

- **Feature ledger** — every feature carried on top of upstream, its PR, and where it runs — is kept
  in the private `scientist-hq/mcp-gateway` repo as `docs/google-workspace-fork-ledger.md`, not here:
  it records company deployment state, and this fork is public.
- [`granular-tool-selection.md`](granular-tool-selection.md) — `--only-tools` / `--exclude-tools`,
  the subject of PR #887 and the flag whose upstream twin (`--disabled-tools`) makes
  `--exclude-tools` retirable.
- [`trusted-gateway-identity.md`](trusted-gateway-identity.md) — the design merged upstream as #891.
