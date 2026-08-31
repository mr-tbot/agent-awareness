# Measurements

Every number in this project, with the command that produced it. Taken on the development machine
(Ubuntu, Linux 7.0.0, 16 cores, 32 GB, VS Code under snap, ~11 concurrent agent sessions). Your
machine will differ — the point is the method, and that nothing here is a guess.

## What actually kills this machine

```console
$ journalctl -u systemd-oomd --since -14d --no-pager | grep -c Killed
7
$ journalctl -u systemd-oomd --since -14d --no-pager | grep Killed | tail -1
Killed /user.slice/user-1001.slice/user@1001.service/app.slice/snap.code.code-….scope
  due to memory pressure for /user.slice/user-1001.slice/user@1001.service
  being 72.98% > 50.00% for > 20s with reclaim activity
```

Observed kill pressures: 55.97%, 64.22%, 72.51%, 72.98%, 75.65%, 78.51%.

The threshold, read rather than assumed:

```console
$ oomctl | grep -A2 'user@1001.service'
	Path: /user.slice/user-1001.slice/user@1001.service
		Memory Pressure Limit: 50.00%
$ systemctl --user show -p ManagedOOMMemoryPressureLimit user@1001.service
ManagedOOMMemoryPressureLimit=2147483648        # 2147483648/4294967295 = 50.000%
```

## The victim

```console
$ cd /sys/fs/cgroup/user.slice/user-1001.slice/user@1001.service/app.slice/snap.code.code-*.scope
$ echo $(( $(cat memory.current) / 1073741824 )) GiB; wc -l < cgroup.procs
20 GiB
95
$ find . -mindepth 1 -maxdepth 1 -type d | wc -l
0
```

Zero children means it is a leaf, and oomd kills leaves. Counting agent processes inside it:

```console
$ while read p; do readlink /proc/$p/exe; done < cgroup.procs | grep -c native-binary/claude
11
```

One kill takes all eleven, plus every editor window.

## Containment works

The whole design rests on this. A scope created from *inside* the snap tree lands as a sibling under
`app.slice`, not as a child of the editor:

```console
$ systemd-run --user --scope --collect --unit=t.scope -p MemoryMax=256M -p MemorySwapMax=0 \
    -- python3 -c "print(open('/proc/self/cgroup').read())"
0::/user.slice/user-1001.slice/user@1001.service/app.slice/t.scope
```

And the cap binds:

```console
$ systemd-run --user --scope --collect --unit=t2.scope -p MemoryMax=256M -p MemorySwapMax=0 \
    -- python3 -c "
b=bytearray()
for i in range(20): b += bytearray(64*1024*1024); print(len(b)//1048576,'MiB',flush=True)"
64 MiB
128 MiB
                       # killed here by its own cgroup; host MemAvailable unchanged
```

**`MemorySwapMax=0` is required, twice over.** Without it the cap does not bind at all — a job capped
at `MemoryMax=768M` allocated 2560 MiB by spilling into swap. And with a *partial* swap allowance the
job livelocks in reclaim instead of being killed. Swap here is zram at priority 100:

```console
$ swapon --show
NAME       TYPE      SIZE USED PRIO
/dev/zram0 partition 15.3G   0B  100
/swapfile  file        24G   0B   -1
```

zram is compressed pages **in RAM**. Letting a contained job swap gives it back the memory the cap
existed to protect.

## Why MemAvailable is the gate and PSI is only the veto

Allocating 2 GiB over 10 s while sampling both, once per second:

```
t=0   MemAvailable 11162 MB   memory.pressure full avg10 0.00
t=5   MemAvailable 10240 MB   memory.pressure full avg10 0.00
t=10  MemAvailable  9412 MB   memory.pressure full avg10 0.00
```

`MemAvailable` tracked the allocation almost 1:1. Memory PSI did not move at all — it only rises once
reclaim is already happening. So PSI cannot answer "should this start"; it can only answer "is it
already too late". It is a decisive veto and a useless gate.

That said, the veto is real. It fired unprompted during development when a leaked test scope drove the
machine up:

```
aw: waiting — memory pressure 55.01% — oomd kills this whole user session at 50% sustained 20s
```

## Signals deliberately rejected

| Signal | Measurement | Verdict |
|---|---|---|
| Load average | `load1=4.80` while `/proc/pressure/cpu some avg10=0.02` | Counts uninterruptible sleep. Would refuse work on an idle box |
| `/proc/pressure/cpu full` | `total=0` permanently, even after a load spike | Structurally always zero at system level. Unreachable threshold |
| `/proc/pressure/io some` | idles at 12–20% with only editors running | No usable baseline without subtracting an editor-specific constant |
| `IOWeight` | `cgroup.subtree_control` on `user@1001.service` = `cpu memory pids` | `io` not delegated. Setting it is a silent no-op |
| `ionice` | every NVMe queue reads `[none]` | Inert without bfq |
| GPU / VRAM | `card1` is an integrated AMD with a 512 MB carve-out | No discrete GPU; the number means nothing |
| `thermal_zone0` | reads 20.0 °C (an acpitz stub) while `k10temp` read 85.1 °C | The obvious file is the wrong one |
| `memory.peak` | a job doing a 1.5 GiB buffered write reported `peak` ≈ 1.6 GB with `anon` = 216 KiB | Charges reclaimable page cache — a ~7,400× overestimate |
| Summed RSS | 29.98 GB summed vs 22.01 GB actual cgroup usage | Double-counts shared pages by 36% |

## Cost measurement

Sampled parent-side at 1 Hz from the job's own cgroup, `anon + swap`:

```console
$ cat memory.stat | grep '^anon '; cat memory.swap.current
```

Not `memory.peak` (see above), and not an exit trap — a trap does not run on `SIGKILL`, and the OOM
path *is* `SIGKILL`, so the trap loses the measurement on exactly the runs whose demand mattered.

## Idle daemons

```console
$ ps -eo rss,comm --no-headers | awk '/java|kotlin/{s+=$1; n++} END {print s/1048576" GB across "n}'
9.1 GB across 8
```

Eight idle JVM build daemons, on a machine with 8 GiB available. `./gradlew --stop` recovers more than
any admission gate can.

## Hook costs

```console
$ time (for i in $(seq 20); do echo '{}' | ./aw.py hook stop >/dev/null; done)
```

~43 ms per invocation, essentially all Python interpreter startup (`python3 -c pass` alone is 11 ms).
Against a 5-second explicit timeout that is 100× headroom. Hook timeout defaults are **not** uniform
across events and can be as high as 600 s, so every entry this tool installs sets one explicitly.
