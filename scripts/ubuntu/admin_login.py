#!/usr/bin/env python3
"""Authenticate locally and save an expiring bearer token, never the password."""
import argparse
import base64
import getpass
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def token_expiry(token: str) -> int:
    # This reads the server-issued JWT's expiry for the operator's convenience;
    # authorization and signature validation remain the server's responsibility.
    payload = token.split(".")[1]
    return int(json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))["exp"])


def request_login(url: str, email: str, password: str) -> dict:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}):
        raise ValueError("Use HTTPS for non-local servers")
    req = urllib.request.Request(
        url.rstrip("/") + "/api/v1/auth/login",
        data=json.dumps({"email": email, "password": password}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as response:
        return json.load(response)


def login(email: str, url: str, target: Path) -> None:
    password = getpass.getpass("Admin password: ")
    try:
        result = request_login(url, email, password)
    finally:
        del password
    if result.get("user", {}).get("role") != "admin":
        raise ValueError("Account is not an administrator")
    token = result["access_token"]
    expiry = token_expiry(token)
    if expiry <= time.time():
        raise ValueError("Server returned an expired token")
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".aatm-token-", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"access_token": token, "expires_at": expiry}, stream)
        os.replace(name, target)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    print(f"Token saved to {target}; expires at Unix time {expiry}.")
    if result.get("user", {}).get("must_change_password"):
        print("Password change required at next login.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("email")
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--token-file", type=Path, default=Path.home() / ".config/aatm/admin-token.json")
    args = parser.parse_args()
    if not sys.stdin.isatty():
        print("A terminal is required for hidden password entry.", file=sys.stderr)
        return 1
    try:
        login(args.email, args.url, args.token_file)
    except (ValueError, KeyError, IndexError, OSError, urllib.error.URLError) as exc:
        print(f"Login failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
