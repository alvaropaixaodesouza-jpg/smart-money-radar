/* Runs in the PAGE's own JS world on fomo.family.
 *
 * Two jobs:
 *   1. learn the Authorization header by watching one request the app makes (the Privy token
 *      expires hourly, so it has to be re-learned rather than stored once);
 *   2. when asked, fetch the fomo endpoints using the page's own `fetch`, so the requests are
 *      indistinguishable from the app's — same origin, same session, same TLS stack.
 *
 * It talks to content_bridge.js through window.postMessage; nothing else can reach it.
 */
(() => {
  // executeScript can re-inject into a page that already has us; installing twice would stack
  // fetch hooks and answer every request more than once.
  if (window.__fomoAgentCollector) return;
  window.__fomoAgentCollector = true;

  const API = 'https://prod-api.fomo.family';
  const TAG = 'fomo-agent';
  let auth = null;

  const post = (type, payload) => window.postMessage({ __fomoAgent: true, type, payload }, '*');

  // --- 1. capture the token ---
  const origFetch = window.fetch;
  window.fetch = function (input, init) {
    try {
      const url = typeof input === 'string' ? input : (input && input.url) || '';
      if (url.includes('prod-api.fomo.family')) {
        const h = new Headers((init && init.headers) || (input instanceof Request ? input.headers : undefined));
        const a = h.get('authorization');
        if (a && a !== auth) {
          auth = a;
          post('token', { ok: true, at: Date.now() });
        }
      }
    } catch (e) { /* never break the app */ }
    return origFetch.apply(this, arguments);
  };

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  async function get(path) {
    const r = await origFetch(API + path, {
      headers: {
        accept: '*/*',
        authorization: auth,
        'content-type': 'application/json',
        'x-supported-chains': '1,56,143,4663,8453,1399811149',
      },
      credentials: 'include',
    });
    if (!r.ok) throw new Error(`${path} -> ${r.status}`);
    return r.json();
  }

  // --- 2. collect on demand ---
  async function collect(opts) {
    if (!auth) throw new Error('no token seen yet — open a page in the app that loads data');
    const periods = opts.periods || ['24h', '7d', '30d'];
    const resolveTop = opts.resolveTop ?? 25;
    // NB: /trades needs a tokenAddress and answers 400 without one. Per-token positions come
    // from topHoldings inside the leaderboard response instead, at no extra request.
    const gap = opts.gapMs ?? 350;
    const out = { exportedAt: Math.floor(Date.now() / 1000), leaderboards: {}, swaps: {}, trades: {}, tradeErrors: {}, holders: {} };

    for (const p of periods) {
      out.leaderboards[p] = await get(`/v2/leaderboard/${p}`);
      await sleep(gap);
    }

    // pick the strongest traders across windows, then learn the wallets they actually trade from
    const byPnl = new Map();
    for (const p of periods) {
      for (const u of ((out.leaderboards[p] || {}).responseObject || {}).leaderboard || []) {
        const pnl = u[`pnl${p}`] ?? u.pnl30d ?? u.pnl7d ?? u.pnl24h ?? 0;
        if (!byPnl.has(u.id) || byPnl.get(u.id) < pnl) byPnl.set(u.id, pnl);
      }
    }
    const skip = new Set(opts.knownUserIds || []);
    const top = [...byPnl.entries()]
      .sort((a, b) => b[1] - a[1])
      .filter(([id]) => !skip.has(id))
      .slice(0, resolveTop);
    for (const [id] of top) {
      try {
        out.swaps[id] = await get(`/v2/users/${id}/swaps`);
      } catch (e) { /* one bad user must not sink the batch */ }
      await sleep(gap);
    }

    // if a token page is open, its holders are free to grab
    const m = location.pathname.match(/\/tokens\/([^/]+)\/([^/?#]+)/);
    if (m) {
      const nets = { solana: 1399811149, robinhood: 4663, base: 8453, bsc: 56, ethereum: 1 };
      const q = encodeURIComponent(JSON.stringify([{ address: m[2], networkId: nets[m[1]] || 1399811149 }]));
      try {
        out.holders[m[2]] = await get(`/hodlers/top?tokens=${q}`);
      } catch (e) { /* optional */ }
    }
    return out;
  }

  // --- 3. accept a session from a browser that is already signed in ---
  // Signing in on a machine with no keyboard in front of it means an Apple or Google password
  // through a remote desktop and a two-factor prompt on a phone somewhere else. Carrying the
  // session across instead is the same act as staying signed in, done deliberately and once.
  function seed(payload) {
    const wrote = { local: 0, session: 0, cookies: 0 };
    for (const [k, v] of Object.entries(payload.local || {})) {
      try { window.localStorage.setItem(k, v); wrote.local++; } catch (e) { /* quota, private mode */ }
    }
    for (const [k, v] of Object.entries(payload.session || {})) {
      try { window.sessionStorage.setItem(k, v); wrote.session++; } catch (e) { /* ditto */ }
    }
    for (const [k, v] of Object.entries(payload.cookies || {})) {
      // A cookie the page could read is a cookie the page can set. HttpOnly ones never left the
      // other browser in the first place, which is the point of them.
      document.cookie = `${k}=${v}; path=/; max-age=${60 * 60 * 24 * 30}; samesite=lax`;
      wrote.cookies++;
    }
    return wrote;
  }

  window.addEventListener('message', async (ev) => {
    const d = ev.data;
    if (ev.source !== window || !d || !d.__fomoAgentReq) return;
    if (d.type === 'ping') return post('pong', { hasToken: !!auth });
    if (d.type === 'seed') {
      let wrote = null;
      try { wrote = seed(d.payload || {}); } catch (e) { /* report it as nothing written */ }
      return post('seeded', { id: d.id, wrote });
    }
    if (d.type !== 'collect') return;
    try {
      post('collected', { id: d.id, data: await collect(d.payload || {}) });
    } catch (e) {
      post('collected', { id: d.id, error: String(e && e.message || e) });
    }
  });

  // --- 4. the schedule, kept here rather than in the extension ---
  //
  // chrome.alarms is the obvious home for this and it does not hold. In Manifest V3 the service
  // worker is torn down when idle and the alarm is meant to wake it; on the collector machine it
  // stopped waking it after a few hours, twice, with the browser still signed in and nothing
  // failing. A page timer has no such lifecycle: this tab is always open, and the browser is
  // started with background timer throttling disabled precisely so this keeps ticking.
  //
  // The page collects and hands the result to the extension, which does the one part the page
  // cannot: posting to a plain-http receiver from an https page.
  const EVERY_MS = 30 * 60 * 1000;
  let busy = false;
  let collected = 0;

  async function tick() {
    // The token is learned by watching the app's own requests, so the first minutes after a load
    // have nothing to collect with. Waiting out a whole period for that would mean a browser that
    // just restarted sits idle for half an hour.
    if (busy || !auth) return false;
    busy = true;
    try {
      post('autocollect', await collect({ resolveTop: 25, withPositions: true }));
      collected += 1;
      return true;
    } catch (e) {
      console.warn(`[${TAG}] scheduled collection failed:`, (e && e.message) || e);
      return false;
    } finally {
      busy = false;
    }
  }

  // Try often until the first one lands, then settle into the real interval.
  const opening = setInterval(async () => {
    if (await tick()) {
      clearInterval(opening);
      setInterval(tick, EVERY_MS);
    }
  }, 20000);

  post('ready', {});
  console.log(`[${TAG}] collector injected`);
})();
