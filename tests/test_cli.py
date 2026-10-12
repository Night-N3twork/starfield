"""CLI skeleton tests (Task 1). Stdlib unittest only."""

import io
import unittest
from contextlib import redirect_stderr, redirect_stdout

from starfield import cli, core


def run_cli(argv):
    """Run cli.main capturing stdout/stderr; argparse --help exits via
    SystemExit, which we swallow so the help text can be asserted on."""
    buf = io.StringIO()
    try:
        with redirect_stdout(buf), redirect_stderr(buf):
            cli.main(argv)
    except SystemExit:
        pass
    return buf.getvalue()


class TestCliHelp(unittest.TestCase):
    def test_help_lists_commands(self):
        self.assertIn("gh-sync", run_cli(["--help"]))
        self.assertIn("gcs-upload", run_cli(["--help"]))

    def test_per_command_help_works(self):
        self.assertIn("--org", run_cli(["gh-sync", "--help"]))
        self.assertIn("--bucket", run_cli(["gcs-upload", "--help"]))


class TestCoreRun(unittest.TestCase):
    def test_run_missing_binary_raises_cli_error(self):
        with self.assertRaises(core.CliError) as ctx:
            core.run(["starfield-bogus-binary-xyz"])
        self.assertIn("starfield-bogus-binary-xyz", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
