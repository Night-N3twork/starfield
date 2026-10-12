# Starfield — design spec (2026-10-12)

Deploy tool for Constellation loaders (and anything shaped like them):
push a folder to a GitHub repo and/or gzip-upload it to a GCS bucket,
then verify and list mirror links. Three surfaces, one core.

## Name

Starfield. Lyra = Daydream loader, Orion = Space loader.

## Layout

New package at workspace root, stdlib-only Python, no third-party deps:

```
starfield/
  pyproject.toml            # packaging, `starfield` console entry
  src/starfield/
    __init__.py / __main__.py
    cli.py                  # argparse, all subcommands
    core.py                 # shared orchestration, result types
    gh_ops.py               # gh CLI wrappers (sync, ensure_repo, list_repos)
    gcs_ops.py              # gcloud CLI wrappers (upload, ensure_bucket, list_buckets)
    verify.py               # public GET + hash compare, mirror probing
    links.py                # codified 44-host mirror table (alive/dead)
    http_server.py          # stdlib HTTP adapter (POST /v1/*, GET /healthz)
    mcp_server.py           # stdio JSON-RPC adapter (tools/list, tools/call)
  docs/2026-10-12-starfield-design.md   # this file
  README.md                 # full API documentation (install/auth/CLI/HTTP/MCP/examples)
  tests/                    # stdlib unittest
```

Auth stays with the CLIs: `gh auth login`, `gcloud init`. Starfield never
handles tokens; it shells out and surfaces CLI errors verbatim.

## Surfaces

### CLI

```
starfield gh-sync --org ORG --repo REPO --folder DIR [--branch main]
                  [--message MSG] [--no-delete] [--dry-run]
starfield gcs-upload --bucket BUCKET --folder DIR [--prefix PFX]
                  [--cache-control CC] [--create-only] [--dry-run]
starfield ensure-repo --org ORG --repo REPO [--public|--private]
starfield ensure-bucket --bucket BUCKET [--location us-central1] [--public]
starfield gcs-list [--bucket BUCKET]            # buckets, or object summary
starfield gh-list --org ORG                     # repos in an org
starfield verify --url URL --sha256 HEX [--mime TYPE] [--min-bytes N]
starfield links --org ORG --repo REPO --ref REF # mirror URLs, alive-marked
starfield serve --port 8765                     # HTTP server
starfield mcp [--org O] [--bucket B] [--allow-create] [--read-only]
                [--timeout S] [--concurrency N]  # MCP stdio server
```

### HTTP (`starfield serve --port`)

- `POST /v1/gh-sync | /v1/gcs-upload | /v1/ensure-repo | /v1/ensure-bucket`
- `POST /v1/verify | /v1/links`
- `GET /v1/gcs-list?bucket= | /v1/gh-list?org= | /healthz`
- JSON in/out: `{"ok": true, ...}` or `{"ok": false, "error": "..."}`.
  Same safety gates as MCP (below) apply per request flags.

### MCP (stdio)

Tools: `gh_sync`, `gcs_upload`, `ensure_repo`, `ensure_bucket`,
`gcs_list`, `gh_list`, `verify_deploy`, `list_mirrors`.
Launch arguments:
- `--org O`, `--bucket B` — defaults injected into tool calls.
- `--allow-create` — without it, `ensure_*` tools refuse (read-only default
  for creation; sync/upload are the deploy path and always allowed).
- `--read-only` — disables every mutating tool; only list/verify/links.
- `--timeout S` (default 300), `--concurrency N` (default 8).

## Operations

### gh-sync (folder -> org/repo@branch)

1. Clone (shallow) to temp, or reuse `--workdir` checkout if clean.
2. Copy folder contents over the checkout (flat or tree-preserving as-is).
3. `git add -A`; delete tracked files absent from the folder (unless
   `--no-delete`); commit with message (default lists file count + date)
   only if dirty; push; return SHA.
4. Safety: refuse on dirty workdir without `--allow-dirty`; never force-push
   (no history rewrites — the purge was a one-off, not a feature).

### gcs-upload (folder -> gs://bucket[/prefix])

1. Stage copies; gzip-9 (mtime=0) for html/svg/js/mjs/css/json/wasm/map;
   filenames preserved, `Content-Encoding: gzip` only on compressed ones.
2. Metadata: correct `Content-Type` per extension; `Content-Disposition:
   inline`; cache rules — entry docs (`index.html`, `index.svg`, `sw.js`,
   `bootstrap-sw.js`) `no-cache,no-transform`; hashed assets
   (`*-HASH.*`) `public,max-age=31536000,immutable,no-transform`;
   everything else `public,max-age=86400,no-transform`; global
   `--cache-control` overrides.
3. Upload order: dependencies first, worker second, entry docs last, with
   `if-generation-match` preconditions (overwrite-safe, `--create-only`
   for no-clobber).
4. Verify every object: public GET, status/MIME/encoding/disposition/cache
   headers, byte length, md5 vs uploaded bytes, gunzip-compare vs source.

### ensure-bucket / ensure-repo

- Bucket: regional `us-central1` (flag-overridable), Standard, uniform
  bucket-level access, public-access-prevention off (inherited-safe),
  soft-delete 0, no versioning/Autoclass. Public `allUsers:objectViewer`
  ONLY with explicit `--public`.
- Repo: `gh repo create ORG/REPO` with `--public/--private` (default private
  unless `--public`); reports exists/created.

### gcs-list / gh-list (discovery, read-only)

- `gcs-list`: all buckets (location/class/bytes), or per-bucket object
  summary (name/size/updated) for a given bucket.
- `gh-list --org`: repos (name/visibility/updated/pushed-at).

### verify

`verify --url --sha256 [--mime] [--min-bytes]`: GET with gzip, assert 200,
optional MIME prefix, optional minimum decoded bytes, compare sha256 of
decoded body. Reports per-URL pass/fail detail. Used for entry docs,
post-upload checks, and mirror spot-checks.

### links

Codified mirror table from the 2026-10-12 probe (22 alive hosts x repo,
plus known-dead list with reasons). Shapes:
jsDelivr-compat `/gh/ORG/REPO@REF/FILE`, statically
`/gh/ORG/REPO/REF/FILE`, githack + raw.githubusercontent
`/ORG/REPO/REF/FILE`, esm.sh `/gh/ORG/REPO@REF/FILE`.
Output: plain list, or `--format md|json`, alive-marked.

## Error handling

- Every operation returns structured errors (tool, command, exit, stderr
  tail); HTTP/MCP surface them unchanged.
- Timeouts bound every network call; parallel uploads capped by
  `--concurrency`; partial failures report per-file, non-zero exit.
- No silent skips: required-asset rules belong to the loader, not here;
  starfield uploads exactly what the folder holds.

## Testing

- `python -m unittest` (stdlib). Unit: URL builders, MIME/cache mapping,
  gzip staging, arg parsing, MCP/HTTP adapters with fakes.
- Live tests (`STARFIELD_LIVE=1` only): real `gh`/`gcloud` against scratch
  repo + bucket; skipped by default.
- Self-review checklist per change: placeholders, contradictions, scope,
  ambiguity — fixed inline before asking for review.

## Out of scope (v1)

History rewrites/purges, DNS/CDN configuration, billing queries, secret
handling, Windows support (paths assume POSIX; best-effort otherwise).
