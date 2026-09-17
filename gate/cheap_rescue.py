#!/usr/bin/env python3
"""The Ihsan Harness gate: the cheap rescue, caught in the diff.

    python3 gate/cheap_rescue.py                       # diff against origin/main
    python3 gate/cheap_rescue.py --base HEAD~1
    python3 gate/cheap_rescue.py --files src/a.ts src/b.py
    python3 gate/cheap_rescue.py --sarif cheap-rescue.sarif

Exit codes are the contract shared by the conduct-harness family:

    0   no findings
    1   findings - a token that buys a green check without earning it
    2   the gate itself failed

The third one is not decoration. A checker that returns 1 when it crashed reads
as "I found something"; one that returns 0 reads as "clean" and fails OPEN. This
gate distinguishes its own failure from its verdict, because a gate that lies
about its own health is the first cheap rescue.

WHAT THIS GATE IS FOR
---------------------
SABR 3 in CODEX.md says: *refuse the cheap rescue - no silenced test, no
suppressed error, no "just for now" hack*, and its falsifier is "a test skipped
or deleted, or a warning muted, to make a check pass". That falsifier is a claim
about a diff, so it can be checked by reading one.

`scripts/check.py` verifies that this repo keeps its own documentary promises.
It says nothing about the code a reader writes under the codex. This gate is the
other half: it reads the change itself and names the tokens by which an agent
turns a red check green without touching the cause.

    @ts-ignore                 the type error is still there, now invisible
    eslint-disable             the lint rule still fires, now unheard
    # noqa                     same, in Python
    @pytest.mark.skip          the test still fails, now unrun
    continue-on-error: true    the CI step still breaks, now green
    --no-verify                the hooks still object, now bypassed
    push --force               the history still conflicts, now overwritten
    except: pass               the error still happens, now swallowed

Every one of them is *legitimate somewhere*. That is exactly why they are worth
a gate rather than a ban: the difference between craft and cowardice is whether
the suppression carries its reason. So the gate does not ask "is the token
present". It asks "did the author say why" - a rule name on the eslint-disable,
an error code on the noqa, a sentence on the ts-expect-error, `--force-with-lease`
instead of `--force`, a log line inside the catch. Where no such form exists
(a skipped test), `.conduct/cheap-rescue-allow.txt` takes the justified case by
name.

ONLY ADDED LINES
----------------
The gate reads the diff, never the whole tree. Auditing every file would turn any
inherited repo into a wall of red on day one, and a gate nobody can ever get to
green is a gate that gets deleted in a week - which leaves the real defect
unwatched. What you added is yours; what you found is not yet your debt.

The one deliberate exception is the swallowed-handler check: an empty `except`
or `catch` is only visible against the block around it, so the gate reads the
whole handler for CONTEXT while still requiring that at least one of its lines
be in the diff before it reports. Replacing a log line with `pass` is an added
line, and it is precisely the cheap rescue this rule exists for.

Deleted lines never appear: they carry no line number on the new side. Removing
an `@ts-ignore` is the opposite of a cheap rescue and the gate stays quiet.
"""

from __future__ import annotations

import argparse
import bisect
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# The root is overridable so the tests can point the gate at a scratch repo. A
# checker that can only ever run on itself cannot be shown to work: the only way
# to prove a check has teeth is to hand it a repo with the defect planted and
# watch it go red.
ROOT = Path(os.environ.get("HARNESS_ROOT") or Path(__file__).resolve().parent.parent)

ALLOWLIST = ROOT / ".conduct" / "cheap-rescue-allow.txt"

# Code and CI configuration only. Prose is excluded on purpose: a README that
# documents `# noqa` is not a repo that suppresses a linter, and a gate that
# cannot tell the two apart teaches its users to ignore it. JSON and lockfiles
# are excluded because nothing there is a suppression, only vendored noise.
SCANNED_SUFFIXES = frozenset({
    ".py", ".pyi", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts",
    ".vue", ".svelte", ".astro", ".swift", ".kt", ".kts", ".go", ".rs", ".rb",
    ".php", ".java", ".sh", ".bash", ".zsh", ".yml", ".yaml", ".toml",
})

PY_LIKE = frozenset({".py", ".pyi"})
JS_LIKE = frozenset({".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts",
                     ".cts", ".vue", ".svelte", ".astro", ".swift", ".kt",
                     ".kts", ".go", ".rs", ".java", ".php"})
YAML_LIKE = frozenset({".yml", ".yaml"})

# A description shorter than this is a gesture, not a reason. "TODO" and "fixme"
# clear a naive non-empty test and explain nothing; twelve characters is roughly
# two real words, which is the smallest thing a later reader can act on.
MIN_REASON_CHARS = 12


@dataclass
class Finding:
    check: str
    message: str
    path: str = ""
    line: int = 0
    text: str = ""


@dataclass
class Target:
    """One file in scope, with the lines the diff put in scope."""
    path: str                 # repo-relative, forward slashes
    lines: list[str]          # the file as it stands now, 0-indexed
    added: set[int]           # 1-based line numbers the diff added

    def line_starts(self) -> list[int]:
        starts, pos = [], 0
        for ln in self.lines:
            starts.append(pos)
            pos += len(ln) + 1
        return starts


# --- collecting the diff -----------------------------------------------------

class GateError(Exception):
    """The gate cannot judge. Always exit 2, never 1 and never 0."""


def git(*args: str) -> str:
    proc = subprocess.run(["git", "-C", str(ROOT), *args],
                          capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise GateError(f"git {' '.join(args)} failed: {proc.stderr.strip()[:200]}")
    return proc.stdout


def resolve_base(base: str) -> str:
    """The commit the diff is measured from: the merge base with `base`.

    Three-dot semantics on purpose. Diffing straight against a moving `main`
    would blame this change for every line someone else landed while it was open.
    """
    try:
        git("rev-parse", "--git-dir")
    except GateError as exc:
        raise GateError(f"{ROOT} is not a git repository ({exc})") from exc
    try:
        git("rev-parse", "--verify", f"{base}^{{commit}}")
    except GateError as exc:
        raise GateError(
            f"base ref {base!r} does not resolve - pass --base <ref> or --files") from exc
    try:
        return git("merge-base", base, "HEAD").strip()
    except GateError:
        # Unrelated histories (a shallow CI clone, a fresh branch): the base
        # itself is the honest floor. Never silently widen to "everything".
        return git("rev-parse", f"{base}^{{commit}}").strip()


HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def added_lines_from_diff(base_sha: str) -> tuple[dict[str, set[int]], set[str]]:
    """(path -> line numbers added on the new side, paths that are wholly new)."""
    out: dict[str, set[int]] = {}
    diff = git("diff", "--unified=0", "--no-color", "--no-ext-diff",
               "--diff-filter=ACMRTUXB", base_sha, "--")
    path: str | None = None
    for raw in diff.splitlines():
        if raw.startswith("+++ "):
            target = raw[4:].strip()
            path = None if target == "/dev/null" else target[2:] if target.startswith(
                ("a/", "b/")) else target
            continue
        if raw.startswith("@@") and path:
            m = HUNK.match(raw)
            if not m:
                continue
            start = int(m.group(1))
            count = int(m.group(2)) if m.group(2) is not None else 1
            if count:
                out.setdefault(path, set()).update(range(start, start + count))
    # An untracked file is entirely new. Leaving it out would let a whole file of
    # suppressions in through the one door the diff does not watch.
    untracked = {p.strip() for p in
                 git("ls-files", "--others", "--exclude-standard").splitlines() if p.strip()}
    for path in untracked:
        out.setdefault(path, set())          # every line is in scope; see load_targets
    return out, untracked


def load_targets(scope: dict[str, set[int]], whole_file: set[str]) -> list[Target]:
    targets: list[Target] = []
    for path in sorted(scope):
        if Path(path).suffix.lower() not in SCANNED_SUFFIXES:
            continue
        abs_path = ROOT / path
        if not abs_path.is_file():
            continue                          # deleted, or a submodule pointer
        try:
            text = abs_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue                          # binary or unreadable: nothing to read
        lines = text.splitlines()
        added = set(range(1, len(lines) + 1)) if path in whole_file else {
            n for n in scope[path] if 1 <= n <= len(lines)}
        if added:
            targets.append(Target(path, lines, added))
    return targets


def targets_from_files(names: list[str]) -> list[Target]:
    """`--files` names the scope explicitly, so the whole file is in scope."""
    scope: dict[str, set[int]] = {}
    for name in names:
        p = Path(name)
        abs_path = p if p.is_absolute() else (Path.cwd() / p)
        try:
            rel = abs_path.resolve().relative_to(ROOT.resolve()).as_posix()
        except ValueError:
            raise GateError(f"{name} is outside HARNESS_ROOT ({ROOT})") from None
        if not abs_path.is_file():
            raise GateError(f"{name} does not exist")
        scope[rel] = set()
    return load_targets(scope, whole_file=set(scope))


# --- the allowlist -----------------------------------------------------------

def load_allowlist() -> list[re.Pattern[str]]:
    """Read `.conduct/cheap-rescue-allow.txt`: one regex or path per line.

    Without an escape hatch a gate with any false positive at all gets switched
    off wholesale, and switching it off is a decision taken once, in a hurry, by
    whoever is most annoyed. An allowlist makes the exemption specific, written
    down, and reviewable in the same diff as the code it excuses.

    A malformed entry is exit 2, not a warning: a gate running on a config it
    could not parse does not know what it is exempting, and reporting "clean"
    from there would be the exact green paint SIDQ forbids.
    """
    if not ALLOWLIST.is_file():
        return []
    patterns: list[re.Pattern[str]] = []
    for n, raw in enumerate(ALLOWLIST.read_text(encoding="utf-8").splitlines(), 1):
        entry = raw.split("#", 1)[0].strip() if not raw.lstrip().startswith("#") else ""
        if not entry:
            continue
        try:
            patterns.append(re.compile(entry))
        except re.error as exc:
            raise GateError(
                f"{ALLOWLIST.name}:{n}: {entry!r} is not a valid regex ({exc})") from exc
    return patterns


def exempt(finding: Finding, patterns: list[re.Pattern[str]]) -> bool:
    for p in patterns:
        if p.search(finding.path) or (finding.text and p.search(finding.text)):
            return True
    return False


# --- shared helpers ----------------------------------------------------------

def scan(t: Target) -> list[tuple[int, str]]:
    """The added lines of a target, as (1-based line number, text)."""
    return [(n, t.lines[n - 1]) for n in sorted(t.added)]


def reason_of(rest: str) -> str:
    """What is left of a suppression comment once the directive is stripped."""
    rest = rest.strip()
    rest = re.sub(r"^[\s:,\-=>]+", "", rest)
    rest = re.sub(r"(?:\*/|-->|\*\)|\"\"\"|''')\s*$", "", rest).strip()
    return rest


def indent_of(line: str) -> int:
    return len(line) - len(line.lstrip())


# --- CHECK 1: TypeScript suppressions ----------------------------------------

TS_SUPPRESS = re.compile(r"@ts-(?P<kind>ignore|expect-error|nocheck)\b(?P<rest>.*)$")


def check_ts_suppressions(targets: list[Target], findings: list[Finding]) -> None:
    """CHECK 1 - `@ts-ignore` / `@ts-expect-error` with no reason attached.

    The compiler found a real disagreement between the code and its types. The
    described form keeps that disagreement legible: a later reader can weigh the
    reason and delete the suppression when it stops being true. The bare form
    deletes the evidence and leaves the bug.
    """
    for t in targets:
        if t.path.endswith(".d.ts"):
            continue                          # ambient declarations, not shipped logic
        for n, line in scan(t):
            m = TS_SUPPRESS.search(line)
            if not m:
                continue
            reason = reason_of(m.group("rest"))
            if len(reason) >= MIN_REASON_CHARS:
                continue
            findings.append(Finding(
                "ts-suppression",
                f"@ts-{m.group('kind')} with no reason - say what the compiler is "
                f"wrong about, in at least {MIN_REASON_CHARS} characters, or fix the type",
                t.path, n, line.strip()))


# --- CHECK 2: eslint-disable without a rule name -----------------------------

ESLINT_DISABLE = re.compile(
    r"eslint-disable(?P<scope>-next-line|-line)?\b(?P<rest>[^*\n]*)")


def check_eslint_disable(targets: list[Target], findings: list[Finding]) -> None:
    """CHECK 2 - a blanket `eslint-disable` with no rule named.

    Naming the rule bounds the damage to the one thing you meant to silence.
    A bare disable turns off every rule on that line - including the ones added
    next year by someone who will never know this line opted out.
    """
    for t in targets:
        for n, line in scan(t):
            m = ESLINT_DISABLE.search(line)
            if not m:
                continue
            # ESLint's own convention: everything after `--` is a description,
            # never a rule name. Splitting there stops "-- because reasons" from
            # passing as if it named a rule.
            before = m.group("rest").split("--", 1)[0]
            if re.search(r"[A-Za-z@][\w@/.-]{2,}", before):
                continue
            what = "eslint-disable" + (m.group("scope") or "")
            findings.append(Finding(
                "eslint-blanket-disable",
                f"{what} with no rule named - name the rule you mean to silence, "
                f"e.g. `{what} no-console -- reason`",
                t.path, n, line.strip()))


# --- CHECK 3: Python suppressions --------------------------------------------

NOQA = re.compile(r"#\s*noqa(?P<rest>.*)$", re.IGNORECASE)
NOQA_CODED = re.compile(r"^\s*:\s*[A-Z]+[0-9]+", re.IGNORECASE)
TYPE_IGNORE = re.compile(r"#\s*type:\s*ignore(?P<rest>.*)$")
TYPE_IGNORE_CODED = re.compile(r"^\s*\[[\w,\s.-]+\]")


def check_python_suppressions(targets: list[Target], findings: list[Finding]) -> None:
    """CHECK 3 - `# noqa` and `# type: ignore` with no code.

    Same shape as CHECK 1 in another language. `# noqa: E501` silences one line
    length; `# noqa` silences the linter's entire opinion of that line forever,
    including the security rule that fires on it next release.
    """
    for t in targets:
        if Path(t.path).suffix.lower() not in PY_LIKE:
            continue
        for n, line in scan(t):
            m = NOQA.search(line)
            if m and not NOQA_CODED.match(m.group("rest")):
                findings.append(Finding(
                    "bare-noqa",
                    "# noqa with no error code - name the rule, e.g. `# noqa: E501`, "
                    "so the next rule to fire here is not silenced too",
                    t.path, n, line.strip()))
            m = TYPE_IGNORE.search(line)
            if m and not TYPE_IGNORE_CODED.match(m.group("rest")):
                findings.append(Finding(
                    "bare-type-ignore",
                    "# type: ignore with no code - name it, e.g. "
                    "`# type: ignore[arg-type]`, or fix the annotation",
                    t.path, n, line.strip()))


# --- CHECK 4: skipped and disabled tests -------------------------------------

# `@pytest.mark.skip` but not `skipif`: a conditional skip states the condition,
# which is the reason, which is the whole difference. `\b` already refuses
# `skipif` (p and i are both word characters); the explicit lookahead says so
# out loud rather than relying on a reader to work it out.
SKIP_TOKENS: tuple[tuple[str, str], ...] = (
    (r"@pytest\.mark\.skip(?![\w])", "@pytest.mark.skip"),
    (r"@pytest\.mark\.xfail(?![\w])", "@pytest.mark.xfail"),
    (r"(?<![\w.$])it\.skip(?![\w])", "it.skip"),
    (r"(?<![\w.$])test\.skip(?![\w])", "test.skip"),
    (r"(?<![\w.$])describe\.skip(?![\w])", "describe.skip"),
    (r"(?<![\w.$])test\.todo(?![\w])", "test.todo"),
    (r"(?<![\w.$])xit\s*\(", "xit("),
    (r"(?<![\w.$])xdescribe\s*\(", "xdescribe("),
)
SKIP_PATTERNS = tuple((re.compile(rx), name) for rx, name in SKIP_TOKENS)


def check_skipped_tests(targets: list[Target], findings: list[Finding]) -> None:
    """CHECK 4 - a test switched off in the same change that made the suite green.

    This is the falsifier of SABR 3 word for word. There is no described form
    that makes a blanket skip honest, so this check has no "with a reason"
    escape: a genuinely quarantined test goes in `.conduct/cheap-rescue-allow.txt`
    by name, where it is a decision someone made rather than a token nobody read.
    `@pytest.mark.skipif(...)` is untouched - it states its condition.
    """
    for t in targets:
        for n, line in scan(t):
            for pattern, name in SKIP_PATTERNS:
                if pattern.search(line):
                    findings.append(Finding(
                        "skipped-test",
                        f"{name} - a test switched off is a check that stopped "
                        f"checking; fix it, delete it, or allowlist it by name",
                        t.path, n, line.strip()))
                    break


# --- CHECK 5: continue-on-error in CI ----------------------------------------

CONTINUE_ON_ERROR = re.compile(
    r"^\s*-?\s*continue-on-error\s*:\s*(?:true|yes|on|'true'|\"true\")\s*(?:#.*)?$",
    re.IGNORECASE)


def check_ci_continue_on_error(targets: list[Target], findings: list[Finding]) -> None:
    """CHECK 5 - `continue-on-error: true`, the green paint of CI.

    The step still fails. The badge still says pass. This is the same act as a
    summary that reads "complete" over a red run, moved from the report into the
    pipeline, and SIDQ 1 does not care which file it lives in.
    """
    for t in targets:
        if Path(t.path).suffix.lower() not in YAML_LIKE:
            continue
        for n, line in scan(t):
            if CONTINUE_ON_ERROR.match(line):
                findings.append(Finding(
                    "ci-continue-on-error",
                    "continue-on-error: true - the step still fails, the badge just "
                    "stops saying so; fix the step or drop it from the required set",
                    t.path, n, line.strip()))


# --- CHECK 6: bypassing the local gates --------------------------------------

NO_VERIFY = re.compile(r"--no-verify\b")
# A `git … push` on the line, options between allowed, nothing that starts a
# new command. The word `push` alone is not enough: `drizzle-kit push --force`
# pushes a schema and rewrites no history - the one false positive the live
# hook's measurement over 27,423 real shell calls turned up.
GIT_PUSH = re.compile(r"(?<![\w-])git\b[^;&|]*?(?<![\w-])push\b")
FORCE_FLAG = re.compile(r"(?<![\w-])(?:--force(?!-with-lease|-if-includes)|-f)(?![\w-])")


def check_git_escape_hatches(targets: list[Target], findings: list[Finding]) -> None:
    """CHECK 6 - `--no-verify` and a force push.

    `--no-verify` does not disable one hook; it disables the whole chain, secret
    scan included, and it is reached for precisely when a hook has just said no.
    `--force` on a `git push` overwrites whatever someone else pushed while you
    were working. `--force-with-lease` refuses when the remote moved, which is
    the same operation with the accident removed - so it passes here. Another
    tool's `push --force` is another tool's business.
    """
    for t in targets:
        for n, line in scan(t):
            if NO_VERIFY.search(line):
                findings.append(Finding(
                    "no-verify",
                    "--no-verify disables the entire hook chain, not the one hook that "
                    "objected - diagnose what the hook caught instead",
                    t.path, n, line.strip()))
            m = GIT_PUSH.search(line)
            if m and FORCE_FLAG.search(line, m.end()):
                findings.append(Finding(
                    "force-push",
                    "a force push overwrites work someone else already pushed - use "
                    "--force-with-lease, which refuses when the remote has moved",
                    t.path, n, line.strip()))


# --- CHECK 7: swallowed errors -----------------------------------------------

EXCEPT_HEADER = re.compile(r"^\s*except\b[^:#]*:(?P<tail>.*)$")
CATCH_HEADER = re.compile(r"(?<![\w.$])catch\s*(?:\([^)]*\))?\s*(?P<brace>\{)")

# Only the two statements that mean "do nothing". `break`, `continue` and
# `return x` are deliberately absent: `except StopIteration: break` is the
# idiomatic, correct way to end a loop, and a gate that calls that a cheap
# rescue is a gate that has to be turned off to get any work done.
DEAD_STATEMENTS = frozenset({"pass", "..."})


def _python_handler_body(lines: list[str], header: int) -> list[int]:
    """0-based indices of the suite under the `except` header at `header`."""
    base = indent_of(lines[header])
    body: list[int] = []
    for i in range(header + 1, len(lines)):
        if not lines[i].strip():
            body.append(i)
            continue
        if indent_of(lines[i]) <= base:
            break
        body.append(i)
    while body and not lines[body[-1]].strip():
        body.pop()
    return body


def _js_block_body(text: str, brace: int) -> tuple[str, int] | None:
    """Body between the `{` at `brace` and its match, minus strings and comments.

    Returns (body, end offset), or None when the block never closes. Strings and
    comments are skipped rather than counted, so a brace inside a string literal
    cannot mis-nest the scan and a `/* ignore */` cannot pass as a statement.
    """
    out: list[str] = []
    depth = 0
    i, n = brace, len(text)
    while i < n:
        ch = text[i]
        if ch in "\"'`":
            quote, i = ch, i + 1
            while i < n:
                if text[i] == "\\":
                    i += 2
                    continue
                if text[i] == quote:
                    i += 1
                    break
                i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            end = text.find("*/", i + 2)
            i = n if end == -1 else end + 2
            continue
        if ch == "{":
            depth += 1
            i += 1
            if depth > 1:
                out.append(ch)
            continue
        if ch == "}":
            depth -= 1
            if depth == 0:
                return "".join(out), i
            out.append(ch)
            i += 1
            continue
        if depth >= 1:
            out.append(ch)
        i += 1
    return None


def check_swallowed_errors(targets: list[Target], findings: list[Finding]) -> None:
    """CHECK 7 - a handler that catches an error and does nothing with it.

    `except: pass` and `catch (e) {}` are the quietest cheap rescue there is: the
    check goes green, the incident goes unlogged, and the failure resurfaces
    somewhere with no stack trace attached. A handler with a log call, a re-raise
    or any real statement in it has made a decision and is left alone; only the
    empty one is reported.

    This is the one check that reads past the added lines - an empty block is
    only empty relative to the block around it. It still requires one line of the
    handler to be in the diff before it reports anything.
    """
    for t in targets:
        suffix = Path(t.path).suffix.lower()
        if suffix in PY_LIKE:
            _check_python_handlers(t, findings)
        if suffix in JS_LIKE:
            _check_brace_handlers(t, findings)


def _check_python_handlers(t: Target, findings: list[Finding]) -> None:
    for idx, line in enumerate(t.lines):
        m = EXCEPT_HEADER.match(line)
        if not m:
            continue
        tail = m.group("tail").split("#", 1)[0].strip()
        if tail:
            body_lines = [tail]
            span = [idx + 1]
        else:
            body = _python_handler_body(t.lines, idx)
            span = [idx + 1] + [i + 1 for i in body]
            body_lines = [t.lines[i].strip() for i in body
                          if t.lines[i].strip() and not t.lines[i].strip().startswith("#")]
        if body_lines and not all(s in DEAD_STATEMENTS for s in body_lines):
            continue
        if not any(n in t.added for n in span):
            continue
        findings.append(Finding(
            "swallowed-error",
            "an except handler that does nothing with the error - log it, re-raise "
            "it, or narrow the except so it only catches what you meant to ignore",
            t.path, idx + 1, line.strip()))


def _check_brace_handlers(t: Target, findings: list[Finding]) -> None:
    text = "\n".join(t.lines)
    starts = t.line_starts()
    for m in CATCH_HEADER.finditer(text):
        result = _js_block_body(text, m.start("brace"))
        if result is None:
            continue                          # unterminated block: nothing to judge
        body, end = result
        if body.strip().strip(";").strip():
            continue                          # it does something with the error
        first = bisect.bisect_right(starts, m.start())
        last = bisect.bisect_right(starts, end)
        if not any(n in t.added for n in range(first, last + 1)):
            continue
        findings.append(Finding(
            "swallowed-error",
            "an empty catch block - the error is caught and discarded; log it, "
            "re-raise it, or say in code why discarding it is correct",
            t.path, first, t.lines[first - 1].strip()))


CHECKS = (
    check_ts_suppressions,
    check_eslint_disable,
    check_python_suppressions,
    check_skipped_tests,
    check_ci_continue_on_error,
    check_git_escape_hatches,
    check_swallowed_errors,
)


# --- output ------------------------------------------------------------------

def to_sarif(findings: list[Finding]) -> dict[str, Any]:
    return {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [{
            "tool": {"driver": {
                "name": "ihsan-harness-cheap-rescue",
                "informationUri": "https://github.com/arnoldwender/ihsan-harness",
                "rules": [{"id": r} for r in sorted({f.check for f in findings})],
            }},
            "results": [{
                "ruleId": f.check,
                "level": "error",
                "message": {"text": f.message},
                "locations": [{"physicalLocation": {
                    "artifactLocation": {"uri": f.path or "."},
                    "region": {"startLine": max(f.line, 1)},
                }}],
            } for f in findings],
        }],
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--base", default="origin/main",
                    help="ref the diff is measured from (default: origin/main)")
    ap.add_argument("--files", nargs="+", metavar="PATH",
                    help="scan these files whole, instead of a diff")
    ap.add_argument("--sarif", metavar="PATH", help="write SARIF 2.1.0 to PATH")
    args = ap.parse_args(argv)

    findings: list[Finding] = []
    try:
        allowlist = load_allowlist()
        if args.files:
            targets = targets_from_files(args.files)
            scope = f"{len(targets)} file(s) named on the command line"
            skipped = len(args.files) - len(targets)
            if skipped:
                # Never let "0 findings" stand in for "I did not read it".
                scope += f"; {skipped} skipped as neither code nor CI configuration"
        else:
            base_sha = resolve_base(args.base)
            scope_map, untracked = added_lines_from_diff(base_sha)
            targets = load_targets(scope_map, whole_file=untracked)
            scope = (f"{len(targets)} file(s), "
                     f"{sum(len(t.added) for t in targets)} added line(s) "
                     f"since {base_sha[:12]}")
        for check in CHECKS:
            check(targets, findings)
        findings = [f for f in findings if not exempt(f, allowlist)]
    except GateError as exc:
        print(f"gate failure: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:                   # noqa: BLE001 - reported, never swallowed
        print(f"gate failure: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    if args.sarif:
        try:
            Path(args.sarif).write_text(json.dumps(to_sarif(findings), indent=2),
                                        encoding="utf-8")
        except OSError as exc:
            print(f"gate failure: cannot write {args.sarif}: {exc}", file=sys.stderr)
            return 2

    print(f"cheap-rescue: {scope}")
    for f in sorted(findings, key=lambda f: (f.path, f.line, f.check)):
        print(f"  FAIL [{f.check}] {f.path}:{f.line}: {f.message}")
        if f.text:
            print(f"         {f.text[:110]}")
    if findings:
        print(f"\n{len(findings)} finding(s) - each one buys a green check without "
              f"earning it")
        return 1
    print("  nothing in this change silences a check instead of satisfying it")
    return 0


if __name__ == "__main__":
    sys.exit(main())
