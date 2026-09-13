"""The redaction checker itself.

The brief warns the API key will be in the scrollback. A checker that echoes the
value it found has moved the problem rather than solved it, so that is the property
these tests actually pin.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "check_transcript",
    Path(__file__).resolve().parent.parent / "tools" / "check_transcript.py",
)
checker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checker)

SECRETS = {
    "openai": "sk-proj-AAAABBBBCCCCDDDDEEEEFFFF",
    "ops_header": "X-Ops-Key: aerlink-ops-local-key",
    "bearer": "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sig",
    "github": "ghp_AAAABBBBCCCCDDDDEEEEFFFFGGGGHHHH11",
    "aws": "AKIAIOSFODNN7EXAMPLE",
    "env": "SOME_SERVICE_TOKEN=abcdef0123456789",
}


def _run(tmp_path, body, capsys):
    path = tmp_path / "session.md"
    path.write_text(body, encoding="utf-8")
    code = checker.main(["check_transcript.py", str(tmp_path)])
    return code, capsys.readouterr().out


def test_it_never_prints_the_value_it_found(tmp_path, capsys):
    """The property that matters most."""
    body = "\n".join(SECRETS.values())
    code, out = _run(tmp_path, body, capsys)
    assert code == 1
    for label, secret in SECRETS.items():
        value = secret.split(":", 1)[-1].split("=", 1)[-1].strip()
        assert value not in out, "{} leaked into the checker's own output".format(label)


def test_it_reports_a_location_and_a_type_for_each_finding(tmp_path, capsys):
    code, out = _run(tmp_path, "line one is fine\n" + SECRETS["openai"] + "\n", capsys)
    assert code == 1
    assert "session.md:2" in out
    assert "OpenAI" in out


@pytest.mark.parametrize("label", sorted(SECRETS))
def test_each_secret_shape_is_detected(tmp_path, capsys, label):
    """More than one key prefix, per the brief: headers and env assignments too."""
    code, _out = _run(tmp_path, "context\n" + SECRETS[label] + "\ncontext\n", capsys)
    assert code == 1, "{} was not detected".format(label)


def test_a_redacted_transcript_passes(tmp_path, capsys):
    body = (
        "We discussed S12.1 authority at length.\n"
        "OPENAI_API_KEY=[REDACTED]\n"
        "Authorization: [REDACTED]\n"
        "The re-routing fare question is in DECISIONS.md section 2.\n"
    )
    code, out = _run(tmp_path, body, capsys)
    assert code == 0
    assert "No key-shaped text" in out


def test_ordinary_development_discussion_is_not_flagged(tmp_path, capsys):
    """It must not be so eager that it pushes someone into deleting real history."""
    body = (
        "The policy preamble says the document is authoritative on what a\n"
        "representative may do. API.md S3 defines fare_gbp as the additional fare\n"
        "payable. Compensation is not listed in S12.1 at any level.\n"
    )
    code, out = _run(tmp_path, body, capsys)
    assert code == 0


def test_a_missing_transcript_is_reported_rather_than_passing_silently(tmp_path, capsys):
    code = checker.main(["check_transcript.py", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 1
    assert "No transcript found" in out


def test_documentation_placeholders_are_not_reported_as_secrets(tmp_path, capsys):
    """`API.md` documents `X-Ops-Key: <key>`. Flagging that sends a reviewer hunting
    for a secret that was never there, which is how a checker teaches people to ignore
    it -- and this one is meant to be read."""
    body = (
        "Authentication: every endpoint requires the header `X-Ops-Key: <key>`.\n"
        "Authorization: Bearer <token>\n"
        "Authorization: Bearer [REDACTED]\n"
        "export OPS_API_KEY=$OPS_KEY\n"
        "OPENAI_API_KEY=YOUR_KEY\n"
    )
    code, out = _run(tmp_path, body, capsys)
    assert code == 0, out
    assert "No key-shaped text" in out


def test_a_real_value_behind_a_scheme_word_is_still_caught(tmp_path, capsys):
    """Skipping past `Bearer` must not become skipping the value after it."""
    body = "Authorization: Bearer sk-proj-abcdefghijklmnopqrst\n"
    code, out = _run(tmp_path, body, capsys)
    assert code == 1
    assert "need redacting" in out
    assert "sk-proj-abcdefghijklmnopqrst" not in out, "never print the value"


def test_a_bare_scheme_word_is_not_mistaken_for_a_value(tmp_path, capsys):
    """The optional scheme group used to backtrack and offer up `Bearer` itself."""
    body = "Authorization: Bearer <token>\n"
    code, _ = _run(tmp_path, body, capsys)
    assert code == 0
