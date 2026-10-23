import pytest

from gartifacts.artifacts_drive import (
    SHORTCUT_MIME,
    AmbiguousNameError,
    NotABundleError,
    NotFoundError,
    NotServableError,
    list_children,
    normalize_path,
    resolve_path,
    scan_bundle,
    verify_owned_bundle,
)
from gartifacts.artifacts_policy import ArtifactPolicy
from tests.gartifacts.fake_drive import ROOT_ID, FakeDrive

OWNER = "owner@scientist.com"


class TestVerifyOwnedBundle:
    @pytest.mark.asyncio
    async def test_valid_bundle(self):
        drive = FakeDrive()
        bundle = drive.bundle()
        name = await verify_owned_bundle(drive, bundle, "Owner@Scientist.com")
        assert name == "20261022-1815-q3-board-deck"

    @pytest.mark.asyncio
    async def test_unknown_id(self):
        with pytest.raises(NotABundleError, match="could not be read"):
            await verify_owned_bundle(FakeDrive(), "nope", OWNER)

    @pytest.mark.asyncio
    async def test_not_a_folder(self):
        drive = FakeDrive()
        file_id = drive.add("a.html")
        with pytest.raises(NotABundleError, match="not a folder"):
            await verify_owned_bundle(drive, file_id, OWNER)

    @pytest.mark.asyncio
    async def test_trashed(self):
        drive = FakeDrive()
        bundle = drive.bundle()
        drive.items[bundle]["trashed"] = True
        with pytest.raises(NotABundleError, match="trash"):
            await verify_owned_bundle(drive, bundle, OWNER)

    @pytest.mark.asyncio
    async def test_other_owner(self):
        drive = FakeDrive()
        bundle = drive.bundle(owner="someone@scientist.com")
        with pytest.raises(NotABundleError, match="owner"):
            await verify_owned_bundle(drive, bundle, OWNER)

    @pytest.mark.asyncio
    async def test_parent_must_be_named_artifacts(self):
        drive = FakeDrive()
        claude = drive.folder("Claude")
        wrong = drive.folder("Reports", claude)
        bundle = drive.folder("b", wrong)
        with pytest.raises(NotABundleError, match="Claude/Artifacts"):
            await verify_owned_bundle(drive, bundle, OWNER)

    @pytest.mark.asyncio
    async def test_artifacts_must_sit_in_claude(self):
        drive = FakeDrive()
        other = drive.folder("Other")
        artifacts = drive.folder("Artifacts", other)
        bundle = drive.folder("b", artifacts)
        with pytest.raises(NotABundleError):
            await verify_owned_bundle(drive, bundle, OWNER)

    @pytest.mark.asyncio
    async def test_claude_must_be_at_top_level(self):
        drive = FakeDrive()
        projects = drive.folder("Projects")
        claude = drive.folder("Claude", projects)
        artifacts = drive.folder("Artifacts", claude)
        bundle = drive.folder("b", artifacts)
        with pytest.raises(NotABundleError, match="top level"):
            await verify_owned_bundle(drive, bundle, OWNER)

    @pytest.mark.asyncio
    async def test_parent_owned_by_someone_else(self):
        drive = FakeDrive()
        claude = drive.folder("Claude", owner="other@scientist.com")
        artifacts = drive.folder("Artifacts", claude, owner="other@scientist.com")
        bundle = drive.folder("b", artifacts)
        with pytest.raises(NotABundleError):
            await verify_owned_bundle(drive, bundle, OWNER)

    @pytest.mark.asyncio
    async def test_multiple_parents_refused(self):
        drive = FakeDrive()
        bundle = drive.bundle()
        drive.items[bundle]["parents"].append(ROOT_ID)
        with pytest.raises(NotABundleError, match="directly in"):
            await verify_owned_bundle(drive, bundle, OWNER)


class TestNormalizePath:
    def test_index_defaults(self):
        assert normalize_path("") == ["index.html"]
        assert normalize_path("/") == ["index.html"]
        assert normalize_path("docs/") == ["docs", "index.html"]
        assert normalize_path("/assets/app.css") == ["assets", "app.css"]

    @pytest.mark.parametrize(
        "bad",
        [
            "../x",
            "a/../b",
            "a//b",
            ".",
            "a\\b",
            "a\x00b",
            "a/" + "x" * 300,
            "/".join("a" * 20),
        ],
    )
    def test_rejects_unsafe_paths(self, bad):
        with pytest.raises(NotFoundError):
            normalize_path(bad)


class TestResolvePath:
    @pytest.mark.asyncio
    async def test_resolves_nested_file(self):
        drive = FakeDrive()
        bundle = drive.bundle()
        assets = drive.folder("assets", bundle)
        css = drive.add("app.css", assets, mime="text/css", content=b"x")
        item = await resolve_path(drive, bundle, "assets/app.css")
        assert item["id"] == css

    @pytest.mark.asyncio
    async def test_default_index(self):
        drive = FakeDrive()
        bundle = drive.bundle()
        index = drive.add("index.html", bundle, mime="text/html")
        assert (await resolve_path(drive, bundle, ""))["id"] == index

    @pytest.mark.asyncio
    async def test_missing_and_folder_targets(self):
        drive = FakeDrive()
        bundle = drive.bundle()
        drive.folder("assets", bundle)
        with pytest.raises(NotFoundError):
            await resolve_path(drive, bundle, "missing.html")
        with pytest.raises(NotFoundError):
            await resolve_path(drive, bundle, "assets")
        with pytest.raises(NotFoundError):
            await resolve_path(drive, bundle, "index.html/x")

    @pytest.mark.asyncio
    async def test_duplicates_refused(self):
        drive = FakeDrive()
        bundle = drive.bundle()
        drive.add("index.html", bundle)
        drive.add("index.html", bundle)
        with pytest.raises(AmbiguousNameError):
            await resolve_path(drive, bundle, "index.html")

    @pytest.mark.asyncio
    async def test_shortcuts_and_google_files_refused(self):
        drive = FakeDrive()
        bundle = drive.bundle()
        drive.add("link.html", bundle, mime=SHORTCUT_MIME)
        drive.add("doc", bundle, mime="application/vnd.google-apps.document")
        with pytest.raises(NotServableError):
            await resolve_path(drive, bundle, "link.html")
        with pytest.raises(NotServableError):
            await resolve_path(drive, bundle, "doc")

    @pytest.mark.asyncio
    async def test_trashed_items_are_invisible(self):
        drive = FakeDrive()
        bundle = drive.bundle()
        drive.add("index.html", bundle, trashed=True)
        with pytest.raises(NotFoundError):
            await resolve_path(drive, bundle, "index.html")

    @pytest.mark.asyncio
    async def test_files_outside_the_bundle_are_unreachable(self):
        drive = FakeDrive()
        bundle = drive.bundle()
        drive.add("secret.html", ROOT_ID)
        with pytest.raises(NotFoundError):
            await resolve_path(drive, bundle, "secret.html")

    @pytest.mark.asyncio
    async def test_names_with_quotes_are_escaped(self):
        drive = FakeDrive()
        bundle = drive.bundle()
        odd = drive.add("it's.html", bundle)
        assert (await resolve_path(drive, bundle, "it's.html"))["id"] == odd
        assert "name = 'it\\'s.html'" in drive.list_calls[-1]


class TestListChildren:
    @pytest.mark.asyncio
    async def test_paginates(self):
        drive = FakeDrive(page_size=2)
        bundle = drive.bundle()
        for n in range(5):
            drive.add(f"f{n}.txt", bundle)
        assert len(await list_children(drive, bundle)) == 5


class TestScanBundle:
    @pytest.mark.asyncio
    async def test_clean_bundle(self):
        drive = FakeDrive()
        bundle = drive.bundle()
        drive.add("index.html", bundle, mime="text/html", content=b"<h1>hi</h1>")
        assets = drive.folder("assets", bundle)
        drive.add("app.css", assets, mime="text/css", content=b"body{}")
        report = await scan_bundle(drive, bundle, ArtifactPolicy())
        assert report.errors == [] and report.warnings == []
        assert report.file_count == 2 and report.total_bytes == 17

    @pytest.mark.asyncio
    async def test_warnings_come_from_nested_files(self):
        drive = FakeDrive()
        bundle = drive.bundle()
        drive.add("index.html", bundle, mime="text/html", content=b"ok")
        assets = drive.folder("assets", bundle)
        drive.add(
            "page.html",
            assets,
            mime="text/html",
            content=b'<script src="https://evil.example.com/x.js"></script>',
        )
        report = await scan_bundle(drive, bundle, ArtifactPolicy())
        assert any(w.startswith("assets/page.html: ") for w in report.warnings)

    @pytest.mark.asyncio
    async def test_missing_entry_page(self):
        drive = FakeDrive()
        bundle = drive.bundle()
        drive.add("other.html", bundle, mime="text/html", content=b"x")
        report = await scan_bundle(drive, bundle, ArtifactPolicy())
        assert any("no index.html" in e for e in report.errors)

    @pytest.mark.asyncio
    async def test_duplicate_names_are_errors(self):
        drive = FakeDrive()
        bundle = drive.bundle()
        drive.add("index.html", bundle, content=b"a")
        drive.add("index.html", bundle, content=b"b")
        report = await scan_bundle(drive, bundle, ArtifactPolicy())
        assert any("more than one item" in e for e in report.errors)

    @pytest.mark.asyncio
    async def test_limits(self):
        drive = FakeDrive()
        bundle = drive.bundle()
        drive.add("index.html", bundle, content=b"x")
        drive.add("big.bin", bundle, size=10_000_000)
        report = await scan_bundle(drive, bundle, ArtifactPolicy())
        assert any("per-file limit" in e for e in report.errors)

        small = ArtifactPolicy(
            limits={
                "max_file_bytes": 100,
                "max_bundle_files": 2,
                "max_bundle_bytes": 150,
            }
        )
        drive2 = FakeDrive()
        b2 = drive2.bundle()
        for n in range(4):
            drive2.add(f"index{n}.html" if n else "index.html", b2, content=b"y" * 60)
        assert any(
            "too many" in e or "total size" in e
            for e in (await scan_bundle(drive2, b2, small)).errors
        )

    @pytest.mark.asyncio
    async def test_shortcuts_warn_and_are_not_counted(self):
        drive = FakeDrive()
        bundle = drive.bundle()
        drive.add("index.html", bundle, content=b"x")
        drive.add("link", bundle, mime=SHORTCUT_MIME)
        report = await scan_bundle(drive, bundle, ArtifactPolicy())
        assert report.file_count == 1
        assert any("not served" in w for w in report.warnings)
