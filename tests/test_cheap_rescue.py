"""Mutation tests for the cheap-rescue gate.

Every check gets the same treatment twice: plant the defect it exists to catch
and require exit 1, then write the same intent in its justified form and require
exit 0. A gate tested only on defects has no false-positive budget and gets
deleted the first week; a gate tested only on clean input proves nothing at all.

    python3 -m pytest tests/ -q

The gate is invoked as a subprocess rather than imported, because the exit code
is part of the contract the whole conduct-harness family shares (0 clean,
1 findings, 2 the gate itself broke). Importing would test the functions and
leave the contract untested.

Each fixture is a real git repository with a real commit, because the gate's
subject is a real diff. Faking the diff would test the parser and leave the one
thing that matters — that a deleted line is not an added one — unexercised.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

GATE = Path(__file__).resolve().parent.parent / "gate" / "cheap_rescue.py"

IDENTITY = ("-c", "user.email=gate@example.invalid", "-c", "user.name=Gate Test")

# A baseline that the gate passes cleanly. `src/legacy.*` carries the tokens on
# purpose: it is the pre-existing debt the gate must NOT report, because nobody
# added it in this change.
BASELINE = {
    "src/app.ts": textwrap.dedent("""\
        export function total(items: number[]): number {
          return items.reduce((a, b) => a + b, 0);
        }
        """),
    "src/app.py": textwrap.dedent("""\
        import logging

        logger = logging.getLogger(__name__)


        def total(items):
            return sum(items)
        """),
    "src/legacy.ts": textwrap.dedent("""\
        // @ts-ignore
        export const legacy = untypedThing();
        """),
    "src/legacy.py": textwrap.dedent("""\
        value = compute()  # noqa
        """),
    ".github/workflows/ci.yml": textwrap.dedent("""\
        name: ci
        on: [push]
        jobs:
          build:
            runs-on: ubuntu-latest
            steps:
              - run: make test
        """),
    "scripts/deploy.sh": textwrap.dedent("""\
        #!/bin/sh
        set -eu
        make build
        """),
}


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args],
                   capture_output=True, text=True, check=True)


def run(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "HARNESS_ROOT": str(root)}
    if not args:
        args = ("--base", "HEAD")
    return subprocess.run([sys.executable, str(GATE), *args],
                          capture_output=True, text=True, env=env, check=False)


def append(root: Path, rel: str, body: str) -> None:
    """Add lines to a tracked file — the shape the gate is meant to read."""
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    path.write_text(existing + textwrap.dedent(body), encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A committed repo the gate passes cleanly."""
    git(tmp_path, "init", "-q", "-b", "main")
    for rel, text in BASELINE.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    git(tmp_path, "add", "-A")
    git(tmp_path, *IDENTITY, "commit", "-q", "-m", "baseline")
    return tmp_path


# --- the control -------------------------------------------------------------

def test_unchanged_repo_passes(repo: Path) -> None:
    """Without this, every test below could pass because the gate always fails.

    It also pins the central claim: `src/legacy.ts` holds a bare `@ts-ignore`
    and `src/legacy.py` a bare `# noqa`, and neither is reported, because
    neither was added in this diff.
    """
    r = run(repo)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "silences a check" in r.stdout


# --- CHECK 1: @ts-ignore / @ts-expect-error ----------------------------------

def test_bare_ts_ignore_is_caught(repo: Path) -> None:
    append(repo, "src/app.ts", """
        // @ts-ignore
        export const wrong: number = "not a number" as any;
        """)
    r = run(repo)
    assert r.returncode == 1, r.stdout
    assert "ts-suppression" in r.stdout


def test_ts_expect_error_with_a_reason_passes(repo: Path) -> None:
    append(repo, "src/app.ts", """
        // @ts-expect-error the vendor types omit `total`; fixed upstream in v4
        export const wrong: number = vendor.total;
        """)
    r = run(repo)
    assert r.returncode == 0, r.stdout


def test_bare_ts_nocheck_is_caught(repo: Path) -> None:
    """`@ts-nocheck` silences a whole file, so it needs a reason most of all."""
    append(repo, "src/app.ts", """
        // @ts-nocheck
        export const anything = whatever();
        """)
    r = run(repo)
    assert r.returncode == 1, r.stdout
    assert "ts-suppression" in r.stdout


def test_ts_nocheck_with_a_reason_passes(repo: Path) -> None:
    append(repo, "src/app.ts", """
        // @ts-nocheck generated from the OpenAPI schema; regenerate, do not edit
        export const anything = whatever();
        """)
    r = run(repo)
    assert r.returncode == 0, r.stdout


def test_a_one_word_reason_is_not_a_reason(repo: Path) -> None:
    """`TODO` clears a naive non-empty test and explains nothing."""
    append(repo, "src/app.ts", """
        // @ts-expect-error TODO
        export const wrong: number = vendor.total;
        """)
    assert run(repo).returncode == 1


# --- CHECK 2: eslint-disable -------------------------------------------------

def test_blanket_eslint_disable_is_caught(repo: Path) -> None:
    append(repo, "src/app.ts", """
        /* eslint-disable */
        export const noisy = () => console.log("hi");
        """)
    r = run(repo)
    assert r.returncode == 1, r.stdout
    assert "eslint-blanket-disable" in r.stdout


def test_eslint_disable_next_line_with_no_rule_is_caught(repo: Path) -> None:
    append(repo, "src/app.ts", """
        // eslint-disable-next-line
        export const noisy = () => console.log("hi");
        """)
    assert run(repo).returncode == 1


def test_eslint_disable_naming_its_rule_passes(repo: Path) -> None:
    append(repo, "src/app.ts", """
        // eslint-disable-next-line no-console -- this file is the CLI entry point
        export const noisy = () => console.log("hi");
        """)
    r = run(repo)
    assert r.returncode == 0, r.stdout


def test_a_description_is_not_a_rule_name(repo: Path) -> None:
    """Everything after `--` is ESLint's description, never a rule."""
    append(repo, "src/app.ts", """
        // eslint-disable-next-line -- we need it here for now
        export const noisy = () => console.log("hi");
        """)
    assert run(repo).returncode == 1


# --- CHECK 3: # noqa / # type: ignore ----------------------------------------

def test_bare_noqa_is_caught(repo: Path) -> None:
    append(repo, "src/app.py", """

        def widest(items):
            return max(items)  # noqa
        """)
    r = run(repo)
    assert r.returncode == 1, r.stdout
    assert "bare-noqa" in r.stdout


def test_noqa_with_a_code_passes(repo: Path) -> None:
    append(repo, "src/app.py", """

        def widest(items):
            return max(items)  # noqa: E501
        """)
    r = run(repo)
    assert r.returncode == 0, r.stdout


def test_bare_type_ignore_is_caught(repo: Path) -> None:
    append(repo, "src/app.py", """

        def widest(items):
            return max(items)  # type: ignore
        """)
    r = run(repo)
    assert r.returncode == 1, r.stdout
    assert "bare-type-ignore" in r.stdout


def test_type_ignore_with_a_code_passes(repo: Path) -> None:
    append(repo, "src/app.py", """

        def widest(items):
            return max(items)  # type: ignore[arg-type]
        """)
    r = run(repo)
    assert r.returncode == 0, r.stdout


# --- CHECK 4: skipped and disabled tests -------------------------------------

@pytest.mark.parametrize("rel,line", [
    ("src/app.py", "@pytest.mark.skip"),
    ("src/app.py", "@pytest.mark.xfail"),
    ("src/app.ts", "it.skip('adds up', () => {});"),
    ("src/app.ts", "test.skip('adds up', () => {});"),
    ("src/app.ts", "describe.skip('totals', () => {});"),
    ("src/app.ts", "test.todo('adds up');"),
    ("src/app.ts", "xit('adds up', () => {});"),
    ("src/app.ts", "xdescribe('totals', () => {});"),
])
def test_every_skip_token_is_caught(repo: Path, rel: str, line: str) -> None:
    """Each token gets its own case: a list is only as good as its weakest entry."""
    append(repo, rel, f"\n{line}\n")
    r = run(repo)
    assert r.returncode == 1, f"{line} was not caught:\n{r.stdout}"
    assert "skipped-test" in r.stdout


def test_a_conditional_skip_states_its_condition_and_passes(repo: Path) -> None:
    """`skipif` names the reason it is not running. That is the whole difference."""
    append(repo, "src/app.py", """

        @pytest.mark.skipif(sys.platform == "win32", reason="POSIX paths only")
        def test_totals():
            assert total([1, 2]) == 3
        """)
    r = run(repo)
    assert r.returncode == 0, r.stdout


def test_the_allowlist_takes_a_justified_skip_by_name(repo: Path) -> None:
    """The escape hatch, which is why the gate survives contact with users."""
    append(repo, "src/app.ts", "\nxit('flaky against the sandbox', () => {});\n")
    assert run(repo).returncode == 1
    conduct = repo / ".conduct"
    conduct.mkdir()
    (conduct / "cheap-rescue-allow.txt").write_text(
        "# quarantined 2026-09-10, tracked in the backlog\n"
        "^src/app\\.ts$\n", encoding="utf-8")
    r = run(repo)
    assert r.returncode == 0, r.stdout


# --- CHECK 5: continue-on-error ----------------------------------------------

def test_continue_on_error_true_is_caught(repo: Path) -> None:
    append(repo, ".github/workflows/ci.yml", """
      lint:
        runs-on: ubuntu-latest
        steps:
          - run: make lint
            continue-on-error: true
    """)
    r = run(repo)
    assert r.returncode == 1, r.stdout
    assert "ci-continue-on-error" in r.stdout


def test_continue_on_error_false_passes(repo: Path) -> None:
    append(repo, ".github/workflows/ci.yml", """
      lint:
        runs-on: ubuntu-latest
        steps:
          - run: make lint
            continue-on-error: false
    """)
    r = run(repo)
    assert r.returncode == 0, r.stdout


# --- CHECK 6: --no-verify and force pushes -----------------------------------

def test_no_verify_is_caught(repo: Path) -> None:
    append(repo, "scripts/deploy.sh", """
        git commit --no-verify -m "ship it"
        """)
    r = run(repo)
    assert r.returncode == 1, r.stdout
    assert "no-verify" in r.stdout


def test_a_plain_commit_passes(repo: Path) -> None:
    append(repo, "scripts/deploy.sh", """
        git commit -m "ship it"
        """)
    r = run(repo)
    assert r.returncode == 0, r.stdout


def test_force_push_is_caught(repo: Path) -> None:
    append(repo, "scripts/deploy.sh", """
        git push --force origin main
        """)
    r = run(repo)
    assert r.returncode == 1, r.stdout
    assert "force-push" in r.stdout


def test_short_force_flag_on_a_push_is_caught(repo: Path) -> None:
    append(repo, "scripts/deploy.sh", """
        git push -f origin main
        """)
    assert run(repo).returncode == 1


def test_force_with_lease_passes(repo: Path) -> None:
    """The same operation with the accident removed."""
    append(repo, "scripts/deploy.sh", """
        git push --force-with-lease origin main
        """)
    r = run(repo)
    assert r.returncode == 0, r.stdout


def test_force_that_is_not_a_push_is_not_a_force_push(repo: Path) -> None:
    """`rm -f` and `npm install --force` are other arguments entirely."""
    append(repo, "scripts/deploy.sh", """
        rm -f dist/stale.log
        npm install --force
        """)
    r = run(repo)
    assert r.returncode == 0, r.stdout


def test_a_push_that_is_not_git_is_not_a_force_push(repo: Path) -> None:
    """`drizzle-kit push --force` pushes a schema and rewrites no history. It was
    the one false positive the live hook's measurement over real sessions found."""
    append(repo, "scripts/deploy.sh", """
        npx drizzle-kit push --force
        """)
    r = run(repo)
    assert r.returncode == 0, r.stdout


def test_git_options_before_push_are_still_a_git_push(repo: Path) -> None:
    append(repo, "scripts/deploy.sh", """
        git -C /srv/app push -f origin main
        """)
    r = run(repo)
    assert r.returncode == 1, r.stdout
    assert "force-push" in r.stdout


# --- CHECK 7: swallowed errors -----------------------------------------------

def test_except_pass_is_caught(repo: Path) -> None:
    append(repo, "src/app.py", """

        def safe_total(items):
            try:
                return total(items)
            except Exception:
                pass
        """)
    r = run(repo)
    assert r.returncode == 1, r.stdout
    assert "swallowed-error" in r.stdout


def test_inline_except_pass_is_caught(repo: Path) -> None:
    append(repo, "src/app.py", """

        def safe_total(items):
            try:
                return total(items)
            except Exception: pass
        """)
    assert run(repo).returncode == 1


def test_a_handler_that_logs_passes(repo: Path) -> None:
    append(repo, "src/app.py", """

        def safe_total(items):
            try:
                return total(items)
            except TypeError as exc:
                logger.warning("total failed: %s", exc)
                return 0
        """)
    r = run(repo)
    assert r.returncode == 0, r.stdout


def test_empty_catch_block_is_caught(repo: Path) -> None:
    append(repo, "src/app.ts", """
        export function safeTotal(items: number[]): number {
          try {
            return total(items);
          } catch (e) {}
          return 0;
        }
        """)
    r = run(repo)
    assert r.returncode == 1, r.stdout
    assert "swallowed-error" in r.stdout


def test_catch_block_holding_only_a_comment_is_caught(repo: Path) -> None:
    """A comment is not error handling; it is a note about not handling it."""
    append(repo, "src/app.ts", """
        export function safeTotal(items: number[]): number {
          try {
            return total(items);
          } catch (e) {
            /* ignore */
          }
          return 0;
        }
        """)
    assert run(repo).returncode == 1


def test_catch_block_that_logs_passes(repo: Path) -> None:
    append(repo, "src/app.ts", """
        export function safeTotal(items: number[]): number {
          try {
            return total(items);
          } catch (e) {
            console.error("total failed", e);
          }
          return 0;
        }
        """)
    r = run(repo)
    assert r.returncode == 0, r.stdout


def test_replacing_a_log_with_pass_is_caught(repo: Path) -> None:
    """The exact move the rule exists for: the handler existed, the log did not.

    Only the body line changes, so this also proves the check anchors on any
    added line of the handler rather than only on its header.
    """
    append(repo, "src/app.py", """

        def safe_total(items):
            try:
                return total(items)
            except TypeError as exc:
                logger.warning("total failed: %s", exc)
        """)
    git(repo, "add", "-A")
    git(repo, *IDENTITY, "commit", "-q", "-m", "a handler that logs")
    src = repo / "src" / "app.py"
    src.write_text(src.read_text(encoding="utf-8").replace(
        '        logger.warning("total failed: %s", exc)\n', "        pass\n"),
        encoding="utf-8")
    r = run(repo)
    assert r.returncode == 1, r.stdout
    assert "swallowed-error" in r.stdout


# --- false positives: the reason a gate survives contact with users ----------

def test_the_word_skip_in_prose_is_not_a_skipped_test(repo: Path) -> None:
    """The required false-positive case: prose and strings are not tokens."""
    append(repo, "src/app.py", """

        # We deliberately skip the cache when the input is empty; see ADR-4.
        HINT = "skip the cache, do not skip the test"


        def total_or_skip(items):
            return None if not items else total(items)
        """)
    r = run(repo)
    assert r.returncode == 0, r.stdout


def test_prose_in_markdown_is_never_scanned(repo: Path) -> None:
    """Documentation that names a token is not a repo that uses one."""
    (repo / "NOTES.md").write_text(
        "Never write a bare `# noqa` or an `@ts-ignore`; use `--force-with-lease`.\n",
        encoding="utf-8")
    r = run(repo)
    assert r.returncode == 0, r.stdout


def test_deleted_lines_carrying_the_tokens_are_not_findings(repo: Path) -> None:
    """Removing an `@ts-ignore` is the opposite of a cheap rescue."""
    (repo / "src" / "legacy.ts").write_text(
        "export const legacy = typedThing();\n", encoding="utf-8")
    (repo / "src" / "legacy.py").write_text(
        "value = compute()\n", encoding="utf-8")
    r = run(repo)
    assert r.returncode == 0, r.stdout


def test_a_deletion_that_adds_nothing_reports_nothing(repo: Path) -> None:
    """Deleting the whole legacy file must not resurrect its tokens."""
    (repo / "src" / "legacy.ts").unlink()
    (repo / "src" / "legacy.py").unlink()
    r = run(repo)
    assert r.returncode == 0, r.stdout


# --- scope: --files ----------------------------------------------------------

def test_files_mode_scans_the_named_file_whole(repo: Path) -> None:
    r = run(repo, "--files", str(repo / "src" / "legacy.ts"))
    assert r.returncode == 1, r.stdout
    assert "ts-suppression" in r.stdout


def test_files_mode_on_a_clean_file_passes(repo: Path) -> None:
    r = run(repo, "--files", str(repo / "src" / "app.ts"))
    assert r.returncode == 0, r.stdout


def test_untracked_files_are_in_scope(repo: Path) -> None:
    """A whole new file is entirely added, diff or no diff."""
    (repo / "src" / "brand_new.py").write_text(
        "value = compute()  # noqa\n", encoding="utf-8")
    r = run(repo)
    assert r.returncode == 1, r.stdout
    assert "bare-noqa" in r.stdout


# --- the contract: exit 2 is the gate's own failure --------------------------

def test_an_unresolvable_base_is_exit_2_not_a_pass(repo: Path) -> None:
    """When the input is missing the answer is never 'clean'."""
    r = run(repo, "--base", "refs/heads/no-such-branch")
    assert r.returncode == 2, r.stdout + r.stderr
    assert "gate failure" in r.stderr


def test_a_directory_that_is_not_a_repo_is_exit_2(tmp_path: Path) -> None:
    r = run(tmp_path)
    assert r.returncode == 2, r.stdout + r.stderr


def test_a_malformed_allowlist_is_exit_2_not_a_silent_pass(repo: Path) -> None:
    """A gate that cannot parse its config does not know what it is exempting."""
    conduct = repo / ".conduct"
    conduct.mkdir()
    (conduct / "cheap-rescue-allow.txt").write_text("^src/(unclosed\n", encoding="utf-8")
    r = run(repo)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "not a valid regex" in r.stderr


def test_files_mode_on_a_missing_path_is_exit_2(repo: Path) -> None:
    r = run(repo, "--files", str(repo / "src" / "nope.ts"))
    assert r.returncode == 2, r.stdout + r.stderr


# --- SARIF output ------------------------------------------------------------

def test_sarif_is_written_and_well_formed(repo: Path, tmp_path: Path) -> None:
    append(repo, "src/app.ts", """
        // @ts-ignore
        export const wrong: number = "not a number" as any;
        """)
    out = tmp_path / "out.sarif"
    r = run(repo, "--base", "HEAD", "--sarif", str(out))
    assert r.returncode == 1
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["version"] == "2.1.0"
    assert doc["runs"][0]["results"], "SARIF carries no results for a failing run"
    assert doc["runs"][0]["results"][0]["ruleId"] == "ts-suppression"
    assert doc["runs"][0]["results"][0]["locations"][0][
        "physicalLocation"]["artifactLocation"]["uri"] == "src/app.ts"


def test_sarif_is_written_for_a_clean_run_too(repo: Path, tmp_path: Path) -> None:
    """An empty results array is evidence the gate ran; a missing file is not."""
    out = tmp_path / "clean.sarif"
    r = run(repo, "--base", "HEAD", "--sarif", str(out))
    assert r.returncode == 0
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["runs"][0]["results"] == []
