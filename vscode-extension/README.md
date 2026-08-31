# agent-awareness — VS Code extension

A status bar item showing what every agent session on this machine is doing, and how much room is
left. It shells out to the `aw` CLI and renders the answer; all the logic lives there, so the
extension can never disagree with what the agents themselves see.

Install the CLI first — see the [main README](../README.md).

## Build a .vsix

No marketplace account needed:

```bash
npx --yes @vscode/vsce package
code --install-extension agent-awareness-1.0.0.vsix
```

## Settings

| Setting | Default | |
|---|---|---|
| `agentAwareness.path` | `aw` | Path to the executable, if it isn't on `PATH` |
| `agentAwareness.refreshSeconds` | `10` | Status bar refresh interval |

Click the status bar item to open the full board in a terminal.
