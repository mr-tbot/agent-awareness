# agent-awareness

**Five editor windows on one machine, each deciding independently that it looks idle.**

Three of them start a build in the same second, a fourth starts a video render, and a minute later
something gets killed. Every session behaved reasonably. None of them could see the others.

`agent-awareness` gives them a shared queue, one admission gate, and — the part that actually matters
— it runs each admitted job in its own memory-capped cgroup, so that when something has to die it is
the build and not your editor.

## The specific thing this fixes

On the machine it was written for, `systemd-oomd` had killed the shared VS Code cgroup **seven times
in fourteen days**:

```console
$ journalctl -u systemd-oomd --since -14d | grep Killed
Killed /user.slice/user-1001.slice/user@1001.service/app.slice/snap.code.code-….scope
  due to memory pressure for /user.slice/user-1001.slice/user@1001.service
  being 78.51% > 50.00% for > 20s with reclaim activity
```

Under snap — and under most desktop installs — **every VS Code window shares one cgroup**, and every
agent session runs inside it:

```console
$ aw doctor
  KILLER          systemd-oomd, user@1001.service, 50.0% for 20s  [oomctl]
                  7 kill(s) in the journal in the last 14 days.  This is the number that has to fall.
  VICTIM          snap.code.code-f015f920-0f9d-4196-8947-a6e4d19
                  22.1 GiB, 94 pids, 0 child cgroup(s)  <- a leaf, so oomd's preferred victim
                  11 agent session(s) inside it — they all die together
```

Zero child cgroups makes that scope a *leaf*, which is exactly what oomd prefers to kill. So one
build overrunning does not lose you a build. It loses you every window and all eleven sessions.

`aw run` puts the job somewhere else:

```console
$ aw run --class build -- ./gradlew assembleDebug
aw: admitted (9377 MiB free − 2048 = 7329 MiB, floor 4096); cost 2048 MiB [learned]
```

The job lands in `user@1001.service/app.slice/aw-<id>.scope` — a *sibling* of the editor, not a child
— with limits derived from its measured cost — `MemoryHigh` at 1.5x to throttle, `MemoryMax` at 3x as
the hard wall, and `MemorySwapMax=0`, without which the cap does not bind at all. Verified: a process
capped at `MemoryMax=256M` was killed by its own cgroup at 128 MiB allocated, while the host's
`MemAvailable` did not move.

## What it looks like

```console
$ aw board

  CLEAR TO BUILD — 6.1 GiB headroom after a 2.0 GiB job

  SESSION    WINDOW    REPO                   STATE     DOING                  METERED JOB
  a3f2 (this one) win-4077  MAIN-NET          working   writing aw.py          —
  7b91       win-4077  HeadzUp                working   running gradlew        build gradlew 4m12s 1.9 GiB
  c04e       win-5523  whitelistwarden        idle 3m   —                      —
  e5da       win-5523  seotrack-app           working   port python3 :8080     —
  1188       win-6001  StreamScope            working   reading Manifest.xml   —

  QUEUE   empty

  MEMORY  8.1 GiB free of 30.6 GiB  ·  oomd pressure 0.00% (kills at 50% for 20s)
  ! 9.1 GiB held by 8 idle JVM daemons.  Reclaim: ./gradlew --stop
```

That last line is not decoration. On the machine above, `./gradlew --stop` reclaims more memory than a
perfect admission gate ever could.

Blocked looks like this, and says why in the units the decision was made in:

```
  WAIT — 2.4 GiB free, a build needs 2.0 GiB plus a 4.0 GiB floor

  QUEUE   #41 build  HeadzUp   waiting 1m18s  (head)
          #42 render MAIN-NET  waiting 0m31s
```

## Install

Linux with systemd 254+ and Python 3.8+. No dependencies.

```bash
git clone https://github.com/mr-tbot/agent-awareness.git
cd agent-awareness
./install.sh
```

That links `aw` into `~/.local/bin` and adds five hooks to `~/.claude/settings.json`. Then restart
your agent sessions — **live sessions do not pick up newly installed hooks.**

```bash
aw doctor       # what is killing this machine, and what to do about it
aw uninstall    # removes only our hooks, restoring the file exactly
```

The installer refuses to touch a `settings.json` that isn't valid JSON, follows symlinks so a dotfiles
repo survives, preserves file mode, keeps one backup, and removes only entries carrying its own
marker. Installing three times leaves one entry.

## Using it

| Command | |
|---|---|
| `aw board` | Who is doing what, and whether there is room |
| `aw status --class build` | One line, one exit code — usable as `aw status && make` |
| `aw run --class build -- make -j8` | Queue, then run in a capped cgroup |
| `aw doctor` | The killer, the victim, and the two things that would help most |
| `aw doctor --reap` | Stop jobs whose runner died but whose work is still running. It stops only jobs **this registry has a record for**; a scope it cannot account for is reported, never killed |

Classes only pick a starting cost: `build` 2 GiB, `render` 4 GiB, `test` 1 GiB, `install` 512 MiB,
`other` 256 MiB. Override with `--mem 6G`. After the first run of a given command in a given repo the
cost is **measured**, not guessed — sampled from the job's own cgroup at 1 Hz and stored per
`class:argv0:repo`.

## What each session reports

The board answers "what is everyone doing", not just "is the machine busy":

| Shown | From |
|---|---|
| `running gradlew` | the command's `argv[0]`, never the command |
| `port python3 :8080` | a port literal in the command |
| `writing server.py` / `reading Manifest.xml` | the file's basename |
| `fetching developer.android.com` | the URL's host |
| `subagent code-reviewer` | the subagent type |

This comes from an **observe-only** `PreToolUse` hook. It never returns a permission decision, so it
cannot livelock the way a denying gate does, and it records only literal fields — never the command
string. Skip it with `aw install --no-activity` if you would rather not spend ~43 ms per tool call.

## How it decides

One predictive term and one veto, both read inside the same lock that records the decision:

```
admit  if  MemAvailable − (already reserved) − (this job's cost)  ≥  floor
   and if  memory pressure on the oomd-watched cgroup < veto
```

`MemAvailable` is the gate because it moves *before* the kill. Memory PSI is the **veto**, never the
gate: allocating 2 GiB on the test machine moved `full avg10` not at all — it stays 0.00 until reclaim
is already underway, which answers "is it already too late", not "should this start". But when it does
rise it is decisive, because that exact file is oomd's own input.

Admission is a **reservation, not a measurement**. A build admitted now allocates over the next
minute, so a gate that merely reads free memory admits every waiting session at once — they all see
the same idle machine. The decision and the claim happen under one lock, so the second caller sees the
first one's claim before a byte has moved.

Both thresholds are starting points, not science. `AW_FLOOR_MIB` (4096) and `AW_VETO_PSI` (20.0) are
environment knobs, every decision — admissions **and refusals** — is appended to
`~/.local/state/agent-awareness/decisions.jsonl`, and
[docs/tuning.md](docs/tuning.md) is one page on fitting them from your own machine.

## Environment

| Variable | |
|---|---|
| `AW_FLOOR_MIB` | MiB that must remain free after a job is admitted (default 4096) |
| `AW_VETO_PSI` | Memory pressure above which nothing new starts (default 20.0) |
| `AW_DIR` | Registry location. Default `$XDG_RUNTIME_DIR/agent-awareness` |
| `AW_STATE` | Learned costs and the decision log. Default `$XDG_STATE_HOME/agent-awareness` |
| `AW_SETTINGS` | The settings file `aw install` edits. Default `~/.claude/settings.json` |

The last three exist so the tool can be tested without touching your real state — `test_aw.py` uses
them, and so should you when trying something out.

## Exit codes

| | |
|---|---|
| 0 | ok / admitted |
| 1 | busy — the gate said wait, or the queue timed out waiting for a slot |
| 2 | refused — a container, a foreign machine-id, a malformed settings file |
| 3 | error |
| other | `aw run` returns **the exit code of the command it ran**, so anything else came from your job, not from `aw` |

## What it does not do

1. **It does not enforce.** Nothing blocks a tool call. An agent that does not call `aw run` is not
   managed. This is cooperative by design — see [docs/hooks.md](docs/hooks.md), including the
   deny-hook that livelocked by denying the very wrapper it was recommending.
2. **No command-string classification.** Guessing "is this a build" from a shell command was 77%
   false-positive on real history. The board reports that a session is `running gradlew` — a literal
   fact — never that it is "building". The cost class is something you declare to `aw run`, never
   something a hook infers.
3. **No command strings anywhere.** Only `argv[0]`, a file's basename, or a URL's host. Real shell
   history is full of credentials, and the board is readable by every session on the machine.
4. **No CPU, IO, GPU or thermal gating.** Each was measured and rejected: the `io` controller is not
   delegated to user slices so `IOWeight` is a silent no-op; `/proc/pressure/cpu full` is structurally
   always zero at system level; load average read 4.80 against a CPU PSI of 0.02. See
   [docs/measurements.md](docs/measurements.md).
5. **No containers.** Inside a container `/proc/meminfo` is the host's, so the gate would read a
   number unrelated to the limit that kills. `aw` detects this and refuses rather than guessing.
6. **No macOS, no fleet view.** The registry lives in a per-user runtime directory precisely so it
   cannot be synced between machines, and refuses to start if it finds a foreign machine-id.
7. **It does not kill or throttle work already running.** It decides what may start. Stopping someone
   else's build is a decision for a person.

## Does it work?

The honest metric is **oomd kills per week**, and `aw doctor` prints it. Baseline on the development
machine was 7 in 14 days. If that number does not fall, the tool failed — however good the board looks.

## Optional: the VS Code status bar

[`vscode-extension/`](vscode-extension/) is a status bar item — session count, running jobs, free
memory — with the full board on hover and a click to open it in a terminal. Packaged as a `.vsix`
(`npx @vscode/vsce package`), no marketplace account needed.

It is honest about one thing that a per-window view naturally implies and should not: **activity is
per session, memory is not.** Every window shares one cgroup, so pressure is shared fate and an oomd
kill takes all of them at once. The tooltip says so.

## Related

- [Auto-Everything](https://github.com/mr-tbot/Auto-Everything) — agent skills that refuse to let a
  project be called finished before it demonstrably is. Its `auto-device-lock` does for *physical
  devices* what this does for memory, using the same registry pattern.

## License

MIT
