"""GCS operations tests (Task 4). Stdlib unittest only.

`core.run` is monkeypatched with a fake emulating `gcloud storage`
side effects; the public-GET verification HTTP layer (`gcs_ops._http_get`)
is monkeypatched with a fake serving canned headers/bodies. The real
`gcloud` CLI and network are never touched. Live tests run only with
STARFIELD_LIVE=1.
"""

import gzip
import hashlib
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from starfield import cli, core


class FakeRun:
    """Callable emulating `gcloud storage ...` via canned responses."""

    def __init__(self):
        self.calls = []
        self.bucket_exists = True
        # object URL (gs://bucket/name) -> generation string
        self.object_generations = {}
        self.buckets_json = json.dumps(
            [{"name": "demo-bucket", "location": "US-CENTRAL1",
              "storageClass": "STANDARD"}]
        )
        self.objects_json = json.dumps(
            [{"name": "index.html", "size": "11",
              "updated": "2026-10-12T00:00:00Z"}]
        )

    def __call__(self, cmd, timeout=300):
        self.calls.append((list(cmd), timeout))
        assert cmd[0] == "gcloud", f"unexpected binary: {cmd[0]}"
        sub = cmd[2:4]
        if sub == ["buckets", "describe"]:
            if self.bucket_exists:
                bucket = cmd[4].removeprefix("gs://")
                return json.dumps({"name": bucket, "location": "US-CENTRAL1"})
            raise core.CliError(
                "gcloud.storage.buckets.describe: 404 bucket does not exist"
            )
        if sub == ["objects", "describe"]:
            url = cmd[4]
            if url in self.object_generations:
                return json.dumps(
                    {"name": url.split("/", 3)[-1],
                     "generation": self.object_generations[url]}
                )
            raise core.CliError(
                "gcloud.storage.objects.describe: 404 object not found"
            )
        if sub == ["buckets", "create"]:
            return json.dumps({"name": cmd[4]})
        if sub == ["buckets", "list"]:
            return self.buckets_json
        if sub == ["objects", "list"]:
            return self.objects_json
        if cmd[1] == "storage" and cmd[2] == "cp":
            return ""
        if sub == ["buckets", "add-iam-policy-binding"]:
            return ""
        return ""


class FakeHttp:
    """Callable emulating the public-GET verification layer.

    `mapping` maps object path (bucket + "/" + name) -> (headers, body).
    """

    def __init__(self, mapping=None):
        self.mapping = mapping or {}
        self.urls = []

    def __call__(self, url, timeout=60):
        self.urls.append(url)
        path = url.split("storage.googleapis.com/", 1)[-1]
        if path not in self.mapping:
            return 404, {}, b"not found"
        headers, body = self.mapping[path]
        return 200, headers, body


class GcsOpsTestBase(unittest.TestCase):
    def setUp(self):
        import starfield.gcs_ops as gcs_ops

        self.gcs_ops = gcs_ops
        self._real_run = core.run
        self.fake = FakeRun()
        core.run = self.fake
        self._real_http = gcs_ops._http_get
        self.http = FakeHttp()
        gcs_ops._http_get = self.http
        self.addCleanup(self._restore)

    def _restore(self):
        core.run = self._real_run
        self.gcs_ops._http_get = self._real_http

    def make_source(self, files):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        for rel, data in files.items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
        return root

    def prime_http(self, folder, bucket="bkt", prefix="",
                   cache_override=None):
        """Stage `folder` for real and serve correct public-GET responses."""
        staged = self.gcs_ops.stage(str(folder))
        mapping = {}
        for item in staged:
            obj = (prefix.strip("/") + "/" if prefix.strip("/") else "") + item.name
            cc = cache_override if cache_override is not None else item.cache_control
            headers = {
                "content-type": item.content_type,
                "cache-control": cc,
                "content-disposition": "inline",
                "content-length": str(len(item.data)),
            }
            if item.content_encoding:
                headers["content-encoding"] = item.content_encoding
            mapping[f"{bucket}/{obj}"] = (headers, item.data)
        self.http.mapping = mapping
        return staged


class TestMimeTable(GcsOpsTestBase):
    def test_svg_and_wasm(self):
        self.assertEqual(self.gcs_ops.content_type("icon.svg"), "image/svg+xml")
        self.assertEqual(self.gcs_ops.content_type("mod.wasm"), "application/wasm")

    def test_common_types(self):
        cases = {
            "i.html": "text/html",
            "a.js": "text/javascript",
            "b.mjs": "text/javascript",
            "c.css": "text/css",
            "d.json": "application/json",
            "e.map": "application/json",
        }
        for path, want in cases.items():
            with self.subTest(path=path):
                self.assertEqual(self.gcs_ops.content_type(path), want)

    def test_unknown_defaults_octet_stream(self):
        self.assertEqual(
            self.gcs_ops.content_type("blob.unknownext"), "application/octet-stream"
        )

    def test_extension_case_insensitive(self):
        self.assertEqual(self.gcs_ops.content_type("ICON.SVG"), "image/svg+xml")


class TestCacheControl(GcsOpsTestBase):
    def test_entry_docs_no_cache(self):
        for name in ("index.html", "index.svg", "sw.js", "bootstrap-sw.js"):
            with self.subTest(name=name):
                self.assertEqual(
                    self.gcs_ops.cache_control(name), "no-cache,no-transform"
                )

    def test_hashed_assets_immutable(self):
        self.assertEqual(
            self.gcs_ops.cache_control("app-a1b2c3d4e5.js"),
            "public,max-age=31536000,immutable,no-transform",
        )

    def test_default_daily(self):
        self.assertEqual(
            self.gcs_ops.cache_control("logo.png"),
            "public,max-age=86400,no-transform",
        )


class TestStage(GcsOpsTestBase):
    def test_gzip_round_trip_preserves_bytes(self):
        src = self.make_source(
            {"index.html": b"<h1>hi</h1>", "app.js": b"console.log(1);"}
        )
        staged = {s.name: s for s in self.gcs_ops.stage(str(src))}
        for name, raw in (("index.html", b"<h1>hi</h1>"),
                          ("app.js", b"console.log(1);")):
            item = staged[name]
            self.assertEqual(item.content_encoding, "gzip")
            self.assertEqual(gzip.decompress(item.data), raw)

    def test_gzip_header_deterministic(self):
        src = self.make_source({"index.html": b"x" * 100})
        (item,) = self.gcs_ops.stage(str(src))
        self.assertEqual(item.data[:2], b"\x1f\x8b")
        self.assertEqual(item.data[4:8], b"\x00\x00\x00\x00")  # mtime=0
        self.assertEqual(item.data[8], 2)  # XFL: max compression (level 9)

    def test_filenames_preserved_and_selective_encoding(self):
        src = self.make_source({"app.js": b"1", "logo.png": b"\x89PNGraw"})
        staged = {s.name: s for s in self.gcs_ops.stage(str(src))}
        self.assertIn("app.js", staged)
        self.assertIn("logo.png", staged)
        self.assertNotIn("app.js.gz", staged)
        self.assertEqual(staged["app.js"].content_encoding, "gzip")
        self.assertIsNone(staged["logo.png"].content_encoding)
        self.assertEqual(staged["logo.png"].data, b"\x89PNGraw")

    def test_staged_carries_type_and_cache(self):
        src = self.make_source({"index.html": b"x"})
        (item,) = self.gcs_ops.stage(str(src))
        self.assertEqual(item.content_type, "text/html")
        self.assertEqual(item.cache_control, "no-cache,no-transform")


    def test_stage_skips_hidden_files_and_dirs(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, ".build-id").write_text("x")
            Path(d, ".hidden-dir").mkdir()
            Path(d, ".hidden-dir", "junk.js").write_text("y")
            Path(d, "index.html").write_text("<html></html>")
            names = [s.name for s in self.gcs_ops.stage(d)]
            self.assertEqual(names, ["index.html"])

class TestUpload(GcsOpsTestBase):
    FILES = {
        "static/app-a1b2c3d4.js": b"dep",
        "sw.js": b"worker",
        "index.html": b"entry",
    }

    def test_missing_bucket_raises_before_uploading(self):
        self.fake.bucket_exists = False
        src = self.make_source({"index.html": b"x"})
        with self.assertRaises(core.CliError):
            self.gcs_ops.upload("nope", str(src))
        self.assertFalse(
            any(c[1] == "storage" and c[2] == "cp" for c, _ in self.fake.calls)
        )

    def test_empty_folder_raises(self):
        with tempfile.TemporaryDirectory() as empty:
            with self.assertRaises(core.CliError):
                self.gcs_ops.upload("bkt", empty)
        self.assertEqual(self.fake.calls, [])

    def test_upload_order_dependencies_worker_entry(self):
        src = self.make_source(dict(self.FILES))
        self.prime_http(src)
        out = self.gcs_ops.upload("bkt", str(src))
        cps = [c for c, _ in self.fake.calls if c[2] == "cp"]
        self.assertEqual(len(cps), 3)
        order = [
            next(a for a in c if a.startswith("gs://bkt/")).split("gs://bkt/")[-1]
            for c in cps
        ]
        self.assertEqual(
            order, ["static/app-a1b2c3d4.js", "sw.js", "index.html"]
        )
        self.assertEqual(len(out["files"]), 3)

    def test_upload_counts_and_files(self):
        src = self.make_source({"index.html": b"<h1>hi</h1>"})
        staged = self.prime_http(src)
        out = self.gcs_ops.upload("bkt", str(src))
        self.assertEqual(out["raw_bytes"], len(b"<h1>hi</h1>"))
        self.assertEqual(out["stored_bytes"], len(staged[0].data))
        self.assertEqual(out["files"][0]["name"], "index.html")
        self.assertIn("content_type", out["files"][0])

    def test_upload_uses_generation_match_and_inline(self):
        src = self.make_source({"index.html": b"x"})
        self.prime_http(src)
        self.gcs_ops.upload("bkt", str(src))
        cps = [c for c, _ in self.fake.calls if c[2] == "cp"]
        self.assertEqual(len(cps), 1)
        cmd = cps[0]
        self.assertIn("--if-generation-match=0", cmd)
        self.assertIn("--content-disposition=inline", cmd)
        self.assertIn("--content-type=text/html", cmd)
        self.assertIn("--content-encoding=gzip", cmd)
        self.assertIn("--cache-control=no-cache,no-transform", cmd)

    def test_global_cache_control_override(self):
        src = self.make_source({"index.html": b"x"})
        self.prime_http(src, cache_override="public,max-age=60")
        self.gcs_ops.upload("bkt", str(src), cache_control="public,max-age=60")
        cps = [c for c, _ in self.fake.calls if c[2] == "cp"]
        self.assertIn("--cache-control=public,max-age=60", cps[0])

    def test_create_only_refuses_existing(self):
        src = self.make_source({"index.html": b"x"})
        self.fake.object_generations = {"gs://bkt/index.html": "123"}
        with self.assertRaises(core.CliError):
            self.gcs_ops.upload("bkt", str(src), create_only=True)
        self.assertFalse(any(c[2] == "cp" for c, _ in self.fake.calls))

    def test_dry_run_makes_no_calls(self):
        src = self.make_source({"index.html": b"x"})
        out = self.gcs_ops.upload("bkt", str(src), dry_run=True)
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(self.http.urls, [])
        self.assertEqual(len(out["files"]), 1)

    def test_verify_mismatch_raises(self):
        src = self.make_source({"index.html": b"real-bytes"})
        self.prime_http(src)
        self.http.mapping["bkt/index.html"] = (
            {"content-type": "text/html", "content-length": "5",
             "content-disposition": "inline",
             "cache-control": "no-cache,no-transform",
             "content-encoding": "gzip"},
            b"wrong",
        )
        with self.assertRaises(core.CliError):
            self.gcs_ops.upload("bkt", str(src))

    def test_md5_and_gunzip_verified(self):
        src = self.make_source({"app.js": b"codes codes"})
        staged = self.prime_http(src)
        out = self.gcs_ops.upload("bkt", str(src))
        want_md5 = hashlib.md5(staged[0].data).hexdigest()
        self.assertEqual(out["files"][0]["md5"], want_md5)


class TestEnsureBucket(GcsOpsTestBase):
    def test_exists_reports_not_created(self):
        self.assertEqual(
            self.gcs_ops.ensure_bucket("bkt"), {"created": False}
        )
        self.assertFalse(
            any("create" in c for c, _ in self.fake.calls if "buckets" in c)
        )

    def test_missing_creates_regional_standard(self):
        self.fake.bucket_exists = False
        self.assertEqual(
            self.gcs_ops.ensure_bucket("new"), {"created": True}
        )
        creates = [c for c, _ in self.fake.calls if c[2:4] == ["buckets", "create"]]
        self.assertEqual(len(creates), 1)
        cmd = creates[0]
        self.assertIn("gs://new", cmd)
        self.assertIn("--location=us-central1", cmd)
        self.assertIn("--default-storage-class=Standard", cmd)
        self.assertIn("--uniform-bucket-level-access", cmd)
        self.assertIn("--soft-delete-duration=0", cmd)

    def test_public_flag_adds_viewer_binding_only_then(self):
        self.fake.bucket_exists = False
        self.gcs_ops.ensure_bucket("new", public=True)
        bindings = [
            c for c, _ in self.fake.calls
            if c[2:4] == ["buckets", "add-iam-policy-binding"]
        ]
        self.assertEqual(len(bindings), 1)
        self.assertIn("--member=allUsers", bindings[0])
        self.assertIn("--role=roles/storage.objectViewer", bindings[0])

    def test_no_binding_without_public(self):
        self.fake.bucket_exists = False
        self.gcs_ops.ensure_bucket("new")
        self.assertFalse(
            any("add-iam-policy-binding" in c for c, _ in self.fake.calls)
        )

    def test_location_flag_passes_through(self):
        self.fake.bucket_exists = False
        self.gcs_ops.ensure_bucket("new", location="eu-west1")
        creates = [c for c, _ in self.fake.calls if c[2:4] == ["buckets", "create"]]
        self.assertIn("--location=eu-west1", creates[0])


class TestListBuckets(GcsOpsTestBase):
    def test_list_returns_parsed_dicts(self):
        buckets = self.gcs_ops.list_buckets()
        self.assertIsInstance(buckets, list)
        self.assertEqual(buckets[0]["name"], "demo-bucket")

    def test_list_bad_json_raises(self):
        self.fake.buckets_json = "not json{{{"
        with self.assertRaises(core.CliError):
            self.gcs_ops.list_buckets()

    def test_list_objects(self):
        objs = self.gcs_ops.list_objects("bkt")
        self.assertEqual(objs[0]["name"], "index.html")


class TestGcsCliWiring(unittest.TestCase):
    def _run(self, argv, patch_target, result):
        from starfield import gcs_ops

        real = getattr(gcs_ops, patch_target)
        setattr(gcs_ops, patch_target, lambda *a, **k: result)
        buf = io.StringIO()
        try:
            with redirect_stdout(buf), redirect_stderr(io.StringIO()):
                code = cli.main(argv)
        finally:
            setattr(gcs_ops, patch_target, real)
        return code, buf.getvalue()

    def test_gcs_upload_wired(self):
        code, out = self._run(
            ["gcs-upload", "--bucket", "b", "--folder", "."],
            "upload",
            {"raw_bytes": 1, "stored_bytes": 1, "files": []},
        )
        self.assertEqual(code, 0)
        self.assertIn("raw_bytes", json.loads(out))

    def test_ensure_bucket_wired(self):
        code, out = self._run(
            ["ensure-bucket", "--bucket", "b"], "ensure_bucket", {"created": False}
        )
        self.assertEqual(code, 0)
        self.assertIn("created", json.loads(out))

    def test_gcs_list_buckets_wired(self):
        code, out = self._run(
            ["gcs-list"], "list_buckets", [{"name": "b"}]
        )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)[0]["name"], "b")

    def test_gcs_list_objects_wired(self):
        code, out = self._run(
            ["gcs-list", "--bucket", "b"], "list_objects", [{"name": "o"}]
        )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)[0]["name"], "o")

    def test_cli_error_exits_nonzero(self):
        from starfield import gcs_ops

        def boom(*a, **k):
            raise core.CliError("gcloud: exited 1: nope")

        real = gcs_ops.upload
        gcs_ops.upload = boom
        buf = io.StringIO()
        try:
            with redirect_stdout(buf), redirect_stderr(io.StringIO()):
                code = cli.main(["gcs-upload", "--bucket", "b", "--folder", "."])
        finally:
            gcs_ops.upload = real
        self.assertEqual(code, 1)


@unittest.skipUnless(
    os.environ.get("STARFIELD_LIVE") == "1", "live: needs STARFIELD_LIVE=1"
)
class TestLive(unittest.TestCase):
    def test_live_upload_round_trip(self):
        from starfield import gcs_ops

        bucket = os.environ.get("STARFIELD_LIVE_BUCKET", "")
        self.assertTrue(bucket, "set STARFIELD_LIVE_BUCKET")
        tmp = tempfile.TemporaryDirectory()
        try:
            root = Path(tmp.name)
            (root / "index.html").write_bytes(b"<h1>starfield-live</h1>")
            prefix = "starfield-live-test"
            out = gcs_ops.upload(bucket, str(root), prefix=prefix)
        finally:
            tmp.cleanup()
        self.assertTrue(out["files"])
        for entry in out["files"]:
            url = f"https://storage.googleapis.com/{bucket}/{entry['name']}"
            import urllib.request

            with urllib.request.urlopen(url, timeout=60) as resp:
                body = resp.read()
            raw = gzip.decompress(body) if body[:2] == b"\x1f\x8b" else body
            self.assertIn(b"starfield-live", raw)


if __name__ == "__main__":
    unittest.main()
