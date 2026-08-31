# Hooks

`aw install` adds four entries to `~/.claude/settings.json`, each tagged `# agent-awareness` inside
the command string — which is how `aw uninstall` finds exactly its own and nothing else.

| Event | Records | Why it is needed |
|---|---|---|
| `SessionStart` | `starting`, and returns the board as `additionalContext` | The only reliable way to make a session aware the tool exists |
| `UserPromptSubmit` | `working` | The turn began |
| `Stop` | `idle` | The turn ended normally |
| `SessionEnd` | removes the record | A session killed mid-turn never fires `Stop` |

`SessionEnd` is not optional. Without it a session that dies mid-turn shows as "working" forever.
(`SIGKILL` fires no hook at all — that case is handled by liveness, not by hooks.)

## Why the board is pushed, not pulled

Any instruction phrased "first check the board" gets skipped. So the board is not something a session
is asked to consult — `SessionStart` returns it as `additionalContext`, which puts it in the session's
context before the first turn:

```json
{"hookSpecificOutput": {"hookEventName": "SessionStart",
  "additionalContext": "agent-awareness: 4 session(s) live, 1 job(s) running (HeadzUp). 6.1 GiB headroom.\nWrap builds, renders, tests and installs: `aw run --class build -- <cmd>` …"}}
```

No `CLAUDE.md` edit, nothing to drift, always current.

## Why PreToolUse and PostToolUse are not installed

Both were tried and rejected on measurements, not taste:

- **A `PreToolUse` deny livelocks.** A hook that denies a build with "re-run it via `aw run`" denies
  the `aw run` wrapper too, because the matcher fires on that command as well. The observed outcome
  was the agent giving up and the work never running.
- **Classifying a Bash command is 77% false-positive** on real shell history — wrapper scripts,
  `make` targets that only run tests, the word "build" inside a heredoc, `ssh` compiling on another
  machine entirely.
- **`PostToolUse` fires when the call returns, not when the work ends.** A 45-second task reported in
  46 ms. It cannot see background work, which is exactly the work worth tracking.
- **Recording command strings leaks credentials.** Real history is full of them.
- **Hook timeouts are not uniform**; an omitted `timeout` on `PreToolUse` was observed running for
  minutes before being killed. On the Bash critical path that is a stall on every tool call.

What is lost by cutting them is *enforcement*. That trade is stated on the tin: this tool is
cooperative. A session that never calls `aw run` is not managed.

## Identity

The hook payload is the only identity a hook gets — the hook environment carries no `CLAUDE_*` or
`VSCODE_*` variables at all. Fields used: `session_id` (the key; **if absent, nothing is recorded**,
because a record that cannot be correlated never updates and never clears) and `cwd`.

Hooks run under `sh -c`, and that shell exits milliseconds later — so `getppid()` is useless for
liveness. `aw` walks up `/proc/<ppid>` until `readlink /proc/N/exe` looks like an agent binary
(1–2 hops). `readlink(exe)`, never `pgrep -f`, which matches the debugging commands themselves.

## Merge safety

The installer:

- refuses a `settings.json` that is not valid JSON, or whose `hooks` key is not an object;
- follows symlinks, so a dotfiles repo is not silently detached;
- preserves the file mode;
- keeps exactly one backup, `settings.json.aw.bak`, rather than accumulating timestamped litter;
- removes only entries carrying its marker, so other tools' hooks survive byte-for-byte;
- refuses to install at all if the settings *directory* is group- or world-writable, since any local
  uid could then add a hook that runs as you (`aw install --fix-perms` will chmod it 0700).

Installing three times leaves one entry. `aw uninstall` restores the file exactly.
