# Troubleshooting

Triage in the order below. Most reports are one of the first three.

## "I installed it and nothing happened"

**Live sessions do not pick up newly installed hooks.** Restart your agent sessions. That is also the
rollback if something goes wrong: work already in flight is unaffected by an install.

Check what is actually registered:

```console
$ aw doctor
  hooks     5/5 installed in /home/mr-tbot/.claude/settings.json  OK
```

`0/5` means the install did not write, or wrote somewhere else — `aw` follows a symlinked
`settings.json` to its real path, so if yours lives in a dotfiles repo, look there.

## "The board says no sessions reporting"

The registry is per-boot and per-user. Reasons it is empty, in order of likelihood:

1. **Sessions have not been restarted** since the install.
2. **They are a different user.** `aw` never reads another user's registry; there is no fleet view.
3. **`/run/user/$UID` is gone.** With `Linger=no` — the default — systemd removes it when your last
   GUI/SSH session ends. Anything held there is gone by design. `aw doctor` prints the path and
   whether it is on tmpfs.
4. **A hook is failing.** Run one by hand and look at stderr:

```console
$ echo '{"session_id":"t","cwd":"/tmp"}' | aw hook session-start
{"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "..."}}
```

Every hook path catches everything and exits 0, so a broken hook is silent by design. That is
deliberate — a hook that can fail a turn is a hook you would remove — but it does mean you have to
run it manually to see the error.

## "It refuses to start anything"

```console
$ aw status --class build
WAIT  build: 2410 MiB free, this needs 2048 MiB, floor is 4096 MiB
```

The refusal always names the arithmetic. Three real causes:

- **The machine genuinely has no headroom.** Check `aw doctor` — on the machine this was written for,
  9 GB was routinely held by idle Gradle daemons, and `./gradlew --stop` freed more than any gate
  could.
- **The floor is too high for your machine.** `AW_FLOOR_MIB` defaults to 4096, which is ~13% of 32 GB.
  On a 8 GB machine that is half your memory. See [tuning.md](tuning.md).
- **A learned cost ratcheted up.** One pathological run raises a cost permanently, by design. Check
  `~/.local/state/agent-awareness/costs.json` and delete the entry to re-learn, or pass `--mem 2G`.

If the refusal says **memory pressure**, that is the veto, not the gate — something is already
thrashing. Look at what, before touching the threshold.

## "A job is stuck in the queue behind something impossible"

The queue is strict FIFO on the head, which is what stops a large job starving behind a stream of
small ones — but it also means a head job that can never be admitted blocks the rest until its
timeout (default 600 s). This is a deliberate trade.

```console
$ aw board
  QUEUE   #41 render MAIN-NET  waiting 4m02s  (head)
```

If the head is impossible — a 30 GiB reservation on a 30 GiB machine — cancel it rather than waiting.

## "Something is running that I did not start"

```console
$ aw board
  ! 1 job(s) still running with no owner (their runner was killed).  Stop them: aw doctor --reap
```

`systemd-run --scope` deliberately outlives whatever spawned it, so a runner killed by a turn
interrupt or a harness timeout leaves real work still consuming real memory. That is usually what you
want. When it is not:

```console
$ aw doctor --reap
```

`SIGKILL` runs no cleanup handler, so this state is detected by *reading* — the job's lock is free
but its scope is still active — never by the runner writing a farewell it may not live to write.

## "adb / esptool commands are being denied"

That is the sibling skill, not this tool. `auto-device-lock` installs its own `PreToolUse` hook that
denies device commands without a lease. It is doing its job:

```console
$ devlock devices        # what is attached and who holds it
$ devlock claim <id> --why "what for"
$ devlock check <id>     # one line, one exit code
```

If it denies something that touches no device, that is a bug — the guard only counts a token in
command position, so `grep adb notes.txt` must be allowed. Report the exact command.

## "The hooks are slowing things down"

Measured on the development machine: ~47 ms for the `aw` activity hook plus ~3 ms for the
`auto-device-lock` prefilter, per Bash tool call. Essentially all of the first is Python interpreter
startup.

```console
$ aw install --no-activity     # drops the per-tool-call hook, keeps the gate
```

You lose the "what is each session touching" column and keep everything else.

## "It says it refuses to run"

| Message | Meaning |
|---|---|
| `refusing to gate inside a container` | `/proc/meminfo` in a container is the **host's**, so the gate would read a number unrelated to the limit that kills you. Run `aw` on the host |
| `was created by a different machine` | The registry directory has a foreign machine-id. It must not be synced between hosts — a `(boot_id, pid, starttime)` tuple from another machine identifies a stranger's process |
| `is not valid JSON. Refusing to touch it` | Your `settings.json` is malformed. `aw` will not guess at repairing it |
| `hooks is a list, expected an object` | Same — a config shape we do not understand is left alone |
| `WORLD-writable` | `~/.claude` is mode `o+w`; any local account could add a hook that runs as you. `chmod 0700 ~/.claude`, or `aw install --fix-perms` |
| `the lock file was replaced underneath us` | Something rewrote `registry.lock`. Retry; if it repeats, find what is writing there |

## Full uninstall

```console
$ aw uninstall                  # restores settings.json exactly; keeps one .aw.bak
$ rm ~/.local/bin/aw
$ rm -rf ~/.local/state/agent-awareness    # costs.json, decisions.jsonl, history
$ rm -rf /run/user/$UID/agent-awareness    # or just reboot; it is tmpfs
```

To remove the sibling device lock's hooks as well, delete the entries marked `# auto-device-lock`
from `~/.claude/settings.json`.

## What is written where

| Path | Contents | Survives reboot |
|---|---|---|
| `/run/user/$UID/agent-awareness/` | `lock`, `seq`, `sessions/`, `jobs/`, `machine-id` | No — tmpfs, by design |
| `~/.local/state/agent-awareness/costs.json` | Learned memory cost per `class:argv0:repo` | Yes |
| `~/.local/state/agent-awareness/decisions.jsonl` | Every gate decision, for fitting the thresholds | Yes |
| `~/.claude/settings.json` | The five hook entries, marked `# agent-awareness` | Yes |
| `~/.claude/settings.json.aw.bak` | One backup, overwritten each install | Yes |

Nothing is written to your project directories.

## Reporting a bug

`aw doctor` output is the right first attachment — it carries the kill threshold, the victim cgroup,
the registry path and mode, and the hook count. Add the exact command and, if the gate is involved,
the last few lines of `decisions.jsonl`.
