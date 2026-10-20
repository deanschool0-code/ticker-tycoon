/* Ticker Tycoon front-end (vanilla JS). */
'use strict';

// ============================================================ helpers
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const TZ = -new Date().getTimezoneOffset() * 60;

function fmtUsd(v, opts = {}) {
  if (v == null || isNaN(v)) return '-';
  const a = Math.abs(v);
  let d = 2;
  if (opts.price) d = a >= 1000 ? 2 : a >= 1 ? 2 : a >= 0.01 ? 4 : 8;
  const s = a.toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d });
  return (v < 0 ? '-' : (opts.sign && v > 0 ? '+' : '')) + '$' + s;
}
const fmtPx = (v) => fmtUsd(v, { price: true });
const fmtPct = (v) => (v == null || isNaN(v)) ? '' : (v > 0 ? '+' : '') + v.toFixed(2) + '%';
const cls = (v) => (v > 0 ? 'up' : v < 0 ? 'down' : '');
function fmtQty(q) { return q >= 100 ? q.toLocaleString('en-US', { maximumFractionDigits: 2 }) : q >= 1 ? q.toFixed(4).replace(/0+$/, '').replace(/\.$/, '') : q.toPrecision(4); }
function ago(ts) {
  const s = Date.now() / 1000 - ts;
  if (s < 60) return 'just now';
  if (s < 3600) return Math.floor(s / 60) + 'm ago';
  if (s < 86400) return Math.floor(s / 3600) + 'h ago';
  return Math.floor(s / 86400) + 'd ago';
}
function dur(s) {
  if (s < 3600) return Math.round(s / 60) + ' min';
  if (s < 86400) return (s / 3600).toFixed(1).replace('.0', '') + ' hours';
  return (s / 86400).toFixed(1).replace('.0', '') + ' days';
}
function avatarHtml(a, lg) {
  const sym = (a.symbol || '?').toUpperCase();
  const hue = [...sym].reduce((h, c) => (h * 31 + c.charCodeAt(0)) % 360, 7);
  let img = a.logo;
  if (!img && a.kind && a.kind !== 'crypto' && a.symbol) img = 'https://financialmodelingprep.com/image-stock/' + encodeURIComponent(a.symbol.replace('.', '-')) + '.png';
  const inner = img ? `<img src="${esc(img)}" alt="" loading="lazy" onerror="this.remove()">` : '';
  return `<div class="avatar ${lg ? 'lg' : ''} ${sym.length > 3 ? 's4' : ''}" style="background:hsl(${hue} 45% 28%)"><span>${esc(sym.slice(0, 4))}</span>${inner}</div>`;
}

async function api(path, opts = {}) {
  const o = { headers: { 'Content-Type': 'application/json' }, ...opts };
  if (o.body && typeof o.body !== 'string') o.body = JSON.stringify(o.body);
  const sep = path.includes('?') ? '&' : '?';
  const url = path.startsWith('/api/') && !/[?&]mode=/.test(path) ? path + sep + 'mode=' + S.mode : path;
  let r;
  try { r = await fetch(url, o); } catch (e) { throw new Error('Server not reachable. Is Ticker Tycoon still running?'); }
  let js = null;
  try { js = await r.json(); } catch (e) { /* ignore */ }
  if (r.status === 401) { location.href = '/login'; throw new Error('Please log in'); }
  if (!r.ok) throw new Error((js && js.error) || ('HTTP ' + r.status));
  return js;
}
const post = (p, body) => api(p, { method: 'POST', body: body || {} });

function toast(msg, kind = '', ms = 4200) {
  const el = document.createElement('div');
  el.className = 'toast ' + kind;
  el.innerHTML = msg;
  const box = $('#toasts');
  box.appendChild(el);
  while (box.children.length > 4) box.firstChild.remove();
  setTimeout(() => { el.classList.add('out'); setTimeout(() => el.remove(), 320); }, ms);
}
const toastErr = (e) => toast(esc(e.message || e), 'bad');

function modal(html, wide) {
  const m = $('#modal');
  m.className = 'modal' + (wide ? ' wide' : '');
  m.innerHTML = html;
  $('#modalBack').classList.remove('hidden');
  S.modalOpen = true;
  return m;
}
function closeModal() {
  $('#modalBack').classList.add('hidden');
  $('#modal').innerHTML = '';
  S.modalOpen = false;
}
$('#modalBack').addEventListener('mousedown', (e) => { if (e.target.id === 'modalBack') closeModal(); });

// ============================================================ state
const S = {
  mode: localStorage.getItem('tt_mode') || 'live',
  tab: 'trade',
  filter: 'all', sector: '', query: '',
  results: [], watch: new Set(),
  sel: localStorage.getItem('tt_sel') || 'S:AAPL',
  asset: null, quote: null,
  tf: localStorage.getItem('tt_tf') || '1D',
  st: null,                 // last /api/state
  unit: 'usd', lev: 1,
  placing: null,            // 'sl' | 'tp' | {pid, which}
  editPid: null,
  modalOpen: false,
  newsTab: 'news',
  lessons: null, lessonSel: null,
  lastPrice: null,
};
S.mode = 'live';  // the game runs on the live market only

// ============================================================ chart
let chart, candleSeries, volSeries, priceLines = [], lastCandleTime = 0, chartToken = 0;
const lines = { formSL: null, formTP: null };

function initChart() {
  const el = $('#chart');
  if (!window.LightweightCharts) {
    $('#chartNote').textContent = 'Chart library could not load from the CDN (unpkg.com). Check your internet connection.';
    $('#chartNote').classList.remove('hidden');
    return;
  }
  chart = LightweightCharts.createChart(el, {
    layout: { background: { type: 'solid', color: 'transparent' }, textColor: '#8e8e98', fontFamily: getComputedStyle(document.body).fontFamily },
    grid: { vertLines: { color: 'rgba(255,255,255,0.035)' }, horzLines: { color: 'rgba(255,255,255,0.035)' } },
    rightPriceScale: { borderColor: 'rgba(255,255,255,0.08)', scaleMargins: { top: 0.08, bottom: 0.22 } },
    timeScale: { borderColor: 'rgba(255,255,255,0.08)', timeVisible: true, secondsVisible: false, rightOffset: 6 },
    crosshair: { mode: 0, vertLine: { color: 'rgba(255,255,255,.25)', labelBackgroundColor: '#2c2d36' }, horzLine: { color: 'rgba(255,255,255,.25)', labelBackgroundColor: '#2c2d36' } },
    handleScroll: true, handleScale: true, autoSize: true, localization: { locale: 'en-US' },
  });
  candleSeries = chart.addCandlestickSeries({ upColor: '#30d158', downColor: '#ff453a', borderVisible: false, wickUpColor: '#30d158', wickDownColor: '#ff453a' });
  volSeries = chart.addHistogramSeries({ priceFormat: { type: 'volume' }, priceScaleId: 'vol', color: 'rgba(255,255,255,.15)' });
  chart.priceScale('vol').applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
  chart.subscribeClick(onChartClick);
  initDrag(el);
}

function precisionFor(p) {
  if (!p) return { precision: 2, minMove: 0.01 };
  if (p >= 1) return { precision: 2, minMove: 0.01 };
  if (p >= 0.01) return { precision: 4, minMove: 0.0001 };
  if (p >= 0.0001) return { precision: 6, minMove: 0.000001 };
  return { precision: 8, minMove: 0.00000001 };
}

const toBar = (c) => ({ time: c.time + TZ, open: c.open, high: c.high, low: c.low, close: c.close });
const toVol = (c) => ({ time: c.time + TZ, value: c.volume, color: c.close >= c.open ? 'rgba(48,209,88,.28)' : 'rgba(255,69,58,.28)' });

async function loadChart(fit = true) {
  if (!chart) return;
  const tok = ++chartToken;
  const note = $('#chartNote');
  if (fit) { candleSeries.setData([]); volSeries.setData([]); clearLines(); }
  note.innerHTML = 'Loading real chart data...';
  note.classList.remove('hidden');
  try {
    const js = await Promise.race([
      api(`/api/candles?id=${encodeURIComponent(S.sel)}&tf=${S.tf}`),
      new Promise((_, rej) => setTimeout(() => rej(new Error('The data source is slow to answer.')), 25000)),
    ]);
    if (tok !== chartToken) return;
    const cs = js.candles || [];
    if (!cs.length) {
      candleSeries.setData([]); volSeries.setData([]);
      note.innerHTML = `<div>${esc(js.note || 'No chart data')}<br><button class="btn ghost retry" onclick="loadChart(true)">Retry</button></div>`;
      note.classList.remove('hidden');
      return;
    }
    note.classList.add('hidden');
    const last = cs[cs.length - 1].close;
    const pf = precisionFor(last);
    candleSeries.applyOptions({ priceFormat: { type: 'price', ...pf } });
    candleSeries.setData(cs.map(toBar));
    volSeries.setData(cs.map(toVol));
    lastCandleTime = cs[cs.length - 1].time;
    if (fit) {
      const n = cs.length;
      chart.timeScale().setVisibleLogicalRange({ from: Math.max(0, n - 120), to: n + 4 });
    }
    drawLines();
  } catch (e) {
    if (tok === chartToken) { note.innerHTML = `<div>${esc(e.message)}<br><button class="btn ghost retry" onclick="loadChart(true)">Retry</button></div>`; note.classList.remove('hidden'); }
  }
}

async function tickChart() {
  if (!chart || S.tab !== 'trade' || !lastCandleTime) return;
  const tok = chartToken;
  try {
    const step = { '1m': 60, '5m': 300, '1h': 3600, '1D': 86400, '1W': 604800 }[S.tf];
    const js = await api(`/api/candles?id=${encodeURIComponent(S.sel)}&tf=${S.tf}&since=${lastCandleTime - step}`);
    if (tok !== chartToken) return;
    for (const c of js.candles || []) {
      if (c.time >= lastCandleTime - step) {
        candleSeries.update(toBar(c)); volSeries.update(toVol(c));
        lastCandleTime = Math.max(lastCandleTime, c.time);
      }
    }
  } catch (e) { /* silent */ }
}

function livePriceIntoChart(px) {
  // keep the newest candle in sync with the latest quote (live mode)
  if (!chart || !lastCandleTime || S.mode !== 'live' || !px || !candleSeries.data) return;
  try {
    const d = candleSeries.data();
    const b = d[d.length - 1];
    if (b && b.time === lastCandleTime + TZ) candleSeries.update({ ...b, close: px, high: Math.max(b.high, px), low: Math.min(b.low, px) });
  } catch (e) { /* ignore */ }
}

function clearLines() {
  for (const l of priceLines) { try { candleSeries.removePriceLine(l.line); } catch (e) { /* */ } }
  priceLines = [];
}
function addLine(price, color, title, style, drag) {
  if (price == null || isNaN(price)) return;
  const line = candleSeries.createPriceLine({ price: +price, color, lineWidth: drag ? 2 : 1, lineStyle: style, axisLabelVisible: true, title });
  priceLines.push({ line, price: +price, drag });
}
function drawLines() {
  if (!candleSeries) return;
  clearLines();
  const ps = (S.st?.positions || []).filter((p) => p.asset_id === S.sel);
  for (const p of ps) {
    const tag = p.bot_id ? 'BOT ' : p.fund_id ? 'FUND ' : '';
    addLine(p.entry, '#8e8e98', `${tag}${p.side.toUpperCase()} entry`, 2, null);
    if (!p.bot_id && !p.fund_id) {
      if (p.sl) addLine(p.sl, '#ff453a', 'SL', 1, { pid: p.id, which: 'sl' });
      if (p.tp) addLine(p.tp, '#30d158', 'TP', 1, { pid: p.id, which: 'tp' });
    }
    if (p.liq_price) addLine(p.liq_price, '#ff9f0a', 'Liquidation', 3, null);
  }
  const sl = parseFloat($('#slInput').value), tp = parseFloat($('#tpInput').value);
  if (!isNaN(sl)) addLine(sl, 'rgba(255,69,58,.75)', 'new SL', 2, { form: 'sl' });
  if (!isNaN(tp)) addLine(tp, 'rgba(48,209,88,.75)', 'new TP', 2, { form: 'tp' });
}

// --- click to place SL/TP
function setPlacing(p) {
  S.placing = p;
  $$('.link-btn[data-place]').forEach((b) => b.classList.toggle('on', p && b.dataset.place === p));
  const hint = $('#placeHint');
  const wrap = $('.chart-wrap');
  if (p) {
    const what = typeof p === 'string' ? (p === 'sl' ? 'stop loss' : 'take profit') : (p.which === 'sl' ? 'stop loss' : 'take profit') + ' for your position';
    hint.textContent = `Click the chart to set the ${what} (Esc to cancel)`;
    hint.classList.remove('hidden'); wrap.classList.add('placing');
  } else { hint.classList.add('hidden'); wrap.classList.remove('placing'); }
}
async function onChartClick(param) {
  if (!S.placing || !param.point) return;
  const price = candleSeries.coordinateToPrice(param.point.y);
  if (price == null) return;
  const pf = precisionFor(price);
  const v = +price.toFixed(pf.precision);
  const p = S.placing;
  setPlacing(null);
  if (typeof p === 'string') {
    $(p === 'sl' ? '#slInput' : '#tpInput').value = v;
    updateOrderSummary(); drawLines();
  } else {
    await savePositionStops(p.pid, p.which, v);
  }
}
async function savePositionStops(pid, which, v) {
  const p = (S.st?.positions || []).find((x) => x.id === pid);
  if (!p) return;
  const body = { sl: p.sl, tp: p.tp };
  body[which] = v;
  try {
    await post(`/api/position/${pid}/stops`, body);
    toast(`${which === 'sl' ? 'Stop loss' : 'Take profit'} set at ${fmtPx(v)}`, 'good');
    await refreshState();
  } catch (e) { toastErr(e); drawLines(); }
}

// --- drag lines
function initDrag(el) {
  let drag = null;
  const near = (y) => {
    for (const l of priceLines) {
      if (!l.drag) continue;
      const c = candleSeries.priceToCoordinate(l.price);
      if (c != null && Math.abs(c - y) < 7) return l;
    }
    return null;
  };
  el.addEventListener('mousemove', (e) => {
    const y = e.clientY - el.getBoundingClientRect().top;
    if (drag) {
      const p = candleSeries.coordinateToPrice(y);
      if (p != null && p > 0) {
        drag.price = p;
        drag.line.applyOptions({ price: p });
      }
      e.preventDefault();
      return;
    }
    el.style.cursor = near(y) ? 'ns-resize' : '';
  });
  el.addEventListener('mousedown', (e) => {
    const y = e.clientY - el.getBoundingClientRect().top;
    const l = near(y);
    if (!l) return;
    drag = l;
    chart.applyOptions({ handleScroll: false, handleScale: false });
    e.preventDefault(); e.stopPropagation();
  }, true);
  window.addEventListener('mouseup', async () => {
    if (!drag) return;
    const d = drag; drag = null;
    chart.applyOptions({ handleScroll: true, handleScale: true });
    const v = +d.price.toFixed(precisionFor(d.price).precision);
    if (d.drag.form) {
      $(d.drag.form === 'sl' ? '#slInput' : '#tpInput').value = v;
      updateOrderSummary();
    } else {
      await savePositionStops(d.drag.pid, d.drag.which, v);
    }
  });
}

// ============================================================ state polling
async function refreshState(hello = false) {
  try {
    const st = await api('/api/state' + (hello ? '?hello=1' : ''));
    S.st = st;
    renderStrip();
    renderWarnings();
    renderTickerPositions();
    if (S.tab === 'trade') drawLines();
    if (S.tab === 'portfolio') renderPortfolio();
    updateOrderSummary();
    for (const t of st.toasts || []) toast((t.type === 'level' ? '&#11088; ' : '&#127942; ') + esc(t.text), 'gold', 6000);
    if (st.away) showAway(st.away);
    return st;
  } catch (e) {
    if (!S._errShown) { toastErr(e); S._errShown = true; setTimeout(() => (S._errShown = false), 20000); }
  }
}

function animateNum(el, val, fmt) {
  const old = parseFloat(el.dataset.v || 'NaN');
  el.dataset.v = val;
  el.textContent = fmt(val);
  if (!isNaN(old) && Math.abs(old - val) > 1e-9) {
    el.classList.remove('flash-up', 'flash-down');
    void el.offsetWidth;
    el.classList.add(val > old ? 'flash-up' : 'flash-down');
  }
}

function renderStrip() {
  const st = S.st; if (!st) return;
  const a = st.account;
  animateNum($('#kNet'), a.networth, (v) => fmtUsd(v));
  const start = 10000;
  const chg = $('#kNetChg');
  chg.textContent = `${fmtUsd(a.networth - start, { sign: true })} all time`;
  chg.className = 'num ' + cls(a.networth - start);
  $('#kCash').textContent = fmtUsd(a.cash);
  const up = $('#kUpnl'); up.textContent = fmtUsd(a.upnl, { sign: true }); up.className = 'num ' + cls(a.upnl);
  const re = $('#kReal'); re.textContent = fmtUsd(a.realized, { sign: true }); re.className = 'num ' + cls(a.realized);
  $('#kDiv').textContent = fmtUsd(a.dividends);
  const p = st.profile;
  $('#lvBadge').textContent = p.level; $('#lvNum').textContent = p.level;
  $('#lvXp').textContent = `${p.xp - p.level_xp} / ${p.next_xp - p.level_xp} XP`;
  $('#lvBar').style.width = Math.min(100, ((p.xp - p.level_xp) / (p.next_xp - p.level_xp)) * 100) + '%';
  $('#lvNext').textContent = p.next_rank ? `${p.rank} \u00b7 next rank: ${p.next_rank} (level ${p.next_rank_level})` : `${p.rank} \u00b7 top rank!`;
  // market pill
  const mp = $('#marketPill');
  if (S.mode === 'turbo') {
    const d = new Date(st.turbo.sim_ts * 1000);
    mp.className = 'pill turbo';
    mp.textContent = `Turbo ${st.turbo.speed}x · ${d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' })} ${d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })}`;
  } else {
    if (st.market.hours_on === false) { mp.className = 'pill open'; mp.textContent = 'Market hours off · trade any time'; }
    else {
      mp.className = 'pill ' + (st.market.open ? 'open' : 'closed');
      mp.textContent = st.market.open ? 'NYSE open' : 'Market closed · opens ' + (st.market.next_open_text || '');
    }
  }
}

function renderWarnings() {
  const w = S.st?.warnings || [];
  const bar = $('#warnBar');
  if (!w.length) { bar.classList.add('hidden'); return; }
  const worst = w.reduce((a, b) => (a.risk > b.risk ? a : b));
  bar.className = 'warn-bar' + (worst.risk >= 0.5 ? ' danger' : '');
  const label = worst.risk >= 0.5 ? 'MARGIN CALL' : 'Margin warning';
  bar.innerHTML = `<span>&#9888;&#65039; <b>${label}:</b> ${esc(worst.side.toUpperCase())} ${esc(worst.symbol)} has lost ${Math.round(worst.risk * 100)}% of its margin.
    Liquidation at ${fmtPx(worst.liq_price)} (90%).${w.length > 1 ? ` +${w.length - 1} more at risk.` : ''}</span>
    <button class="mini" data-addmargin="${worst.id}">Add margin</button><button class="mini red" data-closepos="${worst.id}">Close</button>`;
}

// ============================================================ tabs & mode
function setTab(t) {
  S.tab = t;
  $$('#tabs button').forEach((b) => b.classList.toggle('active', b.dataset.tab === t));
  $$('.view').forEach((v) => v.classList.toggle('active', v.id === 'view-' + t));
  if (t === 'trade') { setTimeout(() => chart && chart.timeScale(), 0); loadChart(false); }
  if (t === 'portfolio') { renderPortfolio(); loadEvents(); }
  if (t === 'bots') loadBots();
  if (t === 'funds') loadFunds();
  if (t === 'learn') loadLearn();
  if (t === 'stats') loadStats();
  if (t === 'settings') loadSettings();
}
$$('#tabs button').forEach((b) => b.addEventListener('click', () => setTab(b.dataset.tab)));

function setMode(m, initial) {
  S.mode = m;
  localStorage.setItem('tt_mode', m);
  document.body.classList.toggle('turbo', m === 'turbo');
  $$('#modeToggle button').forEach((b) => b.classList.toggle('active', b.dataset.mode === m));
  if (!initial) {
    toast(m === 'turbo' ? '&#9889; TURBO mode: real starting prices, simulated fast markets.' : '&#128994; LIVE mode: real prices in real time.', '', 3000);
    refreshState(true).then(() => { selectAsset(S.sel, false); });
    setTab(S.tab);
  }
  restartTimers();
}
$$('#modeToggle button').forEach((b) => b.addEventListener('click', () => { if (b.dataset.mode !== S.mode) setMode(b.dataset.mode); }));

// ============================================================ search / list
let searchTimer = null, quoteTimer = null;
$('#searchInput').addEventListener('input', (e) => {
  S.query = e.target.value;
  clearTimeout(searchTimer);
  searchTimer = setTimeout(doSearch, 110);
});
$('#searchInput').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && S.results[0]) { selectAsset(S.results[0].id); e.target.blur(); }
  if (e.key === 'Escape') { e.target.value = ''; S.query = ''; doSearch(); e.target.blur(); }
});
$$('#chips button').forEach((b) => b.addEventListener('click', () => {
  S.filter = b.dataset.f;
  $$('#chips button').forEach((x) => x.classList.toggle('active', x === b));
  $('#sectorSel').classList.toggle('hidden', S.filter !== 'sector');
  doSearch();
}));
$('#sectorSel').addEventListener('change', (e) => { S.sector = e.target.value; doSearch(); });

async function doSearch() {
  try {
    const q = encodeURIComponent(S.query);
    const js = await api(`/api/search?q=${q}&filter=${S.filter}&sector=${encodeURIComponent(S.sector)}`);
    S.results = js.results;
    S.watch = new Set(js.watch);
    renderList();
    fetchListQuotes();
  } catch (e) { toastErr(e); }
}

function renderList() {
  const el = $('#resultList');
  if (!S.results.length) {
    el.innerHTML = `<div class="empty">${S.filter === 'watchlist' ? 'Your watchlist is empty. Click &#9734; on any ticker.' : 'No matches.'}</div>`;
    return;
  }
  el.innerHTML = S.results.map((r) => `
    <div class="row-item ${r.id === S.sel ? 'sel' : ''}" data-id="${esc(r.id)}">
      ${avatarHtml(r)}
      <div class="meta"><div class="sym">${esc(r.symbol.toUpperCase())}${r.kind === 'etf' ? '<span class="tag">ETF</span>' : r.kind === 'crypto' ? '<span class="tag">' + (r.rank ? '#' + r.rank : 'coin') + '</span>' : ''}</div>
        <div class="nm">${esc(r.name)}</div></div>
      <div class="px num" data-q="${esc(r.id)}">${r.change_pct != null ? `<small class="${cls(r.change_pct)}">${fmtPct(r.change_pct)}</small>` : ''}</div>
      <button class="star ${S.watch.has(r.id) ? 'on' : ''}" data-star="${esc(r.id)}" title="Watchlist">${S.watch.has(r.id) ? '&#9733;' : '&#9734;'}</button>
    </div>`).join('');
}
$('#resultList').addEventListener('click', (e) => {
  const st = e.target.closest('[data-star]');
  if (st) { e.stopPropagation(); toggleWatch(st.dataset.star); return; }
  const row = e.target.closest('.row-item');
  if (row) selectAsset(row.dataset.id);
});

async function fetchListQuotes() {
  const ids = S.results.slice(0, 40).map((r) => r.id);
  if (!ids.length) return;
  try {
    const qs = await api('/api/quotes?ids=' + encodeURIComponent(ids.join(',')));
    for (const [id, q] of Object.entries(qs)) {
      const el = $(`[data-q="${CSS.escape(id)}"]`);
      if (el) el.innerHTML = `${fmtPx(q.price)}<small class="${cls(q.change_pct)}">${fmtPct(q.change_pct)}</small>`;
    }
  } catch (e) { /* silent */ }
}

async function toggleWatch(id) {
  try {
    const js = await post('/api/watchlist', { id });
    js.watch ? S.watch.add(id) : S.watch.delete(id);
    renderList();
    if (id === S.sel) renderStar();
    toast(js.watch ? 'Added to watchlist' : 'Removed from watchlist', '', 1800);
  } catch (e) { toastErr(e); }
}
function renderStar() {
  const on = S.watch.has(S.sel);
  $('#aStar').innerHTML = on ? '&#9733;' : '&#9734;';
  $('#aStar').classList.toggle('on', on);
}
$('#aStar').addEventListener('click', () => toggleWatch(S.sel));

// ============================================================ asset selection
async function selectAsset(id, fit = true) {
  S.sel = id;
  localStorage.setItem('tt_sel', id);
  $$('#resultList .row-item').forEach((r) => r.classList.toggle('sel', r.dataset.id === id));
  S.lastPrice = null;
  loadChart(fit);
  renderTickerPositions();
  await refreshAsset();
  if ($('#newsDrawer').classList.contains('open') && S.newsTab === 'news') loadNews();
}

async function refreshAsset() {
  try {
    const js = await api('/api/asset/' + encodeURIComponent(S.sel));
    if (js.asset.id !== S.sel) return;
    S.asset = js.asset; S.quote = js.quote;
    if (js.watch) S.watch.add(S.sel); else S.watch.delete(S.sel);
    const a = js.asset;
    $('#aLogo').outerHTML = avatarHtml(a, true).replace('<div class="avatar', '<div id="aLogo" class="avatar');
    $('#aSym').textContent = a.symbol.toUpperCase();
    $('#aName').textContent = a.name + (a.exchange ? ' · ' + a.exchange : '') + (a.sector && a.sector !== 'ETF' && a.sector !== 'Crypto' ? ' · ' + a.sector : '');
    $('#aKind').textContent = a.kind === 'crypto' ? 'Crypto' : a.kind === 'etf' ? 'ETF' : 'Stock';
    renderStar();
    const q = js.quote;
    const pe = $('#aPrice');
    if (q) {
      if (S.lastPrice != null && q.price !== S.lastPrice) {
        pe.classList.remove('flash-up', 'flash-down'); void pe.offsetWidth;
        pe.classList.add(q.price > S.lastPrice ? 'flash-up' : 'flash-down');
      }
      S.lastPrice = q.price;
      pe.textContent = fmtPx(q.price);
      const c = $('#aChg');
      c.textContent = `${fmtPct(q.change_pct)} ${a.kind === 'crypto' ? '24h' : 'today'}`;
      c.className = 'num ' + cls(q.change_pct);
      livePriceIntoChart(q.price);
      S.quoteAt = Date.now();
      const lt = $('#aLive');
      lt.classList.toggle('stale', !!q.stale);
      $('#aLiveTxt').textContent = q.stale ? 'reconnecting... showing last price' : 'live \u00b7 ' + new Date().toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    } else {
      pe.textContent = '-';
      $('#aChg').textContent = 'No price available';
      $('#aChg').className = 'num muted';
    }
    const m = $('#aMarket');
    if (S.mode === 'turbo') { m.className = 'pill small turbo'; m.textContent = 'Simulated · always open'; }
    else if (a.kind === 'crypto') { m.className = 'pill small open'; m.textContent = 'Crypto · 24/7'; }
    else if (S.st && S.st.market.hours_on === false) { m.className = 'pill small open'; m.textContent = 'Market hours off · trades any time'; }
    else { m.className = 'pill small ' + (js.market_open ? 'open' : 'closed'); m.textContent = js.market_open ? 'Market open' : 'Market closed · orders queue for the open'; }
    updateOrderSummary();
  } catch (e) { /* silent; next poll */ }
}

$$('#tfSeg button').forEach((b) => {
  b.classList.toggle('active', b.dataset.tf === S.tf);
  b.addEventListener('click', () => {
    S.tf = b.dataset.tf; localStorage.setItem('tt_tf', S.tf);
    $$('#tfSeg button').forEach((x) => x.classList.toggle('active', x === b));
    loadChart(true);
  });
});

// ============================================================ positions (ticker + cards)
function posCard(p, showSym) {
  const tags = [`<span class="tag ${p.side}">${p.side}</span>`];
  if (p.leverage > 1) tags.push(`<span class="tag lev">${(+p.leverage).toFixed(p.leverage % 1 ? 1 : 0)}x</span>`);
  if (p.bot_id) tags.push('<span class="tag bot">bot</span>');
  if (p.fund_id) tags.push('<span class="tag fund">fund</span>');
  const managed = p.bot_id || p.fund_id;
  return `<div class="pos" data-pid="${p.id}">
    <div><div class="v">${showSym ? `<a href="#" data-goto="${esc(p.asset_id)}">${esc(p.symbol)}</a> ` : ''}${tags.join(' ')}</div>
      <div class="lbl">${fmtQty(p.qty)} @ ${fmtPx(p.entry)}</div></div>
    <div><div class="lbl">Price</div><div class="v num">${fmtPx(p.price)}</div></div>
    <div><div class="lbl">Value</div><div class="v num">${fmtUsd(p.value)}</div></div>
    <div><div class="lbl">P&amp;L</div><div class="v num ${cls(p.upnl)}">${fmtUsd(p.upnl, { sign: true })} <small>${fmtPct(p.pnl_pct)}</small></div></div>
    <div><div class="lbl">SL / TP${p.liq_price ? ' / Liq' : ''}</div><div class="v num small">${p.sl ? `<span class="down">${fmtPx(p.sl)}</span>` : '-'} / ${p.tp ? `<span class="up">${fmtPx(p.tp)}</span>` : '-'}${p.liq_price ? ` / <span style="color:var(--orange)">${fmtPx(p.liq_price)}</span>` : ''}</div>
      ${p.liq_price ? `<div class="risk" title="Margin used up"><i style="width:${Math.min(100, p.risk / 0.9 * 100)}%"></i></div>` : ''}</div>
    <div class="acts">${managed ? `<span class="muted small">${p.bot_id ? 'managed by bot' : 'in fund'}</span>` : `
      <button class="mini" data-edit="${p.id}" title="Edit stops (E)">SL/TP</button>
      ${p.liq_price ? `<button class="mini" data-addmargin="${p.id}">+Margin</button>` : ''}
      <button class="mini" data-closefrac="${p.id}" data-f="0.25">25%</button>
      <button class="mini" data-closefrac="${p.id}" data-f="0.5">50%</button>
      <button class="mini red" data-closepos="${p.id}">Close</button>`}</div>
  </div>`;
}

function renderTickerPositions() {
  const el = $('#tickerPositions');
  const ps = (S.st?.positions || []).filter((p) => p.asset_id === S.sel);
  const os = (S.st?.orders || []).filter((o) => o.asset_id === S.sel);
  if (!ps.length && !os.length) { el.innerHTML = '<div class="muted small" style="padding:8px">No open position on this ticker. Size a trade on the right and press LONG or SHORT.</div>'; return; }
  el.innerHTML = ps.map((p) => posCard(p, false)).join('') + os.map((o) => `<div class="pos"><div><span class="tag">queued</span> ${o.kind === 'open' ? esc(o.side.toUpperCase()) + ' ' + fmtUsd(o.amount_usd) : 'close ' + Math.round((o.fraction || 1) * 100) + '%'}</div><div class="muted small" style="grid-column: span 4">${esc(o.note || '')} · fills at the next market open</div><div class="acts"><button class="mini red" data-cancel="${o.id}">Cancel</button></div></div>`).join('');
}

document.addEventListener('click', async (e) => {
  const t = e.target.closest('[data-closepos],[data-closefrac],[data-edit],[data-addmargin],[data-cancel],[data-goto]');
  if (!t) return;
  e.preventDefault();
  try {
    if (t.dataset.goto) { setTab('trade'); selectAsset(t.dataset.goto); return; }
    if (t.dataset.closepos) await closePos(+t.dataset.closepos, 1);
    if (t.dataset.closefrac) await closePos(+t.dataset.closefrac, +t.dataset.f);
    if (t.dataset.edit) editStops(+t.dataset.edit);
    if (t.dataset.addmargin) addMarginDlg(+t.dataset.addmargin);
    if (t.dataset.cancel) { await post(`/api/order/${t.dataset.cancel}/cancel`); toast('Order cancelled'); await refreshState(); }
  } catch (err) { toastErr(err); }
});

async function closePos(pid, frac) {
  const js = await post(`/api/position/${pid}/close`, { fraction: frac });
  if (js.queued) toast(esc(js.message), '');
  else {
    toast(`Closed ${frac < 1 ? Math.round(frac * 100) + '% ' : ''}at ${fmtPx(js.price)}: <b class="${cls(js.pnl)}">${fmtUsd(js.pnl, { sign: true })}</b>${js.xp ? ` · +${js.xp} XP` : ''}`, js.pnl >= 0 ? 'good' : 'bad');
    if (js.level_up) celebrateLevel(js);
  }
  await refreshState();
}

function editStops(pid) {
  const p = (S.st?.positions || []).find((x) => x.id === pid);
  if (!p) return;
  const m = modal(`<h2>Edit stops · ${esc(p.symbol)} ${esc(p.side.toUpperCase())}</h2>
    <p class="muted">Entry ${fmtPx(p.entry)} · now ${fmtPx(p.price)}. ${p.side === 'long' ? 'Stop loss goes below the price, take profit above.' : 'Stop loss goes above the price, take profit below.'}</p>
    <div class="field two"><div><label>Stop loss</label><input id="eSl" type="number" step="any" class="num" value="${p.sl ?? ''}"></div>
    <div><label>Take profit</label><input id="eTp" type="number" step="any" class="num" value="${p.tp ?? ''}"></div></div>
    <div class="row gap mt"><button class="btn ghost" id="ePlaceSl">Click chart for SL</button><button class="btn ghost" id="ePlaceTp">Click chart for TP</button></div>
    <div class="acts"><button class="btn ghost" id="eCancel">Cancel</button><button class="btn primary" id="eSave">Save</button></div>`);
  $('#eSl', m).focus();
  $('#eCancel', m).onclick = closeModal;
  const go = (which) => { closeModal(); if (S.tab !== 'trade') setTab('trade'); if (S.sel !== p.asset_id) selectAsset(p.asset_id); setPlacing({ pid, which }); };
  $('#ePlaceSl', m).onclick = () => go('sl');
  $('#ePlaceTp', m).onclick = () => go('tp');
  $('#eSave', m).onclick = async () => {
    try {
      await post(`/api/position/${pid}/stops`, { sl: $('#eSl').value || null, tp: $('#eTp').value || null });
      closeModal(); toast('Stops updated', 'good'); refreshState();
    } catch (err) { toastErr(err); }
  };
}

function addMarginDlg(pid) {
  const p = (S.st?.positions || []).find((x) => x.id === pid);
  if (!p) return;
  const m = modal(`<h2>Add margin · ${esc(p.symbol)}</h2>
    <p class="muted">Adding cash to this position lowers its effective leverage and pushes the liquidation price further away. Current margin ${fmtUsd(p.margin)}, liquidation at ${fmtPx(p.liq_price)}.</p>
    <div class="field"><label>Amount (cash available ${fmtUsd(S.st.account.cash)})</label><input id="mAmt" type="number" class="num" value="${Math.min(S.st.account.cash, p.margin * 0.5).toFixed(2)}"></div>
    <div class="acts"><button class="btn ghost" id="mCancel">Cancel</button><button class="btn primary" id="mOk">Add margin</button></div>`);
  $('#mCancel', m).onclick = closeModal;
  $('#mOk', m).onclick = async () => {
    try { await post(`/api/position/${pid}/margin`, { amount: +$('#mAmt').value }); closeModal(); toast('Margin added', 'good'); refreshState(); }
    catch (err) { toastErr(err); }
  };
}

// ============================================================ order panel
$$('#unitSeg button').forEach((b) => b.addEventListener('click', () => {
  S.unit = b.dataset.u;
  $$('#unitSeg button').forEach((x) => x.classList.toggle('active', x === b));
  const px = S.quote?.price;
  const v = parseFloat($('#amtInput').value);
  if (px && !isNaN(v)) $('#amtInput').value = S.unit === 'qty' ? +(v / px).toPrecision(6) : (v * px).toFixed(2);
  $('#amtLabel').textContent = S.unit === 'usd' ? 'Amount (USD)' : (S.asset?.kind === 'crypto' ? 'Coins' : 'Shares');
  updateOrderSummary();
}));
$$('#levSeg button').forEach((b) => b.addEventListener('click', () => {
  S.lev = +b.dataset.l;
  $$('#levSeg button').forEach((x) => x.classList.toggle('active', x === b));
  updateOrderSummary();
}));
$$('#pctRow button').forEach((b) => b.addEventListener('click', () => {
  const cash = S.st?.account.cash || 0;
  const usd = Math.floor(cash * (+b.dataset.p / 100) * S.lev * 100) / 100;
  const px = S.quote?.price;
  $('#amtInput').value = S.unit === 'usd' ? usd.toFixed(2) : (px ? +(usd / px).toPrecision(6) : '');
  updateOrderSummary();
}));
['#amtInput', '#slInput', '#tpInput'].forEach((s) => $(s).addEventListener('input', () => { updateOrderSummary(); drawLines(); }));
$$('.link-btn[data-place]').forEach((b) => b.addEventListener('click', () => setPlacing(S.placing === b.dataset.place ? null : b.dataset.place)));

function orderUsd() {
  const v = parseFloat($('#amtInput').value);
  if (isNaN(v) || v <= 0) return 0;
  return S.unit === 'usd' ? v : v * (S.quote?.price || 0);
}
function updateOrderSummary() {
  const px = S.quote?.price;
  const usd = orderUsd();
  const margin = usd / S.lev;
  const sl = parseFloat($('#slInput').value), tp = parseFloat($('#tpInput').value);
  $('#slPct').textContent = px && !isNaN(sl) ? fmtPct((sl / px - 1) * 100) : '';
  $('#tpPct').textContent = px && !isNaN(tp) ? fmtPct((tp / px - 1) * 100) : '';
  const rows = [['Price', px ? fmtPx(px) : '-'], ['Position size', fmtUsd(usd)], ['Margin (cash used)', fmtUsd(margin)],
    [S.asset?.kind === 'crypto' ? 'Coins' : 'Shares', px && usd ? fmtQty(usd / px) : '-']];
  if (px && usd && !isNaN(sl)) rows.push(['Risk if long hits SL', fmtUsd(Math.abs(px - sl) / px * usd)]);
  if (px && usd && S.lev > 1) {
    const d = 0.9 / S.lev;
    rows.push(['Liquidation (long / short)', `${fmtPx(px * (1 - d))} / ${fmtPx(px * (1 + d))}`]);
  }
  rows.push(['Cash available', fmtUsd(S.st?.account.cash || 0)]);
  $('#orderSummary').innerHTML = rows.map(([a, b]) => `<span>${a}</span><span>${b}</span>`).join('');
  const closed = S.asset && S.asset.kind !== 'crypto' && S.st && S.st.market.hours_on !== false && !S.st.market.open;
  $('#orderNote').textContent = closed ? `Market closed: your order will be queued and fill at the next open (${S.st.market.next_open_text}).` :
    (S.lev > 1 ? `${S.lev}x leverage multiplies gains AND losses. A ${(90 / S.lev).toFixed(0)}% move against you liquidates the trade.` : '');
}

async function placeOrder(side) {
  const usd = orderUsd();
  if (!usd) { toast('Enter a trade size first', 'bad'); $('#amtInput').focus(); return; }
  const body = { id: S.sel, side, leverage: S.lev, sl: $('#slInput').value || null, tp: $('#tpInput').value || null };
  if (S.unit === 'usd') body.amount = usd; else body.qty = parseFloat($('#amtInput').value);
  const btn = side === 'long' ? $('#longBtn') : $('#shortBtn');
  btn.disabled = true;
  try {
    const js = await post('/api/order', body);
    if (js.queued) toast('&#9203; ' + esc(js.message));
    else toast(`${side === 'long' ? '&#128200; LONG' : '&#128201; SHORT'} ${esc(S.asset?.symbol?.toUpperCase() || '')} ${fmtUsd(usd)} at ${fmtPx(js.price)}`, 'good');
    $('#slInput').value = ''; $('#tpInput').value = '';
    await refreshState();
  } catch (e) { toastErr(e); }
  btn.disabled = false;
}
$('#longBtn').addEventListener('click', () => placeOrder('long'));
$('#shortBtn').addEventListener('click', () => placeOrder('short'));

// ============================================================ away / level up
function showAway(a) {
  const li = (arr) => arr.length ? `<ul class="away-list">${arr.slice(0, 8).map((x) => `<li>${esc(x)}</li>`).join('')}${arr.length > 8 ? `<li>+${arr.length - 8} more</li>` : ''}</ul>` : '';
  const chg = a.change;
  modal(`<h2>While you were away</h2>
    <p class="muted">You were gone for ${dur(a.seconds)}. ${S.mode === 'live' ? 'Ticker Tycoon replayed the real market' : 'The Turbo market kept simulating'} and settled everything.</p>
    <div class="away-grid">
      <div class="card"><label>Net worth change</label><div class="v num ${cls(chg)}">${chg == null ? '-' : fmtUsd(chg, { sign: true })}</div></div>
      <div class="card"><label>Dividends paid</label><div class="v num">${fmtUsd(a.dividends)}</div><small>${a.dividend_count} payments</small></div>
      <div class="card"><label>Stops / take-profits</label><div class="v num">${a.stops.length} / ${a.take_profits.length}</div><small class="${cls(a.realized)}">${fmtUsd(a.realized, { sign: true })} realized</small></div>
      <div class="card"><label>Bot trades</label><div class="v num">${a.bot_trades}</div></div>
    </div>
    ${a.liquidations.length ? '<b class="down">Liquidations</b>' + li(a.liquidations) : ''}
    ${a.margin_calls.length ? '<b style="color:var(--orange)">Margin calls</b>' + li(a.margin_calls) : ''}
    ${a.stops.length ? '<b>Stop losses hit</b>' + li(a.stops) : ''}
    ${a.take_profits.length ? '<b>Take profits hit</b>' + li(a.take_profits) : ''}
    ${a.filled_orders.length ? '<b>Queued orders filled</b>' + li(a.filled_orders) : ''}
    ${a.bot_log.length ? '<b>Latest bot trades</b>' + li(a.bot_log) : ''}
    <div class="acts"><button class="btn primary" onclick="closeModal()">Nice, let's trade</button></div>`);
}
function celebrateLevel(js) {
  toast(`&#11088; <b>Level ${js.level}!</b>${js.unlocked?.length ? ' Unlocked: ' + esc(js.unlocked.join(', ')) : ''}`, 'gold', 7000);
}

// ============================================================ news & codes drawer
function openNews(tab) {
  $('#newsDrawer').classList.add('open');
  if (tab) S.newsTab = tab;
  $$('#newsSeg button').forEach((b) => b.classList.toggle('active', b.dataset.n === S.newsTab));
  S.newsTab === 'news' ? loadNews() : renderCodes();
}
const closeNews = () => $('#newsDrawer').classList.remove('open');
$('#newsBtn').addEventListener('click', () => ($('#newsDrawer').classList.contains('open') ? closeNews() : openNews()));
$$('[data-close="newsDrawer"]').forEach((b) => b.addEventListener('click', closeNews));
$$('#newsSeg button').forEach((b) => b.addEventListener('click', () => openNews(b.dataset.n)));

async function loadNews() {
  const body = $('#newsBody');
  body.innerHTML = '<div class="empty">Loading headlines...</div>';
  try {
    const js = await api('/api/news?id=' + encodeURIComponent(S.sel));
    const sym = S.asset ? S.asset.symbol.toUpperCase() : '';
    body.innerHTML = `<div class="muted small" style="margin-bottom:8px">Real headlines for <b>${esc(sym)}</b>${js.source ? ' via ' + esc(js.source) : ''}${S.mode === 'turbo' ? ' (Turbo prices are simulated; news is real)' : ''}</div>
      ${js.note ? `<div class="note">${esc(js.note)}</div>` : ''}
      ${(js.items || []).map((n) => `<a class="news-item" href="${esc(n.url)}" target="_blank" rel="noopener">
        ${n.image ? `<img src="${esc(n.image)}" alt="" loading="lazy" onerror="this.remove()">` : ''}
        <div><div class="nt">${esc(n.title)}</div><div class="ns">${esc(n.source)}${n.ts ? ' · ' + ago(n.ts) : ''}</div></div></a>`).join('') || '<div class="empty">No headlines right now.</div>'}`;
  } catch (e) { body.innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
}
function renderCodes() {
  $('#newsBody').innerHTML = `<h3>Redeem a code</h3>
    <p class="muted small">Codes give bonus cash or XP. Cash codes work once per mode (Live and Turbo).</p>
    <div class="row gap"><input id="codeInput" placeholder="Enter code" style="text-transform:uppercase"><button class="btn primary" id="codeGo">Redeem</button></div>
    <div class="note mt">Psst: new players can redeem <b>WELCOME</b> for +$5,000. Finishing lessons? Try <b>LEARNER</b>.</div>`;
  const go = async () => {
    try { const js = await post('/api/redeem', { code: $('#codeInput').value }); toast('&#127881; ' + esc(js.label), 'good'); $('#codeInput').value = ''; refreshState(); }
    catch (e) { toastErr(e); }
  };
  $('#codeGo').onclick = go;
  $('#codeInput').addEventListener('keydown', (e) => { if (e.key === 'Enter') go(); e.stopPropagation(); });
  $('#codeInput').focus();
}

// ============================================================ PORTFOLIO
const PALETTE = ['#0a84ff', '#30d158', '#ff9f0a', '#bf5af2', '#ff375f', '#64d2ff', '#ffd60a', '#ac8e68', '#5e5ce6', '#66d4cf'];
function renderPortfolio() {
  const st = S.st; if (!st) return;
  const a = st.account;
  const cards = [['Net worth', fmtUsd(a.networth), `${fmtUsd(a.networth - 10000, { sign: true })} since start`],
    ['Cash', fmtUsd(a.cash), 'available to trade'], ['In positions', fmtUsd(a.positions_value), `${st.positions.length} open`],
    ['Open P&L', `<span class="${cls(a.upnl)}">${fmtUsd(a.upnl, { sign: true })}</span>`, 'unrealized'],
    ['Realized P&L', `<span class="${cls(a.realized)}">${fmtUsd(a.realized, { sign: true })}</span>`, 'closed trades'],
    ['Dividends', fmtUsd(a.dividends), 'received'], ['Bots cash', fmtUsd(a.bot_cash), 'held by bots']];
  $('#pfCards').innerHTML = cards.map(([l, v, s]) => `<div class="card"><label>${l}</label><div class="v num">${v}</div><small>${s}</small></div>`).join('');
  const groups = {};
  for (const p of st.positions) groups[p.symbol] = (groups[p.symbol] || 0) + Math.max(0, p.value);
  groups.Cash = a.cash + a.bot_cash + a.reserved;
  const tot = Object.values(groups).reduce((x, y) => x + y, 0) || 1;
  const ent = Object.entries(groups).sort((x, y) => y[1] - x[1]);
  $('#pfAlloc').innerHTML = ent.map(([k, v], i) => `<i style="width:${(v / tot) * 100}%;background:${k === 'Cash' ? '#3a3b46' : PALETTE[i % PALETTE.length]}" title="${esc(k)}"></i>`).join('');
  $('#pfLegend').innerHTML = ent.map(([k, v], i) => `<span style="--c:${k === 'Cash' ? '#3a3b46' : PALETTE[i % PALETTE.length]}">${esc(k)} ${((v / tot) * 100).toFixed(1)}%</span>`).join('');
  $('#pfPositions').innerHTML = st.positions.length ? st.positions.map((p) => posCard(p, true)).join('') : '<div class="empty">No open positions yet.</div>';
  $('#pfOrders').innerHTML = st.orders.length ? `<table class="tbl"><tr><th>Ticker</th><th>Type</th><th>Size</th><th>Placed</th><th></th></tr>${st.orders.map((o) => `<tr><td>${esc(o.symbol)}</td><td>${o.kind === 'open' ? esc(o.side.toUpperCase()) : 'Close ' + Math.round((o.fraction || 1) * 100) + '%'}</td><td>${o.amount_usd ? fmtUsd(o.amount_usd) : '-'}</td><td>${ago(o.created_ts)}</td><td><button class="mini red" data-cancel="${o.id}">Cancel</button></td></tr>`).join('')}</table>` : '<div class="muted small">No queued orders.</div>';
}
const EV_ICON = { open: '&#128200;', close: '&#9989;', stop: '&#128721;', tp: '&#127919;', liquidation: '&#128165;', margin_call: '&#9888;&#65039;', dividend: '&#128176;', bot_trade: '&#129302;', code: '&#127873;', level_up: '&#11088;', fund: '&#129530;', order_queued: '&#9203;', order_filled: '&#9989;', starter: '&#127873;', start: '&#128075;', bot: '&#129302;', margin: '&#128737;' };
function eventsHtml(evs) {
  return evs.length ? evs.map((e) => `<div class="ev"><time>${new Date(e.ts * 1000).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })}</time><span class="ico">${EV_ICON[e.type] || '&#8226;'}</span><span>${esc(e.text)}</span></div>`).join('') : '<div class="empty">Nothing yet.</div>';
}
async function loadEvents() {
  try { $('#pfEvents').innerHTML = eventsHtml(await api('/api/events')); } catch (e) { /* */ }
}

// ============================================================ ticker picker (bots/funds)
function picker(input, onPick, filter) {
  const wrap = input.parentElement;
  wrap.classList.add('picker');
  let list = null, t = null;
  input.addEventListener('input', () => {
    clearTimeout(t);
    t = setTimeout(async () => {
      const q = input.value.trim();
      if (!q) { list && list.remove(); list = null; return; }
      const js = await api(`/api/search?q=${encodeURIComponent(q)}&filter=${filter || 'all'}&limit=8`);
      list && list.remove();
      list = document.createElement('div');
      list.className = 'picker-list';
      list.innerHTML = js.results.map((r) => `<div data-id="${esc(r.id)}" data-sym="${esc(r.symbol.toUpperCase())}"><b>${esc(r.symbol.toUpperCase())}</b><span class="muted">${esc(r.name)}</span></div>`).join('') || '<div class="muted">No matches</div>';
      wrap.appendChild(list);
      list.addEventListener('mousedown', (e) => {
        const d = e.target.closest('[data-id]');
        if (!d) return;
        e.preventDefault();
        onPick({ id: d.dataset.id, symbol: d.dataset.sym });
        list.remove(); list = null;
      });
    }, 120);
  });
  input.addEventListener('blur', () => setTimeout(() => { list && list.remove(); list = null; }, 150));
  input.addEventListener('keydown', (e) => e.stopPropagation());
}

// ============================================================ BOTS
const BOT_COLORS = { dca: '#0a84ff', ma_cross: '#30d158', rsi_dip: '#ff9f0a', momentum: '#ff375f', grid: '#bf5af2' };
const BOT_ICON = { dca: 'D', ma_cross: 'MA', rsi_dip: 'R', momentum: 'M', grid: '#' };
function lockedCard() { return ''; }  // nothing is locked
async function loadBots() {
  try {
    const js = await api('/api/bots');
    $('#botsLocked').innerHTML = lockedCard('bots');
    $('#botShop').innerHTML = Object.entries(js.types).map(([k, t]) => `<div class="shop-item">
      <div class="row gap"><div class="ico" style="background:${BOT_COLORS[k]}22;color:${BOT_COLORS[k]}">${BOT_ICON[k]}</div><div><b>${esc(t.name)}</b><div class="price num">${fmtUsd(t.price)}</div></div></div>
      <p>${esc(t.desc)}</p><button class="btn primary" data-buybot="${k}">Buy &amp; configure</button></div>`).join('');
    $('#myBots').innerHTML = js.bots.length ? js.bots.map((b) => `<div class="bot-card">
      <div><b>${esc(b.name)}</b> <span class="tag ${b.active ? 'long' : ''}">${b.active ? 'running' : 'paused'}</span><div class="muted small">${esc(b.symbols.join(', '))}</div></div>
      <div><div class="lbl muted small">Value</div><b class="num">${fmtUsd(b.value)}</b></div>
      <div><div class="lbl muted small">P&amp;L</div><b class="num ${cls(b.pnl)}">${fmtUsd(b.pnl, { sign: true })}</b></div>
      <div><div class="lbl muted small">Cash / budget</div><span class="num">${fmtUsd(b.cash)} / ${fmtUsd(b.budget)}</span></div>
      <div><div class="lbl muted small">Trades</div><span class="num">${b.trades}</span></div>
      <div class="row gap"><button class="mini" data-togglebot="${b.id}">${b.active ? 'Pause' : 'Resume'}</button><button class="mini red" data-sellbot="${b.id}">Sell</button></div></div>`).join('') : '<div class="empty">No bots yet.</div>';
    $('#botLog').innerHTML = eventsHtml(js.log);
  } catch (e) { toastErr(e); }
}
document.addEventListener('click', async (e) => {
  const b = e.target.closest('[data-buybot],[data-togglebot],[data-sellbot]');
  if (!b) return;
  try {
    if (b.dataset.buybot) return buyBotDlg(b.dataset.buybot);
    if (b.dataset.togglebot) { await post(`/api/bots/${b.dataset.togglebot}/toggle`); loadBots(); }
    if (b.dataset.sellbot) {
      const js = await post(`/api/bots/${b.dataset.sellbot}/sell`);
      toast(`Bot sold. ${fmtUsd(js.refund)} returned to cash.`, 'good'); loadBots(); refreshState();
    }
  } catch (err) { toastErr(err); }
});
function buyBotDlg(kind) {
  const crypto = kind === 'grid';
  const tickers = [];
  const m = modal(`<h2>Configure ${esc({ dca: 'DCA Bot', ma_cross: 'MA Crossover Bot', rsi_dip: 'RSI Dip Buyer', momentum: 'Momentum Bot', grid: 'Grid Bot' }[kind])}</h2>
    <div class="field"><label>Tickers (up to 5${crypto ? ', crypto only' : ''})</label><div><input id="bTick" placeholder="${crypto ? 'bitcoin, eth...' : 'AAPL, BTC, SPY...'}"></div><div class="chipset" id="bChips"></div></div>
    <div class="field"><label>Budget ($) moved from your cash into the bot</label><input id="bBudget" type="number" class="num" value="1000"></div>
    ${kind === 'dca' ? `<div class="field two"><div><label>Buy amount per ticker ($)</label><input id="bAmt" type="number" class="num" value="100"></div>
      <div><label>Every</label><select id="bInt"><option value="3600">1 hour</option><option value="14400">4 hours</option><option value="86400" selected>1 day</option><option value="604800">1 week</option></select></div></div>` : ''}
    ${kind === 'grid' ? `<div class="field two"><div><label>Grid levels</label><input id="bLev" type="number" class="num" value="6" min="2" max="20"></div><div><label>Spacing (%)</label><input id="bSp" type="number" class="num" value="1" step="0.1"></div></div>` : ''}
    <p class="muted small">${S.mode === 'turbo' ? 'Turbo intervals use simulated time.' : 'Bots keep trading while the game is closed.'}</p>
    <div class="acts"><button class="btn ghost" id="bCancel">Cancel</button><button class="btn primary" id="bOk">Buy bot</button></div>`);
  const renderChips = () => { $('#bChips').innerHTML = tickers.map((t, i) => `<span>${esc(t.symbol)}<button data-rm="${i}">&times;</button></span>`).join(''); };
  picker($('#bTick', m), (t) => { if (tickers.length < 5 && !tickers.find((x) => x.id === t.id)) tickers.push(t); $('#bTick').value = ''; renderChips(); }, crypto ? 'crypto' : 'all');
  $('#bChips').addEventListener('click', (e) => { const r = e.target.closest('[data-rm]'); if (r) { tickers.splice(+r.dataset.rm, 1); renderChips(); } });
  if (S.sel && (!crypto || S.sel.startsWith('C:'))) { tickers.push({ id: S.sel, symbol: S.asset?.symbol?.toUpperCase() || S.sel.slice(2) }); renderChips(); }
  $('#bCancel').onclick = closeModal;
  $('#bOk').onclick = async () => {
    const params = {};
    if (kind === 'dca') { params.amount = +$('#bAmt').value; params.interval = +$('#bInt').value; }
    if (kind === 'grid') { params.levels = +$('#bLev').value; params.spacing = +$('#bSp').value / 100; }
    try {
      await post('/api/bots', { kind, tickers: tickers.map((t) => t.id), budget: +$('#bBudget').value, params });
      closeModal(); toast('&#129302; Bot is live!', 'good'); loadBots(); refreshState();
    } catch (err) { toastErr(err); }
  };
}

// ============================================================ FUNDS
function fundRow(id, sym, w) {
  const d = document.createElement('div');
  d.className = 'fund-row';
  d.innerHTML = `<div><input class="fTick" placeholder="Ticker (e.g. SPY, NVDA, BTC)" value="${esc(sym || '')}" data-id="${esc(id || '')}"></div><input class="fW num" type="number" placeholder="Weight %" value="${w || ''}"><button class="mini red" title="Remove">&times;</button>`;
  d.querySelector('button').onclick = () => d.remove();
  const inp = d.querySelector('.fTick');
  picker(inp, (t) => { inp.value = t.symbol; inp.dataset.id = t.id; });
  $('#fundRows').appendChild(d);
}
$('#fundAddRow').addEventListener('click', () => fundRow());
$('#fundCreate').addEventListener('click', async () => {
  const holdings = $$('#fundRows .fund-row').map((r) => ({ id: r.querySelector('.fTick').dataset.id, weight: +r.querySelector('.fW').value })).filter((h) => h.id && h.weight > 0);
  try {
    await post('/api/funds', { name: $('#fundName').value || 'My Fund', holdings, invest: +$('#fundInvest').value || 0 });
    toast('&#129530; Fund created', 'good');
    $('#fundRows').innerHTML = ''; fundRow('S:SPY', 'SPY', 50); fundRow('S:QQQ', 'QQQ', 30); fundRow('C:bitcoin', 'BTC', 20);
    loadFunds(); refreshState();
  } catch (e) { toastErr(e); }
});
fundRow('S:SPY', 'SPY', 50); fundRow('S:QQQ', 'QQQ', 30); fundRow('C:bitcoin', 'BTC', 20);
async function loadFunds() {
  try {
    const js = await api('/api/funds');
    $('#fundsLocked').innerHTML = lockedCard('funds');
    $('#myFunds').innerHTML = js.funds.length ? js.funds.map((f) => `<div class="fund-card">
      <div><b>${esc(f.name)}</b><div class="muted small">${f.holdings.map((h) => esc(h.symbol) + ' ' + Math.round(h.weight * 100) + '%').join(' · ')}</div></div>
      <div><div class="muted small">Value</div><b class="num">${fmtUsd(f.value)}</b></div>
      <div><div class="muted small">Invested</div><span class="num">${fmtUsd(f.invested)}</span></div>
      <div><div class="muted small">Return</div><b class="num ${cls(f.pnl)}">${fmtUsd(f.pnl, { sign: true })} <small>${fmtPct(f.pnl_pct)}</small></b></div>
      <div></div>
      <div class="row gap"><button class="mini" data-fundinv="${f.id}">Invest</button><button class="mini" data-fundwd="${f.id}" data-f="0.5">Withdraw 50%</button><button class="mini red" data-fundwd="${f.id}" data-f="1">Withdraw all</button></div></div>`).join('') : '<div class="empty">No funds yet.</div>';
  } catch (e) { toastErr(e); }
}
document.addEventListener('click', async (e) => {
  const b = e.target.closest('[data-fundinv],[data-fundwd]');
  if (!b) return;
  try {
    if (b.dataset.fundinv) {
      const m = modal(`<h2>Invest in fund</h2><div class="field"><label>Amount ($), cash ${fmtUsd(S.st.account.cash)}</label><input id="fiAmt" type="number" class="num" value="500"></div><div class="acts"><button class="btn ghost" onclick="closeModal()">Cancel</button><button class="btn primary" id="fiOk">Invest</button></div>`);
      $('#fiOk', m).onclick = async () => { try { const js = await post(`/api/funds/${b.dataset.fundinv}/invest`, { amount: +$('#fiAmt').value }); closeModal(); toast(js.queued_legs ? `Invested. ${js.queued_legs} stock leg(s) queued for the market open.` : 'Invested', 'good'); loadFunds(); refreshState(); } catch (err) { toastErr(err); } };
    }
    if (b.dataset.fundwd) { const js = await post(`/api/funds/${b.dataset.fundwd}/withdraw`, { fraction: +b.dataset.f }); toast(`Withdrawn (${fmtUsd(js.pnl, { sign: true })} P&L)`, js.pnl >= 0 ? 'good' : 'bad'); loadFunds(); refreshState(); }
  } catch (err) { toastErr(err); }
});

// ============================================================ LEARN
async function loadLearn() {
  try {
    S.lessons = await api('/api/learn');
    renderLessonList();
    if (!S.lessonSel) {
      const next = S.lessons.lessons.find((l) => !l.completed);
      openLesson(next ? next.id : S.lessons.lessons[0].id);
    }
  } catch (e) { toastErr(e); }
}
function renderLessonList() {
  const L = S.lessons;
  $('#lpText').textContent = `${L.done} / ${L.total} complete`;
  $('#lpBar').style.width = (L.done / L.total) * 100 + '%';
  $('#lessonList').innerHTML = L.lessons.map((l, i) => `<li data-lesson="${l.id}" class="${l.completed ? 'done' : ''} ${S.lessonSel === l.id ? 'sel' : ''}">
    <span class="n">${l.completed ? '&#10003;' : i + 1}</span><span class="t">${esc(l.title)}</span>${l.quiz_passed ? '<span class="q" title="Quiz passed">&#9733;</span>' : ''}</li>`).join('');
}
$('#lessonList').addEventListener('click', (e) => { const li = e.target.closest('[data-lesson]'); if (li) openLesson(li.dataset.lesson); });

function videoCards(vs) {
  return `<div class="videos">${vs.map((v) => v.id ? `<div class="video" data-vid="${esc(v.id)}" data-title="${esc(v.title)}">
      <div class="th" style="background-image:url('${esc(v.thumb || '')}')"><div class="play">&#9654;</div></div>
      <div class="info"><div class="ti">${esc(v.title)}</div><div class="ch">${esc(v.channel)}${v.views ? ' · ' + Intl.NumberFormat('en', { notation: 'compact' }).format(v.views) + ' views' : ''}${v.like_ratio ? ' · ' + (v.like_ratio * 100).toFixed(1) + '% likes' : ''}${v.published ? ' · ' + v.published.slice(0, 4) : ''}</div></div></div>`
    : `<a class="video" href="${esc(v.url)}" target="_blank" rel="noopener"><div class="th fb"><div class="play">&#8599;</div></div><div class="info"><div class="ti">${esc(v.title)}</div><div class="ch">${esc(v.channel)} · opens YouTube</div></div></a>`).join('')}</div>`;
}
document.addEventListener('click', (e) => {
  const v = e.target.closest('[data-vid]');
  if (!v) return;
  modal(`<h2 style="font-size:17px">${esc(v.dataset.title)}</h2><iframe class="player" src="https://www.youtube-nocookie.com/embed/${encodeURIComponent(v.dataset.vid)}?autoplay=1&rel=0" allow="autoplay; encrypted-media; picture-in-picture" allowfullscreen></iframe>
    <div class="acts"><a class="btn ghost" href="https://www.youtube.com/watch?v=${encodeURIComponent(v.dataset.vid)}" target="_blank" rel="noopener">Open on YouTube</a><button class="btn primary" onclick="closeModal()">Done</button></div>`, true);
});

async function openLesson(id) {
  S.lessonSel = id;
  renderLessonList();
  const l = S.lessons.lessons.find((x) => x.id === id);
  const idx = S.lessons.lessons.indexOf(l);
  const main = $('#learnMain');
  main.innerHTML = `<div class="muted small">Lesson ${idx + 1} of ${S.lessons.total}</div><h2>${esc(l.title)}</h2>
    ${l.body.map((p) => `<p>${esc(p)}</p>`).join('')}
    <div class="row gap"><button class="btn primary" id="tryBtn">&#9654; Try it in the game</button>
      <button class="btn ghost" id="doneBtn" ${l.completed ? 'disabled' : ''}>${l.completed ? '&#10003; Completed' : 'Mark complete (+20 XP)'}</button></div>
    <h3 class="mt">Watch</h3><div id="lessonVideos"><div class="muted">Finding the best videos...</div></div>
    <h3 class="mt">Quiz ${l.quiz_passed ? '<span class="tag long">passed</span>' : '<span class="muted small">(pass all 3 for +50 XP)</span>'}</h3>
    <div class="quiz" id="quiz">${l.quiz.map((q, i) => `<div class="qq"><b>${i + 1}. ${esc(q.q)}</b>${q.options.map((o, j) => `<label><input type="radio" name="q${i}" value="${j}"> ${esc(o)}</label>`).join('')}<div class="why muted small"></div></div>`).join('')}
      <button class="btn primary" id="quizBtn">Check answers</button> <span id="quizRes" class="muted"></span></div>
    <div class="muted small mt">Educational content only. Not financial advice.</div>`;
  $('#tryBtn').onclick = () => tryPractice(l.practice);
  $('#doneBtn').onclick = async () => {
    try { const js = await post('/api/learn/complete', { lesson: id }); if (js.xp) toast(`+${js.xp} XP: lesson complete`, 'gold'); if (js.level_up) celebrateLevel(js); await loadLearn(); openLesson(id); refreshState(); }
    catch (e) { toastErr(e); }
  };
  $('#quizBtn').onclick = async () => {
    const answers = l.quiz.map((q, i) => { const c = $(`input[name=q${i}]:checked`); return c ? +c.value : -1; });
    try {
      const js = await post('/api/learn/quiz', { lesson: id, answers });
      js.results.forEach((r, i) => {
        const qq = $$('#quiz .qq')[i];
        $$('label', qq).forEach((lab, j) => lab.classList.toggle('right', j === r.answer));
        const chosen = answers[i];
        if (!r.correct && chosen >= 0) $$('label', qq)[chosen].classList.add('wrong');
        $('.why', qq).textContent = (r.correct ? 'Correct. ' : 'Not quite. ') + r.why;
      });
      $('#quizRes').innerHTML = js.passed ? `<b class="up">${js.score}/${js.total} passed!</b>${js.xp ? ` +${js.xp} XP` : ''}` : `<b class="down">${js.score}/${js.total}</b> Try again to earn the XP.`;
      if (js.xp) toast(`&#127891; Quiz passed: +${js.xp} XP`, 'gold');
      if (js.level_up) celebrateLevel(js);
      if (js.passed) { S.lessons = await api('/api/learn'); renderLessonList(); refreshState(); }
    } catch (e) { toastErr(e); }
  };
  try {
    const vs = await api('/api/learn/videos?lesson=' + id);
    if (S.lessonSel !== id) return;
    $('#lessonVideos').innerHTML = (vs.note ? `<div class="note">${esc(vs.note)}</div>` : '') + videoCards(vs.videos);
  } catch (e) { $('#lessonVideos').innerHTML = `<div class="note">${esc(e.message)}</div>`; }
}

function tryPractice(p) {
  if (p.tab) { setTab(p.tab); toast(esc(p.hint)); return; }
  setTab('trade');
  if (p.tf) { S.tf = p.tf; $$('#tfSeg button').forEach((x) => x.classList.toggle('active', x.dataset.tf === p.tf)); }
  selectAsset(p.symbol).then(() => {
    S.unit = 'usd'; $$('#unitSeg button').forEach((x) => x.classList.toggle('active', x.dataset.u === 'usd'));
    $('#amtLabel').textContent = 'Amount (USD)';
    if (p.amount) $('#amtInput').value = p.amount;
    const lev = p.leverage || 1;
    S.lev = lev; $$('#levSeg button').forEach((x) => x.classList.toggle('active', +x.dataset.l === lev));
    const px = S.quote?.price;
    const dir = p.side === 'short' ? -1 : 1;
    const pf = precisionFor(px);
    $('#slInput').value = px && p.sl_pct ? (px * (1 - dir * p.sl_pct / 100)).toFixed(pf.precision) : '';
    $('#tpInput').value = px && p.tp_pct ? (px * (1 + dir * p.tp_pct / 100)).toFixed(pf.precision) : '';
    updateOrderSummary(); drawLines();
    const btn = p.side === 'short' ? $('#shortBtn') : $('#longBtn');
    btn.animate([{ transform: 'scale(1)' }, { transform: 'scale(1.06)' }, { transform: 'scale(1)' }], { duration: 700, iterations: 2 });
    toast('&#127891; Practice: ' + esc(p.hint), '', 8000);
  });
}

$('#howToBtn').addEventListener('click', () => {
  S.lessonSel = null; renderLessonList();
  $('#learnMain').innerHTML = `<h2>How to play Ticker Tycoon</h2><p class="muted">Every button and mechanic in the game.</p>
    <dl class="howto">${S.lessons.how_to_play.map(([t, d]) => `<dt>${esc(t)}</dt><dd>${esc(d)}</dd>`).join('')}</dl>`;
});
async function ytSearch() {
  const q = $('#ytQuery').value.trim();
  if (!q) return;
  S.lessonSel = null; renderLessonList();
  const main = $('#learnMain');
  main.innerHTML = `<h2>Videos: ${esc(q)}</h2><div class="muted">Searching YouTube...</div>`;
  try {
    const js = await api('/api/learn/search?q=' + encodeURIComponent(q));
    main.innerHTML = `<h2>Videos: ${esc(q)}</h2>${js.note ? `<div class="note">${esc(js.note)}</div>` : ''}${videoCards(js.videos)}`;
  } catch (e) { main.innerHTML = `<div class="note">${esc(e.message)}</div>`; }
}
$('#ytGo').addEventListener('click', ytSearch);
$('#ytQuery').addEventListener('keydown', (e) => { if (e.key === 'Enter') ytSearch(); e.stopPropagation(); });

// ============================================================ STATS
let nwChart = null, nwSeries = null;
async function loadStats() {
  try {
    const js = await api('/api/stats');
    if (!nwChart && window.LightweightCharts) {
      nwChart = LightweightCharts.createChart($('#nwChart'), {
        layout: { background: { type: 'solid', color: 'transparent' }, textColor: '#8e8e98' },
        grid: { vertLines: { visible: false }, horzLines: { color: 'rgba(255,255,255,0.04)' } },
        rightPriceScale: { borderVisible: false }, timeScale: { borderVisible: false, timeVisible: true }, autoSize: true,
        handleScroll: false, handleScale: false, localization: { locale: 'en-US' },
      });
      nwSeries = nwChart.addAreaSeries({ lineColor: '#30d158', topColor: 'rgba(48,209,88,.3)', bottomColor: 'rgba(48,209,88,0)', lineWidth: 2 });
    }
    if (nwSeries) {
      const seen = new Set();
      const pts = js.networth.map((p) => ({ time: Math.floor(p.ts) + TZ, value: p.value })).filter((p) => !seen.has(p.time) && seen.add(p.time));
      const up = pts.length && pts[pts.length - 1].value >= (pts[0]?.value || 0);
      nwSeries.applyOptions(up ? { lineColor: '#30d158', topColor: 'rgba(48,209,88,.3)', bottomColor: 'rgba(48,209,88,0)' } : { lineColor: '#ff453a', topColor: 'rgba(255,69,58,.3)', bottomColor: 'rgba(255,69,58,0)' });
      nwSeries.setData(pts);
      nwChart.timeScale().fitContent();
      $('#nwRange').textContent = pts.length > 1 ? `since ${new Date((pts[0].time - TZ) * 1000).toLocaleDateString()}` : 'Snapshots every few minutes';
    }
    const b = js.best, w = js.worst;
    const cards = [['Win rate', js.trades ? js.win_rate.toFixed(0) + '%' : '-', `${js.wins} wins / ${js.trades} trades`],
      ['Realized P&L', `<span class="${cls(js.realized)}">${fmtUsd(js.realized, { sign: true })}</span>`, `${js.bot_trades} bot trades included`],
      ['Best trade', b ? `<span class="up">${fmtUsd(b.pnl, { sign: true })}</span>` : '-', b ? `${esc(b.symbol)} ${esc(b.side)} ${fmtPct(b.pnl_pct)}` : 'no trades yet'],
      ['Worst trade', w ? `<span class="${cls(w.pnl)}">${fmtUsd(w.pnl, { sign: true })}</span>` : '-', w ? `${esc(w.symbol)} ${esc(w.side)} ${fmtPct(w.pnl_pct)}` : ''],
      ['Total dividends', fmtUsd(js.dividends), 'real ex-dates'],
      ['Stops / TPs hit', `${js.by_reason.stop || 0} / ${js.by_reason.tp || 0}`, `${js.by_reason.liquidation || 0} liquidations`],
      ['Level', js.profile.level, `${js.profile.xp} XP total`]];
    $('#stCards').innerHTML = cards.map(([l, v, s]) => `<div class="card"><label>${l}</label><div class="v num">${v}</div><small>${s}</small></div>`).join('');
    loadLeaderboard();
    $('#achGrid').innerHTML = js.achievements.map((a) => `<div class="ach ${a.ts ? 'got' : ''}" title="${esc(a.desc)}"><b>${a.ts ? '&#127942; ' : ''}${esc(a.name)}</b><small>${esc(a.desc)}</small></div>`).join('');
    $('#stTrades').innerHTML = js.recent.length ? `<table class="tbl"><tr><th>Ticker</th><th>Side</th><th>Exit</th><th>P&amp;L</th><th>Why</th><th>XP</th></tr>${js.recent.map((t) => `<tr class="click" data-goto="${esc(t.asset_id)}"><td>${esc(t.symbol)}</td><td>${esc(t.side)}${t.leverage > 1 ? ' ' + t.leverage + 'x' : ''}</td><td>${fmtPx(t.exit)}</td><td class="${cls(t.pnl)}">${fmtUsd(t.pnl, { sign: true })}</td><td>${esc(t.reason)}</td><td>${t.xp || ''}</td></tr>`).join('')}</table>` : '<div class="empty">Close a trade to see it here.</div>';
  } catch (e) { toastErr(e); }
}

async function loadLeaderboard() {
  try {
    const lb = await api('/api/leaderboard');
    $('#lbInfo').textContent = `${lb.players} player${lb.players === 1 ? '' : 's'}${lb.me ? ' · you are #' + lb.me.rank : ''}`;
    $('#leaderboard').innerHTML = `<div class="lb-row muted small"><span>#</span><span>Player</span><span>Net worth</span><span>XP</span></div>` +
      lb.top.map((r) => `<div class="lb-row ${r.me ? 'me' : ''}"><span class="medal">${r.rank}</span><span>${esc(r.username)}${r.me ? ' (you)' : ''}</span>
        <span class="${cls(r.networth - 10000)}">${fmtUsd(r.networth)}</span><span>${r.xp}</span></div>`).join('');
  } catch (e) { /* */ }
}

// ============================================================ SETTINGS
const SHORTCUTS = [['T', 'Start the tutorial'], ['B', 'Go LONG with the current order'], ['S', 'Go SHORT with the current order'], ['C', 'Close your position on this ticker'], ['E', 'Edit stop loss / take profit'], ['N', 'Open news & codes'], ['/', 'Focus search'], ['1-7', 'Switch tabs'], ['?', 'Show this help'], ['Esc', 'Back / close']];
async function loadSettings() {
  $('#kbList').innerHTML = SHORTCUTS.map(([k, d]) => `<kbd>${k}</kbd><span class="muted">${d}</span>`).join('');
  try {
    const js = await api('/api/settings');
    $('#hoursToggle').checked = !!js.market_hours;
    $('#setUser').textContent = js.username;
    $('#adminBox').classList.toggle('hidden', !js.is_admin);
    if (!js.is_admin) return;
    const k = js.keys;
    const keyRow = (name, ok, what, how) => `<div class="status-row"><span><b>${name}</b><div class="muted small">${what}</div></span><span class="${ok ? 'ok' : 'bad'}">${ok ? '&#10003; connected' : 'add key for ' + how}</span></div>`;
    $('#keyStatus').innerHTML = keyRow('FINNHUB_KEY', k.finnhub, 'Company & crypto news, quote fallback, logos (optional, free)', 'better news') +
      keyRow('YOUTUBE_API_KEY', k.youtube, 'Learn tab finds and embeds videos automatically (free)', 'auto videos') +
      keyRow('COINGECKO_KEY', k.coingecko, 'Higher crypto rate limits (optional, free demo key)', 'faster crypto') +
      '<p class="muted small">Put keys in the <code>.env</code> file in the TickerTycoon folder on the server, then restart it. See README for how to get them.</p>';
    const name = { nasdaq: 'NASDAQ Trader symbol lists', coingecko: 'CoinGecko (crypto)', yahoo: 'Yahoo Finance', finnhub: 'Finnhub', youtube: 'YouTube Data API' };
    $('#srcStatus').innerHTML = Object.entries(js.sources || {}).map(([k2, v]) => `<div class="status-row"><span>${name[k2]}</span><span class="${v.ok === false ? 'bad' : v.ok ? 'ok' : 'muted'}">${v.ok === null ? 'not used yet' : v.ok ? '&#10003; ok' : esc(v.error || 'error')}</span></div>`).join('') +
      `<div class="status-row"><span>Symbols loaded</span><span class="num">${js.symbols.stocks.toLocaleString()} stocks · ${js.symbols.etfs.toLocaleString()} ETFs · ${js.symbols.crypto.toLocaleString()} coins</span></div>`;
  } catch (e) { toastErr(e); }
}
$('#hoursToggle').addEventListener('change', async (e) => {
  try {
    await post('/api/settings', { market_hours: e.target.checked });
    toast(e.target.checked ? 'Market hours on: stocks follow real NYSE hours.' : 'Market hours off: stocks trade any time at their latest real price.', 'good');
    refreshState(); refreshAsset();
  } catch (err) { toastErr(err); e.target.checked = !e.target.checked; }
});
$('#refreshSyms').addEventListener('click', async () => { await post('/api/settings', { refresh_symbols: true }); toast('Refreshing symbol lists in the background...'); setTimeout(loadSettings, 15000); });
$('#resetBtn').addEventListener('click', async () => {
  try { await post('/api/reset', { confirm: $('#resetConfirm').value.trim() }); $('#resetConfirm').value = ''; toast('Your account was reset to $10,000', 'good'); refreshState(); }
  catch (e) { toastErr(e); }
});
$('#pwBtn').addEventListener('click', async () => {
  try { await post('/api/auth/password', { old: $('#pwOld').value, new: $('#pwNew').value }); $('#pwOld').value = ''; $('#pwNew').value = ''; toast('Password changed', 'good'); }
  catch (e) { toastErr(e); }
});
['#pwOld', '#pwNew'].forEach((s) => $(s).addEventListener('keydown', (e) => e.stopPropagation()));
async function logout() { try { await post('/api/auth/logout'); } catch (e) { /* */ } location.href = '/login'; }
$('#logoutBtn2').addEventListener('click', logout);
$('#userBtn').addEventListener('click', () => {
  const u = S.user || {};
  const p = S.st ? S.st.profile : null;
  modal(`<h2>${esc(u.username || '')}</h2><p class="muted">${p ? esc(p.rank) + ' · level ' + p.level + ' · ' + p.xp + ' XP' : ''}</p>
    <p class="muted small">Your account works from any computer: just log in at the same address.</p>
    <div class="acts"><button class="btn ghost" onclick="closeModal(); setTab('settings')">Settings</button><button class="btn danger" onclick="logout()">Log out</button></div>`);
});
$('#resetConfirm').addEventListener('keydown', (e) => e.stopPropagation());

// ============================================================ help & keyboard
function showHelp() {
  modal(`<h2>Keyboard shortcuts</h2><div class="kb-list">${SHORTCUTS.map(([k, d]) => `<kbd>${k}</kbd><span>${d}</span>`).join('')}</div>
    <p class="muted small mt">New here? Take the guided tutorial: it walks you through every part of the screen.</p>
    <div class="acts"><button class="btn ghost" onclick="closeModal(); startTour()">Start tutorial</button><button class="btn primary" onclick="closeModal()">Got it</button></div>`);
}
$('#helpBtn').addEventListener('click', showHelp);

document.addEventListener('keydown', (e) => {
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  if (TOUR.active) {
    if (e.key === 'Escape') endTour();
    else if (e.key === 'ArrowRight' || e.key === 'Enter') tourStep(TOUR.i + 1);
    else if (e.key === 'ArrowLeft') tourStep(TOUR.i - 1);
    e.preventDefault();
    return;
  }
  const typing = ['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement?.tagName);
  if (e.key === 'Escape') {
    if (S.placing) { setPlacing(null); return; }
    if (S.modalOpen) { closeModal(); return; }
    if ($('#newsDrawer').classList.contains('open')) { closeNews(); return; }
    if (typing) { document.activeElement.blur(); return; }
    if (S.tab !== 'trade') setTab('trade');
    return;
  }
  if (typing || S.modalOpen) return;
  const k = e.key.toLowerCase();
  if (k === 'b') { if (S.tab !== 'trade') setTab('trade'); placeOrder('long'); }
  else if (k === 's') { if (S.tab !== 'trade') setTab('trade'); placeOrder('short'); }
  else if (k === 'c') {
    const p = (S.st?.positions || []).find((x) => x.asset_id === S.sel && !x.bot_id && !x.fund_id);
    if (p) closePos(p.id, 1).catch(toastErr); else toast('No open position on this ticker');
  } else if (k === 'e') {
    const p = (S.st?.positions || []).find((x) => x.asset_id === S.sel && !x.bot_id && !x.fund_id);
    if (p) editStops(p.id); else { setTab('trade'); $('#slInput').focus(); }
  } else if (k === 'n') { $('#newsDrawer').classList.contains('open') ? closeNews() : openNews(); }
  else if (k === '/') { e.preventDefault(); setTab('trade'); $('#searchInput').focus(); $('#searchInput').select(); }
  else if (k === '?') showHelp();
  else if (k === 't') startTour();
  else if (/^[1-7]$/.test(k)) setTab(['trade', 'portfolio', 'bots', 'funds', 'learn', 'stats', 'settings'][+k - 1]);
});


// ============================================================ TUTORIAL (guided tour)
const TOUR_STEPS = [
  { title: 'Welcome to Ticker Tycoon', text: 'Your account is yours alone: log in from any computer to pick up where you left off. This tour shows you every part of the screen in about 2 minutes. Everything here uses <b>real</b> stocks, ETFs and crypto with <b>real live prices</b>, but the money is virtual. Use the arrow keys or the buttons below. Press Esc to exit anytime.' },
  { sel: '.strip .kpi.big', tab: 'trade', title: 'Net worth', text: 'Everything you own: cash plus the current value of all your positions and bots. You start with <b>$10,000</b> plus a free <b>$1,000 of SPY</b> (an ETF of the 500 biggest US companies).' },
  { sel: '.strip', tab: 'trade', title: 'Your account at a glance', text: '<b>Cash</b> is what you can trade with. <b>Open P&amp;L</b> is your profit or loss on trades that are still open. <b>Realized</b> is profit or loss you locked in by closing trades. <b>Dividends</b> is cash paid to you by companies you own.' },
  { sel: '.level-card', tab: 'trade', title: 'XP, level and rank', text: 'Closing trades, finishing lessons and passing quizzes earns XP. Profitable trades earn the most, and cutting a loss early with a stop loss earns a bonus. Nothing is locked: XP only raises your rank, from Rookie to Ticker Tycoon.' },
  { sel: '#marketPill', tab: 'trade', title: 'Market hours', text: 'US stocks trade 9:30am to 4:00pm New York time on weekdays, like a real broker. If you place a stock order while the market is closed, it waits in line and fills at the next open. <b>Crypto trades 24/7.</b>' },
  { sel: '.search', tab: 'trade', title: 'Search any stock or coin', text: 'Type a ticker or a name: <b>AAPL</b>, <b>nvidia</b>, <b>btc</b>, <b>doge</b>. It searches about 13,000 US stocks and ETFs and 20,000 coins, and handles typos. Shortcut: press <kbd>/</kbd>.' },
  { sel: '#chips', tab: 'trade', title: 'Filters', text: 'Show only Stocks, ETFs or Crypto, browse by Sector, see today’s <b>Top movers</b> (biggest gainers and losers), or your <b>Watchlist</b>.' },
  { sel: '#resultList', tab: 'trade', title: 'Pick a ticker', text: 'Click any row to open its chart. Each row shows the live price and today’s % change (green = up, red = down). Click the <b>star</b> to add it to your watchlist.' },
  { sel: '.asset-head', tab: 'trade', title: 'Live price', text: 'The big number is the current price, refreshed every few seconds. Below it is the % change today (24h for crypto). The green dot shows the time of the last live update.' },
  { sel: '#tfSeg', tab: 'trade', title: 'Timeframes', text: 'Each candle can be 1 minute, 5 minutes, 1 hour, 1 day or 1 week. Use 1m/5m to watch moves happening right now, 1D/1W to see the bigger trend.' },
  { sel: '.chart-wrap', tab: 'trade', title: 'The chart', text: '<b>Green candle</b> = price closed higher than it opened, <b>red</b> = lower. The thin wicks show the highest and lowest prices. The bars at the bottom are volume (how much traded). <b>Scroll</b> to zoom, <b>drag</b> to move back in time. Your entry, stop loss and take profit appear here as lines you can drag.' },
  { sel: '#unitSeg', tab: 'trade', title: 'Trade size', text: 'Choose to size your trade in <b>dollars</b> (e.g. $1,000 of Apple) or in <b>shares/coins</b> (e.g. 5 shares). Fractional shares are allowed, like on real apps.' },
  { sel: '#pctRow', tab: 'trade', title: 'Quick sizing', text: 'Fill in 25%, 50%, 75% or 100% of your cash in one click. Tip: real traders rarely risk more than 1-2% of their account on one trade.' },
  { sel: '#levSeg', tab: 'trade', title: 'Leverage', text: 'Leverage lets you control a bigger position than your cash. <b>5x</b> turns a 2% move into 10%, in BOTH directions. Lose 90% of your margin and the trade is <b>liquidated</b> (closed automatically). Start with 1x.' },
  { sel: '.order-panel .field.two', tab: 'trade', title: 'Stop loss & take profit', text: 'A <b>stop loss</b> closes your trade automatically if the price moves against you. A <b>take profit</b> closes it when you hit your target. Type a price, or click <b>Set on chart</b> and click the chart. They keep working while the game is closed.' },
  { sel: '#orderSummary', tab: 'trade', title: 'Order summary', text: 'Check before you trade: position size, how much cash it uses (margin), how many shares you get, how much you lose if your stop is hit, and the liquidation price if you use leverage.' },
  { sel: '.side-btns', tab: 'trade', title: 'LONG or SHORT', text: '<b class="up">LONG</b> = you profit if the price goes <b>up</b> (shortcut <kbd>B</kbd>). <b class="down">SHORT</b> = you profit if the price goes <b>down</b> (shortcut <kbd>S</kbd>). Shorts are riskier: a price can rise without limit.' },
  { sel: '#tickerPositions', tab: 'trade', title: 'Your open trades', text: 'Your positions on this ticker appear here with live profit/loss. <b>SL/TP</b> edits your stops (or press <kbd>E</kbd>), <b>25%/50%</b> closes part of the trade, <b>Close</b> exits completely (<kbd>C</kbd>).' },
  { sel: '#newsBtn', tab: 'trade', title: 'News & codes', text: 'Real headlines for the ticker you’re viewing, plus the <b>Codes</b> tab. Try the code <b>WELCOME</b> for +$5,000. Shortcut: <kbd>N</kbd>.' },
  { sel: '#tabs button[data-tab="portfolio"]', tab: 'portfolio', title: 'Portfolio', text: 'All your positions in one place, how your money is split between assets (allocation), orders waiting for the market open, and a log of everything that happened.' },
  { sel: '#tabs button[data-tab="bots"]', tab: 'bots', title: 'Trading bots', text: 'Buy a bot with game cash, give it a budget and up to 5 tickers, and it trades on its own using a real strategy: DCA, moving-average crossover, RSI dip buying, momentum or a crypto grid. Bots keep trading while you’re away.' },
  { sel: '#tabs button[data-tab="funds"]', tab: 'funds', title: 'Funds', text: 'Build your own ETF-style basket (for example 50% SPY, 30% QQQ, 20% BTC) and invest in all of it with one click.' },
  { sel: '#tabs button[data-tab="learn"]', tab: 'learn', title: 'Learn', text: '11 short lessons from "What is a stock" to "Common beginner mistakes", each with videos, a <b>Try it in the game</b> button that sets up a practice trade, and a quiz for bonus XP.' },
  { sel: '#tabs button[data-tab="stats"]', tab: 'stats', title: 'Stats', text: 'Your net worth over time, win rate, best and worst trade, total dividends and achievements.' },
  { sel: '#tabs button[data-tab="settings"]', tab: 'settings', title: 'Settings', text: 'Switch <b>market hours</b> off to trade stocks any time (not just 9:30am-4:00pm New York time), change your password, or reset your account. The <b>Stats</b> tab also has a <b>leaderboard</b> of every player.' },
  { sel: '#tourBtn', tab: 'trade', title: 'You’re ready!', text: 'Try your first trade: search <b>AAPL</b> or <b>BTC</b>, type $500, set a stop loss 5% below the price and press LONG. You can replay this tour anytime with this <b>Tutorial</b> button (or press <kbd>T</kbd>). Press <kbd>?</kbd> for all keyboard shortcuts.' },
];
const TOUR = { active: false, i: 0 };

function startTour() {
  if (S.modalOpen) closeModal();
  closeNews();
  TOUR.active = true;
  $('#tour').classList.remove('hidden');
  tourStep(0);
}
function endTour() {
  TOUR.active = false;
  $('#tour').classList.add('hidden');
  localStorage.setItem('tt_tour_done', '1');
  if (S.tab !== 'trade') setTab('trade');
}
function tourStep(i) {
  if (i < 0) return;
  if (i >= TOUR_STEPS.length) { endTour(); toast('&#127891; Tutorial done. Happy trading!', 'gold'); return; }
  TOUR.i = i;
  const st = TOUR_STEPS[i];
  if (st.tab && S.tab !== st.tab) setTab(st.tab);
  const card = $('#tourCard');
  card.innerHTML = `<div class="tour-count">${i + 1} / ${TOUR_STEPS.length}</div><h3>${st.title}</h3><p>${st.text}</p>
    <div class="tour-bar"><i style="width:${((i + 1) / TOUR_STEPS.length) * 100}%"></i></div>
    <div class="row between"><button class="btn ghost small-btn" onclick="endTour()">Skip tour</button>
      <div class="row gap">${i ? '<button class="btn ghost small-btn" onclick="tourStep(TOUR.i - 1)">Back</button>' : ''}
      <button class="btn primary small-btn" onclick="tourStep(TOUR.i + 1)">${i === TOUR_STEPS.length - 1 ? 'Finish' : 'Next'}</button></div></div>`;
  requestAnimationFrame(() => placeTour(st));
}
function placeTour(st) {
  const spot = $('#tourSpot'), card = $('#tourCard');
  const el = st.sel ? $(st.sel) : null;
  const W = window.innerWidth, H = window.innerHeight, pad = 8;
  if (!el) {
    spot.style.cssText = `left:${W / 2}px;top:${H / 2}px;width:0;height:0`;
    card.style.left = Math.max(16, W / 2 - card.offsetWidth / 2) + 'px';
    card.style.top = Math.max(16, H / 2 - card.offsetHeight / 2) + 'px';
    return;
  }
  el.scrollIntoView({ block: 'nearest', inline: 'nearest' });
  const r = el.getBoundingClientRect();
  spot.style.cssText = `left:${r.left - pad}px;top:${r.top - pad}px;width:${r.width + pad * 2}px;height:${r.height + pad * 2}px`;
  const cw = card.offsetWidth, ch = card.offsetHeight;
  let top = r.bottom + 16, left = r.left + r.width / 2 - cw / 2;
  if (top + ch > H - 16) top = r.top - ch - 16;            // not enough room below: go above
  if (top < 16) {                                            // big element: put the card beside it
    top = Math.min(Math.max(16, r.top), H - ch - 16);
    left = r.right + 16 + cw < W ? r.right + 16 : r.left - cw - 16;
  }
  card.style.left = Math.min(Math.max(16, left), W - cw - 16) + 'px';
  card.style.top = Math.min(Math.max(16, top), H - ch - 16) + 'px';
}
window.addEventListener('resize', () => { if (TOUR.active) placeTour(TOUR_STEPS[TOUR.i]); });
$('#tourBtn').addEventListener('click', startTour);
$('#learnTourBtn').addEventListener('click', startTour);

// ============================================================ timers & boot
let timers = [];
function restartTimers() {
  timers.forEach(clearInterval);
  const fast = () => S.tf === '1m' || S.tf === '5m';
  timers = [
    setInterval(() => refreshState(), 5000),
    setInterval(() => { if (S.tab === 'trade') refreshAsset(); }, 5000),
    setInterval(() => { if (fast() || Date.now() % 15000 < 5000) tickChart(); }, 5000),
    setInterval(() => { if (S.tab === 'trade') fetchListQuotes(); }, 30000),
    setInterval(() => { if (S.tab === 'portfolio') loadEvents(); if (S.tab === 'bots') loadBots(); if (S.tab === 'funds') loadFunds(); }, 8000),
  ];
}

async function boot() {
  try {
    const me = await api('/api/auth/me');
    if (!me.user) { location.href = '/login'; return; }
    S.user = me.user;
    $('#userName').textContent = me.user.username;
    $('#userAv').textContent = me.user.username.slice(0, 1);
  } catch (e) { return; }
  initChart();
  setMode(S.mode, true);
  try {
    const sec = await api('/api/sectors');
    $('#sectorSel').innerHTML = '<option value="">All sectors</option>' + sec.sectors.map((s) => `<option>${esc(s)}</option>`).join('');
  } catch (e) { /* */ }
  await refreshState(true);
  doSearch();
  selectAsset(S.sel);
  // first-run nudge
  const seenKey = 'tt_seen:' + S.user.username.toLowerCase();
  if (!localStorage.getItem(seenKey)) {
    localStorage.setItem(seenKey, '1');
    modal(`<h2>Welcome to Ticker Tycoon</h2>
      <p>You start with <b>$10,000</b> of virtual cash and a free <b>$1,000 SPY</b> starter position. Every stock and coin is real, with real prices.</p>
      <p class="muted">Pick a ticker on the left, size your trade on the right, then go <b class="up">LONG</b> (up) or <b class="down">SHORT</b> (down). Everything is unlocked from the start: leverage, bots and funds. XP raises your rank from Rookie to Ticker Tycoon.</p>
      <div class="note">The 2-minute tutorial shows you what every part of the screen does. You can replay it anytime with the <b>Tutorial</b> button at the top.</div>
      <p class="muted small mt">Virtual money only. Educational game, not financial advice.</p>
      <div class="acts"><button class="btn ghost" onclick="closeModal()">Skip</button><button class="btn primary" id="wTour">Start the tutorial</button></div>`);
    $('#wTour').onclick = () => { closeModal(); startTour(); };
  }
  // poll market data status
  try {
    const h = await api('/api/health');
    if (h.symbols.stocks + h.symbols.etfs < 1000) {
      const bar = $('#dataBar');
      bar.textContent = 'Downloading the full symbol lists (NASDAQ Trader + CoinGecko)... search uses a starter list until then.';
      bar.classList.remove('hidden');
      const iv = setInterval(async () => {
        const h2 = await api('/api/health').catch(() => null);
        if (h2 && h2.symbols.stocks + h2.symbols.etfs >= 1000) { bar.classList.add('hidden'); clearInterval(iv); doSearch(); }
        else if (h2 && h2.sources.nasdaq.ok === false) { bar.textContent = 'Could not download the NASDAQ symbol list (' + (h2.sources.nasdaq.error || 'offline') + '). Using the starter list; retry in Settings.'; clearInterval(iv); }
      }, 4000);
    }
  } catch (e) { /* */ }
}
boot();
