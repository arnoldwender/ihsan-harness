#!/usr/bin/env python3
"""The Ihsan Harness — the cheap rescue, refused before it lands.

A Claude Code `PreToolUse` hook for `Bash`, `Edit`, `Write` and `MultiEdit`.
Before the tool runs, it hands that one change to the cheap-rescue gate
(`gate/cheap_rescue.py`) — the same seven checks, the same allowlist — and, if
the change buys a green check without earning it (a bare suppression, a test
switched off, a bypassed hook, a swallowed error), tells the agent so in the
tool result. It warns; it does not block. See hooks/README.md for the wiring
and the README for why.

    "hooks": {"PreToolUse": [{"matcher": "Bash|Edit|Write|MultiEdit", "hooks": [
        {"type": "command", "command": "python3 /abs/path/to/ihsan-harness/hooks/cheap-rescue-before-run.py",
         "timeout": 10}]}]}

WHY A HOOK AND NOT ONLY THE GATE
--------------------------------
The gate reads the added lines of a diff, in CI, after the commit. That is the
right place for a verdict and the wrong place for a correction: by the time it
runs, the suppression has been written, the test has been skipped, the summary
that read "passing" has been sent. SABR 3 — refuse the cheap rescue — is broken
at the moment of the act, under pressure, when the shortcut gleams; a gate that
answers a day later answers a different agent. This hook is the same gate at
that moment, on the one change about to be made.

WHAT IT DOES
------------
1. Reads the hook payload from stdin: `tool_name`, `tool_input`, `cwd`,
   `session_id`. Ignores every tool but the four above (`NotebookEdit` is left
   out: a notebook is JSON, which the gate does not read).
2. Imports the gate with `HARNESS_ROOT` set to the session's working directory,
   so `.conduct/cheap-rescue-allow.txt` is the working repository's, and file
   paths are judged relative to it — not to this repository.
3. Builds ONE of the gate's own `Target(path, lines, added)` for the call:
   * `Bash` — the command text, every line added. It is a script delivered
     differently; the gate judges a `.sh` file the same way.
   * `Edit` / `MultiEdit` — the edit is SIMULATED: the file is read from disk,
     `old_string` is replaced by `new_string` (all occurrences with
     `replace_all`), and the result is what the gate reads. `added` is what a
     line diff between the file on disk and the result marks inserted or
     replaced — exactly the lines `git diff` would show — so the gate gets the
     added lines WITH their context, which is what the swallowed-handler check
     needs: `pass` on its own is nothing; `pass` under an `except` is the whole
     finding. A context line that `new_string` repeats unchanged is not added.
     If `old_string` is not found the tool will fail on its own; `new_string`
     alone is judged, all of it added, and the receipt says `simulated: false`.
   * `Write` — the content, with `added` as above against the file on disk, or
     every line when the file does not exist yet.
   A file the gate itself would skip — prose, JSON, a lockfile, anything not in
   its `SCANNED_SUFFIXES` — is skipped here too, with a receipt. Same scope,
   one definition.
4. Runs the gate's `CHECKS` on that target and applies its allowlist the way
   the gate does. A malformed allowlist is a gate error; here that is fail-open
   with `verdict: error`, never a silent pass and never a warning it did not
   earn.
5. If anything remains: prints `{"hookSpecificOutput": {"hookEventName":
   "PreToolUse", "additionalContext": "..."}}` and exits 0. Claude Code adds
   that text to the agent's context alongside the tool result. The permission
   flow is not touched.
6. Appends one receipt line per run to `CHEAP_RESCUE_RECEIPTS` (default
   `~/.local/state/ihsan-harness/cheap-rescue-receipts.jsonl`; `off` disables):
   `{ts, session, tool, path, verdict, checks, findings, exempted, judged, ms}`
   — names and counts, never a line of the file or of the command. The receipts
   are how the rate gets measured on real sessions, which is the number this
   hook needs before anyone should let it block.

WHAT IT DOES NOT SEE (stated so they stay decisions)
---------------------------------------------------
* A command that merely NAMES a token — a `grep` for a suppression, a `sed`
  that removes one — is judged as if it wrote it. The gate has the same limit
  on a `.sh` file. Whether that matters is a count over the receipts.
* A shell command has no file suffix, so the checks that are bound to one
  (Python's suppressions, CI's `continue-on-error`) do not run on it; a
  suppression written into a `.py` file through `sed -i` is not seen. The
  Edit or Write that does the same thing is.
* An `Edit` whose `old_string` matches more than once without `replace_all`:
  the tool refuses it; the hook judges the first match and says nothing about
  the refusal.
* A file outside the working directory is judged by its absolute path, so an
  allowlist entry anchored on a repository-relative path does not reach it.
* Any runtime other than Claude Code, and any tool but the four named.

MODES AND FAIL-OPEN
-------------------
`CHEAP_RESCUE_HOOK_MODE=warn` (default) injects the text and exits 0.
`CHEAP_RESCUE_HOOK_MODE=block` writes it to stderr and exits 2, which Claude
Code treats as a denial. Block is shipped so the switch exists; it is not the
default, because a guard whose false-positive rate nobody has measured on real
sessions is switched off by the first person it wrongly stops.
Any error of the hook's own is a receipt with `verdict: error` and exit 0. The
hook is never the reason a session cannot proceed.

Tests: tests/test_cheap_rescue_hook.py · mutants: tests/mutation_check_cheap_rescue_hook.py
"""
from __future__ import annotations

import difflib
import importlib.util
import json
import os
import pathlib
import re
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
GATE = pathlib.Path(os.environ.get("CHEAP_RESCUE_GATE") or HERE.parent / "gate" / "cheap_rescue.py")


def _default_receipts() -> str:
    state = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return os.path.join(state, "ihsan-harness", "cheap-rescue-receipts.jsonl")


RECEIPTS = os.environ.get("CHEAP_RESCUE_RECEIPTS") or _default_receipts()
MODE = os.environ.get("CHEAP_RESCUE_HOOK_MODE", "warn")          # warn | block
TOOLS = {"Bash", "Edit", "Write", "MultiEdit"}
# mutation-anchor: TOOLS
MAX_SHOWN = 4
COMMAND_PATH = "<command>"


# --- receipts ----------------------------------------------------------------

def receipt(**row: object) -> None:
    """One JSON line per run. Names and counts only — never a line of the file."""
    if RECEIPTS == "off":
        return
    try:
        pathlib.Path(RECEIPTS).parent.mkdir(parents=True, exist_ok=True)
        row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **row}
        with open(RECEIPTS, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 — a receipt never brings the hook down
        pass


# --- the gate ----------------------------------------------------------------

_GATES: dict[tuple[str, str], object] = {}


def load_gate(root: str):
    """Import the gate by path with `HARNESS_ROOT` = the session's working directory.
    The gate fixes its root — and with it the allowlist — at import time. Cached per
    root, so a measurement over thousands of calls imports it once per repository."""
    key = (str(GATE), root)
    if key in _GATES:
        return _GATES[key]
    os.environ["HARNESS_ROOT"] = root
    spec = importlib.util.spec_from_file_location("ihsan_cheap_rescue_gate", GATE)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod                        # dataclasses resolve annotations here
    spec.loader.exec_module(mod)
    _GATES[key] = mod
    return mod


# --- building the target -----------------------------------------------------

def split_lines(text: str) -> list[str]:
    """The lines the gate would see: `"\\n"`-separated, no empty segment after a final
    newline (as `splitlines()` counts them), a `"\\r"` left where a file carries one."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def added_by_diff(old: list[str], new: list[str]) -> set[int]:
    """1-based lines of `new` that a line diff against `old` marks inserted or replaced —
    the lines `git diff` would show as added. The common prefix and suffix are stripped
    first: an edit touches a few lines of a large file, and the diff is only of what is
    left between them."""
    head = 0
    while head < len(old) and head < len(new) and old[head] == new[head]:
        head += 1
    tail = 0
    while (tail < len(old) - head and tail < len(new) - head
           and old[-1 - tail] == new[-1 - tail]):
        tail += 1
    mid_old, mid_new = old[head:len(old) - tail], new[head:len(new) - tail]
    added: set[int] = set()
    sm = difflib.SequenceMatcher(None, mid_old, mid_new, autojunk=False)
    for tag, _, _, j1, j2 in sm.get_opcodes():
        if tag in ("insert", "replace"):
            added.update(range(head + j1 + 1, head + j2 + 1))
    return added
# mutation-anchor: added_by_diff


def simulate(text: str, edits: list[dict]) -> tuple[str, bool]:
    """Apply the edits in order to `text`. Returns (result, simulated); `simulated` is
    False when an `old_string` was not found — the tool will refuse that edit, and what
    is judged instead is every `new_string` on its own."""
    for e in edits:
        old = str(e.get("old_string") or "")
        new = str(e.get("new_string") or "")
        if not old or old not in text:
            return "\n".join(str(x.get("new_string") or "") for x in edits), False
        text = text.replace(old, new) if e.get("replace_all") else text.replace(old, new, 1)
    return text, True
# mutation-anchor: simulate


def edits_of(tool: str, given: dict) -> list[dict]:
    if tool == "MultiEdit" and isinstance(given.get("edits"), list):
        return [e for e in given["edits"] if isinstance(e, dict)]
    return [given]


def rel_path(target: str, root: str) -> str:
    """The path as the gate spells it: repository-relative, forward slashes. Outside the
    root it stays absolute — an anchored allowlist entry cannot reach it, and should not."""
    rel = os.path.relpath(target, root)
    if rel.startswith(".."):
        return target.replace(os.sep, "/")
    return rel.replace(os.sep, "/")


def build_target(ro, tool: str, given: dict, root: str) -> tuple[object | None, dict]:
    """The gate's `Target` for this one call, and what the receipt should say about it.
    `None` with a reason when there is nothing the gate would read."""
    if tool == "Bash":
        command = str(given.get("command") or "")
        if not command.strip():
            return None, {"skip": "empty"}
        lines = split_lines(command)
        return ro.Target(COMMAND_PATH, lines, set(range(1, len(lines) + 1))), {"path": COMMAND_PATH}

    target = str(given.get("file_path") or "")
    if not target:
        return None, {"skip": "no-path"}
    if not os.path.isabs(target):
        target = os.path.join(root, target)
    target = os.path.normpath(target)
    info: dict = {"path": target}
    if pathlib.Path(target).suffix.lower() not in ro.SCANNED_SUFFIXES:
        return None, {**info, "skip": "not-code"}          # the gate's own scope, one definition
    # mutation-anchor: scope
    exists = os.path.isfile(target)
    try:
        disk = pathlib.Path(target).read_text(encoding="utf-8") if exists else ""
    except (OSError, UnicodeDecodeError):
        return None, {**info, "skip": "unreadable"}       # binary or unreadable: the gate skips it too

    if tool == "Write":
        result, simulated = str(given.get("content") or ""), True
    elif exists:
        result, simulated = simulate(disk, edits_of(tool, given))
        info["simulated"] = simulated
    else:
        result, simulated = "\n".join(str(e.get("new_string") or "")
                                      for e in edits_of(tool, given)), False
        info["simulated"] = simulated
    lines = split_lines(result)
    if simulated and exists:
        added = added_by_diff(split_lines(disk), lines)
        # mutation-anchor: added
    else:
        added = set(range(1, len(lines) + 1))
    if not added:
        return None, {**info, "skip": "nothing-added"}
    return ro.Target(rel_path(target, root), lines, added), info


# --- the predicate -----------------------------------------------------------

def run_gate(ro, target) -> tuple[list, int]:
    """The gate's checks on one target, its allowlist applied as the gate applies it.
    Returns (findings kept, findings exempted)."""
    findings: list = []
    for check in ro.CHECKS:
        check([target], findings)
    allow = ro.load_allowlist()                          # a malformed entry raises: fail-open
    kept = [f for f in findings if not ro.exempt(f, allow)]
    # mutation-anchor: allowlist
    kept.sort(key=lambda f: (f.path, f.line, f.check))
    return kept, len(findings) - len(kept)


def assess(tool: str, given: dict, root: str) -> tuple[list, int, int, dict]:
    """(findings, lines judged, findings exempted, receipt info) for one tool call."""
    ro = load_gate(root)
    target, info = build_target(ro, tool, given, root)
    if target is None:
        return [], 0, 0, info
    kept, exempted = run_gate(ro, target)
    return kept, len(target.added), exempted, info


def judge(tool: str, tool_input: dict, root: str) -> tuple[list, int]:
    """Findings the gate raises for ONE tool call, judged from `root`, and the number of
    lines it judged. Importable, so the rate can be measured over real sessions without
    spawning a process per call."""
    findings, judged, _, _ = assess(tool, tool_input or {}, root)
    return findings, judged


# --- the warning -------------------------------------------------------------

def safe(text: str) -> str:
    """A path without what could break the warning's markdown or smuggle text shaped like
    an instruction (backticks, line breaks, control characters). The agent already saw the
    path in its own tool input; this is depth, not a boundary."""
    return re.sub(r"[`\r\n\t\x00-\x1f\x7f]", "?", text)[:120]


def message(findings: list) -> str:
    parts = [f"[{f.check}] {safe(f.path)}:{f.line}: {f.message}." for f in findings[:MAX_SHOWN]]
    more = f" (+{len(findings) - MAX_SHOWN} more)" if len(findings) > MAX_SHOWN else ""
    return ("cheap-rescue: this change buys a green check without earning it. "
            + " ".join(parts) + more
            + " Sabr 3: refuse the cheap rescue — no silenced test, no suppressed error. Say "
            "why (a rule name, an error code, a sentence) or fix the cause; a justified "
            "exception goes in .conduct/cheap-rescue-allow.txt with its reason. Warning "
            "mode: this change is NOT blocked.")


# --- main --------------------------------------------------------------------

def main() -> int:
    t0 = time.time()
    payload = json.loads(sys.stdin.read() or "{}")
    tool = payload.get("tool_name")
    if tool not in TOOLS:
        return 0
    given = payload.get("tool_input") or {}
    root = str(payload.get("cwd") or os.getcwd())
    session = str(payload.get("session_id") or "")[:8]
    common = {"session": session, "tool": tool, "mode": MODE}

    findings, judged, exempted, info = assess(tool, given, root)
    ms = int((time.time() - t0) * 1000)
    if "skip" in info:
        if info["skip"] in ("empty", "no-path"):
            return 0                                     # nothing was handed in
        receipt(verdict="skip", why=info["skip"], path=info.get("path"), ms=ms, **common)
        return 0
    common["path"] = info.get("path")
    if "simulated" in info:
        common["simulated"] = info["simulated"]
    if not findings:
        receipt(verdict="ok", judged=judged, exempted=exempted, ms=ms, **common)
        return 0
    # mutation-anchor: warning
    receipt(verdict="finding", judged=judged, exempted=exempted, findings=len(findings),
            checks=sorted({f.check for f in findings}), ms=ms, **common)
    text = message(findings)
    if MODE == "block":
        sys.stderr.write(text.replace("Warning mode: this change is NOT blocked.",
                                      "Block mode: this change was not made.") + "\n")
        return 2
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                             "additionalContext": text}}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 — fail open on purpose: the hook is never the blocker
        receipt(verdict="error", error=f"{type(exc).__name__}: {exc}"[:200])
        sys.exit(0)
