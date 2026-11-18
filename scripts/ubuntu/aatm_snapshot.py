#!/usr/bin/env python3
"""Rollback points for r8-aatm: tag + local backup + SNAPSHOTS.md, and a non-destructive rollback."""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import tarfile
from datetime import datetime
from pathlib import Path

BRANCH = "r8-aatm"
TAG_RE = re.compile(r"^snapshot/r8-(\d{8})-(\d{2,})$")
SKIP_ANYWHERE = {"node_modules", ".venv", ".git", "__pycache__"}
SKIP_TOP = {"data", "logs"}  # the database is copied separately and consistently


def root_dir() -> Path:
    return Path(os.environ.get("AATM_ROOT", Path(__file__).resolve().parents[2]))


def backup_dir() -> Path:
    path = Path(os.environ.get("AATM_BACKUP_DIR", Path.home() / "aatm-backups"))
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def git(*args: str, check: bool = True) -> str:
    done = subprocess.run(["git", *args], check=False, cwd=root_dir(), capture_output=True, text=True)
    if check and done.returncode != 0:
        raise SystemExit(f"git {args[0]} failed: {done.stderr.strip()[:300]}")
    return done.stdout.strip() if done.returncode == 0 else ""


def remote_tags() -> dict[str, str]:
    tags: dict[str, str] = {}
    for line in git("ls-remote", "--tags", "origin", "refs/tags/snapshot/*").splitlines():
        sha, ref = line.split()
        name = ref.removeprefix("refs/tags/")
        if name.endswith("^{}"):
            tags[name[:-3]] = sha
        else:
            tags.setdefault(name, sha)
    return {name: sha for name, sha in tags.items() if TAG_RE.match(name)}


def tag_order(name: str) -> tuple[str, int]:
    match = TAG_RE.match(name)
    return match.group(1), int(match.group(2))


def write_backup(name: str) -> tuple[Path, Path | None]:
    target = backup_dir()
    root = root_dir()
    archive = target / f"{name}.tgz"
    if archive.exists():
        raise SystemExit(f"{archive} already exists; backups are never overwritten")

    def keep(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
        parts = Path(info.name).parts[1:]
        if any(part in SKIP_ANYWHERE for part in parts) or (parts and parts[0] in SKIP_TOP):
            return None
        return info if info.isfile() or info.isdir() or info.issym() else None

    with tarfile.open(archive, "w:gz") as tar:
        tar.add(root, arcname=root.name, filter=keep)
    archive.chmod(0o600)
    database = root / "data" / "aatm.db"
    copy = None
    if database.exists():
        copy = target / f"{name}.db"
        source = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        destination = sqlite3.connect(copy)
        with destination:
            source.backup(destination)
        source.close()
        destination.close()
        copy.chmod(0o600)
    return archive, copy


def exclude_notes_file() -> None:
    exclude = Path(git("rev-parse", "--git-path", "info/exclude"))
    exclude = exclude if exclude.is_absolute() else root_dir() / exclude
    current = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    if "SNAPSHOTS.md" not in current.splitlines():
        exclude.parent.mkdir(parents=True, exist_ok=True)
        exclude.write_text(current + ("" if current.endswith("\n") or not current else "\n") + "SNAPSHOTS.md\n", encoding="utf-8")


def changes_since(previous: str | None, head: str, url: str) -> str:
    span = f"{previous}..{head}" if previous else head
    limit = [] if previous else ["-20"]
    numbers: list[str] = []
    for subject in git("log", "--format=%s", *limit, span).splitlines():
        match = re.search(r"\(#(\d+)\)$", subject)
        if match:
            numbers.append(match.group(1))
    if not numbers:
        return "no pull requests listed"
    return " ".join(f"{url}/pull/{number}" for number in reversed(numbers))


def repo_url() -> str:
    url = git("remote", "get-url", "origin")
    match = re.search(r"github\.com[:/]([^/]+/[^/.]+?)(?:\.git)?$", url)
    return f"https://github.com/{match.group(1)}" if match else url


def snapshot() -> int:
    git("fetch", "--quiet", "origin", BRANCH)
    head = git("rev-parse", f"origin/{BRANCH}")
    tags = remote_tags()
    latest = max(tags, key=tag_order) if tags else None
    if latest and tags[latest] == head:
        print(f"[AATM] No new changes since {latest}.")
        return 0
    today = datetime.now().strftime("%Y%m%d")
    number = max([tag_order(name)[1] for name in tags if tag_order(name)[0] == today], default=0) + 1
    tag = f"snapshot/r8-{today}-{number:02d}"
    name = tag.split("/", 1)[1]
    archive, _copy = write_backup(name)  # the backup comes first: a tag must not promise one that failed
    git("tag", tag, head)
    git("push", "--quiet", "origin", f"refs/tags/{tag}")
    exclude_notes_file()
    line = (f"- `{tag}` | {datetime.now().isoformat(timespec='seconds')} | {changes_since(tags.get(latest) if latest else None, head, repo_url())}"
            f" | rollback: `~/AATM/scripts/ubuntu/rollback.sh {tag}`\n")
    notes = root_dir() / "SNAPSHOTS.md"
    if not notes.exists():
        notes.write_text("# AATM snapshots\n\n", encoding="utf-8")
    with notes.open("a", encoding="utf-8") as handle:
        handle.write(line)
    print(f"[AATM] Snapshot {tag} at {head[:8]}; backup {archive}")
    return 0


def is_protected(slug: str) -> bool:
    done = subprocess.run(["gh", "api", f"repos/{slug}/branches/{BRANCH}"], check=False, cwd=root_dir(), capture_output=True, text=True)
    if done.returncode != 0:
        raise SystemExit("Could not read the branch protection from GitHub; nothing was changed.")
    return bool(json.loads(done.stdout).get("protected"))


def rollback(tag: str) -> int:
    if not TAG_RE.match(tag):
        raise SystemExit("Use a tag like snapshot/r8-20261118-01.")
    git("fetch", "--quiet", "origin", BRANCH, f"refs/tags/{tag}:refs/tags/{tag}")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    archive, _copy = write_backup(f"pre-rollback-{stamp}")
    print(f"[AATM] Current state saved: {archive}")
    head = git("rev-parse", f"origin/{BRANCH}")
    tree = git("rev-parse", f"{tag}^{{tree}}")
    if git("rev-parse", f"{head}^{{tree}}") == tree:
        print(f"[AATM] {BRANCH} already matches {tag}; nothing to do.")
        return 0
    commit = git("commit-tree", tree, "-p", head, "-m", f"Roll back {BRANCH} to {tag}")
    slug = repo_url().removeprefix("https://github.com/")
    if is_protected(slug):
        branch = f"claude/rollback-{tag.split('/', 1)[1]}-{stamp}"
        git("push", "--quiet", "origin", f"{commit}:refs/heads/{branch}")
        done = subprocess.run(
            ["gh", "pr", "create", "--repo", slug, "--base", BRANCH, "--head", branch,
             "--title", f"Roll back {BRANCH} to {tag}", "--body", f"Restores the files of `{tag}` in a new commit."],
            check=False, cwd=root_dir(), capture_output=True, text=True)
        if done.returncode != 0:
            raise SystemExit(f"Branch {branch} was pushed, but the PR could not be opened.")
        print(f"[AATM] {BRANCH} is protected. Rollback PR: {done.stdout.strip()}")
        return 0
    git("push", "--quiet", "origin", f"{commit}:refs/heads/{BRANCH}")  # a normal push: it only adds a commit
    print(f"[AATM] {BRANCH} now has the files of {tag} (new commit {commit[:8]}; history is kept).")
    print("[AATM] To use it here: git -C ~/AATM pull --ff-only && ~/AATM/scripts/ubuntu/restart.sh")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("snapshot")
    commands.add_parser("rollback").add_argument("tag")
    args = parser.parse_args()
    return snapshot() if args.command == "snapshot" else rollback(args.tag)


if __name__ == "__main__":
    sys.exit(main())
