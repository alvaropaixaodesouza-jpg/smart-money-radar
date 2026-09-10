/* Service worker: schedules collections and forwards them to the local fomo-agent receiver.
 *
 * MV3 workers are killed when idle, so state lives in chrome.storage and the schedule in an alarm.
 * A collection needs an open fomo.family tab: the fetches deliberately happen in the page, which is
 * what keeps them identical to the app's own requests.
 */
// A page the app fetches authenticated data on, which the leaderboard - counterintuitively - is
// not. Any token page will do; this one is large and long-lived.
const TOKEN_PAGE = 'https://fomo.family/tokens/robinhood/0x39dbed3a2bd333467115de45665cc57f813c4571';

const DEFAULTS = {
  endpoint: 'http://127.0.0.1:8787/ingest',
  token: '',
  // Two hours, not thirty minutes. At the old cadence one account made roughly 1400 authenticated
  // calls a day - three leaderboards and two dozen wallet lookups every half hour, forever, on the
  // dot. That is what a scraper looks like from the other side, and on 2026-09-10 the account was
  // restricted for it. Twelve passes a day keeps every feed current: traders do not change rank by
  // the minute, and the on-chain tape, which does, comes from the chain rather than from here.
  intervalMinutes: 120,
  resolveTop: 8,
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
  // Every status carries the version of the code that wrote it. Twice now a fix has been deployed,
  // unpacked into the profile, confirmed present on disk — and not been the code Chrome was
  // actually running, which is indistinguishable from the fix not working. A version somebody can
  // read from outside is the difference between "installed" and "running".
  const version = chrome.runtime.getManifest().version;
  await chrome.storage.local.set({ status: { ...prev, ...patch, version, at: Date.now() } });
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
    return { error: `receiver unreachable: ${e && e.message ? e.message : e}` };
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
  // Whatever the handover did, say so. A silent failure here looks exactly like "nothing was
  // waiting", and the two need different fixes.
  // Reload first, always. fomo's bearer lives about an hour and the app only mints one while it
  // is working: a tab left sitting makes no requests, so the captured token expires and every
  // request after that is a 401 while the page still looks perfectly signed in.
  //
  // Which page matters, and not in the way you would guess. Measured on the collector: a token
  // page makes 57 calls to prod-api, all of them bearing the token. The leaderboard page makes
  // none at all - it never touches that host - so a tab parked there can never learn a token,
  // however signed in it is. TOKEN_PAGE is therefore a token page on purpose.
  if (!tab.url || !/\/tokens\//.test(tab.url)) {
    await chrome.tabs.update(tab.id, { url: TOKEN_PAGE });
  } else {
    await chrome.tabs.reload(tab.id);
  }
  await new Promise((r) => setTimeout(r, 15000));   // let the app sign a few requests of its own

  const seeded = await takeSeed(tab);
  await setStatus({
    seed: !seeded ? 'nothing waiting'
      : seeded.error ? `failed: ${seeded.error}`
      : `handed over ${seeded.wrote.local} keys`,
  });
  // knownUserIds lets the page skip wallets we already resolved, so repeat runs stay cheap
  const known = (await chrome.storage.local.get('knownUserIds')).knownUserIds || [];
  const pass = (((await chrome.storage.local.get('passCount')).passCount || 0) + 1) % 1000;
  await chrome.storage.local.set({ passCount: pass });
  const msg = {
    type: 'collect',
    payload: {
      resolveTop: c.resolveTop, withPositions: c.withPositions, knownUserIds: known,
      // 24h moves constantly and is worth every pass. 7d and 30d barely move, so they take turns:
      // at a two-hour cadence each is still refreshed twice a day, for a third fewer requests.
      periods: ['24h', (pass % 2 ? '7d' : '30d')],
      // what the server asked for in its reply to the last delivery
      mints: (await chrome.storage.local.get('wantMints')).wantMints || [],
    },
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
  // A stale bearer is the ordinary case, not an exception. fomo's Privy token lives about an
  // hour and the app only mints a new one while it is being used; a tab left open on a token page
  // stops making requests, so the token the collector captured quietly expires and every request
  // after that is a 401. Reloading makes the app sign its own requests again, and the hook picks
  // up the new token on the way past.
  if (reply && reply.error && /\b(401|403|430|no token seen)\b/.test(reply.error)) {
    await setStatus({ ok: false, reason, message: `${reply.error} - reloading to refresh the token` });
    await chrome.tabs.reload(tab.id);
    await new Promise((r) => setTimeout(r, 15000));
    try {
      reply = await chrome.tabs.sendMessage(tab.id, msg);
    } catch (e) {
      await injectInto(tab.id);
      await new Promise((r) => setTimeout(r, 500));
      reply = await chrome.tabs.sendMessage(tab.id, msg);
    }
  }

  if (!reply || reply.error) {
    await setStatus({ ok: false, reason, message: reply ? reply.error : 'no reply' });
    return { error: reply ? reply.error : 'no reply' };
  }

  return deliver(reply.data, reason);
}

/** Post a collected payload to the receiver. The half of collectNow that is worth reusing. */
async function deliver(data, reason) {
  const c = await cfg();
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
    // The reply says which tokens to ask about next. Parsed defensively: an older receiver, or a
    // proxy that rewrote the body, must leave the collector working exactly as it did before.
    // The reply says which tokens to ask about next. Parsed defensively: an older receiver, or a
    // proxy that rewrote the body, must leave the collector working exactly as it did before.
    //
    // The outcome goes into the status because this hop was invisible for a day: the delivery
    // reported success either way, so a list that never arrived and a list that arrived and was
    // ignored produced identical evidence.
    let wanted = 'no wants in the reply';
    try {
      const wants = JSON.parse(body);
      const mints = wants && wants.wants && wants.wants.mints;
      if (Array.isArray(mints)) {
        await chrome.storage.local.set({ wantMints: mints.slice(0, 40) });
        wanted = `stored ${mints.length}`;
      } else if (wants && wants.wants) {
        wanted = `wants.mints was ${typeof mints}`;
      }
    } catch (e) {
      wanted = `reply was not JSON: ${String((e && e.message) || e).slice(0, 60)}`;
    }
    await setStatus({ ok: true, reason, message: `sent ${counts.swaps} wallets`, counts, wanted });
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
  // delayInMinutes as well as periodInMinutes, so a browser that has just started collects within
  // the minute instead of waiting out a whole period. That matters because the watchdog restarts
  // this browser precisely when collection has stalled - waiting another half hour after being
  // restarted for being late would be its own kind of funny.
  if (c.enabled) {
    // A little jitter. Firing at exactly :00 and :30 for weeks is itself a tell, and it costs
    // nothing to arrive at an ordinary-looking time instead.
    const period = Math.max(1, Number(c.intervalMinutes) || 120);
    chrome.alarms.create('collect', {
      delayInMinutes: 0.5,
      periodInMinutes: period + (Math.random() * period * 0.15),
    });
  }
}

/** A session waiting to be handed over should apply now, not at the next collection.
 *
 * Somebody has just run a batch file and is watching to see whether it worked; making them wait
 * half an hour for the alarm is the difference between a tool that works and one that seems not
 * to. The tab needs a moment to exist first, hence the short delay rather than an immediate call.
 */
async function seedOnStart() {
  await new Promise((r) => setTimeout(r, 4000));
  const tab = await fomoTab();
  if (!tab) return;
  const taken = await takeSeed(tab);
  if (taken && taken.wrote) {
    await setStatus({ ok: true, reason: 'seed', message: `session handed over: ${taken.wrote.local} keys` });
    await collectNow('after-seed');
  }
}

/** Put the content scripts into a tab that was already open when this version arrived.
 *
 * A manifest's content_scripts only run on navigation, so installing or updating the extension
 * leaves every open tab running the previous version's - or, on a fresh install, none at all. The
 * collector's tab is open permanently by design, so without this an update silently disables the
 * thing it was updating.
 */
async function adoptOpenTabs() {
  const tab = await fomoTab();
  if (!tab) return;
  try {
    await injectInto(tab.id);
  } catch (e) {
    // A tab mid-navigation refuses the injection and will get the scripts from the manifest anyway.
    console.warn('[fomo-agent] could not adopt the open tab:', e && e.message);
  }
}

setStatus({ reason: 'startup', message: 'service worker started' });
chrome.runtime.onInstalled.addListener(() => { reschedule(); adoptOpenTabs(); seedOnStart(); });
chrome.runtime.onStartup.addListener(() => { reschedule(); adoptOpenTabs(); seedOnStart(); });
// The service worker also starts on demand after Chrome restarts a session, where neither event
// above fires; a seed left waiting then would sit until the next alarm.
seedOnStart();
adoptOpenTabs();
chrome.alarms.onAlarm.addListener((a) => { if (a.name === 'collect') collectNow('alarm'); });

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg.type === 'collectNow') { collectNow('manual').then(sendResponse); return true; }
  if (msg.type === 'reschedule') { reschedule().then(() => sendResponse({ ok: true })); return true; }
  if (msg.type === 'token') { setStatus({ tokenSeenAt: Date.now() }); }
  return false;
});
