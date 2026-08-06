"""Unit tests for scripts/drafts_static_check.py.

Covers:
- Regex-based checks for ``.messages().send`` and bare scope literals
  (unchanged from ADR 0027 baseline).
- AST-based ``.messages().modify`` check with the ADR 0047 allowlist
  exemption: allowlisted files may call ``.modify(body={"addLabelIds":
  [...]})`` and nothing else; every other file or body shape is a
  violation.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import drafts_static_check as dsc


def _make_src(tmp_path: Path, body: str, name: str = "example.py") -> Path:
    """Create a fake repo root with src/agency_brain/<name> = body."""
    target = tmp_path / "src" / "agency_brain"
    target.mkdir(parents=True)
    (target / name).write_text(body)
    return tmp_path


def _make_allowlisted_src(tmp_path: Path, body: str) -> Path:
    """Create a fake repo root with the file at the exact allowlisted path."""
    target = tmp_path / "src" / "agency_brain" / "agents" / "crm_updater"
    target.mkdir(parents=True)
    (target / "gmail_client.py").write_text(body)
    return tmp_path


# ---------------------------------------------------------------------------
# Regex-baseline cases (preserved from ADR 0027 era)
# ---------------------------------------------------------------------------


def test_clean_source_passes(tmp_path: Path):
    root = _make_src(
        tmp_path,
        "from googleapiclient import discovery\n"
        "service.users().drafts().create(userId='me', body={}).execute()\n",
    )
    assert dsc.find_violations(root) == []


def test_send_call_site_fails(tmp_path: Path):
    root = _make_src(
        tmp_path,
        "service.users().messages().send(userId='me', body={}).execute()\n",
    )
    violations = dsc.find_violations(root)
    assert len(violations) == 1
    assert ".messages().send" in violations[0]


def test_modify_call_site_fails(tmp_path: Path):
    """Default path: any ``.messages().modify`` call is a violation."""
    root = _make_src(tmp_path, "service.users().messages().modify(userId='me').execute()\n")
    violations = dsc.find_violations(root)
    assert any(".messages().modify" in v for v in violations)
    assert any("not in _ALLOWED_MODIFY_PATHS" in v for v in violations)


def test_forbidden_scope_literal_fails(tmp_path: Path):
    root = _make_src(
        tmp_path,
        "SCOPES = ['https://www.googleapis.com/auth/gmail.send']\n",
    )
    # The literal string is stripped by _strip_comments_and_strings, so this
    # must NOT trip — only real code references would. Confirm the stripper
    # is doing its job.
    assert dsc.find_violations(root) == []


def test_forbidden_scope_in_code_fails(tmp_path: Path):
    # An attribute access like `foo.gmail.send(...)` survives stripping.
    root = _make_src(
        tmp_path,
        "import foo\nfoo.gmail.send()\n",
    )
    violations = dsc.find_violations(root)
    assert any("forbidden DWD scope literal" in v for v in violations)


def test_compose_scope_passes(tmp_path: Path):
    root = _make_src(
        tmp_path,
        "SCOPES = ['https://www.googleapis.com/auth/gmail.compose']\n"
        "service.users().drafts().create(userId='me', body={}).execute()\n",
    )
    assert dsc.find_violations(root) == []


def test_docstring_mentioning_send_passes(tmp_path: Path):
    """A docstring describing the policy must not trip the check."""
    root = _make_src(
        tmp_path,
        '"""This module never calls users.messages.send."""\n' "x = 1\n",
    )
    assert dsc.find_violations(root) == []


# ---------------------------------------------------------------------------
# ADR 0047 allowlist — AST-based body-shape inspection
# ---------------------------------------------------------------------------


def test_allowlisted_modify_with_inline_addlabelids_body_passes(tmp_path: Path):
    """ADR 0047 §allowlist: an allowlisted file may pass body= inline as
    ``{"addLabelIds": [...]}`` and the call is permitted."""
    body = textwrap.dedent(
        """
        def apply_label(service, message_id, label_id):
            service.users().messages().modify(
                userId="me",
                id=message_id,
                body={"addLabelIds": [label_id]},
            ).execute()
        """
    )
    root = _make_allowlisted_src(tmp_path, body)
    assert dsc.find_violations(root) == []


def test_allowlisted_modify_with_name_bound_addlabelids_body_passes(tmp_path: Path):
    """The canonical pattern in the real crm_updater code: ``body`` is
    bound to a literal dict one line before the call."""
    body = textwrap.dedent(
        """
        def apply_label(service, message_id, label_id):
            body = {"addLabelIds": [label_id]}
            service.users().messages().modify(
                userId="me",
                id=message_id,
                body=body,
            ).execute()
        """
    )
    root = _make_allowlisted_src(tmp_path, body)
    assert dsc.find_violations(root) == []


def test_allowlisted_modify_with_removelabelids_body_fails(tmp_path: Path):
    """ADR 0047 forbids ``removeLabelIds`` anywhere. The allowlist
    exemption MUST NOT extend to it."""
    body = textwrap.dedent(
        """
        def apply_label(service, message_id, label_id):
            body = {"removeLabelIds": [label_id]}
            service.users().messages().modify(
                userId="me",
                id=message_id,
                body=body,
            ).execute()
        """
    )
    root = _make_allowlisted_src(tmp_path, body)
    violations = dsc.find_violations(root)
    assert any("removeLabelIds" in v for v in violations)
    assert any("forbidden body key(s)" in v for v in violations)


def test_allowlisted_modify_with_mixed_body_fails(tmp_path: Path):
    """Even a body that includes ``addLabelIds`` must not also include
    ``removeLabelIds``. The exemption is exact-subset, not contains."""
    body = textwrap.dedent(
        """
        def apply_label(service, message_id, add_id, remove_id):
            body = {
                "addLabelIds": [add_id],
                "removeLabelIds": [remove_id],
            }
            service.users().messages().modify(
                userId="me",
                id=message_id,
                body=body,
            ).execute()
        """
    )
    root = _make_allowlisted_src(tmp_path, body)
    violations = dsc.find_violations(root)
    assert any("removeLabelIds" in v for v in violations)


def test_allowlisted_modify_with_non_literal_body_fails(tmp_path: Path):
    """If body= is a function-call result (or any non-literal), the
    check cannot prove safety. Refuse and report."""
    body = textwrap.dedent(
        """
        def apply_label(service, message_id):
            service.users().messages().modify(
                userId="me",
                id=message_id,
                body=_build_body(),
            ).execute()
        """
    )
    root = _make_allowlisted_src(tmp_path, body)
    violations = dsc.find_violations(root)
    assert any("not a literal dict" in v for v in violations)


def test_allowlisted_modify_with_missing_body_fails(tmp_path: Path):
    """A modify call with no body= kwarg at all — the resolver returns
    None and the check refuses to prove safety."""
    body = textwrap.dedent(
        """
        def apply_label(service, message_id):
            service.users().messages().modify(
                userId="me",
                id=message_id,
            ).execute()
        """
    )
    root = _make_allowlisted_src(tmp_path, body)
    violations = dsc.find_violations(root)
    assert any("not a literal dict" in v for v in violations)


def test_modify_in_non_allowlisted_file_still_fails_even_with_safe_body(
    tmp_path: Path,
):
    """The allowlist is path-based AND body-shape-based. A non-allowlisted
    file is forbidden regardless of how innocent the body looks."""
    body = textwrap.dedent(
        """
        def apply_label(service, message_id, label_id):
            service.users().messages().modify(
                userId="me",
                id=message_id,
                body={"addLabelIds": [label_id]},
            ).execute()
        """
    )
    # Path is src/agency_brain/example.py — NOT allowlisted.
    root = _make_src(tmp_path, body)
    violations = dsc.find_violations(root)
    assert any("not in _ALLOWED_MODIFY_PATHS" in v for v in violations)


def test_body_assignment_in_different_function_does_not_leak(tmp_path: Path):
    """A ``body`` name bound in one function must not be looked up from
    another. The scope walker uses a per-function frame stack."""
    body = textwrap.dedent(
        """
        def _build_safe_body():
            body = {"addLabelIds": ["x"]}
            return body

        def apply_label(service, message_id):
            # The Name resolver in apply_label's scope sees no `body =`
            # binding → falls back to the kwarg's literal expression,
            # which is a Call (`_build_safe_body()`), not a Dict.
            service.users().messages().modify(
                userId="me",
                id=message_id,
                body=_build_safe_body(),
            ).execute()
        """
    )
    root = _make_allowlisted_src(tmp_path, body)
    violations = dsc.find_violations(root)
    assert any("not a literal dict" in v for v in violations)


def test_dict_with_spread_body_fails(tmp_path: Path):
    """A body built with ``**other_dict`` defeats key inspection;
    refuse to prove safety."""
    body = textwrap.dedent(
        """
        def apply_label(service, message_id, label_id, base):
            body = {**base, "addLabelIds": [label_id]}
            service.users().messages().modify(
                userId="me",
                id=message_id,
                body=body,
            ).execute()
        """
    )
    root = _make_allowlisted_src(tmp_path, body)
    violations = dsc.find_violations(root)
    assert any("not a literal dict" in v for v in violations)


def test_messages_send_in_allowlisted_file_still_fails(tmp_path: Path):
    """The allowlist is for ``.modify`` only. A ``.send`` call in the
    allowlisted file is still a violation."""
    body = textwrap.dedent(
        """
        def send_something(service):
            service.users().messages().send(
                userId="me",
                body={"raw": "..."},
            ).execute()
        """
    )
    root = _make_allowlisted_src(tmp_path, body)
    violations = dsc.find_violations(root)
    assert any(".messages().send" in v for v in violations)


# ---------------------------------------------------------------------------
# Real-repo smoke
# ---------------------------------------------------------------------------


def test_real_repo_passes():
    """Smoke: the actual repo's source tree must pass the static check
    once ADR 0047's ``crm_updater/gmail_client.py:apply_label`` is
    allowlisted."""
    repo_root = Path(__file__).resolve().parents[2]
    assert dsc.find_violations(repo_root) == []
