import pytest

from gartifacts.artifacts_policy import ArtifactPolicy
from gartifacts.artifacts_scan import scan_text

POLICY = ArtifactPolicy()


def scan(name, text, policy=POLICY):
    return scan_text(name, text, policy)


def test_clean_page_has_no_warnings():
    html = (
        '<link rel="stylesheet" href="assets/styles/app.css">'
        '<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.js" '
        'integrity="sha384-x" crossorigin="anonymous"></script>'
        '<img src="assets/images/a.png"><a href="https://example.com">out</a>'
    )
    assert scan("index.html", html) == []


def test_blocked_script_host():
    out = scan("index.html", '<script src="https://evil.example.com/x.js"></script>')
    assert any("evil.example.com" in w and "will be blocked" in w for w in out)


def test_protocol_relative_and_stylesheet():
    out = scan("index.html", '<link rel="stylesheet" href="//evil.example.com/a.css">')
    assert any("evil.example.com" in w for w in out)


@pytest.mark.parametrize(
    "url",
    [
        "https://cdn.jsdelivr.net/npm/react/umd/react.production.min.js",
        "https://cdn.jsdelivr.net/npm/react@latest/umd/react.js",
        "https://unpkg.com/react/umd/react.js",
        "https://unpkg.com/@scope/pkg/dist/a.js",
        "https://cdn.jsdelivr.net/gh/user/repo/file.js",
    ],
)
def test_unpinned_versions_warn(url):
    out = scan("index.html", f'<script src="{url}" integrity="sha384-x"></script>')
    assert any("without an exact version" in w for w in out)


@pytest.mark.parametrize(
    "url",
    [
        "https://cdn.jsdelivr.net/npm/react@18.3.1/umd/react.js",
        "https://unpkg.com/react@18.3.1/umd/react.js",
        "https://unpkg.com/@scope/pkg@1.2.3/dist/a.js",
        "https://cdn.jsdelivr.net/gh/user/repo@v1.0.0/file.js",
        "https://cdnjs.cloudflare.com/ajax/libs/d3/7.9.0/d3.min.js",
    ],
)
def test_pinned_versions_pass(url):
    out = scan("index.html", f'<script src="{url}" integrity="sha384-x"></script>')
    assert out == []


def test_missing_integrity_is_advice():
    out = scan(
        "index.html", '<script src="https://unpkg.com/react@18.3.1/a.js"></script>'
    )
    assert len(out) == 1 and "integrity" in out[0] and "advice" in out[0]


def test_frames_and_media_blocked():
    out = scan(
        "index.html",
        '<iframe src="https://example.com"></iframe><video src="https://example.com/v.mp4"></video>',
    )
    assert any("<iframe> is blocked" in w for w in out)
    assert any("<video src>" in w for w in out)


def test_css_import_and_urls():
    css = (
        '@import url("https://evil.example.com/a.css");'
        "@font-face{src:url(https://evil.example.com/f.woff2)}"
        "body{background:url(https://evil.example.com/b.png)}"
        "p{background:url(assets/images/ok.png)}"
    )
    out = scan("assets/styles/app.css", css)
    assert any("@import" in w for w in out)
    assert any("not allowed for fonts" in w for w in out)
    assert any("not allowed for imgs" in w for w in out)


def test_js_imports_and_network_calls():
    js = (
        'import x from "https://evil.example.com/m.js";\n'
        'fetch("https://api.example.com/data");\n'
        'fetch("data.json");\n'
        'new WebSocket("wss://live.example.com");\n'
    )
    out = scan("app.js", js)
    assert any("import" in w and "evil.example.com" in w for w in out)
    assert any("fetch to api.example.com" in w for w in out)
    assert any("WebSocket" in w for w in out)
    assert not any("data.json" in w for w in out)


def test_connect_allowed_host_not_flagged():
    policy = ArtifactPolicy(
        hosts={**ArtifactPolicy().hosts, "connect": ("api.example.com",)}
    )
    out = scan("app.js", 'fetch("https://api.example.com/data");')
    assert out
    assert scan("app.js", 'fetch("https://api.example.com/data");', policy) == []


def test_warnings_are_deduplicated_and_name_the_file():
    html = '<script src="https://evil.example.com/x.js"></script>' * 3
    out = scan("page2.html", html)
    assert len(out) == 1 and out[0].startswith("page2.html: ")
