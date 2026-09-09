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
  let authAt = 0;
  let lastBridge = null;

  const post = (type, payload) => window.postMessage({ __fomoAgent: true, type, payload }, '*');

  // --- 1. capture the token ---
  //
  // Counted as well as captured. A collector that reports "no token" cannot say whether the app
  // made no requests, made them without a bearer, or made them somewhere this hook cannot see -
  // and those need different fixes. The counters are on window.__fomoAgent.
  let apiCalls = 0, authCalls = 0, xhrCalls = 0;

  const origFetch = window.fetch;
  window.fetch = function (input, init) {
    try {
      const url = typeof input === 'string' ? input : (input && input.url) || '';
      if (url.includes('prod-api.fomo.family')) {
        apiCalls += 1;
        const h = new Headers((init && init.headers) || (input instanceof Request ? input.headers : undefined));
        const a = h.get('authorization');
        if (a) authCalls += 1;
        if (a && a !== auth) {
          auth = a;
          authAt = Date.now();
          post('token', { ok: true, at: authAt });
        }
      }
    } catch (e) { /* never break the app */ }
    return origFetch.apply(this, arguments);
  };

  // The app may reach the API through XMLHttpRequest rather than fetch, and a token that arrives
  // that way is just as good. Same treatment, so neither path is the one we happen to miss.
  const origOpen = XMLHttpRequest.prototype.open;
  const origSetHeader = XMLHttpRequest.prototype.setRequestHeader;
  XMLHttpRequest.prototype.open = function (method, url) {
    try { this.__fomoUrl = String(url || ''); } catch (e) { /* ignore */ }
    return origOpen.apply(this, arguments);
  };
  XMLHttpRequest.prototype.setRequestHeader = function (name, value) {
    try {
      if ((this.__fomoUrl || '').includes('prod-api.fomo.family')) {
        xhrCalls += 1;
        if (String(name).toLowerCase() === 'authorization' && value && value !== auth) {
          auth = value;
          authAt = Date.now();
          post('token', { ok: true, at: authAt });
        }
      }
    } catch (e) { /* never break the app */ }
    return origSetHeader.apply(this, arguments);
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
    if (d.type === 'bridged') { lastBridge = d.payload; return; }
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

  // A handle for the outside. The collector lives in a closure, which is right, but it also made
  // every failure look identical from the server: no data, no error, nothing to ask. This says
  // whether the token has been seen yet, which is the difference between "not ready" and "broken".
  // Reading it changes nothing.
  window.__fomoAgent = {
    get hasToken() { return !!auth; },
    get tokenAgeMin() { return authAt ? Math.round((Date.now() - authAt) / 60000) : null; },
    get seen() { return { apiCalls, authCalls, xhrCalls }; },
    get lastBridge() { return lastBridge; },
  };

  post('ready', {});
  console.log(`[${TAG}] collector injected`);
})();
