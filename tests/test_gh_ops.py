"""gh operations tests (Task 3). Stdlib unittest only.

`core.run` is monkeypatched with a fake that emulates `git`/`gh` side
effects on real temp dirs -- the real CLI is never invoked. Live tests
run only with STARFIELD_LIVE=1.
"""

import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from starfield import cli, core, gh_ops

FAKE_SHA = "abc1234def5678abc1234def5678abc1234def5"


def make_source(files):
    """Create a temp folder holding {relpath: bytes} (caller owns cleanup)."""
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    for rel, data in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return tmp, root


class FakeRun:
    """Callable emulating git/gh via canned responses + real FS effects."""

    def __init__(self, test):
        self.test = test
        self.calls = []
        self.status_output = " M index.html\n"
        self.status_script = []
        self.view_ok = True

    def __call__(self, cmd, timeout=300):
        self.calls.append((list(cmd), timeout))
        if cmd[0] == "git" and cmd[1] == "clone":
            dest = Path(cmd[-1])
            dest.mkdir(parents=True, exist_ok=True)
            (dest / ".git").mkdir(exist_ok=True)
            (dest / "old.txt").write_text("stale")
            return ""
        if cmd[0] == "git" and "status" in cmd:
            if self.status_script:
                return self.status_script.pop(0)
            return self.status_output
        if cmd[0] == "git" and "rev-parse" in cmd:
            return FAKE_SHA + "\n"
        if cmd[0] == "gh" and cmd[1:3] == ["repo", "view"]:
            if self.view_ok:
                return json.dumps({"name": cmd[3].split("/")[1]})
            raise core.CliError("gh: exited 1: Could not resolve to a Repository")
        if cmd[0] == "gh" and cmd[1:3] == ["repo", "list"]:
            return json.dumps(
                [
                    {
                        "name": "lyra",
                        "isPrivate": True,
                        "updatedAt": "2026-10-01T00:00:00Z",
                        "pushedAt": "2026-10-02T00:00:00Z",
                    }
                ]
            )
        return ""


class GhOpsTestBase(unittest.TestCase):
    def setUp(self):
        self._real_run = core.run
        self.fake = FakeRun(self)
        core.run = self.fake
        self.addCleanup(self._restore)

    def _restore(self):
        core.run = self._real_run

    def make_source(self, files):
        """Create a temp folder holding {relpath: bytes} (auto-cleaned)."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        for rel, data in files.items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
        return root


class TestSync(GhOpsTestBase):
    def test_sync_copies_files_and_returns_sha(self):
        src = self.make_source({"index.html": b"<h1>hi</h1>", "sub/app.js": b"1"})
        with tempfile.TemporaryDirectory() as wd:
            out = gh_ops.sync("acme", "lyra", str(src), workdir=wd)
            self.assertEqual(out["sha"], FAKE_SHA)
            self.assertTrue(out["changed"])
            self.assertTrue(out["pushed"])
            self.assertEqual((Path(wd) / "index.html").read_bytes(), b"<h1>hi</h1>")
            self.assertEqual((Path(wd) / "sub" / "app.js").read_bytes(), b"1")

    def test_sync_deletes_extraneous_by_default(self):
        self.fake.status_script = [""]
        src = self.make_source({"index.html": b"x"})
        with tempfile.TemporaryDirectory() as wd:
            Path(wd, ".git").mkdir(exist_ok=True)
            Path(wd, "old.txt").write_text("stale")
            gh_ops.sync("acme", "lyra", str(src), workdir=wd)
            self.assertFalse((Path(wd) / "old.txt").exists())

    def test_sync_no_delete_keeps_extraneous(self):
        self.fake.status_script = [""]
        src = self.make_source({"index.html": b"x"})
        with tempfile.TemporaryDirectory() as wd:
            Path(wd, ".git").mkdir(exist_ok=True)
            Path(wd, "old.txt").write_text("stale")
            gh_ops.sync("acme", "lyra", str(src), workdir=wd, delete=False)
            self.assertTrue((Path(wd) / "old.txt").exists())

    def test_sync_clean_skips_commit_and_push(self):
        self.fake.status_script = ["", ""]
        src = self.make_source({"index.html": b"x"})
        with tempfile.TemporaryDirectory() as wd:
            out = gh_ops.sync("acme", "lyra", str(src), workdir=wd)
            self.assertEqual(out["sha"], FAKE_SHA)
            self.assertFalse(out["changed"])
            self.assertFalse(out["pushed"])
            self.assertFalse(
                any("commit" in c for c, _ in self.fake.calls),
                "commit must not run when clean",
            )

    def test_sync_dry_run_never_pushes(self):
        # Empty workdir takes the clone path: single post-add status call
        # uses the default (dirty) output, so no script is needed.
        src = self.make_source({"index.html": b"x"})
        with tempfile.TemporaryDirectory() as wd:
            out = gh_ops.sync("acme", "lyra", str(src), workdir=wd, dry_run=True)
            self.assertTrue(out["changed"])
            self.assertFalse(out["pushed"])
            self.assertFalse(any("push" in c for c, _ in self.fake.calls))

    def test_sync_empty_folder_raises(self):
        with tempfile.TemporaryDirectory() as empty, tempfile.TemporaryDirectory() as wd:
            with self.assertRaises(core.CliError):
                gh_ops.sync("acme", "lyra", empty, workdir=wd)
        self.assertFalse(any(c[0] == "git" and "clone" in c for c, _ in self.fake.calls))

    def test_sync_missing_folder_raises(self):
        with self.assertRaises(core.CliError):
            gh_ops.sync("acme", "lyra", "/no/such/dir-xyz", workdir=tempfile.mkdtemp())

    def test_sync_dirty_workdir_refused(self):
        self.fake.status_script = [" M dirty.txt\n"]
        src = self.make_source({"index.html": b"x"})
        with tempfile.TemporaryDirectory() as wd:
            Path(wd, ".git").mkdir(exist_ok=True)
            with self.assertRaises(core.CliError):
                gh_ops.sync("acme", "lyra", str(src), workdir=wd)

    def test_sync_clones_shallow_to_temp_by_default(self):
        src = self.make_source({"index.html": b"x"})
        out = gh_ops.sync("acme", "lyra", str(src), branch="main")
        self.assertEqual(out["sha"], FAKE_SHA)
        clones = [c for c, _ in self.fake.calls if c[:2] == ["git", "clone"]]
        self.assertEqual(len(clones), 1)
        self.assertIn("--depth", clones[0])
        self.assertIn("main", clones[0])
        for c, _ in self.fake.calls:
            self.assertNotIn("--force", c)


class TestEnsureRepo(GhOpsTestBase):
    def test_exists_reports_not_created(self):
        self.fake.view_ok = True
        self.assertEqual(
            gh_ops.ensure_repo("acme", "lyra"), {"created": False}
        )
        self.assertFalse(any(c[1:3] == ["repo", "create"] for c, _ in self.fake.calls))

    def test_missing_creates_private_by_default(self):
        self.fake.view_ok = False
        self.assertEqual(gh_ops.ensure_repo("acme", "new"), {"created": True})
        creates = [c for c, _ in self.fake.calls if c[1:3] == ["repo", "create"]]
        self.assertEqual(len(creates), 1)
        self.assertIn("--private", creates[0])

    def test_public_flag_passes_through(self):
        self.fake.view_ok = False
        gh_ops.ensure_repo("acme", "new", public=True)
        creates = [c for c, _ in self.fake.calls if c[1:3] == ["repo", "create"]]
        self.assertIn("--public", creates[0])


class TestListRepos(GhOpsTestBase):
    def test_list_returns_parsed_dicts(self):
        repos = gh_ops.list_repos("acme")
        self.assertIsInstance(repos, list)
        self.assertEqual(repos[0]["name"], "lyra")
        self.assertIn("isPrivate", repos[0])


class TestGhCliWiring(unittest.TestCase):
    def _run(self, argv, patch_target, result):
        real = getattr(gh_ops, patch_target)
        setattr(gh_ops, patch_target, lambda *a, **k: result)
        buf = io.StringIO()
        try:
            with redirect_stdout(buf), redirect_stderr(io.StringIO()):
                code = cli.main(argv)
        finally:
            setattr(gh_ops, patch_target, real)
        return code, buf.getvalue()

    def test_gh_sync_wired(self):
        code, out = self._run(
            ["gh-sync", "--org", "acme", "--repo", "lyra", "--folder", "."],
            "sync",
            {"sha": FAKE_SHA, "changed": True, "pushed": True},
        )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["sha"], FAKE_SHA)

    def test_ensure_repo_wired(self):
        code, out = self._run(
            ["ensure-repo", "--org", "acme", "--repo", "lyra"],
            "ensure_repo",
            {"created": False},
        )
        self.assertEqual(code, 0)
        self.assertIn("created", json.loads(out))

    def test_gh_list_wired(self):
        code, out = self._run(
            ["gh-list", "--org", "acme"], "list_repos", [{"name": "lyra"}]
        )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)[0]["name"], "lyra")

    def test_cli_error_exits_nonzero(self):
        def boom(*a, **k):
            raise core.CliError("gh: exited 1: nope")

        real = gh_ops.sync
        gh_ops.sync = boom
        buf = io.StringIO()
        try:
            with redirect_stdout(buf), redirect_stderr(io.StringIO()):
                code = cli.main(
                    ["gh-sync", "--org", "a", "--repo", "r", "--folder", "."]
                )
        finally:
            gh_ops.sync = real
        self.assertEqual(code, 1)


@unittest.skipUnless(shutil.which("git"), "needs git binary")
class TestRealGitWorkdir(unittest.TestCase):
    """Exercise sync against a real local git checkout (dry_run: no push)."""

    def setUp(self):
        self._src_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._src_tmp.cleanup)
        self._wd_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._wd_tmp.cleanup)
        self.src = Path(self._src_tmp.name)
        self.wd = Path(self._wd_tmp.name)
        (self.src / "index.html").write_bytes(b"<h1>real</h1>")
        core.run(["git", "init", "-b", "main", str(self.wd)])
        core.run(["git", "-C", str(self.wd), "config", "user.email", "t@x.y"])
        core.run(["git", "-C", str(self.wd), "config", "user.name", "t"])
        core.run(["git", "-C", str(self.wd), "commit", "--allow-empty", "-m", "init"])

    def test_dry_run_copies_and_reports_changed(self):
        out = gh_ops.sync("acme", "lyra", str(self.src), workdir=str(self.wd), dry_run=True)
        self.assertTrue(out["changed"])
        self.assertFalse(out["pushed"])
        self.assertEqual((self.wd / "index.html").read_bytes(), b"<h1>real</h1>")
        self.assertTrue(out["sha"], "dry-run on a committed repo reports HEAD")

    def test_second_sync_clean_after_commit(self):
        gh_ops.sync("acme", "lyra", str(self.src), workdir=str(self.wd), dry_run=True)
        core.run(["git", "-C", str(self.wd), "add", "-A"])
        core.run(["git", "-C", str(self.wd), "commit", "-m", "data"])
        head = core.run(["git", "-C", str(self.wd), "rev-parse", "HEAD"]).strip()
        out = gh_ops.sync("acme", "lyra", str(self.src), workdir=str(self.wd), dry_run=True)
        self.assertEqual(out, {"sha": head, "changed": False, "pushed": False})

    def test_real_delete_of_tracked_file(self):
        (self.wd / "stale.txt").write_text("stale")
        core.run(["git", "-C", str(self.wd), "add", "-A"])
        core.run(["git", "-C", str(self.wd), "commit", "-m", "stale"])
        gh_ops.sync("acme", "lyra", str(self.src), workdir=str(self.wd), dry_run=True)
        self.assertFalse((self.wd / "stale.txt").exists())


@unittest.skipUnless(
    os.environ.get("STARFIELD_LIVE") == "1", "live: needs STARFIELD_LIVE=1"
)
class TestLive(unittest.TestCase):
    def test_live_sync_resolves_sha(self):
        slug = os.environ.get("STARFIELD_LIVE_REPO", "")
        self.assertIn("/", slug, "set STARFIELD_LIVE_REPO=org/repo")
        org, repo = slug.split("/", 1)
        tmp, src = make_source({"live.txt": b"starfield-live"})
        try:
            out = gh_ops.sync(
                org, repo, str(src), branch="starfield-live-test", timeout=300
            )
        finally:
            tmp.cleanup()
        self.assertTrue(out["sha"])
        resolved = core.run(
            ["gh", "api", f"repos/{slug}/commits/{out['sha']}", "--jq", ".sha"],
            timeout=60,
        )
        self.assertIn(out["sha"][:8], resolved)


if __name__ == "__main__":
    unittest.main()
