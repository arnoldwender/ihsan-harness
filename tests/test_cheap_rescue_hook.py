"""Tests for the live hook, hooks/cheap-rescue-before-run.py.

Same pair as the gate's own suite: the token that buys a green check without
earning it must be WARNED about, and the same intent in its justified form must
pass in silence. The hook is run as a PreToolUse subprocess with the payload on
stdin, inside a small git repository, and the exit code, the stdout JSON and
the receipt are what is asserted.

The hook itself never calls git: the fixture is a repository so it has the
shape of a real working directory, with the same baseline the gate's suite
uses — including `src/legacy.*`, the pre-existing debt the hook must NOT
report when an edit touches another line.

    python3 -m pytest tests/test_cheap_rescue_hook.py -q
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

# The mutation runner points this at a mutated COPY; the real hook is never rewritten.
HOOK = Path(os.environ.get("CHEAP_RESCUE_HOOK_UNDER_TEST")
            or Path(__file__).resolve().parent.parent / "hooks" / "cheap-rescue-before-run.py")

IDENTITY = ("-c", "user.email=gate@example.invalid", "-c", "user.name=Gate Test")

LOG_LINE = '        logger.warning("total failed: %s", exc)\n'

BASELINE = {
    "src/app.ts": textwrap.dedent("""\
        export function total(items: number[]): number {
          return items.reduce((a, b) => a + b, 0);
        }

        export function safeTotal(items: number[]): number {
          try {
            return total(items);
          } catch (e) {
            console.error("total failed", e);
          }
          return 0;
        }
        """),
    "src/app.py": textwrap.dedent("""\
        import logging

        logger = logging.getLogger(__name__)


        def total(items):
            return sum(items)


        def safe_total(items):
            try:
                return total(items)
            except TypeError as exc:
                logger.warning("total failed: %s", exc)
                return 0
        """),
    "src/two.py": textwrap.dedent("""\
        import logging

        logger = logging.getLogger(__name__)


        def first(items):
            try:
                return sum(items)
            except TypeError as exc:
                logger.warning("total failed: %s", exc)


        def second(items):
            try:
                return max(items)
            except TypeError as exc:
                logger.warning("total failed: %s", exc)
        """),
    "src/legacy.ts": "// @ts-ignore\nexport const legacy = untypedThing();\n",
    "src/legacy.py": "value = compute()  # noqa\n",
    ".github/workflows/ci.yml": textwrap.dedent("""\
        name: ci
        on: [push]
        jobs:
          build:
            runs-on: ubuntu-latest
            steps:
              - run: make test
        """),
}


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    for rel, body in BASELINE.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "add", "-A")
    git(tmp_path, *IDENTITY, "commit", "-q", "-m", "baseline")
    return tmp_path


def hook(root: Path, tool: str, given: dict, env: dict[str, str] | None = None
         ) -> tuple[int, dict | None, str, dict | None]:
    receipts = root / "receipts.jsonl"
    payload = {"session_id": "test-session", "cwd": str(root), "hook_event_name": "PreToolUse",
               "tool_name": tool, "tool_input": given, "tool_use_id": "toolu_x"}
    run_env = {**os.environ, "CHEAP_RESCUE_RECEIPTS": str(receipts)}
    run_env.pop("CHEAP_RESCUE_HOOK_MODE", None)
    run_env.update(env or {})
    r = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload),
                       capture_output=True, text=True, env=run_env, cwd=str(root),
                       check=False, timeout=60)
    out = json.loads(r.stdout) if r.stdout.strip() else None
    rec = None
    if receipts.is_file():
        rec = json.loads(receipts.read_text(encoding="utf-8").strip().split("\n")[-1])
    return r.returncode, out, r.stderr, rec


def edit(root: Path, rel: str, old: str, new: str, replace_all: bool = False, **env: str):
    given = {"file_path": str(root / rel), "old_string": old, "new_string": new}
    if replace_all:
        given["replace_all"] = True
    return hook(root, "Edit", given, env or None)


def write(root: Path, rel: str, content: str, **env: str):
    return hook(root, "Write", {"file_path": str(root / rel), "content": content}, env or None)


def bash(root: Path, command: str, **env: str):
    return hook(root, "Bash", {"command": command}, env or None)


def warning(out: dict | None) -> str:
    return ((out or {}).get("hookSpecificOutput") or {}).get("additionalContext") or ""


# --- the control -------------------------------------------------------------

def test_a_harmless_edit_is_silent(repo: Path) -> None:
    """Without this, every test below could pass because the hook always warns."""
    rc, out, _, rec = edit(repo, "src/app.ts", "  return items.reduce((a, b) => a + b, 0);",
                           "  const sum = items.reduce((a, b) => a + b, 0);\n  return sum;")
    assert rc == 0 and out is None, (rc, out)
    assert rec["verdict"] == "ok" and rec["tool"] == "Edit"
    assert rec["judged"] == 2 and rec["simulated"] is True, rec


# --- each check, on the tool where it lands ----------------------------------

def test_a_bare_ts_ignore_in_an_edit_warns(repo: Path) -> None:
    rc, out, _, rec = edit(repo, "src/app.ts", "export function total",
                           '// @ts-ignore\nexport const wrong: number = "x" as any;\nexport function total')
    assert rc == 0
    assert "[ts-suppression]" in warning(out) and "NOT blocked" in warning(out)
    assert out["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    assert "permissionDecision" not in out["hookSpecificOutput"], "the permission flow is not touched"
    assert rec["verdict"] == "finding" and rec["checks"] == ["ts-suppression"]


def test_ts_expect_error_with_a_reason_is_silent(repo: Path) -> None:
    _, out, _, rec = edit(repo, "src/app.ts", "export function total",
                          "// @ts-expect-error the vendor types omit `total`; fixed upstream in v4\n"
                          "export const wrong: number = vendor.total;\nexport function total")
    assert out is None, warning(out)
    assert rec["verdict"] == "ok"


def test_a_blanket_eslint_disable_warns(repo: Path) -> None:
    _, out, _, rec = edit(repo, "src/app.ts", "export function total",
                          '/* eslint-disable */\nexport const noisy = () => console.log("hi");\n'
                          "export function total")
    assert "[eslint-blanket-disable]" in warning(out) and rec["checks"] == ["eslint-blanket-disable"]


def test_an_eslint_disable_naming_its_rule_is_silent(repo: Path) -> None:
    _, out, _, rec = edit(repo, "src/app.ts", "export function total",
                          "// eslint-disable-next-line no-console -- this file is the CLI entry point\n"
                          'export const noisy = () => console.log("hi");\nexport function total')
    assert out is None, warning(out)
    assert rec["verdict"] == "ok"


def test_a_bare_noqa_in_an_edit_warns(repo: Path) -> None:
    _, out, _, rec = edit(repo, "src/app.py", "    return sum(items)", "    return sum(items)  # noqa")
    assert "[bare-noqa]" in warning(out) and rec["checks"] == ["bare-noqa"]


def test_noqa_with_a_code_is_silent(repo: Path) -> None:
    _, out, _, rec = edit(repo, "src/app.py", "    return sum(items)", "    return sum(items)  # noqa: E501")
    assert out is None, warning(out)
    assert rec["verdict"] == "ok"


def test_a_bare_type_ignore_warns_and_a_coded_one_does_not(repo: Path) -> None:
    _, out, _, _ = edit(repo, "src/app.py", "    return sum(items)", "    return sum(items)  # type: ignore")
    assert "[bare-type-ignore]" in warning(out)
    _, out, _, rec = edit(repo, "src/app.py", "    return sum(items)",
                          "    return sum(items)  # type: ignore[arg-type]")
    assert out is None and rec["verdict"] == "ok"


def test_a_pytest_skip_in_an_edit_warns(repo: Path) -> None:
    _, out, _, rec = edit(repo, "src/app.py", "def total(items):",
                          "@pytest.mark.skip\ndef test_totals():\n    assert total([1]) == 1\n\n\ndef total(items):")
    assert "[skipped-test]" in warning(out) and rec["checks"] == ["skipped-test"]


def test_it_skip_in_a_ts_edit_warns(repo: Path) -> None:
    _, out, _, _ = edit(repo, "src/app.ts", "export function total",
                        "it.skip('adds up', () => {});\nexport function total")
    assert "[skipped-test]" in warning(out)


def test_a_conditional_skip_states_its_condition_and_is_silent(repo: Path) -> None:
    _, out, _, rec = edit(repo, "src/app.py", "def total(items):",
                          '@pytest.mark.skipif(sys.platform == "win32", reason="POSIX paths only")\n'
                          "def test_totals():\n    assert total([1]) == 1\n\n\ndef total(items):")
    assert out is None, warning(out)
    assert rec["verdict"] == "ok"


def test_continue_on_error_in_a_written_workflow_warns(repo: Path) -> None:
    body = BASELINE[".github/workflows/ci.yml"] + textwrap.dedent("""\
          lint:
            runs-on: ubuntu-latest
            steps:
              - run: make lint
                continue-on-error: true
        """)
    _, out, _, rec = write(repo, ".github/workflows/ci.yml", body)
    assert "[ci-continue-on-error]" in warning(out) and rec["tool"] == "Write"
    _, out, _, rec = write(repo, ".github/workflows/ci.yml", body.replace("true", "false"))
    assert out is None and rec["verdict"] == "ok"


def test_no_verify_in_a_bash_command_warns(repo: Path) -> None:
    _, out, _, rec = bash(repo, 'git commit --no-verify -m "ship it"')
    assert "[no-verify]" in warning(out)
    assert rec["path"] == "<command>" and rec["judged"] == 1 and rec["tool"] == "Bash"


@pytest.mark.parametrize("flag", ["--force", "-f"])
def test_a_force_push_in_a_bash_command_warns(repo: Path, flag: str) -> None:
    _, out, _, rec = bash(repo, f"git push {flag} origin main")
    assert "[force-push]" in warning(out) and rec["checks"] == ["force-push"]


def test_force_with_lease_is_silent(repo: Path) -> None:
    """The same operation with the accident removed."""
    _, out, _, rec = bash(repo, "git push --force-with-lease origin main")
    assert out is None and rec["verdict"] == "ok"


def test_a_force_that_is_not_a_push_is_silent(repo: Path) -> None:
    _, out, _, rec = bash(repo, "rm -f dist/stale.log && npm install --force")
    assert out is None, warning(out)
    assert rec["verdict"] == "ok"


def test_a_multiline_command_is_judged_line_by_line(repo: Path) -> None:
    _, out, _, rec = bash(repo, "echo start\ngit push --force origin main\necho end")
    assert "<command>:2:" in warning(out) and rec["judged"] == 3


def test_replacing_a_log_with_pass_warns_from_the_simulated_context(repo: Path) -> None:
    """The exact move the rule exists for. `pass` on its own is nothing; `pass` under an
    `except` is the whole finding — and the `except` is in the file, not in the edit."""
    _, out, _, rec = edit(repo, "src/app.py", LOG_LINE + "        return 0\n", "        pass\n")
    assert "[swallowed-error]" in warning(out), warning(out)
    assert rec["checks"] == ["swallowed-error"] and rec["simulated"] is True


def test_a_catch_left_with_only_a_comment_warns_from_the_simulated_context(repo: Path) -> None:
    _, out, _, _ = edit(repo, "src/app.ts", '    console.error("total failed", e);\n',
                        "    /* ignore */\n")
    assert "[swallowed-error]" in warning(out), warning(out)


def test_a_handler_that_logs_is_silent(repo: Path) -> None:
    _, out, _, rec = edit(repo, "src/app.py", "def total(items):", textwrap.dedent("""\
        def widest(items):
            try:
                return max(items)
            except ValueError as exc:
                logger.warning("widest failed: %s", exc)
                return 0


        def total(items):"""))
    assert out is None, warning(out)
    assert rec["verdict"] == "ok"


# --- only the added lines: what you found is not yet your debt ---------------

def test_a_pre_existing_noqa_is_not_reported_when_the_edit_touches_another_line(repo: Path) -> None:
    """The context an Edit repeats in `new_string` is not an added line."""
    _, out, _, rec = edit(repo, "src/legacy.py", "value = compute()  # noqa\n",
                          "value = compute()  # noqa\nother = compute()\n")
    assert out is None, warning(out)
    assert rec["verdict"] == "ok" and rec["judged"] == 1


def test_a_pre_existing_ts_ignore_survives_an_edit_elsewhere(repo: Path) -> None:
    _, out, _, rec = edit(repo, "src/legacy.ts", "export const legacy = untypedThing();",
                          "export const legacy = untypedThing();\nexport const more = 1;")
    assert out is None, warning(out)
    assert rec["verdict"] == "ok" and rec["judged"] == 1


def test_replace_all_judges_every_occurrence(repo: Path) -> None:
    _, out, _, rec = edit(repo, "src/two.py", LOG_LINE, "        pass\n", replace_all=True)
    assert "[swallowed-error]" in warning(out)
    assert rec["findings"] == 2, rec


def test_an_old_string_not_found_judges_the_new_string_alone(repo: Path) -> None:
    """The tool will refuse the edit; the hook still says what the new text carries."""
    _, out, _, rec = edit(repo, "src/app.py", "nothing like this is in the file",
                          "value = compute()  # noqa\n")
    assert "[bare-noqa]" in warning(out)
    assert rec["simulated"] is False and rec["judged"] == 1


def test_an_edit_of_a_missing_file_judges_the_new_string_alone(repo: Path) -> None:
    _, out, _, rec = edit(repo, "src/new.py", "", "value = compute()  # noqa\n")
    assert "[bare-noqa]" in warning(out)
    assert rec["simulated"] is False


def test_multiedit_applies_every_edit_in_order(repo: Path) -> None:
    given = {"file_path": str(repo / "src" / "app.py"), "edits": [
        {"old_string": "    return sum(items)", "new_string": "    return sum(items) + 0"},
        {"old_string": LOG_LINE + "        return 0\n", "new_string": "        pass\n"},
    ]}
    _, out, _, rec = hook(repo, "MultiEdit", given)
    assert "[swallowed-error]" in warning(out) and rec["tool"] == "MultiEdit"
    assert rec["checks"] == ["swallowed-error"]


def test_a_write_that_creates_a_file_judges_every_line(repo: Path) -> None:
    _, out, _, rec = write(repo, "src/new.py", "a = 1\nb = 2  # noqa\n")
    assert "[bare-noqa]" in warning(out) and ":2:" in warning(out)
    assert rec["judged"] == 2


def test_a_write_that_keeps_a_pre_existing_suppression_is_silent(repo: Path) -> None:
    _, out, _, rec = write(repo, "src/legacy.py", "value = compute()  # noqa\nother = compute()\n")
    assert out is None, warning(out)
    assert rec["verdict"] == "ok" and rec["judged"] == 1


def test_a_write_that_adds_a_new_suppression_warns_on_that_line(repo: Path) -> None:
    _, out, _, rec = write(repo, "src/legacy.py",
                           "value = compute()  # noqa\nother = compute()  # noqa\n")
    assert "[bare-noqa]" in warning(out) and "legacy.py:2:" in warning(out)
    assert rec["findings"] == 1


# --- scope: the gate's own -----------------------------------------------------

@pytest.mark.parametrize("rel", ["NOTES.md", "package.json", "poetry.lock"])
def test_a_file_the_gate_would_not_read_is_skipped(repo: Path, rel: str) -> None:
    """Prose that names a token is not a repo that uses one; JSON holds no suppression."""
    _, out, _, rec = write(repo, rel, "// @ts-ignore\nvalue = compute()  # noqa\n")
    assert out is None, warning(out)
    assert rec["verdict"] == "skip" and rec["why"] == "not-code"


def test_a_shell_command_is_not_a_python_file(repo: Path) -> None:
    """Stated limit: a command has no suffix, so the suffix-bound checks do not run on it."""
    _, out, _, rec = bash(repo, 'python3 -c "x = 1  # noqa"')
    assert out is None and rec["verdict"] == "ok"


def test_a_relative_file_path_is_resolved_against_cwd(repo: Path) -> None:
    _, out, _, rec = hook(repo, "Edit", {"file_path": "src/app.py", "old_string": "    return sum(items)",
                                         "new_string": "    return sum(items)  # noqa"})
    assert "[bare-noqa]" in warning(out) and "src/app.py:" in warning(out)
    assert rec["path"] == str(repo / "src" / "app.py")


# --- the allowlist -----------------------------------------------------------

def allow(repo: Path, body: str) -> None:
    (repo / ".conduct").mkdir()
    (repo / ".conduct" / "cheap-rescue-allow.txt").write_text(body, encoding="utf-8")


def test_an_allowlisted_path_is_silent_and_counted(repo: Path) -> None:
    allow(repo, "# quarantined 2026-09-17, tracked in the backlog\n^src/app\\.py$\n")
    _, out, _, rec = edit(repo, "src/app.py", "    return sum(items)", "    return sum(items)  # noqa")
    assert out is None, warning(out)
    assert rec["verdict"] == "ok" and rec["exempted"] == 1


def test_the_allowlist_does_not_silence_everything(repo: Path) -> None:
    allow(repo, "# quarantined 2026-09-17, tracked in the backlog\n^src/app\\.py$\n")
    _, out, _, _ = edit(repo, "src/app.ts", "export function total",
                        "// @ts-ignore\nexport const wrong = 1;\nexport function total")
    assert "[ts-suppression]" in warning(out)


def test_a_malformed_allowlist_fails_open_with_a_receipt(repo: Path) -> None:
    """The gate's exit 2; here, never a silent pass and never a warning it did not earn."""
    allow(repo, "^src/(unclosed\n")
    rc, out, err, rec = edit(repo, "src/app.py", "    return sum(items)", "    return sum(items)  # noqa")
    assert rc == 0 and out is None, (rc, out, err)
    assert rec["verdict"] == "error" and "not a valid regex" in rec["error"]


# --- fail-open and modes -----------------------------------------------------

def test_other_tools_are_ignored_without_a_receipt(repo: Path) -> None:
    for tool in ("Read", "Grep"):
        rc, out, _, rec = hook(repo, tool, {"file_path": str(repo / "src" / "legacy.py"),
                                            "command": "git push --force origin main"})
        assert rc == 0 and out is None and rec is None, tool


def test_notebook_edit_is_left_out(repo: Path) -> None:
    rc, out, _, rec = hook(repo, "NotebookEdit", {"notebook_path": str(repo / "a.ipynb"),
                                                  "new_source": "x = 1  # noqa"})
    assert rc == 0 and out is None and rec is None


def test_an_empty_command_and_a_missing_path_are_ignored(repo: Path) -> None:
    rc, out, _, rec = bash(repo, "   ")
    assert rc == 0 and out is None and rec is None
    rc, out, _, rec = hook(repo, "Edit", {"old_string": "a", "new_string": "b  # noqa"})
    assert rc == 0 and out is None and rec is None


def test_a_missing_gate_fails_open_with_a_receipt(repo: Path) -> None:
    rc, out, _, rec = bash(repo, "git push --force origin main",
                           CHEAP_RESCUE_GATE=str(repo / "none.py"))
    assert rc == 0 and out is None and rec["verdict"] == "error"


def test_block_mode_exits_2_with_the_text_on_stderr(repo: Path) -> None:
    rc, out, err, rec = bash(repo, "git push --force origin main", CHEAP_RESCUE_HOOK_MODE="block")
    assert rc == 2 and out is None and "Block mode" in err and rec["mode"] == "block"


def test_receipts_can_be_switched_off(repo: Path) -> None:
    rc, out, _, _ = bash(repo, "git push --force origin main", CHEAP_RESCUE_RECEIPTS="off")
    assert rc == 0 and warning(out)
    assert not (repo / "receipts.jsonl").exists()


# --- the text ----------------------------------------------------------------

def test_a_hostile_file_name_is_neutralised_in_the_warning(repo: Path) -> None:
    (repo / "a`b\nc.py").write_text("x = 1\n", encoding="utf-8")
    _, out, _, _ = edit(repo, "a`b\nc.py", "x = 1", "x = 1  # noqa")
    w = warning(out)
    assert "a?b?c.py" in w and "\n" not in w.split("cheap-rescue: ")[1][:80]


def test_the_warning_is_capped_and_stays_far_below_the_runtime_cap(repo: Path) -> None:
    _, out, _, rec = write(repo, "src/many.py", "".join(f"x{i} = {i}  # noqa\n" for i in range(60)))
    assert "(+56 more)" in warning(out) and rec["findings"] == 60
    assert 0 < len(warning(out)) < 2000               # the runtime caps hook output at 10,000


def test_the_receipt_carries_names_and_counts_never_the_line(repo: Path) -> None:
    _, _, _, rec = edit(repo, "src/app.py", "    return sum(items)",
                        "    secret_token_value = 1  # noqa\n    return sum(items)")
    for key in ("ts", "session", "tool", "path", "verdict", "checks", "ms", "judged"):
        assert key in rec, (key, rec)
    assert "secret_token_value" not in json.dumps(rec)
