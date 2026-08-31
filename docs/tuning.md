# Tuning

Two knobs. Both are starting points chosen for one machine, and both are wrong for yours until you
check.

| Knob | Default | Meaning |
|---|---|---|
| `AW_FLOOR_MIB` | `4096` | MiB that must remain free *after* a job is admitted |
| `AW_VETO_PSI` | `20.0` | Memory pressure (`full avg10`) above which nothing new starts |

`AW_VETO_PSI` is derived: `systemd-oomd` kills at some limit sustained for a duration — 50% for 20 s
on the development machine, which `aw doctor` reads rather than assumes. 20 is 40% of the way there on
the identical signal, leaving room for an admitted job to ramp and be caught before oomd wins.

`AW_FLOOR_MIB` is **not** derived from a measured kill. It is roughly 13% of total memory, chosen
because reclaim on that machine goes to zram *in RAM* first, so nominal headroom overstates real
headroom. Treat it as a hypothesis.

## Fitting them from your own machine

Every decision is appended to `~/.local/state/agent-awareness/decisions.jsonl`:

```json
{"t":1756700412,"key":"build:gradlew:HeadzUp","cost":2048,"avail":8305,"psi_full10":0.0,"verdict":"admit","waited_s":0}
```

After a week or two:

```bash
# How often did the gate refuse, and at what headroom?
python3 - <<'PY'
import json, collections
rows = [json.loads(l) for l in open("~/.local/state/agent-awareness/decisions.jsonl".replace("~", __import__("os").path.expanduser("~")))]
by = collections.Counter(r["verdict"] for r in rows)
print(by)
admits = [r["avail"] - r["cost"] for r in rows if r["verdict"] == "admit"]
print("headroom after admit: min", min(admits), "median", sorted(admits)[len(admits)//2])
PY

# Did any kill happen anyway?
journalctl -u systemd-oomd --since -14d | grep Killed
```

Then:

- **A kill happened while the gate was admitting.** The floor is too low. Raise `AW_FLOOR_MIB` to
  above the minimum post-admit headroom you observed before the kill.
- **Nothing is ever refused and no kills happen.** Either the floor is generous or your machine is not
  under pressure. Lower it and see whether refusals appear before kills do.
- **`waited_s` is routinely large and no kills happen.** The floor or the cost estimates are too
  conservative. Check `costs.json` — a single pathological run ratchets a cost upward permanently.
- **The veto fires often.** Something is already thrashing. Look at `aw doctor` first; on the
  development machine 9 GB of idle JVM daemons was the answer, not the gate.

## Cost estimates

`~/.local/state/agent-awareness/costs.json`, keyed `class:argv0:repo`. Learned cost is
`max(observed) × 1.15`, so it ratchets up and never down — one pathological run raises it forever.
Delete the entry to re-learn:

```bash
python3 -c "
import json,os; p=os.path.expanduser('~/.local/state/agent-awareness/costs.json')
d=json.load(open(p)); d.pop('build:gradlew:HeadzUp', None); json.dump(d, open(p,'w'), indent=1)"
```

Or declare it and skip learning: `aw run --mem 6G -- ./gradlew assembleDebug`.

## The metric that decides whether any of this worked

```bash
journalctl -u systemd-oomd --since -7d | grep -c Killed
```

`aw doctor` prints it. If it does not fall, the tool failed, however good the board looks.
