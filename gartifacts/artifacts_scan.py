"""
Publish-time check of a bundle's text files against the content policy.

A heuristic over static text: it cannot see URLs built at run time or what a loaded
library fetches itself. The Content-Security-Policy applied when the page is served is
the real enforcement; these warnings only catch the common mistakes early.
"""

import re
from typing import List, Optional
from urllib.parse import urlsplit

from gartifacts.artifacts_policy import ArtifactPolicy, host_allowed

TEXT_EXTENSIONS = (".html", ".htm", ".css", ".js", ".mjs", ".svg")

_TAG = re.compile(
    r"<(script|link|img|source|video|audio|track|iframe|frame|embed|object)\b([^>]*)>",
    re.I,
)
_ATTR = re.compile(
    r"""\b(src|href|data)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""", re.I
)
_REL = re.compile(r"""\brel\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""", re.I)
_CSS_IMPORT = re.compile(r"""@import\s+(?:url\(\s*)?["']?([^"')\s;]+)""", re.I)
_CSS_URL = re.compile(r"""url\(\s*["']?([^"')]+)["']?\s*\)""", re.I)
_JS_IMPORT = re.compile(
    r"""(?:\bfrom\s*|\bimport\s*\(\s*|\bimport\s+)["'](https?:)?(//[^"']+|https?://[^"']+)["']"""
)
_JS_CALL = re.compile(
    r"""\b(fetch|XMLHttpRequest|WebSocket|EventSource|sendBeacon)\b[^;]*?["'`]((?:https?:|wss?:)?//[^"'`\s]+)["'`]""",
    re.I,
)
_FONT_URL = re.compile(r"\.(woff2?|ttf|otf|eot)(\?|#|$)", re.I)

_BLOCKED_ELEMENTS = {
    "iframe": "frames",
    "frame": "frames",
    "embed": "embedded objects",
    "object": "embedded objects",
}
_VERSIONED_HOSTS = ("cdn.jsdelivr.net", "unpkg.com", "esm.sh", "cdn.skypack.dev")


def _external_host(url: str) -> Optional[str]:
    """Return the host of an external http(s)/ws(s) URL, or None for local ones."""
    candidate = url.strip()
    if candidate.startswith("//"):
        candidate = "https:" + candidate
    parts = urlsplit(candidate)
    if parts.scheme in ("http", "https", "ws", "wss") and parts.hostname:
        return parts.hostname.lower()
    return None


def _unpinned(url: str) -> bool:
    """True when a versioned CDN URL has no exact version (or says 'latest')."""
    if "@latest" in url:
        return True
    parts = urlsplit("https:" + url if url.startswith("//") else url)
    host = (parts.hostname or "").lower()
    segments = [s for s in parts.path.split("/") if s]
    package = ""
    if host == "cdn.jsdelivr.net" and len(segments) >= 2:
        if segments[0] == "npm":
            package = (
                "/".join(segments[1:3]) if segments[1].startswith("@") else segments[1]
            )
        elif segments[0] == "gh" and len(segments) >= 3:
            package = segments[2]
    elif host in _VERSIONED_HOSTS and host != "cdn.jsdelivr.net" and segments:
        package = "/".join(segments[:2]) if segments[0].startswith("@") else segments[0]
    return bool(package) and "@" not in package.lstrip("@")


def scan_text(name: str, text: str, policy: ArtifactPolicy) -> List[str]:
    """Return warnings for one text file."""
    warnings: List[str] = []
    seen = set()

    def warn(message: str) -> None:
        if message not in seen:
            seen.add(message)
            warnings.append(f"{name}: {message}")

    def check(url: str, key: str, label: str, tag: str = "") -> None:
        host = _external_host(url)
        if host is None:
            return
        if not host_allowed(policy, key, host):
            warn(
                f"{label} loads {host}, which is not allowed for {key}s and will be blocked"
            )
        elif key in ("script", "style") and _unpinned(url):
            warn(f"{label} loads {url} without an exact version")

    lowered = name.lower()
    if lowered.endswith((".html", ".htm", ".svg")):
        for match in _TAG.finditer(text):
            element, attrs = match.group(1).lower(), match.group(2)
            urls = {
                m.group(1).lower(): next(g for g in m.groups()[1:] if g is not None)
                for m in _ATTR.finditer(attrs)
            }
            if element == "script":
                if "src" in urls:
                    check(urls["src"], "script", "<script src>")
                    host = _external_host(urls["src"])
                    if (
                        host
                        and host_allowed(policy, "script", host)
                        and "integrity" not in attrs.lower()
                    ):
                        warn(
                            f"<script src> from {host} has no integrity attribute "
                            "(advice: add one only if you can compute the real hash)"
                        )
            elif element == "link":
                rel = _REL.search(attrs)
                rel_value = (
                    next((g for g in rel.groups() if g), "").lower() if rel else ""
                )
                target = urls.get("href", "")
                if "stylesheet" in rel_value:
                    check(target, "style", "<link rel=stylesheet>")
                elif "icon" in rel_value or "image" in rel_value:
                    check(target, "img", "<link rel=icon>")
                elif rel_value and _external_host(target):
                    warn(
                        f"<link rel={rel_value}> to {_external_host(target)} is not loaded by the content policy"
                    )
            elif element in ("img", "source", "track"):
                check(urls.get("src", ""), "img", f"<{element} src>")
            elif element in ("video", "audio"):
                host = _external_host(urls.get("src", ""))
                if host:
                    warn(f"<{element} src> loads {host}; external media is blocked")
            else:
                target = urls.get("src") or urls.get("data") or ""
                host = _external_host(target)
                if host or target:
                    warn(
                        f"<{element}> is blocked: the content policy allows no {_BLOCKED_ELEMENTS[element]}"
                    )

    if lowered.endswith((".html", ".htm", ".css", ".svg")):
        for url in _CSS_IMPORT.findall(text):
            check(url, "style", "@import")
        for url in _CSS_URL.findall(text):
            key = "font" if _FONT_URL.search(url) else "img"
            check(url, key, "css url()")

    if lowered.endswith((".html", ".htm", ".js", ".mjs", ".svg")):
        for scheme, rest in _JS_IMPORT.findall(text):
            check((scheme or "") + rest, "script", "import")
        for api, url in _JS_CALL.findall(text):
            host = _external_host(url)
            if host and not host_allowed(policy, "connect", host):
                warn(
                    f"{api} to {host} will be blocked: pages may only fetch the bundle's own files"
                )
        if not policy.hosts.get("connect"):
            for api in sorted(
                set(re.findall(r"\b(WebSocket|EventSource|sendBeacon)\b", text))
            ):
                warn(
                    f"{api} is not available to artifacts (the page has no network access)"
                )
    return warnings
