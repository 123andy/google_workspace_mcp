"""
Content policy for served artifacts.

The default allow-list names well-known public CDNs and can be replaced with a JSON
file (``WORKSPACE_MCP_ARTIFACTS_CSP_PROFILE``). The same policy builds the
Content-Security-Policy header, is reported to the packaging skill, and is used to
check a bundle at publish time, so the list lives in one place.
"""

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Dict, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)

CSP_PROFILE_ENV = "WORKSPACE_MCP_ARTIFACTS_CSP_PROFILE"

# Directive keys accepted in the policy and the CSP directive each one fills.
HOST_DIRECTIVES: Mapping[str, str] = {
    "script": "script-src",
    "style": "style-src",
    "font": "font-src",
    "img": "img-src",
    "connect": "connect-src",
}

DEFAULT_HOSTS: Mapping[str, Tuple[str, ...]] = {
    "script": (
        "cdnjs.cloudflare.com",
        "cdn.jsdelivr.net",
        "unpkg.com",
        "esm.sh",
        "cdn.skypack.dev",
        "ga.jspm.io",
        "ajax.googleapis.com",
        "code.jquery.com",
        "cdn.tailwindcss.com",
        "cdn.plot.ly",
        "d3js.org",
        "code.highcharts.com",
        "cdn.sheetjs.com",
        "cdn.datatables.net",
        "www.gstatic.com",
        "maxcdn.bootstrapcdn.com",
        "stackpath.bootstrapcdn.com",
        "use.fontawesome.com",
    ),
    "style": (
        "cdnjs.cloudflare.com",
        "cdn.jsdelivr.net",
        "unpkg.com",
        "fonts.googleapis.com",
        "cdn.datatables.net",
        "maxcdn.bootstrapcdn.com",
        "stackpath.bootstrapcdn.com",
        "use.fontawesome.com",
    ),
    "font": (
        "fonts.gstatic.com",
        "cdnjs.cloudflare.com",
        "cdn.jsdelivr.net",
        "unpkg.com",
        "use.fontawesome.com",
    ),
    "img": (
        "cdnjs.cloudflare.com",
        "cdn.jsdelivr.net",
        "unpkg.com",
        "upload.wikimedia.org",
        "images.unsplash.com",
        "picsum.photos",
        "placehold.co",
        "tile.openstreetmap.org",
        "*.tile.openstreetmap.org",
        "*.basemaps.cartocdn.com",
        "server.arcgisonline.com",
    ),
    "connect": (),
}

# A host is only worth allowing if nobody else can host their own content on it, so
# these suffixes (where anyone can register a subdomain or bucket) and known-bad
# hosts are refused even when a profile file asks for them.
USER_HOSTABLE_SUFFIXES: Tuple[str, ...] = (
    "github.io",
    "githubusercontent.com",
    "netlify.app",
    "vercel.app",
    "pages.dev",
    "workers.dev",
    "web.app",
    "firebaseapp.com",
    "herokuapp.com",
    "appspot.com",
    "amazonaws.com",
    "cloudfront.net",
    "azurewebsites.net",
    "onrender.com",
    "glitch.me",
    "repl.co",
    "blob.core.windows.net",
)
REFUSED_HOSTS: Tuple[str, ...] = ("polyfill.io", "cdn.polyfill.io")

_HOST_PATTERN = re.compile(
    r"^(\*\.)?[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$"
)

DEFAULT_LIMITS: Mapping[str, int] = {
    "max_file_bytes": 5_000_000,
    "max_bundle_files": 100,
    "max_bundle_bytes": 25_000_000,
}

SANDBOX_LIMITS: Tuple[str, ...] = (
    "No cookies, localStorage or sessionStorage: the page runs in an opaque origin.",
    "No downloads or popups, and no navigation of the surrounding page. Dialogs "
    "(alert, confirm, print) are allowed.",
    "No fetch, XMLHttpRequest, WebSocket or beacon to other origins; "
    "fetching the bundle's own files is allowed.",
    "No Web Workers from bundle files (only blob: workers).",
)


class PolicyError(ValueError):
    """Raised when a policy profile is invalid."""


@dataclass(frozen=True)
class ArtifactPolicy:
    """The effective content policy for this deployment."""

    hosts: Mapping[str, Tuple[str, ...]] = field(
        default_factory=lambda: dict(DEFAULT_HOSTS)
    )
    allow_unsafe_eval: bool = True
    limits: Mapping[str, int] = field(default_factory=lambda: dict(DEFAULT_LIMITS))


def validate_host(host: str) -> str:
    """Normalise a host source or raise PolicyError."""
    value = (host or "").strip().lower()
    if value.startswith("https://"):
        value = value[len("https://") :]
    if not _HOST_PATTERN.match(value):
        raise PolicyError(f"Not a valid host source: {host!r}")
    bare = value[2:] if value.startswith("*.") else value
    if bare in REFUSED_HOSTS or any(bare.endswith("." + h) for h in REFUSED_HOSTS):
        raise PolicyError(f"Host is refused: {host!r}")
    for suffix in USER_HOSTABLE_SUFFIXES:
        if bare == suffix or bare.endswith("." + suffix):
            raise PolicyError(
                f"{host!r} is on a domain where anyone can host content ({suffix})"
            )
    if value.startswith("*.") and bare.count(".") < 1:
        raise PolicyError(f"Wildcard is too broad: {host!r}")
    return value


def load_policy(profile_path: Optional[str] = None) -> ArtifactPolicy:
    """Build the policy from the default list or from a JSON profile file.

    The file may contain ``script``, ``style``, ``font``, ``img`` and ``connect`` host
    lists (each replaces that directive's default), ``allow_unsafe_eval`` and
    ``limits``. Unknown keys are an error so a typo cannot silently widen the policy.
    """
    path = profile_path if profile_path is not None else os.getenv(CSP_PROFILE_ENV, "")
    if not path.strip():
        return ArtifactPolicy()
    try:
        with open(path.strip(), "r", encoding="utf-8") as handle:
            profile = json.load(handle)
    except (OSError, ValueError) as exc:
        raise PolicyError(
            f"Cannot read {CSP_PROFILE_ENV} file {path!r}: {exc}"
        ) from exc
    if not isinstance(profile, dict):
        raise PolicyError("The CSP profile must be a JSON object")
    allowed = set(HOST_DIRECTIVES) | {"allow_unsafe_eval", "limits"}
    unknown = sorted(set(profile) - allowed)
    if unknown:
        raise PolicyError(f"Unknown keys in CSP profile: {unknown}")

    hosts: Dict[str, Tuple[str, ...]] = dict(DEFAULT_HOSTS)
    for key in HOST_DIRECTIVES:
        if key in profile:
            values = profile[key]
            if not isinstance(values, list):
                raise PolicyError(f"CSP profile {key!r} must be a list of hosts")
            hosts[key] = tuple(dict.fromkeys(validate_host(v) for v in values))

    unsafe_eval = profile.get("allow_unsafe_eval", True)
    if not isinstance(unsafe_eval, bool):
        raise PolicyError("allow_unsafe_eval must be true or false")

    limits = dict(DEFAULT_LIMITS)
    for name, value in (profile.get("limits") or {}).items():
        if name not in DEFAULT_LIMITS or not isinstance(value, int) or value <= 0:
            raise PolicyError(f"Invalid limit {name!r}: {value!r}")
        limits[name] = value
    return ArtifactPolicy(hosts=hosts, allow_unsafe_eval=unsafe_eval, limits=limits)


def host_allowed(policy: ArtifactPolicy, directive: str, host: str) -> bool:
    """True when ``host`` matches the allow-list for a directive key (e.g. 'script')."""
    target = (host or "").strip().lower()
    for source in policy.hosts.get(directive, ()):
        if source.startswith("*."):
            if target.endswith(source[1:]):
                return True
        elif target == source:
            return True
    return False


_SELF = "'self'"
_NONE = "'none'"
_UNSAFE_INLINE = "'unsafe-inline'"
_UNSAFE_EVAL = "'unsafe-eval'"


def build_csp(
    policy: ArtifactPolicy,
    *,
    frame_ancestors: Optional[str] = None,
    report_path: Optional[str] = None,
) -> str:
    """Return the Content-Security-Policy header value for content responses."""

    def sources(key: str, *keywords: str) -> str:
        return " ".join(keywords + tuple(policy.hosts.get(key, ())))

    script_keywords = (_SELF, _UNSAFE_INLINE)
    if policy.allow_unsafe_eval:
        script_keywords += (_UNSAFE_EVAL,)
    parts = [
        f"default-src {_NONE}",
        f"script-src {sources('script', *script_keywords)}",
        f"style-src {sources('style', _SELF, _UNSAFE_INLINE)}",
        f"font-src {sources('font', _SELF, 'data:')}",
        f"img-src {sources('img', _SELF, 'data:', 'blob:')}",
        f"connect-src {sources('connect', _SELF, 'data:', 'blob:')}",
        f"media-src {_SELF} data: blob:",
        "worker-src blob:",
        f"frame-src {_NONE}",
        f"object-src {_NONE}",
        f"manifest-src {_NONE}",
        f"form-action {_NONE}",
        f"base-uri {_NONE}",
        f"frame-ancestors {frame_ancestors or _NONE}",
        "sandbox allow-scripts allow-modals",
    ]
    if report_path:
        parts.append(f"report-uri {report_path}")
    return "; ".join(parts)


def policy_version(policy: ArtifactPolicy) -> str:
    """Short hash of the effective policy, so callers can tell when it changed."""
    canonical = json.dumps(
        {
            "hosts": {k: list(v) for k, v in sorted(policy.hosts.items())},
            "unsafe_eval": policy.allow_unsafe_eval,
            "limits": dict(sorted(policy.limits.items())),
        },
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


def rules_text(policy: ArtifactPolicy) -> str:
    """Plain-language rules for the model that is building an artifact."""
    lines = [
        "Build the artifact as a self-contained bundle: one entry page, other pages "
        "beside it, assets in subfolders, relative paths only.",
        "Load libraries only from these hosts, with an exact version in the URL "
        "(never 'latest'): " + ", ".join(sorted(set(policy.hosts["script"]))) + ".",
        'Add an integrity hash (integrity="sha384-..." with crossorigin="anonymous") '
        "only if you can compute the real value; vendor the library into the bundle "
        "for sensitive or long-lived artifacts.",
        "Images and fonts from other sites are limited to: "
        + ", ".join(sorted(set(policy.hosts["img"]) | set(policy.hosts["font"])))
        + ". Put other assets in the bundle.",
    ]
    if policy.hosts.get("connect"):
        lines.append(
            "Network calls from the page are allowed only to: "
            + ", ".join(policy.hosts["connect"])
            + " and the bundle's own files."
        )
    lines.extend(SANDBOX_LIMITS)
    lines.append(
        "Never embed secrets, credentials or restricted data: anyone who can open the "
        "artifact, and any script in it, can read what it holds."
    )
    return "\n".join(f"- {line}" for line in lines)


def describe_policy(policy: ArtifactPolicy) -> Dict[str, object]:
    """Structured form of the policy for the policy tool."""
    return {
        "policy_version": policy_version(policy),
        "hosts": {key: list(policy.hosts.get(key, ())) for key in HOST_DIRECTIVES},
        "allow_unsafe_eval": policy.allow_unsafe_eval,
        "limits": dict(policy.limits),
        "sandbox": list(SANDBOX_LIMITS),
        "rules": rules_text(policy),
    }
