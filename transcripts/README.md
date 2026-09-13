# Transcripts

The brief requires the actual AI assistant sessions, not a summary. This was built in
**one Claude Code session** (Opus). There is no other session and no other tool.

## The export step — this needs your hands

1. In the Claude Code session, run:

   ```
   /export
   ```

   Save the result here as `transcripts/claude-code-session.md`.
   (Alternatively copy the session log out of
   `~/.claude/projects/<project>/<session-id>.jsonl`.)

2. **Check it for the API key before sending anything:**

   ```bash
   python tools/check_transcript.py transcripts/
   ```

   It reports file and line for anything key-shaped and exits non-zero. It does not
   edit the file — replace each value with `[REDACTED]` yourself and run it again.
   `.env` was read during the session, so this matters.

3. Read it for anything personal and unrelated. The brief says you may cut that, as
   long as you say you did.

## What the session contains

Reading the brief, the API reference and the policy; two blocking questions raised
before any code was written; the implementation; the test suite; seven sweeps and the
defects each surfaced; and a corrective audit that reversed two policy readings I had
recorded as settled, found a live double-payment path, and reduced the system's
executed actions from nine to one. Section 9 of `DECISIONS.md` says where the assistant
was overridden, where it was left to run, and what it got wrong.

The audit is the part worth reading. The first version of this system paid £855.00 and
re-booked five passengers on authority it did not have, and every automated check
passed.
