"""Mirror-links tests (Task 2). Stdlib unittest only.

Covers the five URL builders with exact `index.svg` values per the
design-spec shapes, `mirror_urls` plain/md/json output, and the
alive/dead host tables from the 2026-10-12 probe.
"""

import json
import unittest

from starfield import links

ORG = "acme"
REPO = "lyra"
REF = "abc1234"
FILE = "index.svg"


class TestBuilders(unittest.TestCase):
    def test_jsdelivr_url(self):
        self.assertEqual(
            links.jsdelivr_url("cdn.jsdelivr.net", ORG, REPO, REF, FILE),
            "https://cdn.jsdelivr.net/gh/acme/lyra@abc1234/index.svg",
        )

    def test_statically_url(self):
        self.assertEqual(
            links.statically_url("cdn.statically.io", ORG, REPO, REF, FILE),
            "https://cdn.statically.io/gh/acme/lyra/abc1234/index.svg",
        )

    def test_githack_url(self):
        self.assertEqual(
            links.githack_url("raw.githack.com", ORG, REPO, REF, FILE),
            "https://raw.githack.com/acme/lyra/abc1234/index.svg",
        )

    def test_raw_github_url(self):
        self.assertEqual(
            links.raw_github_url("raw.githubusercontent.com", ORG, REPO, REF, FILE),
            "https://raw.githubusercontent.com/acme/lyra/abc1234/index.svg",
        )

    def test_esm_url(self):
        self.assertEqual(
            links.esm_url("esm.sh", ORG, REPO, REF, FILE),
            "https://esm.sh/gh/acme/lyra@abc1234/index.svg",
        )


class TestMirrorUrls(unittest.TestCase):
    def test_plain_is_one_url_per_line(self):
        out = links.mirror_urls(ORG, REPO, REF, FILE, format="plain")
        lines = out.splitlines()
        self.assertGreater(len(lines), 1)
        self.assertIn(
            "https://cdn.jsdelivr.net/gh/acme/lyra@abc1234/index.svg", lines
        )
        for line in lines:
            self.assertTrue(line.startswith("https://"), line)

    def test_md_links_one_host(self):
        out = links.mirror_urls(ORG, REPO, REF, FILE, format="md")
        self.assertIn(
            "- [cdn.jsdelivr.net]"
            "(https://cdn.jsdelivr.net/gh/acme/lyra@abc1234/index.svg)",
            out.splitlines(),
        )

    def test_json_parses_with_host_and_url(self):
        out = links.mirror_urls(ORG, REPO, REF, FILE, format="json")
        entries = json.loads(out)
        self.assertIsInstance(entries, list)
        self.assertIn(
            {
                "host": "cdn.jsdelivr.net",
                "url": "https://cdn.jsdelivr.net/gh/acme/lyra@abc1234/index.svg",
            },
            entries,
        )

    def test_default_format_is_plain(self):
        self.assertEqual(
            links.mirror_urls(ORG, REPO, REF, FILE),
            links.mirror_urls(ORG, REPO, REF, FILE, format="plain"),
        )

    def test_unknown_format_raises(self):
        with self.assertRaises(ValueError):
            links.mirror_urls(ORG, REPO, REF, FILE, format="yaml")


class TestTables(unittest.TestCase):
    def test_alive_hosts_count_and_spot_checks(self):
        self.assertEqual(len(links.ALIVE_HOSTS), 22)
        for host in (
            "cdn.jsdelivr.net",
            "cdn.statically.io",
            "raw.githack.com",
            "raw.githubusercontent.com",
            "esm.sh",
        ):
            self.assertIn(host, links.ALIVE_HOSTS)

    def test_dead_hosts_count_and_reasons(self):
        self.assertEqual(len(links.DEAD_HOSTS), 22)
        self.assertIn("connection", links.DEAD_HOSTS["testingcdn.jsdelivr.net"].lower())
        self.assertIn("404", links.DEAD_HOSTS["script.google.com"])
        self.assertIn("403", links.DEAD_HOSTS["code.webcache.cn"])
        self.assertIn("wrong content", links.DEAD_HOSTS["cdn.onmicrosoft.cn"].lower())

    def test_no_host_in_both_tables(self):
        self.assertFalse(set(links.ALIVE_HOSTS) & set(links.DEAD_HOSTS))

    def test_every_alive_host_gets_a_url(self):
        out = links.mirror_urls(ORG, REPO, REF, FILE, format="plain")
        lines = out.splitlines()
        self.assertEqual(len(lines), len(links.ALIVE_HOSTS))
        self.assertEqual(len(set(lines)), len(lines))


if __name__ == "__main__":
    unittest.main()
