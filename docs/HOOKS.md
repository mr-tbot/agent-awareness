# Hooks reference

`agentaware install-hooks` adds six entries to `~/.claude/settings.json`. Each is tagged with the
comment marker `# agent-awareness`, which is how the tool later finds and removes exactly its own.

| Event | Recorded activity | Why it is needed |
|---|---|---|
| `SessionStart` | `starting`, and prints the machine's state into the session's context | The only reliable way to make a session aware the tool exists |
| `UserPromptSubmit` | `thinking` | The turn began; no tool is running yet |
| `PreToolUse` | classified from the command | Where a build, render, test or install is recognised |
| `PostToolUse` | `working` | The call returned; the turn continues |
| `Stop` | `idle` | The turn finished normally |
| `SessionEnd` | removes the record | A session killed mid-turn never fires `Stop` |

`PreToolUse` is matched against `Bash|Edit|Write|Read|Grep|Glob|NotebookEdit`.

## The merge

The installer never rewrites `settings.json` wholesale. For each event it removes only entries
carrying its own marker, appends one fresh entry, and leaves every other tool's hooks byte-for-byte
intact. It backs the file up to `settings.json.bak-<timestamp>` first, and if the file is not valid
JSON it refuses and changes nothing rather than guessing.

Installing repeatedly is safe: three installs leave one entry. `uninstall-hooks` restores the file to
exactly what it was.

## Payload fields used

From the hook's stdin JSON:

- `session_id` — the key everything is stored under. **If absent, nothing is recorded**, because a
  record that cannot be correlated never updates and never clears.
- `cwd` — the repo shown on the board.
- `tool_name`, and for Bash `tool_input.command` — what the session is doing.

## Redaction

Anything credential-shaped is stripped before it is written: `--password`/`--token`/`--api-key`
arguments, recognisable token prefixes (`ghp_`, `sk-`, `xox…`, `AKIA…`), and passwords embedded in
URLs. Only the first line of a command is kept, truncated to 160 characters.

The board is readable by every session running as your user, and the history at
`~/.local/state/agent-awareness/history.jsonl` is on disk. Treat both as you would a shell history.

## Cost

About 40 ms per tool call, essentially all of it Python interpreter startup, against a 5-second hook
timeout. `agentaware doctor` measures it on your machine.
