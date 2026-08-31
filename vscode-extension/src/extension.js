'use strict';
// A status bar item, and nothing else.
//
// The extension deliberately owns no state: it shells out to `aw board
// --json` and renders the answer. Every rule about liveness, admission and
// locking lives in one place, in the CLI, so the extension cannot disagree with
// what the agents themselves see.
const vscode = require('vscode');
const { execFile } = require('child_process');

let item, timer;

function cfg() {
  const c = vscode.workspace.getConfiguration('agentAwareness');
  return { path: c.get('path', 'aw'), every: Math.max(2, c.get('refreshSeconds', 10)) };
}

function gb(n) { return (n / (1024 ** 3)).toFixed(1) + 'G'; }

function render(board) {
  const m = board.machine || {};
  const sessions = Object.values(board.sessions || {});
  const slots = Object.values(board.slots || {});
  const busy = {};
  for (const s of slots) busy[s.class] = (busy[s.class] || 0) + 1;
  const running = Object.entries(busy).map(([k, n]) => `${n} ${k}`).join(', ');

  item.text = `$(pulse) ${sessions.length} session${sessions.length === 1 ? '' : 's'}`
    + (running ? ` · ${running}` : '')
    + ` · ${gb(m.mem_available || 0)} free`;

  // The kill threshold is the only number worth colouring on.
  const limit = (m.oomd && m.oomd.pressure_pct) || 50;
  const pressure = m.mem_pressure || 0;
  item.backgroundColor = pressure >= limit / 2
    ? new vscode.ThemeColor('statusBarItem.errorBackground')
    : pressure > 0 ? new vscode.ThemeColor('statusBarItem.warningBackground') : undefined;

  const lines = [
    `**${gb(m.mem_available || 0)} available** of ${gb(m.mem_total || 0)}`,
    `memory pressure ${pressure.toFixed(1)}% — systemd-oomd kills at ${limit}%`,
    `swap ${(m.swap_used_pct || 0).toFixed(0)}%  ·  cpu ${(m.cpu_pressure || 0).toFixed(0)}%`,
    '',
  ];
  if (sessions.length) {
    for (const s of sessions.sort((a, b) => (a.since || 0) - (b.since || 0))) {
      lines.push(`\`${(s.repo || '?').padEnd(18)}\` ${s.activity || '?'}` +
                 (s.detail ? ` — ${s.detail}` : ''));
    }
  } else {
    lines.push('_no sessions reporting — run `aw install-hooks`_');
  }
  if (slots.length) {
    lines.push('', '**Holding a slot**');
    for (const s of slots) lines.push(`\`${s.class}\` ${s.repo || '?'} — ${s.why || ''}`);
  }
  const md = new vscode.MarkdownString(lines.join('\n\n'));
  md.isTrusted = false;
  item.tooltip = md;
}

function refresh() {
  const { path } = cfg();
  execFile(path, ['board', '--json'], { timeout: 5000 }, (err, stdout) => {
    if (err) {
      item.text = '$(pulse) agent-awareness: not found';
      item.tooltip = new vscode.MarkdownString(
        `\`${path}\` could not be run.\n\nInstall it, or set **agentAwareness.path**.`);
      item.backgroundColor = undefined;
      return;
    }
    try { render(JSON.parse(stdout)); }
    catch (e) { item.text = '$(pulse) agent-awareness: bad output'; }
  });
}

function activate(context) {
  item = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
  item.command = 'agentAwareness.showBoard';
  item.text = '$(pulse) agent-awareness';
  item.show();
  context.subscriptions.push(item);

  context.subscriptions.push(vscode.commands.registerCommand('agentAwareness.refresh', refresh));
  context.subscriptions.push(vscode.commands.registerCommand('agentAwareness.showBoard', () => {
    // The board is a terminal view: it is the same thing the agents read, and
    // reimplementing it in a webview would be a second thing to keep in sync.
    const t = vscode.window.createTerminal('agent-awareness');
    t.sendText(`${cfg().path} board`);
    t.show();
  }));

  const schedule = () => {
    if (timer) clearInterval(timer);
    timer = setInterval(refresh, cfg().every * 1000);
  };
  context.subscriptions.push(vscode.workspace.onDidChangeConfiguration(e => {
    if (e.affectsConfiguration('agentAwareness')) { schedule(); refresh(); }
  }));
  schedule();
  refresh();
  context.subscriptions.push({ dispose: () => timer && clearInterval(timer) });
}

function deactivate() { if (timer) clearInterval(timer); }

module.exports = { activate, deactivate };
