#!/usr/bin/env bash
# agentaware self-check. Uses a scratch registry and a scratch settings file;
# never touches ~/.claude/settings.json or the real registry.
#   bash selftest.sh
set -u
AA="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/agentaware"
SCRATCH="$(mktemp -d)"
export AGENTAWARE_DIR="$SCRATCH/reg"
export XDG_STATE_HOME="$SCRATCH/state"
mkdir -p "$AGENTAWARE_DIR" "$XDG_STATE_HOME"
trap 'rm -rf "$SCRATCH"; for p in ${KIDS:-}; do kill -9 "$p" 2>/dev/null; done' EXIT
KIDS=""

pass=0; fail=0
ok()  { pass=$((pass+1)); printf '  ok   %s\n' "$1"; }
bad() { fail=$((fail+1)); printf '  FAIL %s\n' "$1"; }
is()  { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 (got '$2', want '$3')"; fi; }

echo "basics"
"$AA" --version >/dev/null 2>&1; is "runs" "$?" "0"
"$AA" board >/dev/null 2>&1;     is "board renders with an empty registry" "$?" "0"
"$AA" gate light >/dev/null 2>&1; is "an unmetered class is always admitted" "$?" "0"

echo "classification"
cls() { python3 - "$1" <<'PY'
import sys, importlib.util, os
src = open(os.environ["AA_PATH"]).read().replace("if __name__", "if False and __name__")
m = {}
exec(compile(src, "agentaware", "exec"), m)
print(m["classify"](sys.argv[1])[0])
PY
}
export AA_PATH="$AA"
is "ffmpeg is a render"          "$(cls 'ffmpeg -i a.mov -c:v libx265 b.mp4')" "render"
is "make is a build"             "$(cls 'make -j16 all')"                      "build"
is "pytest is a test"            "$(cls 'pytest -n 4 tests/')"                 "test"
is "npm install is an install"   "$(cls 'npm install')"                        "install"
is "ls is light"                 "$(cls 'ls -la /tmp')"                        "light"
is "a build over ssh is not ours" "$(cls 'ssh builder make -j32')"             "light"
is "an unknown wrapper is flagged" "$(cls './deploy.sh --prod')"      "unknown-wrapper"

echo "redaction"
red() { python3 - "$1" <<'PY'
import sys, os
m = {}
exec(compile(open(os.environ["AA_PATH"]).read().replace("if __name__","if False and __name__"),
             "agentaware","exec"), m)
print(m["redact"](sys.argv[1]))
PY
}
case "$(red 'curl -H x --token ghp_abcdefghijklmnop123')" in
  *ghp_abcdefghijkl*) bad "a github token is redacted" ;;
  *) ok "a github token is redacted" ;;
esac
case "$(red 'git clone https://user:hunter2@example.com/r.git')" in
  *hunter2*) bad "a URL password is redacted" ;;
  *) ok "a URL password is redacted" ;;
esac

echo "admission"
"$AA" run --class render -- sleep 8 >/dev/null 2>&1 & A=$!; KIDS="$KIDS $A"
sleep 1
n=$("$AA" board --json 2>/dev/null | python3 -c 'import json,sys;print(len(json.load(sys.stdin)["slots"]))')
is "a running job holds a slot" "$n" "1"
"$AA" gate render >/dev/null 2>&1; is "the gate refuses while the slot is taken" "$?" "1"
kill -9 $A 2>/dev/null; sleep 0.5
n=$("$AA" board --json 2>/dev/null | python3 -c 'import json,sys;print(len(json.load(sys.stdin)["slots"]))')
is "a SIGKILLed runner frees its slot, with no reaper" "$n" "0"
"$AA" gate render >/dev/null 2>&1; is "  and the gate admits again" "$?" "0"

CAP=$(( $(nproc) / 4 )); [ "$CAP" -lt 1 ] && CAP=1
for i in $(seq 1 8); do "$AA" run --class build --timeout 60 -- sleep 4 >/dev/null 2>&1 & KIDS="$KIDS $!"; done
sleep 2
n=$("$AA" board --json 2>/dev/null | python3 -c 'import json,sys;print(len(json.load(sys.stdin)["slots"]))')
is "8 concurrent builds settle at the machine's cap" "$n" "$CAP"
for p in $KIDS; do kill -9 "$p" 2>/dev/null; done; KIDS=""; sleep 0.5

echo "hooks and settings"
S="$SCRATCH/settings.json"
cat > "$S" <<'JSON'
{"model":"opus","hooks":{"SessionStart":[{"hooks":[{"type":"command","command":"echo other-tool"}]}],
 "Notification":[{"hooks":[{"type":"command","command":"echo theirs"}]}]}}
JSON
cp "$S" "$S.orig"
AGENTAWARE_SETTINGS="$S" "$AA" install-hooks >/dev/null 2>&1; is "install-hooks succeeds" "$?" "0"
AGENTAWARE_SETTINGS="$S" "$AA" install-hooks >/dev/null 2>&1
AGENTAWARE_SETTINGS="$S" "$AA" install-hooks >/dev/null 2>&1
n=$(python3 -c "
import json;d=json.load(open('$S'))
print(sum(1 for g in d['hooks']['SessionStart'] if 'agent-awareness' in json.dumps(g)))")
is "installing three times leaves one entry" "$n" "1"
n=$(python3 -c "
import json;d=json.load(open('$S'))
print(sum(1 for g in d['hooks']['SessionStart'] if 'other-tool' in json.dumps(g)))")
is "another tool's hook survives" "$n" "1"
AGENTAWARE_SETTINGS="$S" "$AA" uninstall-hooks >/dev/null 2>&1
python3 -c "
import json,sys
a=json.load(open('$S.orig'));b=json.load(open('$S'))
sys.exit(0 if a==b else 1)"
is "uninstall restores the file exactly" "$?" "0"

printf '{ not json' > "$SCRATCH/bad.json"
AGENTAWARE_SETTINGS="$SCRATCH/bad.json" "$AA" install-hooks >/dev/null 2>&1
is "malformed settings is refused, not clobbered" "$?" "3"
is "  and left untouched" "$(cat "$SCRATCH/bad.json")" "{ not json"

echo "hook payloads"
printf '%s' '{"session_id":"s1","cwd":"/tmp/demo","tool_name":"Bash","tool_input":{"command":"make -j8"}}' \
  | "$AA" hook PreToolUse >/dev/null 2>&1; is "PreToolUse is accepted" "$?" "0"
a=$("$AA" board --json 2>/dev/null | python3 -c 'import json,sys;d=json.load(sys.stdin);print(list(d["sessions"].values())[0]["activity"])')
is "  and a make command reads as a build" "$a" "build"
printf '%s' 'not json at all' | "$AA" hook PreToolUse >/dev/null 2>&1
is "a garbage payload fails open" "$?" "0"
printf '%s' '{"session_id":"s1"}' | "$AA" hook SessionEnd >/dev/null 2>&1
n=$("$AA" board --json 2>/dev/null | python3 -c 'import json,sys;print(len(json.load(sys.stdin)["sessions"]))')
is "SessionEnd removes the session" "$n" "0"

echo "robustness"
printf 'not json {{{' > "$AGENTAWARE_DIR/registry.json"
"$AA" board >/dev/null 2>&1; is "a corrupt registry does not crash" "$?" "0"

echo
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
