#!/usr/bin/env python3
"""Check an exported assistant transcript before it is sent anywhere.

    python tools/check_transcript.py transcripts/

The brief requires the transcripts and warns that the API key will be in the
scrollback. This reports **where** a secret is and **what kind** it is. It never
prints the value, because a tool that echoes secrets to a terminal or a CI log has
moved the problem rather than solved it.

It does not edit anything either. Silently rewriting a transcript would remove
substantive development history along with the secret, and the brief asks for the real
session.

Exit code 0 if clean, 1 if anything needs attention or nothing was found to check.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# Documentation writes `X-Ops-Key: <key>` and shell examples write `KEY=$OPS_KEY`.
# Flagging those sends a reviewer hunting for a secret that was never there, which is
# how a checker teaches people to ignore it. Anything that is plainly a stand-in is
# skipped. Anything that merely looks harmless is not.
PLACEHOLDER = (
    r"(?!<)"                     # <key>, <your-token>
    r"(?!\$)"                    # $OPS_KEY, ${OPS_KEY}
    r"(?!\.\.\.)"                # ...
    r"(?!\*)"                    # ***
    r"(?!x{3,}\b)"               # xxxxx
    r"(?!\[redacted\])"
    r"(?!\[REDACTED\])"
    r"(?!your[-_]?(?:key|token|secret|password)\b)"
    r"(?!YOUR[-_]?(?:KEY|TOKEN|SECRET|PASSWORD)\b)"
    r"(?!changeme\b)(?!CHANGEME\b)(?!placeholder\b)"
)

# (pattern, what it is). Deliberately broader than one vendor prefix: a transcript of
# this build touches .env, request headers and shell environment lines.
PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"sk-[A-Za-z0-9_\-]{16,}"), "an OpenAI-style key (sk-...)"),
    (re.compile(r"sk-proj-[A-Za-z0-9_\-]{8,}"), "an OpenAI project key (sk-proj-...)"),
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"), "an Anthropic-style key (sk-ant-...)"),
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"), "a GitHub token"),
    (re.compile(r"AKIA[0-9A-Z]{12,}"), "an AWS access key id"),
    (re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\."), "a JWT"),
    # Environment assignments: any *_KEY / *_TOKEN / *_SECRET / *_PASSWORD with a value.
    (
        re.compile(
            r"(?i)\b[A-Z0-9_]*(?:API_KEY|_TOKEN|_SECRET|PASSWORD)\s*[=:]\s*"
            r"(?!\s*$)(?!\"\")(?!'')" + PLACEHOLDER + r"\S+"
        ),
        "an environment assignment carrying a value",
    ),
    # Request headers.
    (
        # The scheme word sits between the colon and the value, so the placeholder
        # test has to skip past it or `Bearer <token>` reads as a live credential.
        # The scheme word sits between the colon and the value, so the placeholder test
        # has to skip past it or `Bearer <token>` reads as a live credential. The
        # second lookahead stops the optional group from backtracking and offering the
        # scheme word itself up as the value.
        re.compile(
            r"(?i)\bauthorization\s*:\s*(?:(?:bearer|token|basic|apikey)\s+)?"
            r"(?!bearer\b)(?!token\b)(?!basic\b)(?!apikey\b)"
            + PLACEHOLDER
            + r"\S+"
        ),
        "an Authorization header with a value",
    ),
    (
        re.compile(r"(?i)\bx-ops-key\s*:\s*" + PLACEHOLDER + r"\S+"),
        "an X-Ops-Key header with a value",
    ),
    (
        re.compile(r"(?i)\b(?:api[-_ ]?key|bearer)\s*[:=]\s*(?!\[redacted\])[A-Za-z0-9_\-]{12,}"),
        "an inline credential",
    ),
]

TEXT_SUFFIXES = {".md", ".txt", ".jsonl", ".json", ".html", ".log"}
SKIP_NAMES = {"README.md"}


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 1
    target = Path(argv[1])
    if target.is_dir():
        files = [
            p
            for p in sorted(target.rglob("*"))
            if p.is_file() and p.suffix.lower() in TEXT_SUFFIXES and p.name not in SKIP_NAMES
        ]
    else:
        files = [target]

    if not files:
        print(
            "No transcript found in {}.\n\n"
            "The brief requires the actual session, not a summary. Run /export in the "
            "Claude Code session, save it as transcripts/claude-code-session.md, then "
            "run this again.".format(target)
        )
        return 1

    findings: list[tuple[Path, int, str]] = []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            print("could not read {}: {}".format(path, exc))
            return 1
        for number, line in enumerate(text.splitlines(), start=1):
            seen: set[str] = set()
            for pattern, what in PATTERNS:
                if pattern.search(line) and what not in seen:
                    seen.add(what)
                    findings.append((path, number, what))

    print("Checked {} file(s):".format(len(files)))
    for path in files:
        print("  {}  ({:,} bytes)".format(path, path.stat().st_size))
    print()

    if findings:
        print("{} location(s) need redacting. Values are not shown.".format(len(findings)))
        for path, number, what in findings:
            print("  {}:{}  {}".format(path, number, what))
        print()
        print(
            "Replace each value with [REDACTED] and run this again. Do not delete the "
            "surrounding discussion -- the brief asks for the real session, and "
            "removing substantive history to hide a key loses both."
        )
        return 1

    print("No key-shaped text, environment assignment or credential header found.")
    print(
        "This checks for secrets only. Read it yourself for anything genuinely "
        "personal and unrelated, which the brief says you may cut as long as you say "
        "you did."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
