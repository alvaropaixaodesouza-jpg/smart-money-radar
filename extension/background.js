/* Service worker: schedules collections and forwards them to the local fomo-agent receiver.
 *
 * MV3 workers are killed when idle, so state lives in chrome.storage and the schedule in an alarm.
 * A collection needs an open fomo.family tab: the fetches deliberately happen in the page, which is
 * what keeps them identical to the app's own requests.
 */
const DEFAULTS = {
  endpoint: 'http://127.0.0.1:8787/ingest',
  token: '',
  intervalMinutes: 30,
  resolveTop: 25,
  withPositions: true,
  enabled: true,
};

// Managed values come from an enterprise policy file on the machine, which is how the server
// install hands over the receiver token without anybody pasting it into a popup over VNC. They
// win over whatever is in local storage, so the policy stays the single source of truth there;
// on a laptop there is no policy and this is exactly what it was before.
const cfg = async () => {
  const local = await chrome.storage.local.get(Object.keys(DEFAULTS));
  let managed = {};
  try {
    managed = (await chrome.storage.managed.get(Object.keys(DEFAULTS))) || {};
  } catch (e) { /* no policy on this machine, which is the normal case */ }
  const set = (o) => Object.fromEntries(Object.entries(o).filter(([, v]) => v !== undefined && v !== ''));
  return { ...DEFAULTS, ...set(local), ...set(managed) };
};

async function setStatus(patch) {
  const prev = (await chrome.storage.local.get('status')).status || {};
  await chrome.storage.local.set({ status: { ...prev, ...patch, at: Date.now() } });
}

async function injectInto(tabId) {
  // The collector must run in the page's own world so its fetches are the app's own; the bridge
  // stays isolated because only it can talk to the extension.
  await chrome.scripting.executeScript({ target: { tabId }, files: ['content_main.js'], world: 'MAIN' });
  await chrome.scripting.executeScript({ target: { tabId }, files: ['content_bridge.js'], world: 'ISOLATED' });
}

async function fomoTab() {
  const tabs = await chrome.tabs.query({ url: 'https://fomo.family/*' });
  return tabs.find((t) => t.status === 'complete') || tabs[0] || null;
}

/** Take a session handed over from a browser that is already signed in, if one is waiting.
 *
 * The receiver holds it for exactly one read and deletes it, so this is a transfer rather than a
 * stored credential. It only matters on a machine nobody sits at: signing in there by hand means
 * a password through a remote desktop and a two-factor prompt on a phone in another building.
 */
async function takeSeed(tab) {
  const c = await cfg();
  let payload;
  try {
    const r = await fetch(c.endpoint.replace(/\/ingest$/, '/seed'), {
      headers: c.token ? { 'x-agent-token': c.token } : {},
    });
    if (r.status === 404) return null;          // nothing waiting, which is the normal case
    if (!r.ok) return { error: `seed ${r.status}` };
    payload = await r.json();
  } catch (e) {
    return { error: `receiver unreachable: ${e.message}` };
  }
  let reply;
  try {
    reply = await chrome.tabs.sendMessage(tab.id, { type: 'seed', payload });
  } catch (e) {
    await injectInto(tab.id);
    await new Promise((r) => setTimeout(r, 400));
    reply = await chrome.tabs.sendMessage(tab.id, { type: 'seed', payload });
  }
  if (!reply || reply.error) return { error: reply ? reply.error : 'no reply' };
  await chrome.tabs.reload(tab.id);             // the app reads its session at load, not later
  await new Promise((r) => setTimeout(r, 6000));
  return { wrote: reply.wrote };
}

async function collectNow(reason = 'manual') {
  const c = await cfg();
  const tab = await fomoTab();
  if (!tab) {
    await setStatus({ ok: false, reason, message: 'no fomo.family tab open' });
    return { error: 'no fomo.family tab open' };
  }
  const seeded = await takeSeed(tab);
  if (seeded && seeded.wrote) {
    await setStatus({ ok: true, reason, message: `session handed over: ${seeded.wrote.local} keys` });
  }
  // knownUserIds lets the page skip wallets we already resolved, so repeat runs stay cheap
  const known = (await chrome.storage.local.get('knownUserIds')).knownUserIds || [];
  const msg = {
    type: 'collect',
    payload: { resolveTop: c.resolveTop, withPositions: c.withPositions, knownUserIds: known },
  };
  let reply;
  try {
    reply = await chrome.tabs.sendMessage(tab.id, msg);
  } catch (e) {
    // A tab opened before the extension was loaded has no content scripts. Rather than asking
    // for a reload, put them in now and try once more.
    try {
      await injectInto(tab.id);
      await new Promise((r) => setTimeout(r, 400));
      reply = await chrome.tabs.sendMessage(tab.id, msg);
    } catch (e2) {
      const message = `could not reach the page: ${e2.message || e2}`;
      await setStatus({ ok: false, reason, message });
      return { error: message };
    }
  }
  if (!reply || reply.error) {
    await setStatus({ ok: false, reason, message: reply ? reply.error : 'no reply' });
    return { error: reply ? reply.error : 'no reply' };
  }

  const data = reply.data;
  const counts = {
    leaderboards: Object.keys(data.leaderboards || {}).length,
    swaps: Object.keys(data.swaps || {}).length,
    holders: Object.keys(data.holders || {}).length,
    trades: Object.keys(data.trades || {}).length,
  };
  try {
    const r = await fetch(c.endpoint, {
      method: 'POST',
      headers: { 'content-type': 'application/json', ...(c.token ? { 'x-agent-token': c.token } : {}) },
      body: JSON.stringify(data),
    });
    const body = await r.text();
    if (!r.ok) throw new Error(`${r.status} ${body.slice(0, 120)}`);
    // remember who we resolved so the next run spends its budget on new faces
    const known = new Set((await chrome.storage.local.get('knownUserIds')).knownUserIds || []);
    Object.keys(data.swaps || {}).forEach((id) => known.add(id));
    await chrome.storage.local.set({ knownUserIds: [...known].slice(-2000) });
    await setStatus({ ok: true, reason, message: `sent ${counts.swaps} wallets`, counts, response: body.slice(0, 200) });
    return { ok: true, counts };
  } catch (e) {
    await setStatus({ ok: false, reason, message: `receiver unreachable: ${e.message}`, counts });
    return { error: `receiver unreachable: ${e.message}` };
  }
}

async function reschedule() {
  const c = await cfg();
  await chrome.alarms.clear('collect');
  // Chrome's own floor for alarms is 30s; 1 minute is as fast as this is ever worth running,
  // since a single pass already takes ~20s of paced requests.
  if (c.enabled) chrome.alarms.create('collect', { periodInMinutes: Math.max(1, Number(c.intervalMinutes) || 30) });
}

chrome.runtime.onInstalled.addListener(reschedule);
chrome.runtime.onStartup.addListener(reschedule);
chrome.alarms.onAlarm.addListener((a) => { if (a.name === 'collect') collectNow('alarm'); });

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg.type === 'collectNow') { collectNow('manual').then(sendResponse); return true; }
  if (msg.type === 'reschedule') { reschedule().then(() => sendResponse({ ok: true })); return true; }
  if (msg.type === 'token') { setStatus({ tokenSeenAt: Date.now() }); }
  return false;
});
