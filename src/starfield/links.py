"""Mirror-link URL builders + alive/dead host tables.

Codifies CDN-mirror availability: 17 alive hosts across three URL shapes
(jsDelivr-compat, githack, esm.sh) plus 27 known-dead hosts with reasons,
last corrected 2026-10-13 after user browser checks retired five hosts
that the 2026-10-12 probe had marked alive.

Shapes (ORG/REPO/REF/FILE placeholders):
  jsDelivr-compat:  https://HOST/gh/ORG/REPO@REF/FILE
  githack:          https://HOST/ORG/REPO/REF/FILE
  esm.sh:           https://HOST/gh/ORG/REPO@REF/FILE
"""

from __future__ import annotations

import concurrent.futures
import json

from . import verify

#: Hosts serving the jsDelivr-compat `/gh/ORG/REPO@REF/FILE` shape.
JSDELIVR_HOSTS = (
    "cdn.jsdelivr.net",
    "quantil.jsdelivr.net",
    "cdn.jsdmirror.com",
    "fastly.jsdelivr.net",
    "gcore.jsdelivr.net",
    "jsd.onmicrosoft.cn",
    "jsdelivr.b-cdn.net",
    "originfastly.jsdelivr.net",
    "testingcf.jsdelivr.net",
    "jsdelivr.codeqihan.com",
    "jsd.nmmsl.top",
    "jsd.yizex.cn",
    "cdn.jsdelivr.net.cn",
    "jsd-proxy.ygxz.in",
)

#: Hosts serving the githack `/ORG/REPO/REF/FILE` shape.
GITHACK_HOSTS = ("rawcdn.githack.com", "raw.githack.com")

#: Hosts serving the esm.sh `/gh/ORG/REPO@REF/FILE` shape.
#: `raw.esm.sh` serves bytes differing from other mirrors but is
#: user-confirmed working in a real browser.
ESM_HOSTS = ("raw.esm.sh",)

#: Every alive host, in stable output order.
ALIVE_HOSTS = (
    *JSDELIVR_HOSTS,
    *GITHACK_HOSTS,
    *ESM_HOSTS,
)

#: Known-dead hosts from the 2026-10-12 probe, mapping host -> reason.
DEAD_HOSTS = {
    # Connection failures.
    "testingcdn.jsdelivr.net": "connection failure",
    "rawcdn.jsdelivr.net": "connection failure",
    "origin.jsdelivr.net": "connection failure",
    "jsd.proxy.aks.moe": "connection failure",
    "jsd.cdn.zzko.cn": "connection failure",
    "jsd.duolaa.top": "connection failure",
    "jsdelivr.cdn.net": "connection failure",
    "raw.gitmirror.com": "connection failure",
    # HTTP 404.
    "cdn.jsdelivr.us": "HTTP 404",
    "cdn.jsdmirror.cn": "HTTP 404",
    "jsdelivr.topthink.com": "HTTP 404",
    "cdn.staticdelivr.com": "HTTP 404",
    "cdn.emulatorjs.org": "HTTP 404",
    "githubraw.com": "HTTP 404",
    "cdn.githubraw.com": "HTTP 404",
    "raw.github.com": "HTTP 404",
    "script.google.com": "HTTP 404",
    # HTTP 403.
    "jsdelivr.qaq.qa": "HTTP 403",
    "jsdelivr.aby.pub": "HTTP 403",
    "code.webcache.cn": "HTTP 403",
    # Wrong content (not the raw file).
    "cdn.onmicrosoft.cn": "wrong content",
    "github.com": "wrong content (blob page, not the raw file)",
    # Retired 2026-10-13 after user browser checks (alive in 2026-10-12 probe).
    "raw.githubusercontent.com": "HTTP 404 (verified 2026-10-13)",
    "cdn.bili33.top": "blocked from reporter network; serves correctly elsewhere",
    "esm.sh": "wrong content (2455B served vs expected file)",
    "cdn.esm.sh": "wrong content (2455B served vs expected file)",
    "cdn.statically.io": "wrong content (2626B served vs expected file)",
}


def jsdelivr_url(host: str, org: str, repo: str, ref: str, file: str) -> str:
    """Build a jsDelivr-compat URL: `https://HOST/gh/ORG/REPO@REF/FILE`."""
    return f"https://{host}/gh/{org}/{repo}@{ref}/{file}"


def statically_url(host: str, org: str, repo: str, ref: str, file: str) -> str:
    """Build a statically URL: `https://HOST/gh/ORG/REPO/REF/FILE`."""
    return f"https://{host}/gh/{org}/{repo}/{ref}/{file}"


def githack_url(host: str, org: str, repo: str, ref: str, file: str) -> str:
    """Build a githack URL: `https://HOST/ORG/REPO/REF/FILE`."""
    return f"https://{host}/{org}/{repo}/{ref}/{file}"


def raw_github_url(host: str, org: str, repo: str, ref: str, file: str) -> str:
    """Build a raw.githubusercontent URL: `https://HOST/ORG/REPO/REF/FILE`."""
    return f"https://{host}/{org}/{repo}/{ref}/{file}"


def esm_url(host: str, org: str, repo: str, ref: str, file: str) -> str:
    """Build an esm.sh URL: `https://HOST/gh/ORG/REPO@REF/FILE`."""
    return f"https://{host}/gh/{org}/{repo}@{ref}/{file}"


def _url_for(host: str, org: str, repo: str, ref: str, file: str) -> str:
    """Dispatch `host` to the builder matching its URL shape."""
    if host in JSDELIVR_HOSTS:
        return jsdelivr_url(host, org, repo, ref, file)
    if host in GITHACK_HOSTS:
        return githack_url(host, org, repo, ref, file)
    if host in ESM_HOSTS:
        return esm_url(host, org, repo, ref, file)
    raise ValueError(f"unknown mirror host: {host}")


def _check_one(args: tuple[str, str, float]) -> tuple[str, str, bool, str]:
    """GET one URL; return (host, url, alive, detail)."""
    host, url, timeout = args
    try:
        out = verify.check(url, min_bytes=1, timeout=timeout)
    except Exception as exc:
        return host, url, False, str(exc)[:160]
    if out.get("ok"):
        return host, url, True, ""
    return host, url, False, str(out.get("error", "failed"))[:160]


def mirror_urls(
    org: str,
    repo: str,
    ref: str,
    file: str,
    format: str = "plain",
    check: bool = False,
    timeout: float = 60,
) -> str:
    """Render mirror URLs for every alive host as a string.

    `format` is `"plain"` (one URL per line), `"md"` (one
    `- [host](url)` bullet per line), or `"json"` (a JSON array of
    `{"host", "url"}` objects). Anything else raises `ValueError`.

    With `check=True` every URL is fetched first (8 at a time):
    unreachable hosts are dropped from the listing and reported as
    `# DEAD <url> (<detail>)` lines (plain/md), while `json` objects
    gain `"alive"`/`"detail"` fields.
    """
    pairs = [(host, _url_for(host, org, repo, ref, file)) for host in ALIVE_HOSTS]
    dead: list[tuple[str, str]] = []
    if check:
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = pool.map(
                _check_one, [(url_host[0], url_host[1], timeout) for url_host in pairs]
            )
        alive_pairs = []
        for host, url, alive, detail in results:
            if alive:
                alive_pairs.append((host, url))
            else:
                dead.append((url, detail))
        pairs = alive_pairs
    if format == "plain":
        lines = [url for _, url in pairs]
    elif format == "md":
        lines = [f"- [{host}]({url})" for host, url in pairs]
    elif format == "json":
        objs: list[dict[str, object]] = [{"host": host, "url": url} for host, url in pairs]
        if check:
            objs = [
                {**o, "alive": True, "detail": ""} for o in objs
            ] + [
                {"host": "", "url": url, "alive": False, "detail": detail}
                for url, detail in dead
            ]
        return json.dumps(objs, indent=2)
    else:
        raise ValueError(f"unknown format: {format!r} (want plain|md|json)")
    lines.extend(f"# DEAD {url} ({detail})" for url, detail in dead)
    return "\n".join(lines)
