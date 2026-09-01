#!/usr/bin/env python3
"""aw — agent-awareness: a cooperative admission gate for machines running
several agent sessions at once.

The problem it exists for, measured on the machine it was written on:
systemd-oomd killed the shared VS Code cgroup six times in eleven days, each
time taking every editor window and all eleven agent sessions inside it. Every
session had independently looked at an idle-seeming machine and started a build.

So: one queue, one gate, and every admitted job wrapped in its own memory-capped
cgroup so that when something has to die it is the build and not the editor.

No daemon, no dependencies, Linux + systemd only. Python 3.8+.
"""
from __future__ import annotations

import argparse
import errno
import fcntl
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

VERSION = "1.1.1"
MARK = "# agent-awareness"
UID = os.getuid()

# Both are starting points, not science. Every decision is logged so they can be
# fitted from your own machine — see docs/tuning.md.
FLOOR_MIB = int(os.environ.get("AW_FLOOR_MIB", "4096"))
VETO_PSI = float(os.environ.get("AW_VETO_PSI", "20.0"))

# Default cost in MiB. --class only picks one of these; learned values override.
COSTS = {"build": 2048, "render": 4096, "test": 1024, "install": 512, "other": 256}

RC_OK, RC_BUSY, RC_REFUSED, RC_ERROR, RC_TIMEOUT = 0, 1, 2, 3, 75

USER_CG = Path(f"/sys/fs/cgroup/user.slice/user-{UID}.slice/user@{UID}.service")
PSI_FILE = USER_CG / "memory.pressure"


def die(msg: str, rc: int = RC_ERROR) -> "NoReturn":       # noqa: F821
    print(f"aw: {msg}", file=sys.stderr)
    raise SystemExit(rc)


# ------------------------------------------------------------------ platform

def require_linux_systemd() -> None:
    if sys.platform != "linux":
        die("Linux only. macOS has no /run/user/$UID and no systemd-oomd, so the "
            "gate would have nothing to read and nothing to contain a job with.")
    if not USER_CG.is_dir():
        die(f"no user cgroup at {USER_CG}. systemd 254+ with cgroup v2 is required.")


def in_container() -> str:
    """Containers see the HOST's /proc/meminfo, so the gate would read a number
    unrelated to the limit that actually kills the container. Refuse rather than
    give a confident wrong answer."""
    if Path("/.dockerenv").exists():
        return "/.dockerenv exists"
    try:
        if "docker" in Path("/proc/1/cgroup").read_text() or \
           "libpod" in Path("/proc/1/cgroup").read_text():
            return "pid 1 is in a container cgroup"
    except OSError:
        pass
    return ""


def boot_id() -> str:
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def machine_id() -> str:
    for p in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            return Path(p).read_text().strip()
        except OSError:
            continue
    return "unknown"


# ------------------------------------------------------------------ liveness

def stat_field(pid: int, index: int) -> str | None:
    """Field `index` (1-based, as in proc(5)) of /proc/<pid>/stat.

    Split after the LAST ') '. Field 2 is the comm in parentheses and may contain
    spaces and ')' — a process named 'ev il) ) name' makes awk '{print $22}'
    print 0, and two unrelated dead processes then compare equal.
    """
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    try:
        return raw.rsplit(") ", 1)[1].split()[index - 3]
    except IndexError:
        return None


def starttime(pid: int) -> str | None:
    return stat_field(pid, 22)


def alive(pid: int, start: str | None, rec_boot: str) -> bool:
    if rec_boot != boot_id():
        return False
    cur = starttime(pid)
    return cur is not None and cur == start


def agent_pid() -> tuple[int, str | None]:
    """The agent process this hook or command belongs to.

    Not getppid(): hooks run under `sh -c`, and that shell exits milliseconds
    later, so every liveness check would mark every session dead. Walk up until
    /proc/N/exe looks like an agent binary. readlink(exe), never pgrep -f —
    pgrep matches the debugging commands themselves.
    """
    pid = os.getpid()
    for _ in range(24):
        try:
            ppid = int(stat_field(pid, 4) or 0)
        except (TypeError, ValueError):
            break
        if ppid <= 1:
            break
        pid = ppid
        try:
            exe = os.readlink(f"/proc/{pid}/exe")
        except OSError:
            continue
        if exe.endswith("/claude") or "/native-binary/claude" in exe or \
           os.path.basename(exe) in ("claude", "codex"):
            return pid, starttime(pid)
    return os.getpid(), starttime(os.getpid())


def window_id(apid: int) -> str:
    """The editor process the session belongs to. Display only — several
    sessions share one window, and under snap every window shares one cgroup."""
    pid = apid
    for _ in range(6):
        try:
            ppid = int(stat_field(pid, 4) or 0)
        except (TypeError, ValueError):
            return "-"
        if ppid <= 1:
            return "-"
        pid = ppid
        try:
            if os.path.basename(os.readlink(f"/proc/{pid}/exe")) in (
                    "code", "code-insiders", "codium", "cursor", "windsurf"):
                return f"win-{pid}"
        except OSError:
            continue
    return "-"


# ------------------------------------------------------------------ registry

def reg_dir() -> Path:
    """Per-user, per-boot. Deliberately somewhere that cannot be synced: a
    (boot_id, pid, starttime) tuple shared between machines identifies a
    stranger's process."""
    base = os.environ.get("AW_DIR") or os.environ.get("XDG_RUNTIME_DIR") \
        or f"/run/user/{UID}"
    d = Path(base) / "agent-awareness" if not os.environ.get("AW_DIR") else Path(base)
    (d / "sessions").mkdir(mode=0o700, parents=True, exist_ok=True)
    (d / "jobs").mkdir(mode=0o700, parents=True, exist_ok=True)
    mid = d / "machine-id"
    if mid.exists():
        if mid.read_text().strip() != machine_id():
            die(f"{d} was created by a different machine. If this directory is "
                f"synced between hosts, it must not be — remove it and re-run.",
                RC_REFUSED)
    else:
        mid.write_text(machine_id())
    return d


def state_dir() -> Path:
    """Survives reboot: the cost estimator and the decision log must, or the
    tool re-learns everything from scratch every morning."""
    base = os.environ.get("AW_STATE") or os.environ.get("XDG_STATE_HOME") \
        or os.path.expanduser("~/.local/state")
    d = Path(base) / "agent-awareness"
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    return d


class Lock:
    """One short-lived exclusive flock over the whole decide-and-record step.

    The gate MUST decide inside this. Sampling /proc outside and reusing the
    value inside is the check-then-act bug that admits every waiting session at
    once: they all read the same idle machine before any of them allocates.
    """

    def __init__(self, wait: float = 20.0):
        self.path = reg_dir() / "lock"
        self.wait = wait
        self.fd = -1

    def __enter__(self):
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
        deadline = time.monotonic() + self.wait
        while True:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as e:
                if e.errno not in (errno.EWOULDBLOCK, errno.EACCES):
                    raise
                if time.monotonic() >= deadline:
                    os.close(self.fd)
                    die("registry lock busy for 20s; something is wedged")
                time.sleep(0.02)
        if os.stat(self.path).st_ino != os.fstat(self.fd).st_ino:
            os.close(self.fd)
            die("the lock file was replaced underneath us; retry")
        return self

    def __exit__(self, *a):
        os.close(self.fd)          # releases the flock on any exit path
        return False


def write_json(path: Path, obj: dict) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent))
    try:
        os.write(fd, json.dumps(obj, sort_keys=True).encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


def write_inplace(fd: int, obj: dict) -> None:
    """Rewrite a file we hold an flock on, WITHOUT replacing its inode.

    write_json() renames a temp file into place, which is atomic but produces a
    NEW inode — and an flock follows the inode, not the path. Rewriting a locked
    job file that way silently drops the lock: the next reader opens the fresh
    inode, finds it unlocked, and concludes the job is dead while it is running.
    """
    data = json.dumps(obj, sort_keys=True).encode()
    os.ftruncate(fd, 0)
    os.pwrite(fd, data, 0)
    os.fsync(fd)


def read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def next_seq() -> int:
    """Monotonic ticket. Callers already hold the registry lock."""
    p = reg_dir() / "seq"
    try:
        n = int(p.read_text().strip()) + 1
    except (OSError, ValueError):
        n = 1
    p.write_text(str(n))
    return n


def live_sessions() -> list[dict]:
    out = []
    for f in sorted((reg_dir() / "sessions").glob("*.json")):
        rec = read_json(f)
        if not rec:
            f.unlink(missing_ok=True)
            continue
        if alive(rec.get("agent_pid", 0), rec.get("starttime"), rec.get("boot_id", "")):
            out.append(rec)
        else:
            f.unlink(missing_ok=True)
        # A session record whose agent process is gone is not stale data to be
        # aged out — it is provably wrong, and deleting it is the whole reason
        # this design needs no heartbeat and no TTL.
    return out


def scope_active(unit: str) -> bool:
    if not unit:
        return False
    try:
        r = subprocess.run(["systemctl", "--user", "is-active", unit],
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip() == "active"
    except Exception:
        return False


def live_jobs() -> list[dict]:
    """A job is alive while its own file is flocked by the runner, OR while its
    scope unit is still active.

    Both clauses are load-bearing. The flock dies with the runner — but
    `systemd-run --scope` survives SIGKILL of whatever spawned it, so flock
    alone would free a slot whose memory is still very much in use.
    """
    out = []
    for f in sorted((reg_dir() / "jobs").glob("*.json")):
        rec = read_json(f)
        if not rec:
            f.unlink(missing_ok=True)
            continue
        held = True
        try:
            fd = os.open(f, os.O_RDWR | os.O_CLOEXEC)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held = False                      # we got it: nobody holds it
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                held = True
            finally:
                os.close(fd)
        except OSError:
            continue
        active = scope_active(rec.get("scope", ""))
        if held or active:
            # Flock gone but the scope still alive means the runner died and the
            # work did not. SIGKILL runs no cleanup handler, so this is detected
            # by reading, never by the runner writing a farewell it may not live
            # to write. The memory is still committed, so the job still counts
            # against the budget — it just has nobody to stop it.
            rec["ownerless"] = (not held) and active
            out.append(rec)
        else:
            f.unlink(missing_ok=True)
    return sorted(out, key=lambda r: r.get("seq", 0))


# ---------------------------------------------------------------------- gate

def mem_available_mib() -> int:
    m = re.search(rb"MemAvailable:\s+(\d+)", Path("/proc/meminfo").read_bytes())
    return int(m.group(1)) // 1024 if m else 0


def mem_total_mib() -> int:
    m = re.search(rb"MemTotal:\s+(\d+)", Path("/proc/meminfo").read_bytes())
    return int(m.group(1)) // 1024 if m else 0


def psi_full10() -> float:
    """Memory pressure of the cgroup systemd-oomd actually watches.

    This exact file is oomd's own input — not /proc/pressure/memory. It is the
    veto, never the gate: measured here, allocating 2 GiB moved it not at all
    (0.00 throughout) while MemAvailable fell by 1.7 GB. It only rises once
    reclaim is already happening, which answers 'is it already too late', not
    'should this start'.
    """
    try:
        m = re.search(r"full avg10=([\d.]+)", PSI_FILE.read_text())
        return float(m.group(1)) if m else 0.0
    except OSError:
        return 0.0


def oomd_limit() -> tuple[float, str]:
    """The threshold that will actually kill us, read rather than invented."""
    try:
        r = subprocess.run(["systemctl", "--user", "show", "-p",
                            "ManagedOOMMemoryPressureLimit", f"user@{UID}.service"],
                           capture_output=True, text=True, timeout=5)
        raw = int(r.stdout.strip().split("=", 1)[1])
        if raw:
            return raw / 4294967295 * 100, "systemctl"
    except Exception:
        pass
    try:
        out = subprocess.run(["oomctl"], capture_output=True, text=True,
                             timeout=10).stdout
        mine = f"/user.slice/user-{UID}.slice/user@{UID}.service"
        m = re.search(re.escape(mine) + r"\s*\n\s*Memory Pressure Limit:\s*([\d.]+)%", out)
        if m:
            return float(m.group(1)), "oomctl"
        m = re.search(r"Default Memory Pressure Limit:\s*([\d.]+)%", out)
        if m:
            return float(m.group(1)), "oomctl default"
    except Exception:
        pass
    return 50.0, "assumed"


def gate(cost_mib: int) -> tuple[bool, str]:
    """One predictive term, one veto. Two file reads. Call inside the lock."""
    psi = psi_full10()
    limit, _ = oomd_limit()
    if psi >= VETO_PSI:
        return False, (f"memory pressure {psi:.2f}% — oomd kills this whole user "
                       f"session at {limit:.0f}% sustained 20s")
    avail = mem_available_mib()
    if avail - cost_mib < FLOOR_MIB:
        return False, (f"{avail} MiB free, this needs {cost_mib} MiB, "
                       f"floor is {FLOOR_MIB} MiB")
    return True, f"{avail} MiB free − {cost_mib} = {avail - cost_mib} MiB, floor {FLOOR_MIB}"


def cost_key(cls: str, cmd: list[str]) -> str:
    arg0 = os.path.basename(cmd[0]) if cmd else "?"
    return f"{cls}:{arg0}:{os.path.basename(os.getcwd())}"


def costs_file() -> Path:
    return state_dir() / "costs.json"


def cost_for(cls: str, cmd: list[str]) -> tuple[int, str]:
    d = read_json(costs_file()) or {}
    rec = d.get(cost_key(cls, cmd))
    if rec and rec.get("mib"):
        return int(rec["mib"]), "learned"
    return COSTS.get(cls, COSTS["other"]), "default"


def learn_cost(cls: str, cmd: list[str], observed_mib: int) -> None:
    """Learned cost = max observed x 1.15. Ratchets up fast, decays slowly.

    Measured from anon+swap, never memory.peak: memory.peak charges reclaimable
    page cache, so a job doing a 1.5 GiB buffered write reported a peak 7,400x
    its actual anonymous memory — a number that would refuse every future build
    that writes artifacts.
    """
    if observed_mib <= 0:
        return
    d = read_json(costs_file()) or {}
    k = cost_key(cls, cmd)
    prev = int((d.get(k) or {}).get("mib", 0))
    d[k] = {"mib": max(prev, int(observed_mib * 1.15)),
            "observed": observed_mib, "at": int(time.time()),
            "runs": int((d.get(k) or {}).get("runs", 0)) + 1}
    write_json(costs_file(), d)


def log_decision(rec: dict) -> None:
    """Every gate decision, so the two thresholds can be fitted from evidence
    instead of argued about. One os.write of one line: a single write(2) to a
    regular file is serialised under the inode lock, which is what makes
    concurrent appends from several sessions safe."""
    line = (json.dumps(rec, sort_keys=True) + "\n").encode()[:2048]
    if not line.endswith(b"\n"):
        line = line[:-1] + b"\n"
    try:
        fd = os.open(state_dir() / "decisions.jsonl",
                     os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_CLOEXEC, 0o600)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
    except OSError:
        pass


# --------------------------------------------------------------------- run

def cmd_run(args) -> int:
    require_linux_systemd()
    why = in_container()
    if why:
        die(f"refusing to gate inside a container ({why}). /proc/meminfo here is "
            f"the host's, so the gate would read a number unrelated to the limit "
            f"that kills this container. Run aw on the host.", RC_REFUSED)
    cmd = list(args.cmd)
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        die("nothing to run. Usage: aw run --class build -- make -j8")

    cost, src = cost_for(args.klass, cmd)
    if args.mem:
        cost, src = parse_mib(args.mem), "declared"
    apid, astart = agent_pid()
    uid = uuid.uuid4().hex[:8]
    scope = f"aw-{uid}.scope"

    # Claim a queue position immediately, so arrival order is fixed before any
    # waiting happens and a big job cannot be starved by a stream of small ones.
    with Lock():
        seq = next_seq()
        job = {"seq": seq, "uuid": uid, "class": args.klass, "cost_mib": cost,
               "cost_src": src, "scope": scope, "state": "waiting",
               "agent_pid": apid, "starttime": astart, "boot_id": boot_id(),
               "repo": os.path.basename(os.getcwd()), "cwd": os.getcwd(),
               "cmd_display": " ".join(cmd[:2])[:80],
               "queued_at": time.time(), "started_at": 0}
        jf = reg_dir() / "jobs" / f"{seq:08d}-{uid}.json"
        write_json(jf, job)

    # Hold this job file for the life of the run. Python fds are close-on-exec
    # by default, so the child cannot inherit it and keep the slot after the
    # runner is gone — the phantom-slot bug that `flock -o` exists to avoid.
    jfd = os.open(jf, os.O_RDWR | os.O_CLOEXEC)
    fcntl.flock(jfd, fcntl.LOCK_EX)
    deadline = time.monotonic() + args.timeout
    waited, announced = 0.0, False
    try:
        while True:
            with Lock():
                jobs = live_jobs()
                running = [j for j in jobs if j.get("state") == "running"]
                waiting = [j for j in jobs if j.get("state") == "waiting"]
                head = waiting[0] if waiting else None
                is_head = head is not None and head.get("uuid") == uid
                if is_head:
                    ok, reason = gate(cost + sum(j["cost_mib"] for j in running))
                    if ok:
                        job["state"] = "running"
                        job["started_at"] = time.time()
                        write_inplace(jfd, job)
                        log_decision({"t": int(time.time()), "key": cost_key(args.klass, cmd),
                                      "cost": cost, "avail": mem_available_mib(),
                                      "psi_full10": psi_full10(), "verdict": "admit",
                                      "waited_s": int(waited)})
                        break
                else:
                    ahead = [j for j in waiting if j.get("seq", 0) < seq]
                    reason = f"{len(ahead)} job(s) ahead of you in the queue"
            if time.monotonic() >= deadline:
                log_decision({"t": int(time.time()), "key": cost_key(args.klass, cmd),
                              "cost": cost, "avail": mem_available_mib(),
                              "psi_full10": psi_full10(), "verdict": "timeout",
                              "waited_s": int(waited)})
                print(f"aw: gave up after {int(waited)}s — {reason}", file=sys.stderr)
                print("aw: retry, or run it yourself knowing the machine is loaded",
                      file=sys.stderr)
                return RC_TIMEOUT
            if not announced:
                print(f"aw: waiting — {reason}", file=sys.stderr)
                announced = True
            nap = 2.0 + random.random()      # jitter: lockstep polling wastes lock traffic
            time.sleep(nap)
            waited += nap

        print(f"aw: admitted ({reason}); cost {cost} MiB [{src}]", file=sys.stderr)
        return launch(cmd, scope, cost, args.klass, jf, job)
    finally:
        try:
            os.close(jfd)
        except OSError:
            pass
        # Do not delete the job file while the scope is still alive.
        #
        # `systemd-run --scope` deliberately outlives whatever spawned it, so a
        # runner killed by a turn interrupt or a harness timeout leaves real
        # work still consuming real memory. Deleting the record there would free
        # the slot against memory that is very much still in use — and leave the
        # scope as an orphan nothing knows how to find. Mark it detached instead:
        # live_jobs() still counts it via scope_active(), the board shows it, and
        # `aw doctor --reap` can stop it.
        if scope_active(scope):
            job["state"] = "detached"
            job["detached_at"] = time.time()
            try:
                write_json(jf, job)      # the flock is gone by here anyway
            except OSError:
                pass
        else:
            jf.unlink(missing_ok=True)


def parse_mib(s: str) -> int:
    m = re.fullmatch(r"(\d+)\s*([KMGkmg])?i?[Bb]?", s.strip())
    if not m:
        die(f"cannot parse memory size: {s!r} (try 2G, 512M)")
    n, unit = int(m.group(1)), (m.group(2) or "M").upper()
    return {"K": n // 1024, "M": n, "G": n * 1024}[unit]


def launch(cmd, scope, cost_mib, cls, jf, job) -> int:
    """Run the job in its own transient scope, capped.

    This is the part that changes who dies. Under snap, every VS Code window
    shares ONE cgroup scope — measured here at 20 GiB with 95 processes and 11
    agent sessions in it, and zero child cgroups, which makes it a leaf and so
    oomd's preferred victim. A build started as a plain child is charged to that
    scope, and when oomd fires the whole editor dies. `systemd-run --user
    --scope` places the job as a SIBLING under app.slice instead, so the cap
    binds the build and the build is what gets killed.
    """
    unit = ["systemd-run", "--user", "--scope", "--collect", f"--unit={scope}",
            # Explicit: never let systemd expand $VARS in the user's command.
            "--expand-environment=no", "--quiet",
            "-p", "MemoryAccounting=yes",
            "-p", f"MemoryHigh={int(cost_mib * 1.5)}M",
            "-p", f"MemoryMax={int(cost_mib * 3)}M",
            # MemorySwapMax=0 is required, twice over. Without it the cap does
            # not bind at all — measured, a job capped at MemoryMax=768M
            # allocated 2560 MiB by spilling into swap. And with a *partial*
            # swap allowance the job livelocks in reclaim instead of being
            # killed — also measured. Zero swap makes memory.max a hard wall the
            # kernel enforces promptly. On a box where swap is zram at priority
            # 100, "swap" is compressed pages in RAM anyway: exactly the memory
            # the cap exists to protect.
            "-p", "MemorySwapMax=0",
            "-p", "ManagedOOMMemoryPressure=kill", "--"] + cmd
    try:
        proc = subprocess.Popen(unit)
    except FileNotFoundError:
        print("aw: systemd-run not found; running uncontained (no memory cap, and "
              "this job is charged to the editor's cgroup)", file=sys.stderr)
        try:
            return subprocess.call(cmd)
        except FileNotFoundError:
            die(f"no such command: {cmd[0]}", 127)

    peak = poll_cost(scope, proc)
    rc = proc.wait()
    if peak > 0:
        learn_cost(cls, cmd, peak)
        print(f"aw: peak {peak} MiB (anon+swap); recorded", file=sys.stderr)
    return rc


def poll_cost(scope: str, proc) -> int:
    """Sample anon+swap at 1 Hz from the job's own cgroup, parent-side.

    Not an exit trap: the trap does not run on SIGKILL, and the OOM path IS
    SIGKILL — so the trap loses the measurement on exactly the runs whose demand
    mattered most.
    """
    cg = USER_CG / "app.slice" / scope
    peak = 0
    while proc.poll() is None:
        try:
            m = re.search(rb"^anon (\d+)", (cg / "memory.stat").read_bytes(), re.M)
            anon = int(m.group(1)) if m else 0
            swap = int((cg / "memory.swap.current").read_text().strip() or 0)
            peak = max(peak, (anon + swap) // (1024 * 1024))
        except (OSError, ValueError, AttributeError):
            pass
        time.sleep(1.0)
    return peak


# -------------------------------------------------------------------- board

def gib(mib: int) -> str:
    return f"{mib / 1024:.1f} GiB"


def _age(ts: float) -> str:
    d = max(0, int(time.time() - ts))
    return f"{d}s" if d < 60 else f"{d // 60}m{d % 60:02d}s" if d < 3600 \
        else f"{d // 3600}h{(d % 3600) // 60:02d}m"


def jvm_holding() -> tuple[int, int]:
    """Idle build daemons routinely hold more memory than the gate can ever free.
    Measured here: 9.1 GB across 8 JVMs on a box with 8 GiB available."""
    total = n = 0
    try:
        for p in Path("/proc").iterdir():
            if not p.name.isdigit():
                continue
            try:
                comm = (p / "comm").read_text().strip()
                if comm not in ("java", "kotlin-daemon", "GradleDaemon"):
                    continue
                m = re.search(rb"VmRSS:\s+(\d+)", (p / "status").read_bytes())
                if m:
                    total += int(m.group(1)) // 1024
                    n += 1
            except OSError:
                continue
    except OSError:
        pass
    return total, n


def board_text(me_apid: int | None = None) -> str:
    avail, total = mem_available_mib(), mem_total_mib()
    psi = psi_full10()
    limit, _ = oomd_limit()
    with Lock():
        sessions = live_sessions()
        jobs = live_jobs()
    running = [j for j in jobs if j.get("state") == "running" or j.get("ownerless")]
    waiting = [j for j in jobs if j.get("state") == "waiting"]
    detached = [j for j in jobs if j.get("ownerless")]
    reserved = sum(j["cost_mib"] for j in running)
    probe = COSTS["build"]
    out = []

    if psi >= VETO_PSI:
        out.append(f"  STOP — memory pressure {psi:.1f}%, oomd kills this whole "
                   f"user session at {limit:.0f}% sustained 20s")
    elif avail - reserved - probe < FLOOR_MIB:
        out.append(f"  WAIT — {gib(avail)} free, a build needs {gib(probe)} "
                   f"plus a {gib(FLOOR_MIB)} floor")
    else:
        out.append(f"  CLEAR TO BUILD — {gib(avail - reserved - probe)} headroom "
                   f"after a {gib(probe)} job")
    out.append("")

    if sessions:
        out.append(f"  {'SESSION':<10} {'WINDOW':<9} {'REPO':<22} {'STATE':<9} "
                   f"{'DOING':<26} METERED JOB")
        run_by = {}
        for j in running:
            run_by.setdefault(j.get("agent_pid"), []).append(j)
        for s in sorted(sessions, key=lambda r: r.get("started_at", 0)):
            tag = s["session_id"][:4] + (" (this one)" if s.get("agent_pid") == me_apid else "")
            jobs_here = run_by.get(s.get("agent_pid"), [])
            doing = "  ".join(f"{j['class']} {j['cmd_display']} {_age(j['started_at'])} "
                              f"{gib(j['cost_mib'])}" for j in jobs_here) or "—"
            state = s.get("state", "?")
            if state == "idle" and s.get("last_seen"):
                state = f"idle {_age(s['last_seen'])}"
            act = " ".join(x for x in (s.get("verb", ""), s.get("object", "")) if x)
            out.append(f"  {tag:<10} {s.get('window','-'):<9} "
                       f"{s.get('repo','?')[:22]:<22} {state:<9} "
                       f"{act[:26]:<26} {doing}")
    else:
        out.append("  no sessions reporting — run `aw install`, then restart them")
    out.append("")

    if waiting:
        out.append("  QUEUE")
        for i, j in enumerate(waiting):
            out.append(f"    #{j['seq']} {j['class']:<7} {j.get('repo','?'):<18} "
                       f"waiting {_age(j['queued_at'])}" + ("  (head)" if i == 0 else ""))
    else:
        out.append("  QUEUE   empty")
    out.append("")
    if detached:
        out.append(f"  ! {len(detached)} job(s) still running with no owner "
                   f"(their runner was killed).  Stop them: aw doctor --reap")
        out.append("")
    out.append(f"  MEMORY  {gib(avail)} free of {gib(total)}"
               f"  ·  oomd pressure {psi:.2f}% (kills at {limit:.0f}% for 20s)")
    jvm, njvm = jvm_holding()
    if jvm > 1024:
        out.append(f"  ! {gib(jvm)} held by {njvm} idle JVM daemons.  "
                   f"Reclaim: ./gradlew --stop")
    return "\n".join(out)


def cmd_board(args) -> int:
    if args.json:
        with Lock():
            sessions, jobs = live_sessions(), live_jobs()
        limit, src = oomd_limit()
        print(json.dumps({
            "machine": {"mem_available_mib": mem_available_mib(),
                        "mem_total_mib": mem_total_mib(),
                        "psi_full10": psi_full10(), "oomd_limit_pct": limit,
                        "oomd_limit_source": src, "floor_mib": FLOOR_MIB,
                        "veto_psi": VETO_PSI},
            "sessions": sessions, "jobs": jobs}, indent=1))
        return RC_OK
    apid, _ = agent_pid()
    print()
    print(board_text(apid))
    print()
    return RC_OK


def cmd_status(args) -> int:
    """One line, one exit code: may a job of this cost start right now?"""
    cost = parse_mib(args.mem) if args.mem else COSTS.get(args.klass, COSTS["other"])
    with Lock():
        running = [j for j in live_jobs() if j.get("state") == "running"]
        ok, why = gate(cost + sum(j["cost_mib"] for j in running))
    print(("CLEAR " if ok else "WAIT  ") + f"{args.klass}: {why}")
    return RC_OK if ok else RC_BUSY


# -------------------------------------------------------------------- hooks

EVENTS = [("SessionStart", "session-start"), ("UserPromptSubmit", "prompt"),
          ("Stop", "stop"), ("SessionEnd", "end")]
# Optional, installed unless --no-activity: reports WHAT a session is touching.
ACTIVITY_EVENT = ("PreToolUse", "activity")
# The `activity` hook is PreToolUse, and it is strictly OBSERVE-ONLY: it never
# returns a permission decision, so it cannot livelock the way a denying gate
# does (a deny that says "re-run via aw run" denies the wrapper too, and the
# agent abandons the work). It records only literal fields from the payload —
# the tool name, a file's basename, a command's argv[0], a URL's host — and
# never a command string, because real shell history is full of credentials.
# It costs ~43 ms on a tool call against a 5 s timeout, which is why it is
# separable: `aw install --no-activity` leaves it out.
#
# PostToolUse is absent unconditionally. Each of these was measured to be
# independently disqualifying: classifying Bash commands is 77% false-positive
# on real history; a PreToolUse deny that says "re-run via aw run" denies the
# wrapper too and the agent gives up; PostToolUse fires when the CALL returns,
# not when the WORK ends; and recording command strings leaks credentials. What
# is lost is enforcement — this tool is cooperative, and says so on the tin.


_PORT = re.compile(r"(?:(?:--|-)port[= ]|localhost:|127\.0\.0\.1:|0\.0\.0\.0:|:)(\d{2,5})\b")


def describe(payload: dict) -> tuple[str, str]:
    """What is this session touching? Literal fields only — never a guess.

    Returns (verb, object). Nothing here classifies: `make` is reported as
    running `make`, not as "a build", because deciding whether a command is a
    build from its text was 77% false-positive on real history. The cost class
    is something the caller declares to `aw run`, not something a hook infers.
    """
    tool = payload.get("tool_name", "")
    ti = payload.get("tool_input") or {}
    if tool == "Bash":
        cmd = str(ti.get("command", ""))
        argv0 = ""
        for tok in cmd.strip().split():
            if "=" in tok and not tok.startswith("-"):
                continue                       # leading VAR=value assignments
            argv0 = os.path.basename(tok.strip("\"'()"))
            break
        m = _PORT.search(cmd)
        if m and 1 <= int(m.group(1)) <= 65535:
            return "port", f"{argv0} :{m.group(1)}"
        return "running", argv0[:24]
    if tool in ("Edit", "MultiEdit", "Write", "NotebookEdit"):
        return "writing", os.path.basename(str(ti.get("file_path", "")))[:32]
    if tool in ("Read", "Grep", "Glob"):
        target = ti.get("file_path") or ti.get("path") or ti.get("pattern") or ""
        return "reading", os.path.basename(str(target))[:32]
    if tool in ("WebFetch", "WebSearch"):
        url = str(ti.get("url", ""))
        m = re.match(r"https?://([^/]+)", url)
        return "fetching", (m.group(1) if m else "web")[:32]
    if tool == "Task":
        return "subagent", str(ti.get("subagent_type", ""))[:24]
    return "using", tool[:24]


def cmd_hook(args) -> int:
    """Hooks never block a tool call and never fail a turn."""
    try:
        raw = sys.stdin.read() if not sys.stdin.isatty() else ""
        payload = json.loads(raw) if raw.strip() else {}
    except ValueError:
        payload = {}
    try:
        return _hook(args.event, payload)
    except Exception as e:                                     # noqa: BLE001
        print(f"# agent-awareness hook error (ignored): {e}", file=sys.stderr)
        return 0


def _hook(event: str, payload: dict) -> int:
    sid = payload.get("session_id")
    if not sid:
        # Nothing to correlate against. A record we cannot key is a row on the
        # board that never updates and never clears.
        return 0
    apid, astart = agent_pid()
    f = reg_dir() / "sessions" / f"{re.sub(r'[^A-Za-z0-9_-]', '', sid)[:64]}.json"

    if event == "end":
        f.unlink(missing_ok=True)
        return 0

    state = {"session-start": "starting", "prompt": "working", "stop": "idle"}.get(
        event, "working")
    verb = obj = ""
    if event == "activity":
        verb, obj = describe(payload)
        state = "working"
    cwd = payload.get("cwd") or os.getcwd()
    prev = read_json(f) or {}
    write_json(f, {
        "session_id": sid, "agent_pid": apid, "starttime": astart,
        "boot_id": boot_id(), "window": window_id(apid),
        "cwd": cwd, "repo": os.path.basename(cwd) or cwd,
        "branch": git_branch(cwd), "state": state,
        "verb": verb or (prev.get("verb", "") if event != "stop" else ""),
        "object": obj or (prev.get("object", "") if event != "stop" else ""),
        "started_at": prev.get("started_at") or time.time(),
        "last_seen": time.time(),
    })

    if event == "session-start":
        # The board is not something the agent is asked to check — any step
        # phrased "first check the board" is skipped. It is the first thing in
        # the session instead.
        with Lock():
            n = len(live_sessions())
            running = [j for j in live_jobs() if j.get("state") == "running"]
        avail = mem_available_mib()
        reserved = sum(j["cost_mib"] for j in running)
        head = (f"agent-awareness: {n} session(s) live, {len(running)} job(s) running"
                + (f" ({', '.join(j['repo'] for j in running)})" if running else "")
                + f". {gib(max(0, avail - reserved - FLOOR_MIB))} headroom.")
        ctx = (head + "\nWrap builds, renders, tests and installs: "
                      "`aw run --class build -- <cmd>` — it queues behind other "
                      "windows and caps the job so an overrun kills the build "
                      "instead of every editor window. `aw board` shows who is "
                      "doing what.")
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "SessionStart", "additionalContext": ctx}}))
    return 0


def git_branch(cwd: str) -> str:
    try:
        r = subprocess.run(["git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"],
                           capture_output=True, text=True, timeout=3)
        return r.stdout.strip()[:20] if r.returncode == 0 else ""
    except Exception:
        return ""


# ------------------------------------------------------------------- install

def settings_path() -> Path:
    return Path(os.environ.get("AW_SETTINGS")
                or os.path.expanduser("~/.claude/settings.json"))


def cmd_install(args) -> int:
    p = settings_path()
    d = p.parent
    if d.exists():
        mode = d.stat().st_mode & 0o777
        if args.fix_perms and (mode & 0o022):
            d.chmod(0o700)
            print(f"aw: tightened {d} from {oct(mode)[2:]} to 0700")
        elif mode & 0o002:
            # World-writable is not a style question: any account on the box can
            # drop a hook in here that runs as you, with your keys, on every
            # session. Refuse rather than add one more file to that directory.
            die(f"{d} is mode {oct(mode)[2:]} — WORLD-writable. Any local account "
                f"can add a hook there that runs as you, on every session.\n"
                f"  Fix: chmod 0700 {d}   (or: aw install --fix-perms)", RC_REFUSED)
        elif mode & 0o020:
            print(f"aw: warning — {d} is mode {oct(mode)[2:]} (group-writable). "
                  f"Anyone in that group can add hooks that run as you. "
                  f"chmod 0700 {d} if that group is not just you.", file=sys.stderr)

    # Follow the symlink: writing a new file over it would silently detach a
    # dotfiles repo and the user's next commit would show nothing.
    target = Path(os.path.realpath(p))
    try:
        cfg = json.loads(target.read_text()) if target.exists() and target.read_text().strip() else {}
    except ValueError as e:
        die(f"{target} is not valid JSON ({e}). Refusing to touch it.", RC_REFUSED)
    if not isinstance(cfg, dict):
        die(f"{target} is not a JSON object. Refusing.", RC_REFUSED)
    if "hooks" in cfg and not isinstance(cfg["hooks"], dict):
        die(f"settings.json 'hooks' is a {type(cfg['hooks']).__name__}, expected an "
            f"object. Refusing rather than overwriting a config we do not understand.",
            RC_REFUSED)

    exe = shutil.which("aw") or os.path.abspath(sys.argv[0])
    added = []
    events = list(EVENTS) + ([] if args.no_activity else [ACTIVITY_EVENT])
    # Strip every hook of ours from every event we could ever install, before
    # adding back only the ones selected. Removing just the ones being installed
    # would leave a previously-installed activity hook in place when the user
    # runs --no-activity precisely to get rid of it.
    for ev, _ in list(EVENTS) + [ACTIVITY_EVENT]:
        groups = (cfg.get("hooks") or {}).get(ev)
        if isinstance(groups, list):
            groups[:] = [g for g in groups if MARK not in json.dumps(g)]
            if not groups:
                cfg["hooks"].pop(ev, None)
    for ev, arg in events:
        entries = cfg.setdefault("hooks", {}).setdefault(ev, [])
        if not isinstance(entries, list):
            die(f"hooks.{ev} is not a list. Refusing.", RC_REFUSED)
        entry = {"hooks": [{"type": "command",
                            "command": f'"{exe}" hook {arg}   {MARK}',
                            "timeout": 5}]}
        if ev == "PreToolUse":
            entry["matcher"] = "Bash|Edit|MultiEdit|Write|NotebookEdit|Read|Grep|Glob|WebFetch|Task"
        entries.append(entry)
        added.append(ev)

    if args.dry_run:
        print(json.dumps(cfg, indent=2))
        return RC_OK

    if target.exists():
        shutil.copyfile(target, str(target) + ".aw.bak")     # one backup, overwritten
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent))
    try:
        os.write(fd, (json.dumps(cfg, indent=2) + "\n").encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    if target.exists():
        shutil.copymode(target, tmp)
    os.replace(tmp, target)
    print(f"installed {len(added)} hooks into {target}"
          + (f" (via symlink {p})" if str(target) != str(p) else ""))
    print("Restart your agent sessions — live sessions do not pick up newly "
          "installed hooks.")
    return RC_OK


def cmd_uninstall(args) -> int:
    p = Path(os.path.realpath(settings_path()))
    if not p.exists():
        print(f"{p} does not exist")
        return RC_OK
    try:
        cfg = json.loads(p.read_text())
    except ValueError:
        die(f"{p} is not valid JSON; refusing to touch it", RC_REFUSED)
    n = 0
    for ev, entries in list((cfg.get("hooks") or {}).items()):
        if not isinstance(entries, list):
            continue
        keep = [e for e in entries if MARK not in json.dumps(e)]
        n += len(entries) - len(keep)
        if keep:
            cfg["hooks"][ev] = keep
        else:
            cfg["hooks"].pop(ev, None)
    if not cfg.get("hooks"):
        cfg.pop("hooks", None)
    shutil.copyfile(p, str(p) + ".aw.bak")
    fd, tmp = tempfile.mkstemp(dir=str(p.parent))
    try:
        os.write(fd, (json.dumps(cfg, indent=2) + "\n").encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    shutil.copymode(p, tmp)
    os.replace(tmp, p)
    print(f"removed {n} hook entr{'y' if n == 1 else 'ies'} from {p}")
    return RC_OK


# ------------------------------------------------------------------- doctor

def oomd_kills(since: str = "-14d") -> tuple[int, list[str]]:
    """The success metric. If this number does not fall, the tool failed —
    however good the board looks."""
    try:
        r = subprocess.run(["journalctl", "-u", "systemd-oomd", "--since", since,
                            "--no-pager", "-o", "cat"],
                           capture_output=True, text=True, timeout=25)
        lines = [l for l in r.stdout.splitlines() if "Killed" in l]
        return len(lines), lines[-3:]
    except Exception:
        return -1, []


def editor_scope() -> Path | None:
    app = USER_CG / "app.slice"
    best, best_mem = None, 0
    try:
        for d in app.iterdir():
            if not d.is_dir() or "code" not in d.name:
                continue
            try:
                cur = int((d / "memory.current").read_text().strip())
            except (OSError, ValueError):
                continue
            if cur > best_mem:
                best, best_mem = d, cur
    except OSError:
        pass
    return best


def cmd_doctor(args) -> int:
    require_linux_systemd()
    limit, src = oomd_limit()
    print()
    n, recent = oomd_kills()
    print(f"  KILLER          systemd-oomd, user@{UID}.service, "
          f"{limit:.1f}% for 20s  [{src}]")
    if n >= 0:
        print(f"                  {n} kill(s) in the journal in the last 14 days."
              + ("  This is the number that has to fall." if n else ""))
        for l in recent:
            m = re.search(r"Killed (\S+).*being ([\d.]+)%", l)
            if m:
                print(f"                    {os.path.basename(m.group(1))} at {m.group(2)}%")

    sc = editor_scope()
    if sc:
        try:
            mem = int((sc / "memory.current").read_text().strip()) // (1024 * 1024)
            pids = len((sc / "cgroup.procs").read_text().split())
            kids = sum(1 for x in sc.iterdir() if x.is_dir())
            claude = 0
            for p in (sc / "cgroup.procs").read_text().split():
                try:
                    if "claude" in os.readlink(f"/proc/{p}/exe"):
                        claude += 1
                except OSError:
                    pass
            print(f"  VICTIM          {sc.name[:46]}")
            print(f"                  {gib(mem)}, {pids} pids, {kids} child cgroup(s)"
                  + ("  <- a leaf, so oomd's preferred victim" if kids == 0 else ""))
            if claude:
                print(f"                  {claude} agent session(s) inside it — "
                      f"they all die together")
        except OSError:
            pass

    jvm, njvm = jvm_holding()
    print()
    if jvm > 1024:
        print(f"  FIX 1  reclaim {gib(jvm)} now              ./gradlew --stop")
    if sc:
        print(f"  FIX 2  make the editor the last victim  aw doctor --protect-editor")

    if args.protect_editor:
        if not sc:
            die("no editor scope found to protect")
        print(f"\n  This sets ManagedOOMPreference=avoid on {sc.name},")
        print("  so systemd-oomd prefers almost anything else. It is a live change.")
        if not args.yes:
            print("  Re-run with --yes to apply.")
        else:
            r = subprocess.run(["systemctl", "--user", "set-property", sc.name,
                                "ManagedOOMPreference=avoid"],
                               capture_output=True, text=True)
            print("  applied" if r.returncode == 0 else f"  failed: {r.stderr.strip()}")

    if args.reap:
        stopped = 0
        with Lock():
            known = {j.get("scope") for j in live_jobs() if not j.get("ownerless")}
        try:
            for d in (USER_CG / "app.slice").iterdir():
                if d.name.startswith("aw-") and d.name.endswith(".scope") \
                        and d.name not in known:
                    subprocess.run(["systemctl", "--user", "stop", d.name],
                                   capture_output=True)
                    stopped += 1
                    for jf in (reg_dir() / "jobs").glob("*.json"):
                        r = read_json(jf) or {}
                        if r.get("scope") == d.name:
                            jf.unlink(missing_ok=True)
        except OSError:
            pass
        print(f"\n  reaped {stopped} orphaned aw-*.scope unit(s)")

    print()
    d = reg_dir()
    tmpfs = ""
    try:
        import subprocess as _sp
        fs = _sp.run(["stat", "-f", "-c", "%T", str(d)], capture_output=True,
                     text=True, timeout=5).stdout.strip()
        tmpfs = fs
    except Exception:
        pass
    mode = oct(d.stat().st_mode & 0o777)[2:]
    print(f"  registry  {d}  {tmpfs} {mode}"
          + ("  OK" if tmpfs == "tmpfs" else "  <- not tmpfs; must not be synced"))
    sp = Path(os.path.realpath(settings_path()))
    installed = 0
    try:
        cfg = json.loads(sp.read_text()) if sp.exists() else {}
        for ev, _ in list(EVENTS) + [ACTIVITY_EVENT]:
            if any(MARK in json.dumps(e) for e in (cfg.get("hooks") or {}).get(ev, [])):
                installed += 1
    except (OSError, ValueError):
        pass
    total = len(EVENTS) + 1
    print(f"  hooks     {installed}/{total} installed in {sp}"
          + ("  OK" if installed == total else "  -> aw install"))
    pd = settings_path().parent
    if pd.exists():
        pm = pd.stat().st_mode & 0o777
        if pm & 0o002:
            print(f"  RISK      {pd} is mode {oct(pm)[2:]} — WORLD-writable. Any local "
                  f"account can add a hook that runs as you.  aw install --fix-perms")
        elif pm & 0o020:
            print(f"  warn      {pd} is mode {oct(pm)[2:]} — group-writable.  "
                  f"aw install --fix-perms")
    c = in_container()
    if c:
        print(f"  warn      running in a container ({c}); aw refuses to gate here")
    print(f"  knobs     AW_FLOOR_MIB={FLOOR_MIB}  AW_VETO_PSI={VETO_PSI}  "
          f"(starting points — fit them from decisions.jsonl, see docs/tuning.md)")
    print()
    return RC_OK


def main() -> int:
    p = argparse.ArgumentParser(
        prog="aw", description=__doc__.split("\n\n")[0],
        epilog="AW_FLOOR_MIB and AW_VETO_PSI are starting points, not science. "
               "Every decision is logged to ~/.local/state/agent-awareness/"
               "decisions.jsonl so you can fit them. See docs/tuning.md.")
    p.add_argument("--version", action="version", version=VERSION)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("board", help="who is doing what, and is there room")
    s.add_argument("--json", action="store_true"); s.set_defaults(fn=cmd_board)

    s = sub.add_parser("status", help="one line, one exit code: may I start?")
    s.add_argument("--class", dest="klass", default="build", choices=sorted(COSTS))
    s.add_argument("--mem", default="", help="override the cost estimate, e.g. 6G")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("run", help="queue, then run the job in a capped cgroup")
    s.add_argument("--class", dest="klass", default="build", choices=sorted(COSTS))
    s.add_argument("--mem", default="", help="declare the cost, e.g. 6G")
    s.add_argument("--timeout", type=float, default=600.0,
                   help="seconds to wait in the queue (default 600)")
    s.add_argument("cmd", nargs=argparse.REMAINDER)
    s.set_defaults(fn=cmd_run)

    s = sub.add_parser("hook", help="hook entry point")
    s.add_argument("event", choices=[a for _, a in EVENTS] + [ACTIVITY_EVENT[1]])
    s.set_defaults(fn=cmd_hook)

    s = sub.add_parser("install", help="add the four hooks, leaving others alone")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--fix-perms", action="store_true",
                   help="chmod 0700 the settings directory if it is world-writable")
    s.add_argument("--no-activity", action="store_true",
                   help="skip the observe-only PreToolUse hook that reports which "
                        "file, port or command each session is touching")
    s.set_defaults(fn=cmd_install)

    sub.add_parser("uninstall", help="remove only our hooks").set_defaults(fn=cmd_uninstall)

    s = sub.add_parser("doctor", help="what is killing this machine, and what to do")
    s.add_argument("--protect-editor", action="store_true",
                   help="prefer oomd to kill almost anything before the editor")
    s.add_argument("--reap", action="store_true", help="stop orphaned aw-*.scope units")
    s.add_argument("--yes", action="store_true", help="apply a live change")
    s.set_defaults(fn=cmd_doctor)

    args = p.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
