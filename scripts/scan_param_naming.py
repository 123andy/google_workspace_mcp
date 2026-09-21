#!/usr/bin/env python3
"""Scan the MCP tool surface for the same concept exposed under different
parameter names.

Why this exists
---------------
Every tool in this server is registered with a JSON schema that declares
``additionalProperties: false``. That means an argument name the schema does not
list is not ignored -- it is a hard validation failure, raised before the request
ever reaches Google. So when two tools express the *same* idea under two
different parameter names (``page_size`` vs ``max_results``), a model that
carries the spelling from one tool to the next gets a schema error rather than a
result.

This script measures that divergence so the claim can be re-checked against a
future upstream head instead of being re-argued from memory.

It also reports singular/batch sibling tools (``get_X`` / ``get_X_batch``) whose
argument lists differ. That is a separate defect from a shared concept spelled
two ways: it is one operation offered twice with two signatures, so an argument
that works on one sibling is a hard rejection on the other.

Usage
-----
    python3 scripts/scan_param_naming.py [git-ref]      # default: upstream/main

Run it from inside a checkout of this repo (or any worktree of it); it reads the
ref's blobs via ``git show`` and never touches the working tree.
"""

from __future__ import annotations

import ast
import collections
import subprocess
import sys

# Concepts that plausibly want ONE name across the surface. Each entry maps a
# human label to the set of spellings currently in use for that single idea.
#
# Deliberately NOT included:
#
# 1. The ``*_id`` family (document_id, spreadsheet_id, file_id, presentation_id,
#    ...). Those name genuinely distinct resource types, and one name per type is
#    correct -- a Doc id and a Sheet id are not interchangeable, so collapsing
#    them would be a regression, not a fix.
#
# 2. ``mime_type`` / ``source_format`` / ``format``. An earlier revision of this
#    script grouped these as one concept. That was WRONG, and acting on it would
#    have been harmful. They are three different things, verified in the source:
#      * ``format`` (get_gmail_messages_content_batch) is Gmail's message DETAIL
#        level -- ``Literal["full", "metadata"]``. Unrelated to MIME.
#      * ``source_format`` is an extension key ("md", "docx", "txt", "html",
#        "rtf", "odt"), which _resolve_import_media turns into ".md" and looks up
#        in an allowlist, raising ValueError on a miss.
#      * ``mime_type`` is a MIME string, defaulting to "text/plain".
#    ``update_drive_file`` declaring BOTH ``mime_type`` and ``source_format`` is
#    evidence that they are distinct, not evidence of drift. Aliasing them would
#    make ``create_drive_file(source_format="md")`` silently ask Drive to create a
#    file with mimeType "md" -- wrong behaviour replacing a clear schema error.
CONCEPTS: dict[str, set[str]] = {
    "result cap": {"page_size", "max_results", "limit", "count", "maxResults"},
    "pagination cursor": {"page_token", "next_page_token", "cursor", "pageToken"},
    "free-text query": {"query", "q", "search_query", "text_query"},
    "body / content": {"body", "content", "text", "message_body", "body_text"},
}


def git_show(ref: str, path: str, repo: str | None = None) -> str:
    return subprocess.run(
        ["git", "show", f"{ref}:{path}"],
        capture_output=True,
        text=True,
        cwd=repo,
        check=True,
    ).stdout


def tool_files(ref: str, repo: str | None = None) -> list[str]:
    """Every non-test Python module at ``ref``.

    Deliberately NOT restricted to ``*_tools.py``: ``start_google_auth`` is
    registered with a real ``@server.tool(`` in ``core/server.py``, so a
    ``*_tools.py`` filter silently undercounts the surface by one. Scanning
    everything and letting ``is_mcp_tool`` do the filtering keeps the count
    honest against a future head that moves or adds a registration.
    """
    out = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", ref],
        capture_output=True,
        text=True,
        cwd=repo,
        check=True,
    ).stdout.split()
    return [f for f in out if f.endswith(".py") and not f.startswith("tests")]


def is_mcp_tool(node: ast.AST) -> bool:
    """True when the function carries an @server.tool(...) registration.

    This is the authoritative signal: it is what actually publishes a schema to
    the client. The ``user_google_email`` heuristic is close but not identical --
    it misses tools that do not take a caller identity.
    """
    for dec in getattr(node, "decorator_list", []):
        try:
            src = ast.unparse(dec)
        except Exception:
            continue
        if "server.tool" in src:
            return True
    return False


def collect(ref: str, repo: str | None = None):
    """-> (param name -> set of tool names, tool name -> list of params)"""
    params: dict[str, set[str]] = collections.defaultdict(set)
    tools: dict[str, list[str]] = {}
    for path in tool_files(ref, repo):
        try:
            tree = ast.parse(git_show(ref, path, repo))
        except SyntaxError:
            print(f"  ! skipped (unparseable): {path}", file=sys.stderr)
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                continue
            if not is_mcp_tool(node):
                continue
            names = [a.arg for a in node.args.args + node.args.kwonlyargs]
            tools[node.name] = names
            for p in names:
                params[p].add(node.name)
    return params, tools


# Arguments every tool takes, plus the id/ids argument that legitimately differs
# between a singular tool and its batch sibling (message_id vs message_ids).
_IGNORED_IN_PAIRS = {"service", "user_google_email"}


def _is_id_param(name: str) -> bool:
    return name.endswith("_id") or name.endswith("_ids")


def singular_batch_pairs(tools: dict[str, list[str]]) -> list[tuple[str, str]]:
    """Pair each ``*_batch`` / ``batch_*`` tool with its singular sibling, if any.

    ``get_gmail_messages_content_batch`` -> ``get_gmail_message_content``: drop the
    batch marker, then try de-pluralising one underscore-token at a time. A batch
    tool with no singular sibling (``batch_update_doc`` wraps Google's own
    batchUpdate call and has none) is simply not a pair.
    """
    pairs = []
    for name in sorted(tools):
        if name.endswith("_batch"):
            base = name[: -len("_batch")]
        elif name.startswith("batch_"):
            base = name[len("batch_") :]
        else:
            continue
        tokens = base.split("_")
        candidates = [base] + [
            "_".join(tokens[:i] + [tok[:-1]] + tokens[i + 1 :])
            for i, tok in enumerate(tokens)
            if tok.endswith("s") and len(tok) > 1
        ]
        for cand in candidates:
            if cand in tools and cand != name:
                pairs.append((cand, name))
                break
    return pairs


def report_pair_drift(tools: dict[str, list[str]]) -> int:
    """Print where a singular tool and its batch sibling take different arguments."""
    drifted = 0
    print("== singular / batch siblings")
    for single, batch in singular_batch_pairs(tools):
        a = {
            p
            for p in tools[single]
            if p not in _IGNORED_IN_PAIRS and not _is_id_param(p)
        }
        b = {
            p
            for p in tools[batch]
            if p not in _IGNORED_IN_PAIRS and not _is_id_param(p)
        }
        if a == b:
            print(f"   {single} / {batch}: same arguments")
            continue
        drifted += 1
        print(f"   {single} / {batch}  <-- DIFFERENT")
        if a - b:
            print(f"      only on the singular: {', '.join(sorted(a - b))}")
        if b - a:
            print(f"      only on the batch:    {', '.join(sorted(b - a))}")
    print()
    return drifted


def main() -> int:
    ref = sys.argv[1] if len(sys.argv) > 1 else "upstream/main"
    params, tools = collect(ref)

    print(f"ref: {ref}")
    print(f"MCP tools found (@server.tool): {len(tools)}")
    print(f"distinct parameter names:       {len(params)}\n")

    findings = 0
    for label, spellings in CONCEPTS.items():
        present = sorted(
            (p for p in spellings if p in params),
            key=lambda p: (-len(params[p]), p),
        )
        if len(present) < 2:
            status = "consistent" if len(present) == 1 else "absent"
            print(f"== {label}: {status}")
            if present:
                print(f"   {present[0]:16} {len(params[present[0]]):3} tool(s)")
            print()
            continue
        findings += 1
        total = sum(len(params[p]) for p in present)
        print(
            f"== {label}: {len(present)} names across {total} tool uses  <-- DIVERGENT"
        )
        for p in present:
            users = sorted(params[p])
            shown = ", ".join(users[:4]) + (", ..." if len(users) > 4 else "")
            print(f"   {p:16} {len(params[p]):3} tool(s)  {shown}")
        print()

    print(f"divergent concepts: {findings}/{len(CONCEPTS)}\n")
    drifted = report_pair_drift(tools)
    print(f"singular/batch pairs with different arguments: {drifted}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
