#!/usr/bin/env python3
"""Prove the live hook's tests actually defend it.

    python3 tests/mutation_check_cheap_rescue_hook.py

For each mechanism in hooks/cheap-rescue-before-run.py: neuter it in a scratch
COPY, run the hook's suite against the copy, and require the suite to go RED.
The real hook is never rewritten; its bytes are compared before and after
anyway. The gate's own checks are defended by tests/mutation_check.py; this
runner covers only what the hook adds on top of the gate.

Exit 0 when every mutant was killed; 1 when any survived; 2 when this script
itself could not run.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOOK = ROOT / "hooks" / "cheap-rescue-before-run.py"
SUITE = ROOT / "tests" / "test_cheap_rescue_hook.py"

# (name, exact text to replace, replacement). Each `old` is unique in the file.
MUTANTS = [
    ("TOOLS every tool is judged, not only the four",
     'TOOLS = {"Bash", "Edit", "Write", "MultiEdit"}\n# mutation-anchor: TOOLS',
     'TOOLS = {"Bash", "Edit", "Write", "MultiEdit", "Read", "Grep", "NotebookEdit"}\n# mutation-anchor: TOOLS'),
    ("SIMULATE the edit is not simulated; new_string is judged on its own",
     "        if not old or old not in text:", "        if True:"),
    ("REPLACE_ALL only the first occurrence is replaced",
     '        text = text.replace(old, new) if e.get("replace_all") else text.replace(old, new, 1)',
     "        text = text.replace(old, new, 1)"),
    ("ADDED the whole file is judged, not the lines the edit added",
     "        added = added_by_diff(split_lines(disk), lines)\n        # mutation-anchor: added",
     "        added = set(range(1, len(lines) + 1))\n        # mutation-anchor: added"),
    ("SCOPE prose and JSON are judged as code",
     "    if pathlib.Path(target).suffix.lower() not in ro.SCANNED_SUFFIXES:", "    if False:"),
    ("ALLOWLIST the working directory's allowlist is ignored",
     "    kept = [f for f in findings if not ro.exempt(f, allow)]\n    # mutation-anchor: allowlist",
     "    kept = list(findings)\n    # mutation-anchor: allowlist"),
    ("WARNING a finding produces no text",
     '    if not findings:\n        receipt(verdict="ok", judged=judged, exempted=exempted, ms=ms, **common)\n        return 0',
     '    if True:\n        receipt(verdict="ok", judged=judged, exempted=exempted, ms=ms, **common)\n        return 0'),
]


def run_suite(hook: Path) -> bool:
    # The mutant lives in a scratch directory with no gate/ beside it: it must still
    # find the REAL gate, or every mutant dies of fail-open and this measures nothing.
    env = {**os.environ, "CHEAP_RESCUE_HOOK_UNDER_TEST": str(hook),
           "CHEAP_RESCUE_GATE": str(ROOT / "gate" / "cheap_rescue.py")}
    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(SUITE), "-q", "-x", "--no-header",
         "-p", "no:cacheprovider"],
        capture_output=True, text=True, cwd=ROOT, env=env, check=False)
    return result.returncode == 0


def main() -> int:
    original = HOOK.read_text(encoding="utf-8")
    if not run_suite(HOOK):
        print("the suite is RED before any mutation — fix that first", file=sys.stderr)
        return 2
    survivors: list[str] = []
    with tempfile.TemporaryDirectory() as scratch:
        mutant = Path(scratch) / "cheap-rescue-before-run.py"
        for name, old, new in MUTANTS:
            if original.count(old) != 1:
                print(f"  ?? {name}: anchor appears {original.count(old)} times — the "
                      f"mutation list is stale, so this script is measuring nothing")
                survivors.append(f"{name} (stale)")
                continue
            mutant.write_text(original.replace(old, new, 1), encoding="utf-8")
            if run_suite(mutant):
                print(f"  SURVIVED  {name} — removed it and the suite stayed green")
                survivors.append(name)
            else:
                print(f"  killed    {name}")
    if HOOK.read_text(encoding="utf-8") != original:
        print("the real hook file changed during the run — it must never be touched",
              file=sys.stderr)
        return 2
    if survivors:
        print(f"\n{len(survivors)} mutant(s) survived: {', '.join(survivors)}")
        return 1
    print(f"\nall {len(MUTANTS)} mutants killed; the real hook was never rewritten")
    return 0


if __name__ == "__main__":
    sys.exit(main())
