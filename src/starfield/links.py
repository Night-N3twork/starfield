"""Mirror-link URL builders + alive/dead host tables (Task 2).

Codifies the 2026-10-12 probe: 22 alive hosts across four URL shapes
(jsDelivr-compat, statically, githack/raw-githubusercontent, esm.sh)
plus 22 known-dead hosts with reasons.

Shapes (ORG/REPO/REF/FILE placeholders):
  jsDelivr-compat:  https://HOST/gh/ORG/REPO@REF/FILE
  statically:       https://HOST/gh/ORG/REPO/REF/FILE
  githack/raw:      https://HOST/ORG/REPO/REF/FILE
  esm.sh:           https://HOST/gh/ORG/REPO@REF/FILE
"""

from __future__ import annotations

import json

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
    "cdn.bili33.top",
    "jsd.yizex.cn",
    "cdn.jsdelivr.net.cn",
    "jsd-proxy.ygxz.in",
)

#: Host serving the statically `/gh/ORG/REPO/REF/FILE` shape.
STATICALLY_HOSTS = ("cdn.statically.io",)

#: Hosts serving the githack `/ORG/REPO/REF/FILE` shape.
GITHACK_HOSTS = ("rawcdn.githack.com", "raw.githack.com")

#: Host serving the raw `/ORG/REPO/REF/FILE` shape.
RAW_GITHUB_HOSTS = ("raw.githubusercontent.com",)

#: Hosts serving the esm.sh `/gh/ORG/REPO@REF/FILE` shape.
ESM_HOSTS = ("esm.sh", "raw.esm.sh", "cdn.esm.sh")

#: Every alive host from the 2026-10-12 probe, in stable output order.
ALIVE_HOSTS = (
    *JSDELIVR_HOSTS,
    *STATICALLY_HOSTS,
    *GITHACK_HOSTS,
    *RAW_GITHUB_HOSTS,
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
    if host in STATICALLY_HOSTS:
        return statically_url(host, org, repo, ref, file)
    if host in GITHACK_HOSTS:
        return githack_url(host, org, repo, ref, file)
    if host in RAW_GITHUB_HOSTS:
        return raw_github_url(host, org, repo, ref, file)
    if host in ESM_HOSTS:
        return esm_url(host, org, repo, ref, file)
    raise ValueError(f"unknown mirror host: {host}")


def mirror_urls(
    org: str, repo: str, ref: str, file: str, format: str = "plain"
) -> str:
    """Render mirror URLs for every alive host as a string.

    `format` is `"plain"` (one URL per line), `"md"` (one
    `- [host](url)` bullet per line), or `"json"` (a JSON array of
    `{"host", "url"}` objects). Anything else raises `ValueError`.
    """
    pairs = [(host, _url_for(host, org, repo, ref, file)) for host in ALIVE_HOSTS]
    if format == "plain":
        return "\n".join(url for _, url in pairs)
    if format == "md":
        return "\n".join(f"- [{host}]({url})" for host, url in pairs)
    if format == "json":
        return json.dumps(
            [{"host": host, "url": url} for host, url in pairs], indent=2
        )
    raise ValueError(f"unknown format: {format!r} (want plain|md|json)")
