# Starfield Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the Starfield deploy package: one Python core plus CLI, HTTP, and MCP surfaces for gh/GCS deploys.

**Architecture:** Single core (`gh_ops`, `gcs_ops`, `verify`, `links`) shells out to `gh`/`gcloud` CLIs; `cli.py`, `http_server.py`, `mcp_server.py` are thin adapters over identical function signatures. Stdlib only.

**Tech Stack:** Python 3.11+ stdlib (argparse, http.server, json, unittest, gzip, subprocess, urllib).

**Spec:** `starfield/docs/2026-10-12-starfield-design.md`

## Global Constraints

- Stdlib only; no third-party dependencies.
- Auth stays with the CLIs (`gh auth login`, `gcloud init`); never handle tokens.
- gh-sync never force-pushes.
- Bucket public access only with explicit `--public`.
- Tests run with `python -m unittest`; live tests skip unless `STARFIELD_LIVE=1`.
- Filenames on GCS preserved exactly; gzip in place-names with `Content-Encoding: gzip`.

## Review Focus

- Empty folder deploy: refuses with a clear error instead of pushing an empty tree (gh-sync) or uploading nothing (gcs-upload) — pin in Task 3/4 tests.
- Missing `gh`/`gcloud` binary: structured error naming the missing CLI, not a traceback — pin in Task 1 tests.
- `gcs-upload` to a nonexistent bucket without create: fails before uploading anything — pin in Task 4 tests.
- MCP `--read-only` with a mutating call: tool returns an error result, exit 0 on the server — pin in Task 7 tests.
- HTTP unknown path/method: 404 JSON envelope, never HTML stack — pin in Task 6 tests.

---

### Task 1: Scaffold + result types + CLI skeleton

**Files:**
- Create: `starfield/pyproject.toml`, `starfield/src/starfield/__init__.py`, `starfield/src/starfield/__main__.py`, `starfield/src/starfield/cli.py`, `starfield/src/starfield/core.py`
- Test: `starfield/tests/test_cli.py`

**Interfaces:**
- Consumes: nothing (first task).
- Produces: `core.Result(ok, data, error)`, `core.CliError(message, exit_code)`, `core.run(cmd, timeout) -> str` (raises `CliError` on missing binary / non-zero exit, stderr tail attached); `cli.main(argv) -> int` with `starfield --help` and per-command `--help` working.

- [ ] **Step 1: Write the failing test**

```python
def test_help_lists_commands(self):
    self.assertIn("gh-sync", run_cli(["--help"]))
    self.assertIn("gcs-upload", run_cli(["--help"]))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd starfield && python -m unittest tests.test_cli -v`
Expected: FAIL (module/missing names)

- [ ] **Step 3: Implement `core.Result`, `core.CliError`, `core.run`, and `cli.main` with argparse subcommands (stubs returning "not implemented" `Result`)**

- [ ] **Step 4: Run test to verify it passes**

Run: `cd starfield && python -m unittest tests.test_cli -v`
Expected: PASS; plus add the missing-binary test (`core.run` on a bogus command raises `CliError` naming it) and watch it pass.

- [ ] **Step 5: Commit**

```bash
git add -A 2>/dev/null || true
```

(Note: no git repo exists yet for starfield; if the user wants one, `git init` here. Otherwise skip committing.)

### Task 2: Mirror-links module

**Files:**
- Create: `starfield/src/starfield/links.py`
- Test: `starfield/tests/test_links.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `links.jsdelivr_url(host, org, repo, ref, file)`, `links.statically_url(...)`, `links.githack_url(host, ...)`, `links.raw_github_url(...)`, `links.esm_url(host, ...)`; `links.mirror_urls(org, repo, ref, file, format="plain"|"md"|"json") -> str`; `links.ALIVE_HOSTS`, `links.DEAD_HOSTS` (dict host->reason) codifying the 2026-10-12 probe.

- [ ] **Step 1: Write failing tests** for all five URL builders with the exact lyra `index.svg` values from the spec, plus `mirror_urls` plain/md/json shape for one host.

- [ ] **Step 2: Run to verify they fail** (`python -m unittest tests.test_links -v`).

- [ ] **Step 3: Implement the builders + table.**

- [ ] **Step 4: Run to verify pass.**

### Task 3: gh operations

**Files:**
- Create: `starfield/src/starfield/gh_ops.py`
- Test: `starfield/tests/test_gh_ops.py`
- Modify: `starfield/src/starfield/cli.py` (wire `gh-sync`, `ensure-repo`, `gh-list`)

**Interfaces:**
- Consumes: `core.run`, `core.Result` from Task 1.
- Produces: `gh_ops.sync(org, repo, folder, branch="main", message=None, delete=True, dry_run=False, workdir=None, timeout=300) -> dict{sha, changed, pushed}`; `gh_ops.ensure_repo(org, repo, public=False, timeout=60) -> dict{created}`; `gh_ops.list_repos(org, timeout=60) -> list[dict]`; empty-folder input raises `CliError`.

- [ ] **Step 1: Write failing tests** with a fake `core.run` (monkeypatched): sync copies files, deletes extraneous tracked file by default, keeps it with `delete=False`, skips commit when clean, returns SHA; empty folder raises; `ensure_repo` creates vs exists paths.

- [ ] **Step 2: Run to verify they fail.**

- [ ] **Step 3: Implement `gh_ops` (clone-shallow-to-temp or reuse clean `--workdir`, copy tree, `git add -A`, `git commit` iff `git status --porcelain` non-empty, push) + wire the three CLI subcommands.**

- [ ] **Step 4: Run full unit suite** (`python -m unittest -v`); expected: all PASS.

- [ ] **Step 5: Live check (only with `STARFIELD_LIVE=1`)**: sync a fixture folder to a scratch repo, assert returned SHA resolves via `gh api`.

### Task 4: GCS operations

**Files:**
- Create: `starfield/src/starfield/gcs_ops.py`
- Test: `starfield/tests/test_gcs_ops.py`
- Modify: `starfield/src/starfield/cli.py` (wire `gcs-upload`, `ensure-bucket`, `gcs-list`)

**Interfaces:**
- Consumes: `core.run`, `core.Result` from Task 1.
- Produces: `gcs_ops.content_type(path)`, `gcs_ops.cache_control(name)`, `gcs_ops.stage(folder) -> list[Staged(...)]` (gzip-9 in place-names); `gcs_ops.upload(bucket, folder, prefix="", cache_control=None, create_only=False, dry_run=False, timeout=300) -> dict{raw_bytes, stored_bytes, files[]}` with dependency/worker/entry ordering and per-object public-GET verification; `gcs_ops.ensure_bucket(bucket, location="us-central1", public=False, timeout=60)`; `gcs_ops.list_buckets()` / `list_objects(bucket)`; upload to missing bucket fails before any upload.

- [ ] **Step 1: Write failing tests**: MIME table spot-checks (`.svg`→`image/svg+xml`, `.wasm`→`application/wasm`), cache rules (entry `no-cache,no-transform`, hashed `immutable`, default 86400), gzip round-trip preserves bytes, missing-bucket upload raises before uploading (fake `run`).

- [ ] **Step 2: Run to verify they fail.**

- [ ] **Step 3: Implement staging + upload + ensure + list** (generation-match preconditions, `Content-Disposition: inline`, per-object verify via `urllib`) + wire CLI.

- [ ] **Step 4: Run full unit suite**; expected: all PASS.

- [ ] **Step 5: Live check (only with `STARFIELD_LIVE=1`)**: upload fixture to scratch bucket, assert public GET bytes match.

### Task 5: verify module

**Files:**
- Create: `starfield/src/starfield/verify.py`
- Test: `starfield/tests/test_verify.py`
- Modify: `starfield/src/starfield/cli.py` (wire `verify`)

**Interfaces:**
- Consumes: nothing new.
- Produces: `verify.check(url, sha256=None, mime=None, min_bytes=1, timeout=60) -> dict{ok, status, bytes, content_type}` using `urllib` with gzip; mismatch details in `error`.

- [ ] **Step 1: Write failing tests** against a local `http.server` fixture (200+match PASS; wrong hash FAIL; 404 FAIL with status recorded).

- [ ] **Step 2: Run to verify they fail.**

- [ ] **Step 3: Implement + wire CLI.**

- [ ] **Step 4: Run full unit suite**; expected: all PASS.

### Task 6: HTTP server

**Files:**
- Create: `starfield/src/starfield/http_server.py`
- Test: `starfield/tests/test_http.py`
- Modify: `starfield/src/starfield/cli.py` (wire `serve`)

**Interfaces:**
- Consumes: Task 3/4/5 functions + `links.mirror_urls`.
- Produces: `http_server.serve(port, allow_create=False, timeout=300, concurrency=8)`; routes `POST /v1/{gh-sync,gcs-upload,ensure-repo,ensure-bucket,verify,links}`, `GET /v1/{gcs-list,gh-list}`, `GET /healthz`; envelope `{"ok": bool, ...}`; unknown routes 404 JSON.

- [ ] **Step 1: Write failing tests** (start server on ephemeral port in-thread via `urllib`: `/healthz` 200, `POST /v1/links` returns md text, unknown path 404 JSON, `ensure-bucket` refused without allow-listed flag).

- [ ] **Step 2: Run to verify they fail.**

- [ ] **Step 3: Implement stdlib handler + wire `serve`.**

- [ ] **Step 4: Run full unit suite**; expected: all PASS.

### Task 7: MCP server

**Files:**
- Create: `starfield/src/starfield/mcp_server.py`
- Test: `starfield/tests/test_mcp.py`
- Modify: `starfield/src/starfield/cli.py` (wire `mcp`)

**Interfaces:**
- Consumes: Task 3/4/5 + `links`.
- Produces: `mcp_server.run(org=None, bucket=None, allow_create=False, read_only=False, timeout=300, concurrency=8)` speaking JSON-RPC over stdio (`initialize`, `tools/list`, `tools/call`); tools `gh_sync`, `gcs_upload`, `ensure_repo`, `ensure_bucket`, `gcs_list`, `gh_list`, `verify_deploy`, `list_mirrors`; mutating tools return error results under `--read-only`; `ensure_*` refuse without `--allow-create`.

- [ ] **Step 1: Write failing tests** driving the server over pipes in-process: `tools/list` names, `list_mirrors` content, read-only refusal, allow-create gate.

- [ ] **Step 2: Run to verify they fail.**

- [ ] **Step 3: Implement + wire `mcp`.**

- [ ] **Step 4: Run FULL unit suite** (`python -m unittest -v`); expected: all PASS.

### Task 8: README documentation

**Files:**
- Create: `starfield/README.md`

**Interfaces:**
- Consumes: final CLI `--help` text, tool list, endpoint list.
- Produces: install/auth, CLI reference, HTTP endpoints, MCP tools + client-config snippet, worked lyra/orion examples with the real pins.

- [ ] **Step 1: Write README** from the spec + actual `--help` output (no placeholders; every command/flag real).
- [ ] **Step 2: Verify** each documented command exists (`starfield <cmd> --help` exits 0) and each example URL shape matches `links` output.
