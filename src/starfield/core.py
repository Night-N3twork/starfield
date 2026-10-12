"""Shared result types and subprocess helper (Task 1 scaffold)."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from typing import Any, Sequence

#: Number of trailing stderr lines attached to a CliError on failure.
STDERR_TAIL_LINES = 20

DEFAULT_TIMEOUT = 300


@dataclass
class Result:
    """Structured outcome of an operation.

    `ok` is True on success; on failure `error` carries the message and
    `data` may hold partial detail.
    """

    ok: bool
    data: Any = None
    error: str = ""


class CliError(Exception):
    """Raised when a subprocess cannot run or exits non-zero."""

    def __init__(self, message: str, exit_code: int = 1) -> None:
        super().__init__(message)
        self.message = message
        self.exit_code = exit_code


def run(cmd: Sequence[str], timeout: float = DEFAULT_TIMEOUT) -> str:
    """Run `cmd`, return stdout. Raises CliError naming the binary when it
    is missing, timing out, or exiting non-zero (stderr tail attached)."""
    try:
        proc = subprocess.run(
            list(cmd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise CliError(f"{cmd[0]}: command not found: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise CliError(f"{cmd[0]}: timed out after {timeout}s") from exc
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.splitlines()[-STDERR_TAIL_LINES:])
        raise CliError(
            f"{cmd[0]}: exited {proc.returncode}: {tail}",
            exit_code=proc.returncode,
        )
    return proc.stdout


def not_implemented(name: str) -> Result:
    """Placeholder Result for subcommands wired up by later tasks."""
    return Result(ok=False, data=None, error=f"not implemented: {name}")
