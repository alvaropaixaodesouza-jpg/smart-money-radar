/* Hand your fomo.family session to the collector browser on the server.
 *
 * The server runs a real, signed-in Chrome so fomo's data can be collected without a laptop being
 * awake. Signing in there means typing an Apple or Google password through a remote desktop, with
 * two-factor prompts landing on a phone that is nowhere near it. This moves the session instead.
 *
 * Run it in YOUR browser, on a fomo.family tab where you are already signed in:
 *   F12 -> Console -> paste -> Enter.
 * It downloads fomo-session.json. Nothing is uploaded from here and nothing is printed: the file
 * is the only copy, and radar-session-push.bat sends it over the same SSH key you already use.
 *
 * What it takes: the login-related keys this origin keeps in local storage, and the cookies the
 * page can see. Not your passwords - the site never has those - and nothing from any other site.
 */
(() => {
  const KEEP = /^(privy|fomo|wagmi|walletconnect|__fomo)/i;

  const pick = (store) => {
    const out = {};
    for (let i = 0; i < store.length; i++) {
      const k = store.key(i);
      if (KEEP.test(k)) out[k] = store.getItem(k);
    }
    return out;
  };

  const local = pick(window.localStorage);
  const session = pick(window.sessionStorage);
  const cookies = Object.fromEntries(
    document.cookie.split(';').map((c) => c.trim()).filter(Boolean).map((c) => {
      const i = c.indexOf('=');
      return [c.slice(0, i), c.slice(i + 1)];
    }).filter(([k]) => KEEP.test(k)),
  );

  const payload = {
    origin: location.origin,
    at: Math.floor(Date.now() / 1000),
    local,
    session,
    cookies,
  };

  const counts = `${Object.keys(local).length} local, ${Object.keys(session).length} session, ${Object.keys(cookies).length} cookies`;
  if (!Object.keys(local).length && !Object.keys(cookies).length) {
    console.warn('[fomo] nothing to take - are you signed in on this tab?');
    return;
  }

  const blob = new Blob([JSON.stringify(payload)], { type: 'application/json' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'fomo-session.json';
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 5000);
  console.log(`[fomo] saved fomo-session.json (${counts}). Keys only, values not shown:`,
              Object.keys(local).concat(Object.keys(cookies)));
})();
