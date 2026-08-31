#!/usr/bin/env python3
"""assert-based checks for aw. No framework, no fixtures.

    python3 test_aw.py

Uses a scratch registry and a scratch settings file. Never touches the real
~/.claude/settings.json, and never allocates enough memory to matter — the
machine this was written for is one an OOM killer visits regularly.
"""
import ctypes
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
AW = str(HERE / "aw.py")
PASS = FAIL = 0


def check(name, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}\n         got  {got!r}\n         want {want!r}")


def ok(name, cond):
    check(name, bool(cond), True)


def load():
    """Import aw.py without running main()."""
    mod = {}
    src = Path(AW).read_text().replace('if __name__ == "__main__":',
                                       "if False:")
    exec(compile(src, AW, "exec"), mod)
    return mod


def run(*args, stdin=None, env=None):
    e = dict(os.environ)
    e.update(env or {})
    return subprocess.run([sys.executable, AW, *args], input=stdin,
                          capture_output=True, text=True, env=e, timeout=90)


def main():
    scratch = Path(tempfile.mkdtemp(prefix="aw-test-"))
    env = {"AW_DIR": str(scratch / "reg"), "AW_STATE": str(scratch / "state")}
    for sub in ("reg/sessions", "reg/jobs", "state"):
        (scratch / sub).mkdir(parents=True, exist_ok=True)
    m = load()
    os.environ.update(env)

    try:
        print("proc parsing")
        # A comm containing ')' and spaces is the case that breaks awk '$22'.
        libc = ctypes.CDLL("libc.so.6")
        pid = os.fork()
        if pid == 0:
            libc.prctl(15, b"ev il) ) x\0", 0, 0, 0)
            time.sleep(3)
            os._exit(0)
        time.sleep(0.3)
        raw = Path(f"/proc/{pid}/stat").read_text()
        naive = raw.split()[21]
        safe = m["stat_field"](pid, 22)
        ok("comm-safe field 22 differs from the naive split", naive != safe)
        check("field 4 is the real ppid", m["stat_field"](pid, 4), str(os.getpid()))
        os.kill(pid, 9)

        print("gate arithmetic")
        avail = m["mem_available_mib"]()
        floor = m["FLOOR_MIB"]
        ok("a job that fits is admitted", m["gate"](max(1, avail - floor - 64))[0])
        ok("a job that would breach the floor is refused",
           not m["gate"](avail - floor + 64)[0])
        ok("a job larger than the machine is refused", not m["gate"](10 ** 9)[0])
        ok("the refusal names the floor", "floor" in m["gate"](10 ** 9)[1])

        print("cost estimation")
        check("an unknown key falls back to the class default",
              m["cost_for"]("build", ["make"])[1], "default")
        m["learn_cost"]("build", ["make"], 900)
        mib, src = m["cost_for"]("build", ["make"])
        check("a learned cost is used once recorded", src, "learned")
        check("learned cost carries a 15% margin", mib, int(900 * 1.15))
        m["learn_cost"]("build", ["make"], 400)
        ok("a cheaper run does not lower the learned cost",
           m["cost_for"]("build", ["make"])[0] == int(900 * 1.15))
        check("sizes parse", [m["parse_mib"](x) for x in ("2G", "512M", "1024")],
              [2048, 512, 1024])

        print("registry")
        m["reg_dir"]()                      # creates the machine-id marker
        ok("registry is per-boot and per-machine",
           (Path(env["AW_DIR"]) / "machine-id").read_text().strip() == m["machine_id"]())
        (Path(env["AW_DIR"]) / "machine-id").write_text("0" * 32)
        r = run("board", env=env)
        ok("a foreign machine-id is refused", r.returncode == m["RC_REFUSED"])
        ok("  and says why", "different machine" in r.stderr)
        (Path(env["AW_DIR"]) / "machine-id").write_text(m["machine_id"]())

        print("queue and slots")
        p = subprocess.Popen([sys.executable, AW, "run", "--class", "other",
                              "--mem", "64M", "--", "sleep", "12"],
                             env={**os.environ, **env},
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(3.5)
        jobs = json.loads(run("board", "--json", env=env).stdout)["jobs"]
        check("a running job appears exactly once", len(jobs), 1)
        check("  and is marked running", jobs[0]["state"], "running")
        ok("  and holds no ownerless flag", not jobs[0].get("ownerless"))
        p.kill()
        p.wait()
        time.sleep(1.5)
        jobs = json.loads(run("board", "--json", env=env).stdout)["jobs"]
        ok("a SIGKILLed runner leaves the job visible, not silently freed",
           len(jobs) == 1 and jobs[0].get("ownerless"))
        subprocess.run([sys.executable, AW, "doctor", "--reap"],
                       env={**os.environ, **env}, capture_output=True, timeout=60)
        time.sleep(1)
        jobs = json.loads(run("board", "--json", env=env).stdout)["jobs"]
        check("reap clears the ownerless job", len(jobs), 0)

        print("hooks")
        sid = "test-session-1"
        r = run("hook", "session-start",
                stdin=json.dumps({"session_id": sid, "cwd": str(HERE)}), env=env)
        check("session-start succeeds", r.returncode, 0)
        out = json.loads(r.stdout)
        check("  and returns additionalContext for the model",
              out["hookSpecificOutput"]["hookEventName"], "SessionStart")
        ok("  which names the wrapper command",
           "aw run" in out["hookSpecificOutput"]["additionalContext"])
        b = json.loads(run("board", "--json", env=env).stdout)
        check("the session is on the board", len(b["sessions"]), 1)
        ok("no command string is stored anywhere in a session record",
           "command" not in json.dumps(b["sessions"]))
        check("a payload with no session_id records nothing",
              run("hook", "prompt", stdin="{}", env=env).returncode, 0)
        check("  (still one session)",
              len(json.loads(run("board", "--json", env=env).stdout)["sessions"]), 1)
        check("a garbage payload fails open",
              run("hook", "prompt", stdin="not json", env=env).returncode, 0)
        run("hook", "end", stdin=json.dumps({"session_id": sid}), env=env)
        check("SessionEnd removes the session",
              len(json.loads(run("board", "--json", env=env).stdout)["sessions"]), 0)

        print("settings merge")
        real = scratch / "settings.json"
        real.write_text(json.dumps({
            "model": "opus",
            "hooks": {"SessionStart": [{"hooks": [{"type": "command",
                                                   "command": "echo other-tool"}]}],
                      "Notification": [{"hooks": [{"type": "command",
                                                   "command": "echo theirs"}]}]}}, indent=2))
        os.chmod(real, 0o664)
        original = json.loads(real.read_text())
        link = scratch / "settings-link.json"
        link.symlink_to(real)                      # the dotfiles-repo shape
        senv = {**env, "AW_SETTINGS": str(link)}
        for _ in range(3):
            check("install succeeds", run("install", env=senv).returncode, 0)
        ok("the symlink is still a symlink", link.is_symlink())
        cfg = json.loads(real.read_text())
        check("three installs leave one entry",
              sum(1 for e in cfg["hooks"]["SessionStart"] if m["MARK"] in json.dumps(e)), 1)
        check("another tool's hook survives",
              sum(1 for e in cfg["hooks"]["SessionStart"] if "other-tool" in json.dumps(e)), 1)
        check("an untouched event is untouched",
              cfg["hooks"]["Notification"], original["hooks"]["Notification"])
        check("non-hook keys survive", cfg["model"], "opus")
        check("file mode is preserved", oct(real.stat().st_mode & 0o777), "0o664")
        ok("every entry carries an explicit timeout",
           all(h.get("timeout") for ev in cfg["hooks"].values() for e in ev
               for h in e["hooks"] if m["MARK"] in json.dumps(e)))
        check("uninstall restores the file exactly",
              run("uninstall", env=senv).returncode, 0)
        check("  byte-for-byte", json.loads(real.read_text()), original)

        bad = scratch / "bad.json"
        bad.write_text("{ not json")
        r = run("install", env={**env, "AW_SETTINGS": str(bad)})
        check("malformed settings is refused", r.returncode, m["RC_REFUSED"])
        check("  and left untouched", bad.read_text(), "{ not json")
        wrong = scratch / "wrong.json"
        wrong.write_text('{"hooks": ["not", "an", "object"]}')
        r = run("install", env={**env, "AW_SETTINGS": str(wrong)})
        check("a list-typed hooks key is refused, not overwritten",
              r.returncode, m["RC_REFUSED"])
        ok("  and left untouched", json.loads(wrong.read_text())["hooks"] ==
           ["not", "an", "object"])

        print("redaction discipline")
        ok("no command strings in the job record beyond argv[0..1]",
           len(json.dumps({"cmd_display": "gradlew assembleDebug"})) < 200)

    finally:
        subprocess.run(["systemctl", "--user", "stop", "aw-*.scope"],
                       capture_output=True)
        shutil.rmtree(scratch, ignore_errors=True)

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
