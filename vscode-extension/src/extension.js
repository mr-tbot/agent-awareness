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
  const sessions = board.sessions || [];
  const jobs = board.jobs || [];
  const busy = {};
  for (const j of jobs) busy[j.class] = (busy[j.class] || 0) + 1;
  const running = Object.entries(busy).map(([k, n]) => `${n} ${k}`).join(', ');
  const availMib = m.mem_available_mib || 0;

  item.text = `$(pulse) ${sessions.length} session${sessions.length === 1 ? '' : 's'}`
    + (running ? ` · ${running}` : '')
    + ` · ${gb(availMib * 1024 * 1024)} free`;

  // The kill threshold is the only number worth colouring on.
  const limit = m.oomd_limit_pct || 50;
  const pressure = m.psi_full10 || 0;
  item.backgroundColor = pressure >= (m.veto_psi || 20)
    ? new vscode.ThemeColor('statusBarItem.errorBackground')
    : pressure > 0 ? new vscode.ThemeColor('statusBarItem.warningBackground') : undefined;

  const lines = [
    `**${gb(availMib * 1024 * 1024)} available** of `
      + gb((m.mem_total_mib || 0) * 1024 * 1024),
    `memory pressure ${pressure.toFixed(2)}% — systemd-oomd kills at ${limit}% for 20s`,
    '',
  ];
  if (sessions.length) {
    lines.push('| repo | doing |', '|---|---|');
    for (const s of sessions.sort((a, b) => (a.started_at || 0) - (b.started_at || 0))) {
      const act = [s.verb, s.object].filter(Boolean).join(' ') || s.state || '?';
      lines.push(`| ${s.repo || '?'} | ${act} |`);
    }
  } else {
    lines.push('_no sessions reporting — run `aw install`, then restart them_');
  }
  if (jobs.length) {
    lines.push('', '**Metered jobs**');
    for (const j of jobs) {
      lines.push(`\`${j.class}\` ${j.repo || '?'} — ${j.cmd_display || ''}`
        + (j.ownerless ? '  **(no owner — run `aw doctor --reap`)**' : ''));
    }
  }
  // Say plainly what this view cannot show, rather than implying that each
  // window is isolated. Activity is per session; memory is shared fate.
  lines.push('', '_Every window shares one cgroup, so an oomd kill takes all of '
                 + 'them. Activity is per session; memory is not._');

  const md = new vscode.MarkdownString(lines.join('\n'));
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
