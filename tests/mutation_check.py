#!/usr/bin/env python3
"""Prove the cheap-rescue gate's tests actually defend it.

    python3 tests/mutation_check.py

For each check in gate/cheap_rescue.py: remove it from the CHECKS tuple, run the
suite, and require the suite to go RED. A test that still passes with the
mechanism removed is not testing the mechanism — it is decoration that reports
green forever, which is the same cheap rescue this gate exists to catch.

Exit 0 when every mutant was killed; 1 when any survived; 2 when this script
itself could not run (same contract as the gate).

The file is restored from an in-memory copy in a `finally`, never with
`git checkout`: this repo may hold uncommitted work, and a checkout to undo a
mutation would take that work with it. The restore is then verified, because a
mutation runner that leaves the file mutated has done more harm than the bug it
was hunting.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GATE = ROOT / "gate" / "cheap_rescue.py"

# (name, the entry in the CHECKS tuple to remove). Removing the registration
# rather than the function body keeps the module importable, so a survivor means
# "the tests do not need this check", never "the mutant did not compile".
MUTANTS = [
    ("CHECK 1 ts-suppression", "    check_ts_suppressions,\n"),
    ("CHECK 2 eslint-blanket-disable", "    check_eslint_disable,\n"),
    ("CHECK 3 bare-noqa / bare-type-ignore", "    check_python_suppressions,\n"),
    ("CHECK 4 skipped-test", "    check_skipped_tests,\n"),
    ("CHECK 5 ci-continue-on-error", "    check_ci_continue_on_error,\n"),
    ("CHECK 6 no-verify / force-push", "    check_git_escape_hatches,\n"),
    ("CHECK 7 swallowed-error", "    check_swallowed_errors,\n"),
    # A false-positive guard is a mechanism too. Strip the `git` anchor from the
    # force-push pattern and the bare word `push` is enough again - the shape
    # (`drizzle-kit push --force`) the live measurement over real sessions found.
    ("CHECK 6 anchors the force push on git, not on the word push",
     r"(?<![\w-])git\b[^;&|]*?"),
]


def run_suite() -> bool:
    """True when the suite is green."""
    r = subprocess.run([sys.executable, "-m", "pytest", str(ROOT / "tests"), "-q",
                        "-x", "--no-header"],
                       capture_output=True, text=True, cwd=ROOT, check=False)
    return r.returncode == 0


def main() -> int:
    original = GATE.read_text(encoding="utf-8")

    if not run_suite():
        print("the suite is RED before any mutation — fix that first", file=sys.stderr)
        return 2

    survivors: list[str] = []
    try:
        for name, entry in MUTANTS:
            if original.count(entry) != 1:
                print(f"  ?? {name}: registration not found exactly once in the gate — "
                      f"the mutation list is stale")
                survivors.append(f"{name} (stale)")
                continue
            GATE.write_text(original.replace(entry, "", 1), encoding="utf-8")
            if run_suite():
                print(f"  SURVIVED  {name} — removed it and the suite stayed green")
                survivors.append(name)
            else:
                print(f"  killed    {name}")
    finally:
        GATE.write_text(original, encoding="utf-8")

    if GATE.read_text(encoding="utf-8") != original:
        print("gate file was NOT restored cleanly", file=sys.stderr)
        return 2

    if survivors:
        print(f"\n{len(survivors)} mutant(s) survived: {', '.join(survivors)}")
        return 1
    print(f"\nall {len(MUTANTS)} mutants killed; gate restored and verified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
