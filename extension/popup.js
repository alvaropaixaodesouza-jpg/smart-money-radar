const FIELDS = ['endpoint', 'token', 'intervalMinutes', 'resolveTop'];
const DEFAULTS = { endpoint: 'http://127.0.0.1:8787/ingest', token: '', intervalMinutes: 30, resolveTop: 25, enabled: true, withPositions: true };

const el = (id) => document.getElementById(id);

async function render() {
  const c = { ...DEFAULTS, ...(await chrome.storage.local.get([...FIELDS, 'enabled', 'status', 'knownUserIds'])) };
  FIELDS.forEach((f) => { el(f).value = c[f]; });
  el('enabled').checked = !!c.enabled;
  el('withPositions').checked = c.withPositions !== false;
  const s = c.status || {};
  const when = s.at ? new Date(s.at).toLocaleTimeString() : 'never';
  const known = (c.knownUserIds || []).length;
  el('status').innerHTML =
    `<span class="${s.ok ? 'ok' : 'bad'}">${s.ok ? 'ok' : 'idle / error'}</span> · last run ${when}` +
    `\n${s.message || ''}` +
    (s.counts ? `\nleaderboards ${s.counts.leaderboards}, wallets ${s.counts.swaps}, positions ${s.counts.trades || 0}` : '') +
    `\nresolved so far: ${known}` +
    (s.response ? `\nreceiver: ${s.response}` : '');
}

el('save').onclick = async () => {
  const patch = { enabled: el('enabled').checked, withPositions: el('withPositions').checked };
  FIELDS.forEach((f) => { patch[f] = el(f).type === 'number' ? Number(el(f).value) : el(f).value.trim(); });
  await chrome.storage.local.set(patch);
  await chrome.runtime.sendMessage({ type: 'reschedule' });
  render();
};

el('now').onclick = async () => {
  el('status').textContent = 'collecting… (keep the fomo.family tab open)';
  await chrome.runtime.sendMessage({ type: 'collectNow' });
  render();
};

el('forget').onclick = async () => {
  await chrome.storage.local.set({ knownUserIds: [] });
  render();
};

render();
