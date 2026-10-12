"""GitHub operations via the `gh`/`git` CLIs (Task 3).

All subprocess work goes through `core.run` so unit tests can fake it;
file copies/deletes are real filesystem operations. Never force-pushes.
"""

from __future__ import annotations

import datetime
import json
import os
import shutil
import tempfile
from pathlib import Path

from . import core

#: Fields requested from `gh repo list --json`.
_LIST_FIELDS = "name,isPrivate,updatedAt,pushedAt"


def _git(workdir: Path, *args: str, timeout: float) -> str:
    return core.run(["git", "-C", str(workdir), *args], timeout=timeout)


def _clone_shallow(url: str, dest: Path, branch: str, timeout: float) -> None:
    """Shallow-clone `url` to `dest`, creating `branch` if it is new."""
    try:
        core.run(
            ["git", "clone", "--depth", "1", "--branch", branch, url, str(dest)],
            timeout=timeout,
        )
    except core.CliError:
        core.run(["git", "clone", "--depth", "1", url, str(dest)], timeout=timeout)
        try:
            core.run(["git", "-C", str(dest), "checkout", "-b", branch], timeout=timeout)
        except core.CliError:
            core.run(["git", "-C", str(dest), "checkout", branch], timeout=timeout)


def _prepare_workdir(
    checkout: Path, url: str, branch: str, timeout: float
) -> None:
    """Reuse `checkout`: clone into it if empty, else require it clean."""
    if checkout.exists() and not checkout.is_dir():
        raise core.CliError(f"workdir is not a directory: {checkout}")
    checkout.mkdir(parents=True, exist_ok=True)
    if not (checkout / ".git").exists():
        if any(checkout.iterdir()):
            raise core.CliError(f"workdir is not a git checkout: {checkout}")
        _clone_shallow(url, checkout, branch, timeout)
        return
    if _git(checkout, "status", "--porcelain", timeout=timeout).strip():
        raise core.CliError(
            f"workdir is dirty: {checkout} (commit or clean it first)"
        )
    try:
        _git(checkout, "checkout", branch, timeout=timeout)
    except core.CliError:
        _git(checkout, "checkout", "-b", branch, timeout=timeout)


def _copy_tree(src: Path, checkout: Path, delete: bool) -> None:
    """Copy `src` files over `checkout`; drop checkout-only files (not `.git`)."""
    wanted = set()
    for path in sorted(p for p in src.rglob("*") if p.is_file()):
        rel = path.relative_to(src)
        if rel.parts[0] == ".git":
            continue
        wanted.add(rel)
        dest = checkout / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dest)
    if not delete:
        return
    for path in sorted(p for p in checkout.rglob("*") if p.is_file()):
        rel = path.relative_to(checkout)
        if rel.parts[0] == ".git" or rel in wanted:
            continue
        path.unlink()
    for dirpath, dirnames, filenames in os.walk(checkout, topdown=False):
        if ".git" in dirnames:
            continue
        if ".git" in Path(dirpath).relative_to(checkout).parts:
            continue
        if not dirnames and not filenames and Path(dirpath) != checkout:
            try:
                Path(dirpath).rmdir()
            except OSError:
                pass


def sync(
    org: str,
    repo: str,
    folder: str,
    branch: str = "main",
    message: str | None = None,
    delete: bool = True,
    dry_run: bool = False,
    workdir: str | None = None,
    timeout: float = 300,
) -> dict:
    """Push `folder` contents to `org/repo@branch`; return sha/changed/pushed.

    Raises `core.CliError` when `folder` is missing or holds no files.
    """
    src = Path(folder)
    if not src.is_dir():
        raise core.CliError(f"folder not found: {folder}")
    files = [
        p
        for p in src.rglob("*")
        if p.is_file() and p.relative_to(src).parts[0] != ".git"
    ]
    if not files:
        raise core.CliError(f"refusing to sync empty folder: {folder}")
    url = f"https://github.com/{org}/{repo}.git"

    tmpdir = None
    try:
        if workdir is None:
            tmpdir = tempfile.mkdtemp(prefix="starfield-gh-sync-")
            checkout = Path(tmpdir) / "repo"
            _clone_shallow(url, checkout, branch, timeout)
        else:
            checkout = Path(workdir)
            _prepare_workdir(checkout, url, branch, timeout)

        _copy_tree(src, checkout, delete)
        _git(checkout, "add", "-A", timeout=timeout)
        dirty = bool(
            _git(checkout, "status", "--porcelain", timeout=timeout).strip()
        )
        if not dirty:
            sha = _git(checkout, "rev-parse", "HEAD", timeout=timeout).strip()
            return {"sha": sha, "changed": False, "pushed": False}
        if dry_run:
            try:
                sha = _git(checkout, "rev-parse", "HEAD", timeout=timeout).strip()
            except core.CliError:
                sha = ""
            return {"sha": sha, "changed": True, "pushed": False}
        if message is None:
            today = datetime.date.today().isoformat()
            message = f"sync {len(files)} files ({today})"
        _git(checkout, "commit", "-m", message, timeout=timeout)
        sha = _git(checkout, "rev-parse", "HEAD", timeout=timeout).strip()
        _git(checkout, "push", "origin", branch, timeout=timeout)
        return {"sha": sha, "changed": True, "pushed": True}
    finally:
        if tmpdir is not None:
            shutil.rmtree(tmpdir, ignore_errors=True)


#: Substrings (lowercased) of a `gh repo view` error that mean "missing".
_NOT_FOUND_HINTS = ("could not resolve", "not found", "404")


def _is_not_found(exc: core.CliError) -> bool:
    msg = exc.message.lower()
    return any(hint in msg for hint in _NOT_FOUND_HINTS)


def ensure_repo(org: str, repo: str, public: bool = False, timeout: float = 60) -> dict:
    """Create `org/repo` when missing; return `{"created": bool}`."""
    slug = f"{org}/{repo}"
    try:
        core.run(["gh", "repo", "view", slug, "--json", "name"], timeout=timeout)
        return {"created": False}
    except core.CliError as exc:
        if not _is_not_found(exc):
            raise
    visibility = "--public" if public else "--private"
    core.run(["gh", "repo", "create", slug, visibility], timeout=timeout)
    return {"created": True}


def list_repos(org: str, timeout: float = 60) -> list[dict]:
    """List repos in `org` as parsed `gh repo list --json` dicts."""
    out = core.run(
        ["gh", "repo", "list", org, "--json", _LIST_FIELDS, "--limit", "100"],
        timeout=timeout,
    )
    try:
        data = json.loads(out)
    except json.JSONDecodeError as exc:
        raise core.CliError(f"gh repo list: unparseable output: {exc}") from exc
    if not isinstance(data, list):
        raise core.CliError(f"gh repo list: unexpected output: {out[:200]}")
    return data
