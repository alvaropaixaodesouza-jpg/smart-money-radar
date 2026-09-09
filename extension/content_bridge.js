/* Relay between the page-world collector and the extension service worker.
 * Only messages carrying the __fomoAgent marker and coming from this window are forwarded. */
if (window.__fomoAgentBridge) throw new Error('bridge already installed');
window.__fomoAgentBridge = true;

const pending = new Map();

window.addEventListener('message', (ev) => {
  const d = ev.data;
  if (ev.source !== window || !d || !d.__fomoAgent) return;
  if (d.type === 'collected' || d.type === 'seeded') {
    const resolve = pending.get(d.payload.id);
    if (resolve) {
      pending.delete(d.payload.id);
      resolve(d.payload);
    }
    return;
  }
  // Report the outcome back into the page. This is the one hop nothing could see: a message that
  // fails here disappears with a swallowed rejection, and from the server the symptom is a
  // collector that says it ran and a receiver that says nothing arrived.
  chrome.runtime.sendMessage({ type: d.type, payload: d.payload })
    .then(() => window.postMessage({ __fomoAgent: true, type: 'bridged', payload: { of: d.type } }, '*'))
    .catch((e) => window.postMessage({
      __fomoAgent: true, type: 'bridged',
      payload: { of: d.type, error: (e && e.message) || String(e) },
    }, '*'));
});

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg.type !== 'collect' && msg.type !== 'seed') return false;
  const id = Math.random().toString(36).slice(2);
  const timer = setTimeout(() => {
    if (pending.delete(id)) sendResponse({ error: `${msg.type} timed out (page may still be loading)` });
  }, msg.type === 'seed' ? 15000 : 180000);
  pending.set(id, (payload) => {
    clearTimeout(timer);
    sendResponse(payload);
  });
  window.postMessage({ __fomoAgentReq: true, type: msg.type, id, payload: msg.payload || {} }, '*');
  return true; // keep the channel open for the async reply
});
