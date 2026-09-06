/* Relay between the page-world collector and the extension service worker.
 * Only messages carrying the __fomoAgent marker and coming from this window are forwarded. */
if (window.__fomoAgentBridge) throw new Error('bridge already installed');
window.__fomoAgentBridge = true;

const pending = new Map();

window.addEventListener('message', (ev) => {
  const d = ev.data;
  if (ev.source !== window || !d || !d.__fomoAgent) return;
  if (d.type === 'collected') {
    const resolve = pending.get(d.payload.id);
    if (resolve) {
      pending.delete(d.payload.id);
      resolve(d.payload);
    }
    return;
  }
  chrome.runtime.sendMessage({ type: d.type, payload: d.payload }).catch(() => {});
});

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg.type !== 'collect') return false;
  const id = Math.random().toString(36).slice(2);
  const timer = setTimeout(() => {
    if (pending.delete(id)) sendResponse({ error: 'collector timed out (page may still be loading)' });
  }, 180000);
  pending.set(id, (payload) => {
    clearTimeout(timer);
    sendResponse(payload);
  });
  window.postMessage({ __fomoAgentReq: true, type: 'collect', id, payload: msg.payload || {} }, '*');
  return true; // keep the channel open for the async reply
});
