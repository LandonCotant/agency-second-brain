#!/usr/bin/env python3
"""Static repo guard: no Gmail send/modify code paths in the Brain source tree.

PRD §4.7: agents have draft-only Gmail access. ADR 0027 records that the
original DWD scope set was ``gmail.compose`` only, which permits
``users.drafts.create`` but not ``users.messages.send`` or
``users.messages.modify``.

ADR 0047 expanded the DWD allowlist to ``{gmail.compose, calendar.readonly,
gmail.readonly, gmail.modify}`` so the CRM Auto-updater can apply the
``secondbrain-processed`` label after processing a message. That call
ONLY ever needs ``addLabelIds`` — never ``removeLabelIds``, never
``users.messages.send``. To enforce that narrower invariant without
gutting the static check, this script now:

- Forbids ``.messages().send(`` everywhere (no exemption).
- Forbids the literals ``gmail.send`` / ``gmail.modify`` as bare code
  references everywhere.
- Permits ``.messages().modify(...)`` **only** in files listed in
  ``_ALLOWED_MODIFY_PATHS``, **only** when the call's ``body=`` kwarg
  resolves to a literal dict whose keys are a subset of
  ``_ALLOWED_BODY_KEYS = {"addLabelIds"}``. Anywhere else, or with any
  other body shape (``removeLabelIds`` present, non-literal body, etc.)
  the call is a violation.

Detection mechanics:

- ``.messages().send`` + scope literals: still regex on
  comment/string-stripped source. No legitimate exemption, no
  arg-inspection needed.
- ``.messages().modify``: AST walk so the body kwarg's shape can be
  inspected. Single-function-scope local assignments to a literal dict
  are followed (covers the common ``body = {"addLabelIds": [...]}``
  pattern preceding the call).

The AST walker is intentionally pessimistic:

- Body that's an attribute access, function-call result, comprehension,
  ``**unpack``, or any non-literal expression → violation. The intent
  is "prove safe at the call site, not at runtime".
- ``.messages()`` itself must be called with no args. Anything else is
  not the googleapiclient shape and is left to the regex check (which
  won't fire on a different shape — flagged here as an "unknown shape"
  violation only when the file is allowlisted, because the allowlisted
  file's contract is exactly the discovery client's chained calls).

Usage:
    python scripts/drafts_static_check.py [root]   # default: repo root

Exit 0 if clean, 1 if violations found.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Files permitted to call ``.messages().modify(...)``, scoped to
# ``addLabelIds``-only body. Each entry is a repo-root-relative path.
# Adding a path here is a load-bearing security decision — every entry
# implicitly inherits ADR 0047's threat-model commitments.
_ALLOWED_MODIFY_PATHS: frozenset[Path] = frozenset(
    {
        Path("src/agency_brain/agents/crm_updater/gmail_client.py"),
    }
)

# The only body keys an allowlisted ``.messages().modify`` call may use.
# ``removeLabelIds`` is intentionally absent — removing system labels is
# a Gmail-write surface that ADR 0047's threat model rules out.
_ALLOWED_BODY_KEYS: frozenset[str] = frozenset({"addLabelIds"})

# Forbidden patterns checked via regex (no legitimate exemption). After
# stripping comments + string literals so a docstring mentioning
# "gmail.send" (e.g., in the audit module's allowlist message) does not
# trip the check.
_FORBIDDEN_REGEX_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"\.messages\s*\(\s*\)\s*\.\s*send\b"),
        "Gmail send call site (.messages().send)",
    ),
    (
        re.compile(r"\bgmail\.send\b"),
        "forbidden DWD scope literal in code",
    ),
    (
        re.compile(r"\bgmail\.modify\b"),
        "forbidden DWD scope literal in code",
    ),
)

# Python under src/ AND scripts/ is in scope: both are exec-worthy and could
# carry a Gmail send/modify call. Other languages (TF, YAML, JSON) and
# tests/docs are deliberately skipped — they describe the policy or
# configure infrastructure that doesn't make Gmail calls.
_SCAN_SUBDIRS = (Path("src") / "agency_brain", Path("scripts"))

# Strip Python triple-quoted strings + line comments before pattern match.
_TRIPLE_STRING_RE = re.compile(r'(""".*?"""|\'\'\'.*?\'\'\')', re.DOTALL)
_LINE_COMMENT_RE = re.compile(r"#[^\n]*")
_DOUBLE_QUOTED_RE = re.compile(r'"(?:\\.|[^"\\])*"')
_SINGLE_QUOTED_RE = re.compile(r"'(?:\\.|[^'\\])*'")


def _strip_comments_and_strings(src: str) -> str:
    """Remove Python comments and string literals so we only match real code.

    Order matters: triple-quoted first (they can contain # and quotes), then
    line comments, then single-line strings. Replace with whitespace of equal
    length to preserve line numbers in any future diagnostics.
    """
    src = _TRIPLE_STRING_RE.sub(lambda m: " " * len(m.group(0)), src)
    src = _LINE_COMMENT_RE.sub(lambda m: " " * len(m.group(0)), src)
    src = _DOUBLE_QUOTED_RE.sub(lambda m: " " * len(m.group(0)), src)
    src = _SINGLE_QUOTED_RE.sub(lambda m: " " * len(m.group(0)), src)
    return src


# ---------------------------------------------------------------------------
# AST-based ``.messages().modify`` detection
# ---------------------------------------------------------------------------


def _is_messages_modify_call(node: ast.Call) -> bool:
    """Return True if ``node`` is ``<expr>.messages().modify(...)``.

    Specifically: ``node.func`` is an ``Attribute(attr='modify')`` whose
    ``.value`` is a zero-arg ``Call`` to an ``Attribute(attr='messages')``.
    Matches the googleapiclient discovery shape exactly; an unusual shape
    (e.g. ``.modify(...)`` called on a non-``.messages()`` builder) is
    deliberately NOT flagged here — the regex pass picks up
    ``gmail.modify`` literals separately.
    """
    func = node.func
    if not isinstance(func, ast.Attribute) or func.attr != "modify":
        return False
    inner = func.value
    if not isinstance(inner, ast.Call):
        return False
    inner_func = inner.func
    if not isinstance(inner_func, ast.Attribute) or inner_func.attr != "messages":
        return False
    if inner.args or inner.keywords:
        return False
    return True


def _dict_string_keys(node: ast.expr | None) -> frozenset[str] | None:
    """Extract string keys from a literal ``ast.Dict``.

    Returns the frozenset of string keys, or ``None`` when the node is
    not a pure dict literal (None target = ``**spread``, computed key,
    non-string key, etc.). ``None`` is treated as a violation by the
    caller — we refuse to reason about non-literal bodies.
    """
    if not isinstance(node, ast.Dict):
        return None
    keys: set[str] = set()
    for k in node.keys:
        if k is None:  # **{...} spread
            return None
        if not isinstance(k, ast.Constant) or not isinstance(k.value, str):
            return None
        keys.add(k.value)
    return frozenset(keys)


def _body_kwarg(call: ast.Call) -> ast.expr | None:
    """Return the AST node for the ``body=`` kwarg of ``call``, or None
    if absent. Positional args are intentionally not consulted — the
    googleapiclient discovery shape uses keyword args, and a positional
    body would defeat dict-literal inspection."""
    for kw in call.keywords:
        if kw.arg == "body":
            return kw.value
    return None


class _ModifyCallChecker(ast.NodeVisitor):
    """Walks a parsed module and records every ``.messages().modify``
    call that violates the allowlist rules.

    Scope model: a stack of name→Dict mappings, one frame per
    function/method body. Module-level Dict assignments live in the
    bottom frame. The walker follows ``body=<Name>`` references back to
    the most recent assignment in the **same function scope** — this
    matches the canonical pattern in
    ``crm_updater/gmail_client.py:apply_label`` where ``body`` is
    assigned one line before the ``.modify(...)`` call.

    A reference that doesn't resolve to a literal dict (e.g. ``body =
    some_func()``) is treated as a violation: we cannot prove
    label-add-only at the call site.
    """

    def __init__(self, *, allow_label_add: bool) -> None:
        self._allow = allow_label_add
        self.violations: list[tuple[int, str]] = []
        self._scope_stack: list[dict[str, ast.expr]] = [{}]

    def _scope(self) -> dict[str, ast.expr]:
        return self._scope_stack[-1]

    def _enter_scope(self) -> None:
        self._scope_stack.append({})

    def _exit_scope(self) -> None:
        self._scope_stack.pop()

    # NOTE: each function-scope visit MUST manage its own enter/exit to
    # contain ``body = {...}`` bindings to the function that wrote them.
    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._enter_scope()
        self.generic_visit(node)
        self._exit_scope()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._enter_scope()
        self.generic_visit(node)
        self._exit_scope()

    def visit_Assign(self, node: ast.Assign) -> None:
        if isinstance(node.value, ast.Dict):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    self._scope()[tgt.id] = node.value
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if isinstance(node.target, ast.Name) and isinstance(node.value, ast.Dict):
            self._scope()[node.target.id] = node.value
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if _is_messages_modify_call(node):
            self._record(node)
        self.generic_visit(node)

    def _record(self, node: ast.Call) -> None:
        line = node.lineno
        if not self._allow:
            self.violations.append(
                (
                    line,
                    "Gmail modify call site (.messages().modify) — "
                    "file not in _ALLOWED_MODIFY_PATHS",
                )
            )
            return

        body_node: ast.expr | None = _body_kwarg(node)
        if isinstance(body_node, ast.Name):
            body_node = self._scope().get(body_node.id)

        keys = _dict_string_keys(body_node)
        if keys is None:
            self.violations.append(
                (
                    line,
                    "Gmail modify call site (.messages().modify) — "
                    "body= not a literal dict (or not resolvable in scope); "
                    "cannot prove label-add-only",
                )
            )
            return
        forbidden = keys - _ALLOWED_BODY_KEYS
        if forbidden:
            self.violations.append(
                (
                    line,
                    "Gmail modify call site (.messages().modify) — "
                    f"forbidden body key(s): {', '.join(sorted(forbidden))}; "
                    f"only {{'addLabelIds'}} is allowed",
                )
            )


def _ast_modify_violations(*, py_path: Path, raw: str, allow_label_add: bool) -> list[str]:
    try:
        tree = ast.parse(raw)
    except SyntaxError:
        # A syntactically-broken file is somebody else's lint problem;
        # don't double-report.
        return []
    checker = _ModifyCallChecker(allow_label_add=allow_label_add)
    checker.visit(tree)
    return [f"{py_path}: line {ln}: {msg}" for ln, msg in checker.violations]


# ---------------------------------------------------------------------------
# Public API — preserved signatures
# ---------------------------------------------------------------------------


def file_violations(py_path: Path, *, repo_root: Path | None = None) -> list[str]:
    """Return the list of policy violations in a single Python file.

    The optional ``repo_root`` kwarg is used to resolve the path against
    ``_ALLOWED_MODIFY_PATHS``. When unset, no file is allowlisted —
    safer-default behavior preserved for callers that don't supply a
    root (e.g., the existing test that passes a single path).
    """
    raw = py_path.read_text(encoding="utf-8", errors="replace")
    stripped = _strip_comments_and_strings(raw)
    out: list[str] = []
    for pattern, label in _FORBIDDEN_REGEX_PATTERNS:
        if pattern.search(stripped):
            out.append(f"{py_path}: {label}")

    allow = _is_allowlisted_modify_path(py_path=py_path, repo_root=repo_root)
    out.extend(_ast_modify_violations(py_path=py_path, raw=raw, allow_label_add=allow))
    return out


def _is_allowlisted_modify_path(*, py_path: Path, repo_root: Path | None) -> bool:
    if repo_root is None:
        return False
    try:
        rel = py_path.resolve().relative_to(repo_root.resolve())
    except ValueError:
        return False
    return rel in _ALLOWED_MODIFY_PATHS


def find_violations(root: Path) -> list[str]:
    violations: list[str] = []
    for subdir in _SCAN_SUBDIRS:
        target = root / subdir
        if not target.exists():
            continue
        for py in target.rglob("*.py"):
            violations.extend(file_violations(py, repo_root=root))
    return violations


def main(argv: list[str]) -> int:
    root = Path(argv[1]).resolve() if len(argv) > 1 else REPO_ROOT
    violations = find_violations(root)
    if violations:
        print("drafts_static_check FAILED (PRD §4.7 / ADR 0027 / ADR 0047):")
        for v in violations:
            print(f"  - {v}")
        return 1
    print("drafts_static_check passed (no Gmail send/modify policy violations).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
