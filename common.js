// Shared by ui.html and lol.html: DOM helper, saved settings, and the page runtime that polls the server.
const $ = s => document.querySelector(s);

// Feed and Riot text is untrusted: build DOM with textContent only, never innerHTML.
function el(tag, props = {}, ...kids) {
  const e = Object.assign(document.createElement(tag), props);
  e.append(...kids);
  return e;
}
const keyed = (node, k) => (node.dataset.key = k, node);  // lets render() put keyboard focus back on the same control
const post = (path, body) => fetch(path, { method: 'POST', body: JSON.stringify(body || {}) }).then(r => r.json());
const toTop = () => scrollTo(0, 0);

// Settings kept in this browser only. Returns [state, save].
function loadState(key, defaults) {
  const S = { ...defaults };
  try { Object.assign(S, JSON.parse(localStorage.getItem(key) || '{}')); } catch {}
  return [S, () => { try { localStorage.setItem(key, JSON.stringify(S)); } catch {} }];
}

const STOPPED = 'Server stopped. Run run.sh (run.bat on Windows) to start it again.';

// The runtime both pages share. It polls `url()`, calls render() only when the data changed (so an open
// page is not rebuilt under the reader), shows status and errors in #status, drives the Refresh button, and
// keeps a connection open so the server knows the page is there (it exits when the last page closes).
// status(data) gives the text shown while idle.
function createPage({ url, initial, render, status }) {
  const P = { data: initial, error: '', flashMsg: '', text: '', timer: null, flashTimer: null };
  P.renderStatus = () => {
    $('#status').replaceChildren(...(P.flashMsg || P.error ? [P.flashMsg || P.error]
      : P.data.refreshing ? [el('span', { className: 'spin' }), 'Fetching…'] : [status ? status(P.data) : '']));
    $('#refresh').disabled = P.data.refreshing || !!P.error;
  };
  P.flash = msg => {
    P.flashMsg = msg; P.renderStatus();
    clearTimeout(P.flashTimer); P.flashTimer = setTimeout(() => { P.flashMsg = ''; P.renderStatus(); }, 5000);
  };
  P.load = async () => {
    clearTimeout(P.timer);
    let text = null;
    try { text = await (await fetch(url())).text(); } catch {}
    if (text === null) { P.error = STOPPED; render(); }
    else {
      const same = text === P.text && !P.error, wasRefreshing = P.data.refreshing;
      P.error = ''; P.text = text; P.data = JSON.parse(text);
      if (!same || wasRefreshing !== P.data.refreshing) render();
    }
    P.timer = setTimeout(P.load, P.data.refreshing ? 3000 : 300000);
  };
  P.reload = () => { P.text = ''; return P.load(); };  // load and redraw even if the text looks the same
  P.refresh = async () => {
    if (P.data.refreshing) return;
    P.data.refreshing = true; render();
    try {
      const r = await post('/api/refresh');
      if (r.error) P.flash('Refresh failed: ' + r.error);
    } catch { P.flash('Refresh failed. Is the server still running?'); }
    P.load();
  };
  const live = new EventSource('/api/live');
  live.onopen = () => { if (P.error) { P.error = ''; P.load(); } };
  live.onerror = () => { P.error = STOPPED; render(); };
  return P;
}
