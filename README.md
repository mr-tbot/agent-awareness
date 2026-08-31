# agent-awareness

**Five editor windows, one machine.** Each agent session reports what it is doing, and asks before
starting anything heavy — so five builds don't launch at once and the box stops falling over.

If you run several Claude Code sessions side by side, you already know the failure. Every window
independently decides the machine looks idle. Three of them start a build in the same second, a fourth
kicks off a video render, and the next thing you see is a session killed by the OOM reaper, or a
machine so deep in swap that everything stops. Each session behaved reasonably. None of them could see
the others.

`agent-awareness` gives them a shared view, and a gate:

```console
$ agentaware board
10.9G available of 30.6G  ·  mem pressure 0.0% (oomd kills at 50% for 20s)  ·  swap 0%  ·  cpu 12%

SESSION    REPO                 DOING        FOR    WHAT
sess4000   headzup              test         9m     pytest -n 4 tests/
sess3000   MAIN-NET             thinking     7m
sess2000   whitelistwarden      reading      htaccess.sh
sess1000   schoolphones         building     2m     npm run build
sess0000   sid3kick             render       40s    ffmpeg -i take3.mov -c:v libx265 out.mp4

HOLDING A SLOT
  render    sid3kick             3m     ffmpeg -i take3.mov -c:v libx265 out.mp4

CAN I START ONE NOW?
  NO   render   1/1 render slots in use (sid3kick)
  YES  build
  YES  test
```

And instead of starting work and hoping:

```console
$ agentaware run --class render -- ffmpeg -i take.mov -c:v libx265 out.mp4
agentaware: waiting for a render slot — 1/1 render slots in use (sid3kick)
```

It waits, then runs, then releases. That is the whole idea.

## How it works

**There is no daemon.** Sessions report through Claude Code's hooks, which write into a small registry
in your per-user runtime directory (`/run/user/$UID/agent-awareness`, tmpfs, cleared at boot). Reads
and writes are serialised by a `flock` held only for the length of a transaction.

**Nothing needs cleaning up after a crash.** A session's record is valid only while the process that
wrote it is still running, proven by boot ID, PID *and* process start time — so a killed window, a
crashed extension host or a `kill -9` on a build frees its slot the next time anyone looks. There is
no TTL to tune and no heartbeat to miss. That matters more than it sounds: a heartbeat would report an
agent that has been *thinking* for four minutes as dead, because a thinking agent runs no commands.

**The gate is built on the number that actually decides whether you OOM.** On a systemd machine,
`systemd-oomd` watches your user session's cgroup and kills the leaf reclaiming hardest once memory
pressure stays above a threshold for a set duration. `agentaware` reads that threshold from `oomctl`
rather than inventing one, and refuses new heavy work well below it:

```console
$ agentaware doctor
resource signals
  ok   memory pressure    /sys/fs/cgroup/user.slice/user-1001.slice/user@1001.service/memory.pressure
  ok   oomd thresholds    kill at 50% for 20s, swap 90%  [oomctl (this user's slice)]
  ok   MemAvailable       10.6G of 30.6G
  note io control         not delegated to this user slice — IO cannot be capped, only observed
```

**Admission is a reservation, not a measurement.** This is the part that makes it work at all. A build
admitted now does not allocate its memory until a minute from now, so a gate that merely *looks* at
free memory lets every waiting session through at once. Instead the decision and the claim happen
inside the same lock: the second caller sees the first one's reservation even though the first has not
touched a byte yet.

## Install

Requires Python 3.8+ and Claude Code. Linux and macOS.

```bash
git clone https://github.com/mr-tbot/agent-awareness.git
cd agent-awareness
./install.sh
```

That puts `agentaware` on your `PATH` (`~/.local/bin`) and adds its hooks to
`~/.claude/settings.json`. **It does not disturb hooks other tools installed** — it adds one entry per
event, tagged so it can find and remove exactly its own later, backs the file up first, and refuses
outright to touch a `settings.json` that isn't valid JSON. Restart your agent sessions afterwards.

To check what actually works on your machine:

```bash
agentaware doctor
```

To remove it completely:

```bash
agentaware uninstall-hooks   # restores settings.json exactly
rm ~/.local/bin/agentaware
```

## Using it

| Command | What it does |
|---|---|
| `agentaware board` | The one screen: who is doing what, and whether there is room |
| `agentaware gate build` | One line, one exit code — may I start a build right now? |
| `agentaware run --class build -- make -j8` | Wait for room, run it holding a slot, release when done |
| `agentaware doctor` | What works on this machine, and what doesn't |
| `agentaware install-hooks` / `uninstall-hooks` | Manage only this tool's hooks |

`gate` is meant for shell conditions:

```bash
agentaware gate render && ffmpeg ...     # start only if there is room
```

`run` is what you actually want most of the time, because it waits rather than failing.

### Classes

A class says how heavy the work is. It sets how much memory must be free before the work starts, and
how many jobs of that kind may run at once.

| Class | Reserves | Concurrent | For |
|---|---|---|---|
| `render` | 4 GB | 1 | video and 3D renders, encodes |
| `build` | 2 GB | cores ÷ 4 | compiles, bundlers, packaging |
| `test` | 1 GB | 2 | test suites |
| `install` | 1 GB | 2 | dependency resolution |
| `light` | — | unlimited | everything not worth gating |

Those are starting points, not truths about your machine. `agentaware` records the memory peak it
actually observes for each class and raises the reservation when reality exceeds the default, so the
numbers get more honest the more you use it.

## What the hooks record, and what they never do

Six hooks: `SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `Stop`, `SessionEnd`.

`SessionEnd` is not optional. A session killed mid-turn never fires `Stop`, and without `SessionEnd`
its row would read "building" until the process died.

Each hook records the session, its repo, and a one-line description of the current activity.
Credential-shaped strings are stripped before anything is written — API tokens, `--password` arguments,
passwords embedded in URLs — because the board is visible to every session on the machine and the
history is written to disk.

**Every hook fails open.** If it errors, times out, or gets a payload it cannot parse, your turn
proceeds normally. A hook that can break a turn is a hook you will remove within the hour, and then
nothing is coordinated at all. The cost is about 40 ms per tool call, essentially all of it Python
interpreter startup, against a 5-second timeout.

## What it deliberately does not do

- **It does not kill or throttle anything.** It decides what may *start*. Work already running is left
  alone — freezing or killing another window's build is not a decision this tool should make for you.
- **It does not see work it wasn't told about.** A build you started in a terminal, another user's
  render, a container — none of those report. The machine-level numbers on the board still reflect
  them, so the gate still tightens, but they will not appear as rows.
- **It is one machine.** There is no fleet view. Pointing two machines at a shared directory would be
  actively wrong, and the registry deliberately lives somewhere that cannot be synced.
- **It does not control disk I/O.** On most systemd machines the `io` controller is not delegated to
  user slices, so I/O can be observed but not capped. `doctor` tells you which case you are in rather
  than pretending.
- **Command classification is a heuristic.** A wrapper script that runs a build, a `make` target that
  only runs tests, an `ssh` that compiles on another machine — it will get these wrong. So the guess
  only ever labels a row on the board. It never silently gates anything: `--class` is always explicit,
  and always wins.

## Related

- [Auto-Everything](https://github.com/mr-tbot/Auto-Everything) — agent skills that refuse to let a
  project be called finished before it demonstrably is. Its `auto-device-lock` does for *physical
  devices* what this does for machine resources, and uses the same registry pattern.

## License

MIT
