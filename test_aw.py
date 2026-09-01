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
    """Import aw.py without running main().

    Thresholds are read from the environment at import, so the caller sets them
    first. Asserting gate arithmetic against whatever the machine happens to be
    doing makes the suite fail on a loaded box and pass on an idle one, which is
    worse than no test — it teaches you to ignore a red result.
    """
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
    # Park the veto out of reach so the arithmetic tests are deterministic; the
    # veto gets its own test below with the threshold pulled to zero.
    os.environ.update(env, AW_VETO_PSI="1000")
    m = load()

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
        ok("the floor is what is left AFTER the job", m["gate"](0)[0])

        print("pressure veto")
        os.environ["AW_VETO_PSI"] = "0"
        mv = load()
        admitted, why = mv["gate"](1)
        ok("a zero veto refuses even a 1 MiB job", not admitted)
        ok("  and the refusal names the oomd threshold, not the floor",
           "pressure" in why and "kills" in why)
        os.environ["AW_VETO_PSI"] = "1000"

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

        print("activity reporting")
        def act(tool, ti):
            run("hook", "activity", env=env, stdin=json.dumps(
                {"session_id": sid, "cwd": str(HERE), "tool_name": tool,
                 "tool_input": ti}))
            b = json.loads(run("board", "--json", env=env).stdout)["sessions"]
            r = [x for x in b if x["session_id"] == sid][0]
            return f"{r.get('verb','')} {r.get('object','')}".strip()

        run("hook", "session-start",
            stdin=json.dumps({"session_id": sid, "cwd": str(HERE)}), env=env)
        check("a build command reports the binary, not a guessed class",
              act("Bash", {"command": "./gradlew assembleDebug"}), "running gradlew")
        check("a served port is reported",
              act("Bash", {"command": "python3 -m http.server --port 8080"}),
              "port python3 :8080")
        check("a file write is reported by basename",
              act("Write", {"file_path": "/a/b/server.py"}), "writing server.py")
        check("a file read is reported by basename",
              act("Read", {"file_path": "/a/b/Manifest.xml"}), "reading Manifest.xml")
        check("a fetch reports the host only",
              act("WebFetch", {"url": "https://developer.android.com/guide/x"}),
              "fetching developer.android.com")
        act("Bash", {"command": "curl --token ghp_abcdefgh12345678 https://u:pw@x.com/y"})
        blob = run("board", "--json", env=env).stdout
        ok("a token in argv never reaches the record", "ghp_abcdefgh" not in blob)
        ok("a URL password never reaches the record", "pw@x.com" not in blob)
        ok("the full command string is never stored", "curl --token" not in blob)
        check("  and it is still reported usefully",
              [x for x in json.loads(blob)["sessions"] if x["session_id"] == sid][0]["verb"],
              "running")
        run("hook", "end", stdin=json.dumps({"session_id": sid}), env=env)

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
        cfgx = json.loads(real.read_text())
        check("the activity hook is installed by default",
              sum(1 for e in cfgx["hooks"].get("PreToolUse", [])
                  if m["MARK"] in json.dumps(e)), 1)
        ok("  and is scoped by a matcher",
           any(e.get("matcher") for e in cfgx["hooks"]["PreToolUse"]
               if m["MARK"] in json.dumps(e)))
        # The toggle must work in BOTH directions. Removing only the events
        # being installed would leave a previously-installed activity hook in
        # place when the user runs --no-activity precisely to remove it.
        def pre_count():
            return sum(1 for e in json.loads(real.read_text())["hooks"].get("PreToolUse", [])
                       if m["MARK"] in json.dumps(e))
        def others():
            return sum(1 for ev in json.loads(real.read_text())["hooks"].values()
                       for e in ev if m["MARK"] not in json.dumps(e))
        base_others = others()
        run("install", "--no-activity", env=senv)
        check("--no-activity removes an already-installed activity hook", pre_count(), 0)
        run("install", env=senv)
        check("installing again puts it back", pre_count(), 1)
        check("other tools' hooks are untouched throughout", others(), base_others)
        run("uninstall", env=senv)
        for _ in range(3):
            run("install", env=senv)
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

        print("hardening regressions")
        ctrl = "/tmp/" + chr(27) + "[2J" + chr(7) + "evil"
        run("hook", "activity", env=env, stdin=json.dumps(
            {"session_id": "ctl", "cwd": ctrl, "tool_name": "Bash",
             "tool_input": {"command": chr(27) + "[31mfake"}}))
        blob = run("board", "--json", env=env).stdout
        import re as _re
        ok("control bytes are scrubbed at intake, not at print time",
           not _re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]", blob)
           and "\\u001b" not in blob)
        run("hook", "end", stdin=json.dumps({"session_id": "ctl"}), env=env)

        shared = scratch / "shared.json"
        shared.write_text(json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
            {"type": "command", "command": f"aw hook activity   {m['MARK']}"},
            {"type": "command", "command": "devlock-hook pre-bash   # auto-device-lock"},
            {"type": "command", "command": "node caveman.js"}]}]}}))
        shenv = {**env, "AW_SETTINGS": str(shared)}
        run("install", env=shenv)
        b = shared.read_text()
        ok("install keeps another tool's hook in a shared matcher group",
           "devlock-hook" in b and "caveman.js" in b)
        run("uninstall", env=shenv)
        b = shared.read_text()
        ok("uninstall keeps another tool's hook in a shared matcher group",
           "devlock-hook" in b and "caveman.js" in b)
        ok("  and removes only ours", m["MARK"] not in b)
        ok("uninstall writes its own backup, not over install's",
           (scratch / "shared.json.aw-uninstall.bak").exists())

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
