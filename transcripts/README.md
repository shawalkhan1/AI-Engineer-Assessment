# Actual assistant sessions

This folder contains the actual Claude Code build session and Codex adversarial audit sessions, including delegated reviews and interrupted/resumed work. They are JSONL exports, not summaries.

`export-manifest.json` records every source filename, exported record count, credential replacements, checksum and snapshot cutoff. Only credentials and matching test credentials were replaced with `[REDACTED]`; no personal sections or substantive conversation were cut. Original logs were not modified.

The current conversation can continue after a snapshot. Immediately before submission, run:

```sh
python tools/export_transcripts.py
python tools/check_transcript.py transcripts/
```

The scanner decodes JSONL text rather than treating escaped newlines or source-code variable references as keys. It also scans known token shapes and credential assignments without printing their values. Section 9 of DECISIONS.md explains the roles of the assistants.
