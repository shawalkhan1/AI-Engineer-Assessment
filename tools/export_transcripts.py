#!/usr/bin/env python3
"""Copy actual project sessions, redacting credentials while preserving every record.

Run ``python tools/export_transcripts.py`` again after the final assistant turn.
Only sessions whose recorded working directory belongs to this workspace are copied.
Source logs are never modified. The manifest makes the export cutoff explicit.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TOKEN_PATTERNS = [
    re.compile(r"sk-(?:proj-|ant-)?[A-Za-z0-9_-]{8,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{12,}"),
    re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+"),
]


def normalise(path: str) -> str:
    return path.replace("\\", "/").rstrip("/").casefold()


def in_workspace(cwd: str, workspace: Path) -> bool:
    actual, expected = normalise(cwd), normalise(str(workspace))
    return actual == expected or actual.startswith(expected + "/")


def session_belongs(path: Path, workspace: Path) -> bool:
    # Claude mode/queue records can precede the first event with a cwd.
    with path.open(encoding="utf-8") as stream:
        for index, line in enumerate(stream):
            if index > 1000:
                return False
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = row.get("payload", row)
            if isinstance(payload, dict) and isinstance(payload.get("cwd"), str):
                return in_workspace(payload["cwd"], workspace)
    return False


def discover(home: Path, workspace: Path) -> list[tuple[str, Path]]:
    found = []
    # Project-scoped Claude paths avoid touching unrelated sessions unnecessarily.
    claude_name = re.sub(r"[^A-Za-z0-9]", "-", str(workspace))
    claude = home / ".claude" / "projects" / claude_name
    for path in sorted(claude.rglob("*.jsonl")) if claude.is_dir() else []:
        if session_belongs(path, workspace):
            found.append(("claude-code", path))
    codex = home / ".codex" / "sessions"
    for path in sorted(codex.rglob("*.jsonl")) if codex.is_dir() else []:
        if session_belongs(path, workspace):
            found.append(("codex", path))
    return found


def known_secrets(root: Path) -> set[str]:
    secrets = set()
    for path in (root / ".env", root / ".env.example"):
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            key, sep, value = line.partition("=")
            if sep and not key.lstrip().startswith("#") and re.search(r"KEY|TOKEN|SECRET|PASSWORD", key):
                value = value.strip().strip("\"'")
                if len(value) >= 8:
                    secrets.add(value)
    for key, value in os.environ.items():
        if re.search(r"KEY|TOKEN|SECRET|PASSWORD", key) and len(value) >= 8:
            secrets.add(value)
    return secrets


def redact(text: str, secrets: set[str]) -> tuple[str, int]:
    count = 0
    for secret in sorted(secrets, key=len, reverse=True):
        count += text.count(secret)
        text = text.replace(secret, "[REDACTED]")
    for pattern in TOKEN_PATTERNS:
        text, replacements = pattern.subn("[REDACTED]", text)
        count += replacements
    return text, count


def export_session(source: Path, destination: Path, secrets: set[str]) -> dict:
    # Snapshot only complete JSONL records: an active session may be appending a
    # record while this export runs. It is picked up by the next invocation.
    raw = source.read_bytes()
    end = raw.rfind(b"\n") + 1
    snapshot = raw[:end].decode("utf-8")
    from tools.check_transcript import credential_matches, text_leaves
    secrets = set(secrets)
    for line in snapshot.splitlines():
        row = json.loads(line)
        for leaf in text_leaves(row):
            for match, _kind in credential_matches(leaf):
                if "value" in match.groupdict():
                    secrets.add(match.group("value"))
    redacted, count = redact(snapshot, secrets)
    for line in redacted.splitlines():
        json.loads(line)
    data = redacted.encode("utf-8")
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_bytes(data)
    temporary.replace(destination)
    return {
        "file": destination.name,
        "source_file": source.name,
        "source_bytes_at_export": len(raw),
        "complete_source_bytes_exported": end,
        "records": len(snapshot.splitlines()),
        "credential_replacements": count,
        "export_sha256": hashlib.sha256(data).hexdigest(),
        "export_bytes": len(data),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=ROOT.parent)
    parser.add_argument("--output", type=Path, default=ROOT / "transcripts")
    args = parser.parse_args(argv)
    sessions = discover(Path.home(), args.workspace.resolve())
    if not sessions:
        print("No project session logs found. Use your assistant's export command.")
        return 1
    args.output.mkdir(parents=True, exist_ok=True)
    secrets = known_secrets(ROOT)
    manifest = {
        "exported_at_utc": datetime.now(timezone.utc).isoformat(),
        "content": "Actual raw session records; no conversation summarisation or personal cuts.",
        "redaction": "Only known credentials and credential-shaped tokens were replaced with [REDACTED], including matching test fixtures.",
        "cutoff": "Active sessions are snapshots through their last complete record; rerun after the final assistant turn to include subsequent messages.",
        "sessions": [],
    }
    for tool, source in sessions:
        destination = args.output / (tool + "-" + source.name)
        entry = export_session(source, destination, secrets)
        entry["tool"] = tool
        manifest["sessions"].append(entry)
        print("Exported {}: {} records, {} credential replacements.".format(
            destination.name, entry["records"], entry["credential_replacements"]))
    (args.output / "export-manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
