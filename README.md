# Starfield

Deploy tool: push folders to GitHub repos and/or GCS buckets, then verify
and list mirror links. Python 3.11+, stdlib only — no third-party
dependencies.

Three surfaces, one core:

- **CLI** — `starfield <command>` (10 subcommands, JSON output)
- **HTTP** — `starfield serve` (`POST /v1/*`, `GET /v1/*`, `GET /healthz`)
- **MCP** — `starfield mcp` (stdio JSON-RPC, 8 tools)

## Install

Requires Python 3.11 or newer. From this directory:

```sh
pip install .
# or, for development:
pip install -e .
```

This installs the `starfield` console script. Without installing, you can
also run it as a module (from this directory, with `src` on the path):

```sh
python -m starfield --help
```

Run the test suite with the standard library only:

```sh
python -m unittest
```

Live tests that shell out to real `gh`/`gcloud` run only with
`STARFIELD_LIVE=1` and are skipped by default.

## Auth

Starfield never handles tokens. Auth stays with the CLIs it shells out to:

```sh
gh auth login
gcloud init
```

`gh` must be authenticated for `gh-sync`, `ensure-repo`, and `gh-list`.
`gcloud` must be authenticated for `gcs-upload`, `ensure-bucket`, and
`gcs-list`. CLI errors are surfaced verbatim. `verify`, `links`, `serve`,
and `mcp` (list/verify/links tools) need no auth.

## CLI reference

Top level:

```sh
starfield --help          # list commands
starfield <command> --help  # flags for one command
```

Every mutating or deploying command prints a JSON object on stdout.
`verify` exits non-zero when the check fails.

### `gh-sync` — push a folder to `org/repo@branch`

```sh
starfield gh-sync --org ORG --repo REPO --folder DIR [--branch main]
                  [--message MSG] [--no-delete] [--dry-run]
                  [--workdir DIR] [--timeout 300]
```

- Clones shallow to a temp dir (or reuses `--workdir` when given), copies
  the folder contents over the checkout, `git add -A`, deletes tracked
  files absent from the folder unless `--no-delete`, commits (default
  message lists the file count and date) only when dirty, pushes, and
  returns the new SHA.
- Never force-pushes. `--dry-run` reports what would change without
  pushing. `--timeout` is seconds (default 300).

### `gcs-upload` — gzip-upload a folder to a bucket

```sh
starfield gcs-upload --bucket BUCKET --folder DIR [--prefix PFX]
                     [--cache-control CC] [--create-only] [--dry-run]
                     [--timeout 300]
```

- Gzip-9 (mtime=0) compresses `html/svg/js/mjs/css/json/wasm/map` files;
  filenames are preserved and `Content-Encoding: gzip` is set only on
  compressed objects.
- Correct `Content-Type` per extension, `Content-Disposition: inline`,
  entry docs (`index.html`, `index.svg`, `sw.js`, `bootstrap-sw.js`)
  `no-cache,no-transform`, hashed assets (`*-HASH.*`)
  `public,max-age=31536000,immutable,no-transform`, everything else
  `public,max-age=86400,no-transform`; `--cache-control` overrides all.
- Dependencies upload first, entry docs last, with
  `if-generation-match` preconditions; `--create-only` refuses to
  overwrite. Every object is verified after upload (public GET, headers,
  byte length, md5, gunzip-compare). `--timeout` is seconds
  (default 300).

### `ensure-repo` — create `org/repo` when missing

```sh
starfield ensure-repo --org ORG --repo REPO [--public | --private]
                      [--timeout 60]
```

Default visibility is private unless `--public` is given. Mirror links
only resolve for **public** repos, so mirror deployments must pass
`--public`. Reports whether
the repo already existed or was created.

### `ensure-bucket` — create a bucket when missing

```sh
starfield ensure-bucket --bucket BUCKET [--location us-central1] [--public]
                        [--timeout 60]
```

Regional (`us-central1` unless overridden), Standard class, uniform
bucket-level access. `allUsers:objectViewer` is granted ONLY with
`--public`.

### `gcs-list` — list buckets or objects

```sh
starfield gcs-list [--bucket BUCKET] [--timeout 60]
```

No `--bucket`: lists all buckets (location/class/bytes). With
`--bucket`: object summary (name/size/updated) for that bucket.

### `gh-list` — list repos in an org

```sh
starfield gh-list --org ORG [--timeout 60]
```

Lists repos (name/visibility/updated/pushed-at).

### `verify` — GET a URL and compare its sha256

```sh
starfield verify --url URL --sha256 HEX [--mime TYPE] [--min-bytes N]
```

GETs with gzip, asserts HTTP 200, optionally asserts a MIME prefix and a
minimum decoded byte count, and compares the sha256 of the decoded body
against `--sha256`. Prints a JSON pass/fail detail object; exits 1 on
failure.

### `links` — mirror URLs for `org/repo@ref`

```sh
starfield links --org ORG --repo REPO --ref REF [--file index.svg]
                [--format plain]
```

Renders one URL per alive mirror host (17 hosts, corrected 2026-10-13;
was 22 in the 2026-10-12 probe) for the
given file. `--format` is `plain` (one URL per line, the default), `md`
(one `- [host](url)` bullet per line), or `json` (array of
`{"host", "url"}` objects). `--check` fetches each URL first and drops
unreachable hosts (annotated as `# DEAD` lines, or `alive`/`detail`
fields in json). URL shapes per host family:

- jsDelivr-compat: `https://HOST/gh/ORG/REPO@REF/FILE`
- statically: `https://HOST/gh/ORG/REPO/REF/FILE`
- githack / raw.githubusercontent: `https://HOST/ORG/REPO/REF/FILE`
- esm.sh: `https://HOST/gh/ORG/REPO@REF/FILE`

### `serve` — run the HTTP server

```sh
starfield serve [--port 8765] [--allow-create] [--timeout 300]
                [--concurrency 8]
```

Without `--allow-create`, the `ensure-repo`/`ensure-bucket` routes answer
403. `--timeout` bounds network calls (seconds), `--concurrency` caps
parallel uploads.

### `mcp` — run the MCP stdio server

```sh
starfield mcp [--org O] [--bucket B] [--allow-create] [--read-only]
              [--timeout 300] [--concurrency 8]
```

- `--org`/`--bucket` inject defaults into tool calls that omit them.
- Without `--allow-create`, the `ensure_*` tools refuse (creation is
  opt-in; sync/upload are the deploy path and always allowed).
- `--read-only` disables every mutating tool; only list/verify/links run.

## HTTP endpoints

Start with `starfield serve --port 8765`. JSON in, JSON out:
`{"ok": true, ...}` or `{"ok": false, "error": "..."}`. Unknown routes
answer 404 JSON.

| Method | Route | Required JSON body / query |
| ------ | ----- | -------------------------- |
| `POST` | `/v1/gh-sync` | `org`, `repo`, `folder`; optional `branch` (default `"main"`), `message`, `delete` (default `true`), `dry_run`, `workdir`, `timeout` |
| `POST` | `/v1/gcs-upload` | `bucket`, `folder`; optional `prefix`, `cache_control`, `create_only`, `dry_run`, `timeout` |
| `POST` | `/v1/ensure-repo` | `org`, `repo`; optional `public`. Needs `--allow-create` or 403 |
| `POST` | `/v1/ensure-bucket` | `bucket`; optional `location` (default `"us-central1"`), `public`. Needs `--allow-create` or 403 |
| `POST` | `/v1/verify` | `url`; optional `sha256`, `mime`, `min_bytes` (default `1`) |
| `POST` | `/v1/links` | `org`, `repo`, `ref`; optional `file` (default `"index.svg"` — mirrors are unreliable for `.html`), `format` (`plain`/`md`/`json`, default `"plain"`), `check` (live-verify each URL) |
| `GET` | `/v1/gcs-list?bucket=` | no query: all buckets; `?bucket=B`: objects in `B`; optional `timeout` |
| `GET` | `/v1/gh-list?org=` | `org` required; optional `timeout` |
| `GET` | `/healthz` | — answers `{"ok": true}` |

Example (links needs no auth and touches no cloud CLIs):

```sh
starfield serve --port 8765 &
curl -s -X POST localhost:8765/v1/links \
  -H 'content-type: application/json' \
  -d '{"org":"acme","repo":"lyra","ref":"abc1234","file":"index.svg","format":"md"}'
```

## MCP tools

`starfield mcp` speaks stdio JSON-RPC: `initialize` answers the handshake,
`tools/list` returns the catalogue below, `tools/call` runs one tool and
returns an MCP content envelope. Malformed input yields JSON-RPC errors,
never a dropped connection.

| Tool | Required args | Notes |
| ---- | ------------- | ----- |
| `gh_sync` | `org`, `repo`, `folder` | optional `branch`, `message`, `delete`, `dry_run`, `workdir`, `timeout`; refused under `--read-only` |
| `gcs_upload` | `bucket`, `folder` | optional `prefix`, `cache_control`, `create_only`, `dry_run`, `timeout`; refused under `--read-only` |
| `ensure_repo` | `org`, `repo` | optional `public`, `timeout`; needs `--allow-create`; refused under `--read-only` |
| `ensure_bucket` | `bucket` | optional `location`, `public`, `timeout`; needs `--allow-create`; refused under `--read-only` |
| `gcs_list` | — | optional `bucket`, `timeout` |
| `gh_list` | `org` | optional `timeout` |
| `verify_deploy` | `url` | optional `sha256`, `mime`, `min_bytes`, `timeout` |
| `list_mirrors` | `org` (or server `--org` default), `repo`, `ref` | optional `file` (default `"index.svg"`), `format` (`plain`/`md`/`json`), `check` (live-verify) |

Client-config snippet (stdio; substitute your org/bucket and flags —
every flag below is a real `starfield mcp` flag):

```json
{
  "mcpServers": {
    "starfield": {
      "command": "starfield",
      "args": ["mcp", "--org", "acme", "--bucket", "acme-loaders", "--allow-create"]
    }
  }
}
```

Add `"--read-only"` to the args for a list/verify/links-only server, or
drop `"--allow-create"` to keep `ensure_*` refused while sync/upload stay
available.

## Worked examples

> All values below are **examples** — no live lyra/orion pins are stored in
> this repo. Substitute your own org, repo, commit SHA (`--ref`), bucket,
> and file hashes. Every command shape and URL shape is real and matches
> `links` output verbatim.

Deploy a loader folder to a GitHub repo and a GCS bucket, then verify and
list mirrors (example: repo `lyra`, SHA `abc1234`):

```sh
# 1. Make sure the repo and bucket exist (needs gh/gcloud auth).
starfield ensure-repo --org acme --repo lyra --private
starfield ensure-bucket --bucket acme-loaders

# 2. Push ./dist to both.
starfield gh-sync --org acme --repo lyra --folder ./dist
starfield gcs-upload --bucket acme-loaders --folder ./dist --prefix lyra

# 3. Verify the entry doc against its expected hash.
starfield verify \
  --url https://cdn.jsdelivr.net/gh/acme/lyra@abc1234/index.svg \
  --sha256 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef \
  --mime image/svg+xml

# 4. List every mirror URL for the pinned file (real output shape).
starfield links --org acme --repo lyra --ref abc1234 --file index.svg --format md
```

```text
- [cdn.jsdelivr.net](https://cdn.jsdelivr.net/gh/acme/lyra@abc1234/index.svg)
- [quantil.jsdelivr.net](https://quantil.jsdelivr.net/gh/acme/lyra@abc1234/index.svg)
- [cdn.jsdmirror.com](https://cdn.jsdmirror.com/gh/acme/lyra@abc1234/index.svg)
...
```

Same flow for a second loader (example: repo `orion`, SHA `def5678`,
plain format, first lines of real output):

```sh
starfield links --org acme --repo orion --ref def5678 --file index.svg
```

```text
https://cdn.jsdelivr.net/gh/acme/orion@def5678/index.html
https://quantil.jsdelivr.net/gh/acme/orion@def5678/index.html
https://cdn.jsdmirror.com/gh/acme/orion@def5678/index.html
https://fastly.jsdelivr.net/gh/acme/orion@def5678/index.html
https://gcore.jsdelivr.net/gh/acme/orion@def5678/index.html
```

(22 lines total: 15 jsDelivr-compat, 1 statically, 2 githack, 1 raw
githubusercontent, 3 esm.sh. The known-dead hosts from the 2026-10-12
probe are recorded in `src/starfield/links.py` as `DEAD_HOSTS` with
reasons and are never emitted.)

Spot-check any single mirror with `verify`, discover existing state with
`gh-list`/`gcs-list`, and preview destructive syncs/uploads with
`--dry-run` before pushing.

## Out of scope (v1)

History rewrites/purges, DNS/CDN configuration, billing queries, secret
handling, Windows support (paths assume POSIX; best-effort otherwise).
