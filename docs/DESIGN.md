# Design notes

Why the thing is shaped the way it is. Each section states a constraint that was measured, not
assumed, and what it forced.

## An agent's shell call cannot hold a lock

Every Bash tool call in an agent session is a fresh `bash -c` that exits within milliseconds, while
the session process lives for hours. This is measurable: `$$` differs on every call, and a lock taken
with `flock` in one call is provably free by the next.

That rules out the obvious design — hold a lock for as long as you hold a resource. It also rules out
`export`ing anything: a variable set in one call is gone by the next.

So a *slot* is not a held lock. It is a record in a registry, and its validity is derived from whether
the process that wrote it is still alive. The `flock` is used only to make a transaction on that
registry atomic, and is held for well under a millisecond.

## Liveness beats heartbeats

The obvious way to expire a stale record is a TTL refreshed by a heartbeat. It is wrong here in both
directions.

An agent that has been *thinking* for four minutes runs no commands, so it emits no heartbeat, and a
TTL-based reaper would declare a perfectly healthy session dead and hand its slot away. Meanwhile a
wedged process happily keeps heart-beating.

Instead, a record names `(boot_id, pid, start_time)`. The record is valid if and only if that exact
process is still running. PID reuse cannot resurrect a dead record, because the start time will not
match. A reboot invalidates everything, because the boot ID will not match. There is no timer, and
nothing to tune.

One subtlety that costs people real bugs: field 22 of `/proc/<pid>/stat` cannot be read with
`awk '{print $22}'`. Field 2 is the process name in parentheses and may itself contain spaces and
`)`, so the naive split puts every later field at the wrong index. A process named `ev il) ) name`
makes that `awk` print `0` — and two unrelated dead processes then compare equal. Split after the
*last* `") "` instead.

## The gate must reserve, not measure

A build admitted at t=0 has not allocated anything at t=0. It allocates over the following minute. So
a gate that reads free memory and returns lets every waiting session through at once — they all see
the same idle machine.

Admission therefore happens *inside* the same lock that read the state, and writes a reservation
before releasing it. The second caller sees the first one's claim even though no memory has moved. The
reservation is the class's expected cost, which starts as a declared default and is raised whenever an
observed peak exceeds it.

## Gate on what actually does the killing

`MemAvailable` is a decent number but it is not what kills you. On a systemd machine, `systemd-oomd`
watches your user session's cgroup and kills the leaf reclaiming hardest once memory pressure stays
above a limit for a duration — both of which are configuration, readable with `oomctl`:

```
Swap Used Limit: 90.00%
Default Memory Pressure Limit: 60.00%
Default Memory Pressure Duration: 20s
Memory Pressure Monitored CGroups:
	Path: /user.slice/user-1001.slice/user@1001.service
		Memory Pressure Limit: 50.00%
```

Note the per-cgroup limit overriding the default. `agentaware` reads these rather than hardcoding a
threshold, and gates at half the kill limit — because oomd acts on *sustained* pressure, so arriving
at the limit means something is already about to die.

Pressure is read from that same cgroup's `memory.pressure`, `full avg10`. PSI is used rather than load
average because load average counts uninterruptible sleep: a machine thrashing on swap and a machine
compiling happily can report the same load.

## The lock file is never renamed

The registry data is replaced by `rename`, which is atomic. The *lock* file is not, and must not be:
locking a file that is then renamed over gives every later arrival a lock on a fresh inode, and mutual
exclusion silently disappears with no error anywhere. For an admission gate that means every session
being told simultaneously that it may start.

So the flock target is a separate, zero-byte file that is created once and never renamed, never
truncated and never unlinked. A cheap tripwire compares the inode we locked against the inode now at
that path, in case something else replaces it.

## Failing open is a requirement, not a nicety

A hook that can break a turn is a hook the user disables within the hour, and a disabled hook
coordinates nothing. Every hook path catches everything, and a hook that cannot parse its payload
returns success without recording anything. Failing open means not breaking the turn — it explicitly
does not mean inventing a record, because a record that cannot be correlated with a session is a row
on the board that never updates and never clears.

## What was deliberately left out

**A queue.** With five windows, the contention is shallow and the retry loop with jitter resolves it.
A priority queue with aging would be more code, more state, and another thing to be wrong. The current
design can starve a waiter in principle; in practice the slot count is small and the holds are short.
If that stops being true, the fix is a ticket file per waiter, ordered by arrival.

**Throttling running work.** Freezing or killing another window's build is a decision with a blast
radius, and it belongs to a person. This tool decides what may start.

**A fleet view.** The registry lives in a per-user runtime directory precisely because it must not be
syncable. Two machines sharing a registry would reserve against each other's memory.

**Tracking background work.** `PostToolUse` fires when the tool call returns, not when the work ends,
so a `nohup`ed or backgrounded build is invisible to the hooks. `agentaware run` covers this properly,
because it holds the slot for the life of the process it started. That asymmetry is the reason `run`
is the recommended path rather than relying on the hooks alone.
