"""Regenerate AUTHORS.md from the git history.

Contributors are the commit authors and the people named in
``Co-authored-by:`` trailers, normalised through ``.mailmap``. Bots and
automated tools are left out. Only names and GitHub handles are written;
e-mail addresses never appear in the output.

Usage:
    python scripts/update_authors.py           # rewrite AUTHORS.md
    python scripts/update_authors.py --check   # exit 1 if AUTHORS.md is stale
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AUTHORS_FILE = ROOT / "AUTHORS.md"

ORIGINAL_AUTHOR_HANDLE = "corneel27"

EXCLUDED_PATTERNS = (
    re.compile(r"\[bot\]", re.IGNORECASE),
    re.compile(r"^github-actions$", re.IGNORECASE),
    re.compile(r"^copilot\b", re.IGNORECASE),
    re.compile(r"^claude\b", re.IGNORECASE),
)
EXCLUDED_EMAILS = {"noreply@anthropic.com", "noreply@github.com"}

NOREPLY_RE = re.compile(
    r"^(?:\d+\+)?(?P<login>[^@]+)@users\.noreply\.github\.com$", re.IGNORECASE
)
IDENTITY_RE = re.compile(r"^\s*(?P<name>.*?)\s*<(?P<email>[^>]*)>\s*$")

HEADER = """# Authors

Day Ahead Optimizer was created by **Cees van Beek** (@corneel27), who
maintains the upstream project at https://github.com/corneel27/day-ahead.

Each contributor keeps the copyright of their own contributions, which are
licensed under the Apache License 2.0 (see LICENSE.txt and NOTICE).

## Contributors

This list is generated from the git history (commit authors and
`Co-authored-by` trailers) by `scripts/update_authors.py`; run it to refresh
the list. Identities are merged through `.mailmap`. To change how you are
listed, add a `.mailmap` entry in a pull request.

"""


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout


def _raw_identities(rev: str) -> list[str]:
    log = _git(
        "log",
        "--format=%an <%ae>%n%(trailers:key=Co-authored-by,valueonly)",
        rev,
    )
    return sorted({line.strip() for line in log.splitlines() if line.strip()})


def _normalise(identities: list[str]) -> list[tuple[str, str]]:
    if not identities:
        return []
    mapped = _git("check-mailmap", *identities).splitlines()
    result = []
    for line in mapped:
        match = IDENTITY_RE.match(line)
        if match:
            result.append((match["name"], match["email"]))
    return result


def _is_excluded(name: str, email: str) -> bool:
    if email.lower() in EXCLUDED_EMAILS:
        return True
    return any(pattern.search(name) for pattern in EXCLUDED_PATTERNS)


def collect_contributors(rev: str = "HEAD") -> list[str]:
    """Return the formatted contributor lines, sorted case-insensitively."""
    entries: dict[str, str] = {}
    for name, email in _normalise(_raw_identities(rev)):
        if not name or _is_excluded(name, email):
            continue
        login_match = NOREPLY_RE.match(email)
        login = login_match["login"] if login_match else None
        if login and login.lower() == ORIGINAL_AUTHOR_HANDLE:
            continue
        if login and login.lower() == name.lower():
            label = f"@{login}"
        elif login:
            label = f"{name} (@{login})"
        else:
            label = name
        key = (login or name).lower()
        entries.setdefault(key, label)
    return sorted(entries.values(), key=lambda label: label.lstrip("@").lower())


def render(contributors: list[str]) -> str:
    return HEADER + "".join(f"- {label}\n" for label in contributors)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="only check whether AUTHORS.md is up to date",
    )
    parser.add_argument(
        "--rev",
        default="HEAD",
        help="revision whose history is scanned (default: HEAD)",
    )
    args = parser.parse_args()

    content = render(collect_contributors(args.rev))
    current = AUTHORS_FILE.read_text(encoding="utf-8") if AUTHORS_FILE.exists() else ""
    if args.check:
        if current != content:
            print(
                "AUTHORS.md is out of date; run python scripts/update_authors.py",
                file=sys.stderr,
            )
            return 1
        return 0
    if current != content:
        AUTHORS_FILE.write_text(content, encoding="utf-8")
        print(f"Updated {AUTHORS_FILE.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
