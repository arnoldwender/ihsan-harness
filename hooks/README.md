# Hooks — keeping the virtues present

The Codex only works if it's *in context* when the agent acts. A one-time paste into
`AGENTS.md` works; a hook makes it automatic, every session, and opens each run with the
precept.

## `session-start.sh`

Emits, to stdout:

1. The **fixed precept** (the measure of ihsan) + a rotating **precept of the day**
   (`bin/precept`, drawn from `precepts.txt`).
2. The **conduct block** — the four virtues, precedence, and the hard limit (`codex-block.md`).

It's harness-agnostic: any harness that can run a command at session start can use it, and
its stdout is plain readable text.

## Wiring it into Claude Code

Claude Code injects a `SessionStart` hook's stdout into the session context. Add to your
`settings.json` (use the **absolute** path, and check your Claude Code version's hook docs —
the schema evolves):

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          { "type": "command", "command": "/abs/path/to/ihsan-harness/hooks/session-start.sh" }
        ]
      }
    ]
  }
}
```

## Wiring it into any other harness

Run `hooks/session-start.sh` as the first step of your session bootstrap and prepend its
output to the system prompt. The precept goes first, the virtues stay present.

## `cheap-rescue-before-run.py` — the cheap rescue, refused before it lands

A Claude Code `PreToolUse` hook for `Bash`, `Edit`, `Write` and `MultiEdit`. Before the tool
runs, it hands that one change to [`gate/cheap_rescue.py`](../gate/cheap_rescue.py) — the same
seven checks, the same allowlist — and, if the change buys a green check without earning it,
tells the agent so in the tool result. It **warns**; it does not block:

> cheap-rescue: this change buys a green check without earning it. [bare-noqa] src/app.py:7:
> # noqa with no error code - name the rule, e.g. `# noqa: E501`, so the next rule to fire here
> is not silenced too. Sabr 3: refuse the cheap rescue — no silenced test, no suppressed error.
> … Warning mode: this change is NOT blocked.

Why a hook when the gate exists: the gate reads the added lines of a diff, in CI, after the
commit — a verdict, not a correction. By the time it runs the suppression has been written, the
test has been skipped, the summary that read "passing" has been sent. *Sabr* 3 is broken at the
moment of the act, under pressure, when the shortcut gleams; this is the same gate at that moment.

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash|Edit|Write|MultiEdit",
        "hooks": [
          { "type": "command",
            "command": "python3 /abs/path/to/ihsan-harness/hooks/cheap-rescue-before-run.py",
            "timeout": 10 }
        ]
      }
    ]
  }
}
```

What it hands the gate: a `Bash` command as a script, every line added. An `Edit` or
`MultiEdit` **simulated** — the file read from disk, `old_string` replaced by `new_string`
(`replace_all` honoured), and the lines a diff against the disk marks added, so the gate keeps
the context its swallowed-handler check needs: `pass` on its own is nothing; `pass` under an
`except` is the whole finding. A `Write` the same way against the file on disk, or every line
when the file is new. A file the gate would not read (prose, JSON, a lockfile) is skipped here
too, with a receipt; `NotebookEdit` is left out.

The gate is imported with the session's working directory as its root, so
`.conduct/cheap-rescue-allow.txt` — and the repository-relative paths its entries anchor on — is
the repository the agent is working in, not this one. A malformed entry is the gate's exit 2;
here it is fail-open with `verdict: error`, never a silent pass.

Every run leaves a receipt in `~/.local/state/ihsan-harness/cheap-rescue-receipts.jsonl`
(`CHEAP_RESCUE_RECEIPTS=…` to move it, `off` to disable) — verdict, checks, counts, never a line
of the file or of the command. `CHEAP_RESCUE_HOOK_MODE=block` makes it deny the change instead
(exit 2); shipped so the switch exists, not the default. Any error of its own is a receipt and
exit 0 — the hook is never the reason a session cannot proceed.

Limits, stated in the file's header so they stay decisions: a command that merely names a token
(a `grep` for a suppression) is judged as if it wrote it, as the gate does with a `.sh` file; a
shell command has no suffix, so the checks bound to one (Python's suppressions, CI's
`continue-on-error`) do not run on it; a file outside the working directory is judged by its
absolute path, out of the allowlist's reach.

Tests: [`tests/test_cheap_rescue_hook.py`](../tests/test_cheap_rescue_hook.py) ·
mutants: [`tests/mutation_check_cheap_rescue_hook.py`](../tests/mutation_check_cheap_rescue_hook.py).

## Just want to see it?

```sh
./hooks/session-start.sh
```
