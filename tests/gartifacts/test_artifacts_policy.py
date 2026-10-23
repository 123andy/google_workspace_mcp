import json

import pytest

from gartifacts import artifacts_policy as policy_mod
from gartifacts.artifacts_policy import (
    ArtifactPolicy,
    PolicyError,
    build_csp,
    describe_policy,
    host_allowed,
    load_policy,
    policy_version,
    rules_text,
    validate_host,
)


def test_default_policy_has_expected_hosts():
    policy = ArtifactPolicy()
    assert "cdn.jsdelivr.net" in policy.hosts["script"]
    assert "fonts.gstatic.com" in policy.hosts["font"]
    assert policy.hosts["connect"] == ()


def test_default_csp_shape():
    csp = build_csp(ArtifactPolicy())
    directives = dict(part.split(" ", 1) for part in csp.split("; ") if " " in part)
    assert directives["default-src"] == "'none'"
    assert directives["connect-src"] == "'self' data: blob:"
    assert directives["frame-src"] == "'none'"
    assert directives["form-action"] == "'none'"
    assert directives["frame-ancestors"] == "'none'"
    assert directives["worker-src"] == "blob:"
    assert directives["sandbox"] == "allow-scripts"
    assert "'unsafe-eval'" in directives["script-src"]
    assert "cdn.jsdelivr.net" in directives["script-src"]
    assert "report-uri" not in directives


def test_csp_options():
    policy = ArtifactPolicy(allow_unsafe_eval=False)
    csp = build_csp(
        policy, frame_ancestors="https://artifacts.s7t.io", report_path="/csp-report"
    )
    assert "'unsafe-eval'" not in csp
    assert "frame-ancestors https://artifacts.s7t.io" in csp
    assert csp.endswith("report-uri /csp-report")


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "*",
        "*.com",
        "https:",
        "'self'",
        "evil.github.io",
        "*.netlify.app",
        "bucket.s3.amazonaws.com",
        "polyfill.io",
        "cdn.polyfill.io",
        "has space.com",
        "no-dot",
    ],
)
def test_validate_host_refuses(bad):
    with pytest.raises(PolicyError):
        validate_host(bad)


def test_validate_host_accepts_and_normalises():
    assert validate_host("CDN.Example.com") == "cdn.example.com"
    assert validate_host("https://cdn.example.com") == "cdn.example.com"
    assert validate_host("*.tiles.example.com") == "*.tiles.example.com"


def test_host_allowed_wildcards():
    policy = ArtifactPolicy()
    assert host_allowed(policy, "img", "a.tile.openstreetmap.org")
    assert not host_allowed(policy, "img", "tile.openstreetmap.org.evil.com")
    assert host_allowed(policy, "script", "unpkg.com")
    assert not host_allowed(policy, "script", "evil.example.com")
    assert not host_allowed(policy, "connect", "unpkg.com")


def test_load_policy_from_file(tmp_path, monkeypatch):
    path = tmp_path / "profile.json"
    path.write_text(
        json.dumps(
            {
                "script": ["cdn.example.com"],
                "connect": ["api.example.com"],
                "allow_unsafe_eval": False,
                "limits": {"max_bundle_files": 10},
            }
        )
    )
    monkeypatch.setenv(policy_mod.CSP_PROFILE_ENV, str(path))
    policy = load_policy()
    assert policy.hosts["script"] == ("cdn.example.com",)
    assert policy.hosts["style"] == policy_mod.DEFAULT_HOSTS["style"]
    assert policy.hosts["connect"] == ("api.example.com",)
    assert not policy.allow_unsafe_eval
    assert policy.limits["max_bundle_files"] == 10
    assert (
        policy.limits["max_file_bytes"] == policy_mod.DEFAULT_LIMITS["max_file_bytes"]
    )


@pytest.mark.parametrize(
    "profile",
    [
        [],
        {"scripts": ["x.example.com"]},
        {"script": "cdn.example.com"},
        {"script": ["evil.github.io"]},
        {"allow_unsafe_eval": "yes"},
        {"limits": {"nope": 1}},
        {"limits": {"max_file_bytes": 0}},
    ],
)
def test_load_policy_rejects_bad_profiles(tmp_path, profile):
    path = tmp_path / "p.json"
    path.write_text(json.dumps(profile))
    with pytest.raises(PolicyError):
        load_policy(str(path))


def test_load_policy_missing_file(tmp_path):
    with pytest.raises(PolicyError):
        load_policy(str(tmp_path / "missing.json"))


def test_load_policy_default_when_unset(monkeypatch):
    monkeypatch.delenv(policy_mod.CSP_PROFILE_ENV, raising=False)
    assert load_policy() == ArtifactPolicy()


def test_policy_version_changes_with_policy():
    base = policy_version(ArtifactPolicy())
    assert base == policy_version(ArtifactPolicy())
    assert base != policy_version(ArtifactPolicy(allow_unsafe_eval=False))
    assert len(base) == 12


def test_rules_text_and_description():
    policy = ArtifactPolicy()
    rules = rules_text(policy)
    assert "cdn.jsdelivr.net" in rules
    assert "Never embed secrets" in rules
    assert "latest" in rules
    described = describe_policy(policy)
    assert described["policy_version"] == policy_version(policy)
    assert described["hosts"]["connect"] == []
    assert described["rules"] == rules
