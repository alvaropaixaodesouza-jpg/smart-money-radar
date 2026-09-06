/*
 * fomo-agent — browser-side export (phase 0 fallback).
 *
 * Why: Cloudflare rejects any non-browser client at the edge with 430 {"error":"unauthorized"},
 * even when replaying the exact cURL that Chrome produces. Rather than defeating that, we let the
 * page you are already logged into make its own requests, exactly as the app does, and save the
 * answers to a file that `fomo-agent fomo-import` reads.
 *
 * HOW TO USE
 *   1. Open https://fomo.family and log in.
 *   2. F12 -> Console. Paste this whole file, press Enter.
 *   3. Click anything in the app that loads data (e.g. the Leaderboard tab). The script needs to
 *      see one authenticated request to learn the token the app uses.
 *   4. It then downloads fomo_export_<timestamp>.json. Give that file to fomo-agent:
 *        .venv/Scripts/python -m fomo_agent.cli fomo-import fomo_export_....json
 *
 * It reads only; nothing is sent anywhere except back to fomo's own API.
 */
(async () => {
  const API = 'https://prod-api.fomo.family';
  const PERIODS = ['24h', '7d', '30d'];
  const RESOLVE_TOP = 25;      // users whose execution wallets we look up
  const GAP_MS = 350;          // polite pacing, the app itself is burstier than this

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const log = (...a) => console.log('%c[fomo-agent]', 'color:#0a0', ...a);

  // --- 1. learn the Authorization header by watching one request the app makes ---
  const auth = await new Promise((resolve) => {
    const fromStorage = (() => {
      for (const store of [localStorage, sessionStorage]) {
        for (let i = 0; i < store.length; i++) {
          const v = store.getItem(store.key(i)) || '';
          const m = v.match(/eyJ[\w-]+\.eyJ[\w-]+\.[\w-]+/);
          if (m) {
            try {
              const p = JSON.parse(atob(m[0].split('.')[1].replace(/-/g, '+').replace(/_/g, '/')));
              if (p.iss && String(p.iss).includes('privy')) return 'Bearer ' + m[0];
            } catch (e) { /* not a JWT we understand */ }
          }
        }
      }
      return null;
    })();
    if (fromStorage) { log('token found in storage'); return resolve(fromStorage); }

    log('waiting for an authenticated request — click the Leaderboard tab in the app...');
    const orig = window.fetch;
    const timer = setTimeout(() => { window.fetch = orig; resolve(null); }, 120000);
    window.fetch = function (input, init) {
      try {
        const h = new Headers((init && init.headers) || (input instanceof Request ? input.headers : undefined));
        const a = h.get('authorization');
        const url = typeof input === 'string' ? input : (input && input.url) || '';
        if (a && url.includes('prod-api.fomo.family')) {
          clearTimeout(timer);
          window.fetch = orig;
          log('token captured');
          resolve(a);
        }
      } catch (e) { /* ignore */ }
      return orig.apply(this, arguments);
    };
  });

  if (!auth) { console.error('[fomo-agent] no token seen in 2 minutes. Reload the page and try again.'); return; }

  const headers = {
    accept: '*/*',
    authorization: auth,
    'content-type': 'application/json',
    'x-supported-chains': '1,56,143,4663,8453,1399811149',
  };
  const get = async (path) => {
    await sleep(GAP_MS);
    const r = await fetch(API + path, { headers, credentials: 'include' });
    if (!r.ok) { log('!', path, r.status); return null; }
    return r.json();
  };

  const out = { exportedAt: Math.floor(Date.now() / 1000), leaderboards: {}, swaps: {}, holders: {} };

  // --- 2. leaderboards ---
  for (const p of PERIODS) {
    const d = await get(`/v2/leaderboard/${p}`);
    if (d) {
      out.leaderboards[p] = d;
      log(`leaderboard ${p}:`, ((d.responseObject || {}).leaderboard || []).length, 'entries');
    }
  }

  // --- 3. execution wallets for the strongest traders ---
  const byPnl = new Map();
  for (const p of PERIODS) {
    for (const u of ((out.leaderboards[p] || {}).responseObject || {}).leaderboard || []) {
      const pnl = u[`pnl${p}`] ?? u.pnl30d ?? u.pnl7d ?? u.pnl24h ?? 0;
      if (!byPnl.has(u.id) || byPnl.get(u.id) < pnl) byPnl.set(u.id, pnl);
    }
  }
  const top = [...byPnl.entries()].sort((a, b) => b[1] - a[1]).slice(0, RESOLVE_TOP);
  log(`resolving execution wallets for ${top.length} users...`);
  for (const [id] of top) {
    const d = await get(`/v2/users/${id}/swaps`);
    if (d) out.swaps[id] = d;
  }

  // --- 4. top holders of whatever token page you have open ---
  const m = location.pathname.match(/\/tokens\/([^/]+)\/([^/?#]+)/);
  if (m) {
    const netIds = { solana: 1399811149, robinhood: 4663, base: 8453, bsc: 56, ethereum: 1 };
    const nid = netIds[m[1]] || 1399811149;
    const d = await get(`/hodlers/top?tokens=${encodeURIComponent(JSON.stringify([{ address: m[2], networkId: nid }]))}`);
    if (d) { out.holders[m[2]] = d; log('holders for', m[2]); }
  }

  // --- 5. save ---
  const blob = new Blob([JSON.stringify(out)], { type: 'application/json' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `fomo_export_${out.exportedAt}.json`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  log('done. Saved', a.download, '- now run: fomo-agent fomo-import <file>');
})();
