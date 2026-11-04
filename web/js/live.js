(() => {
  const mount = document.getElementById('liveMount');
  if (!mount) return;
  const $ = id => document.getElementById(id);
  const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const price = v => v == null || v === '' ? '—' : Number(v).toLocaleString('zh-CN', {minimumFractionDigits: 2, maximumFractionDigits: 2});
  const CONTRACT_RE = /[A-Za-z]{1,2}\d{3,4}/;
  const contractCode = s => ((String(s || '').match(CONTRACT_RE) || [''])[0]).toUpperCase();

  mount.innerHTML = `<div class="heading"><div><h1>实时 K 线</h1><p>行情来自 SimNow/CTP 或内置模拟源，1 分钟 bar 实时合成；模拟盘的虚拟成交会以买卖点标在对应价格上。</p></div>
  <div class="toolbar"><span class="tag amber" id="liveState">未启动</span><button class="btn" id="liveStop">停止采集</button></div></div>

  <div class="risk-banner" id="liveNotice"><span>ⓘ</span><div>这是**只读行情**：不发送任何委托、不改动模拟盘账本。模拟源的数据由随机数生成，只用于验证链路，不能用于交易决策。</div></div>

  <div class="grid live-layout">
    <div class="card"><div class="card-head" style="flex-wrap:wrap;gap:10px">
      <div class="toolbar">
        <input type="text" id="liveSymbol" class="select" style="width:104px" value="IF2612" title="合约代码，如 IF2612 / rb2601">
        <select id="liveExchange" class="select" title="交易所">
          <option value="CFFEX">CFFEX 中金所</option><option value="SHFE">SHFE 上期所</option>
          <option value="DCE">DCE 大商所</option><option value="CZCE">CZCE 郑商所</option>
          <option value="INE">INE 能源中心</option><option value="GFEX">GFEX 广期所</option>
        </select>
        <select id="livePeriod" class="select"><option value="1m">1 分钟</option><option value="5m">5 分钟</option><option value="15m">15 分钟</option><option value="30m">30 分钟</option><option value="60m">60 分钟</option></select>
      </div>
      <div class="live-quote" id="liveQuote"><span class="tag">尚无行情</span></div>
    </div>
    <div class="kline-wrap"><div id="kline" class="kline-canvas"></div><div id="markerLayer" class="marker-layer"></div></div>
    <div class="toolbar" style="margin-top:12px">
      <button class="btn primary" id="liveStartCtp">连接 SimNow 实时行情</button>
      <button class="btn" id="liveStartSim">启动内置模拟源</button>
      <span class="sub" id="liveSpeedWrap">模拟源加速 <input type="number" id="liveSpeed" value="20" min="1" max="120" step="1" style="width:58px;padding:4px 6px"> ×</span>
      <span class="sub" id="liveMsg"></span>
    </div></div>

    <div class="card"><div class="card-head"><h2>虚拟成交点</h2><span class="tag" id="markerCount">0 笔</span></div>
      <label class="checkline"><input type="checkbox" id="showMarkers" checked> 在 K 线上标出买卖点</label>
      <div class="field" style="margin-top:10px"><label>显示哪次运行的成交</label><select id="liveRun" class="select"><option value="">（暂无运行记录）</option></select></div>
      <div class="live-trades" id="liveTradeList"><div class="empty">暂无虚拟成交</div></div>
      <div class="note" id="alignHint" style="display:none"></div>
      <div class="toolbar" style="margin-top:10px"><button class="btn" id="liveRefreshTrades">刷新成交</button></div>
    </div>
  </div>`;

  let chart = null, stream = null, trades = [], markers = [], showMarkers = true;
  let chartSymbol = 'IF2612', period = '1m', multiPeriodTimer = null;

  // ---- 通用请求 --------------------------------------------------------
  async function api(path, options = {}) {
    const response = await fetch(path, options);
    let body = {};
    try { body = await response.json(); } catch {}
    if (!response.ok) throw Error(body.error || `请求失败 (${response.status})`);
    return body;
  }
  const post = (path, payload) => api(path, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload || {})});

  function setState(text, tone) {
    const el = $('liveState');
    el.textContent = text;
    el.className = 'tag ' + (tone || 'amber');
  }

  // ---- 图表 ------------------------------------------------------------
  // 绘图交给 web/js/kline-core.js（仪表盘组件共用同一份），这里只保留页面逻辑
  let view = null;

  function ensureChart() {
    const holder = $('kline');
    if (!holder) return null;
    if (view) { view.resize(); return view.chart; }
    if (typeof klinecharts === 'undefined') { $('liveMsg').textContent = '图表库未加载'; return null; }
    view = window.qiceKline.create(holder, {onViewChange: () => scheduleMarkers()});
    if (!view) return null;                         // 页面还没显示，等切换过来再初始化
    chart = view.chart;
    return chart;
  }

  function applyBars(bars) {
    const c = ensureChart();
    if (!c || !view || !bars || !bars.length) return;
    const clean = view.apply(bars);
    if (!clean.length) return;
    if (clean.length >= 5) {                  // 攒够历史了，撤掉"正在累积"的提示
      const notice = $('liveNotice');
      if (notice) delete notice.dataset.thin;
    }
    scheduleMarkers();
    updateQuote(clean[clean.length - 1]);
  }

  function onBar(bar) {
    if (!bar) return;
    updateQuote(bar);
    if (period !== '1m') return;              // 多分钟周期用轮询刷新，不在前端重复实现聚合
    const c = ensureChart();
    if (!c || !view) return;
    view.update(bar);                         // 内核会丢弃迟到/乱序的 bar
    scheduleMarkers();
  }

  function updateQuote(bar) {
    if (!bar) return;
    const up = +bar.close >= +bar.open;
    $('liveQuote').innerHTML = `<b class="${up ? 'positive' : 'negative'}">${price(bar.close)}</b>`
      + `<span>高 ${price(bar.high)}</span><span>低 ${price(bar.low)}</span>`
      + `<span>量 ${Number(bar.volume || 0).toFixed(0)}</span>`
      + `<span class="muted">·</span>`
      + `<span>${esc(String(bar.timestamp || '').replace('T', ' ').slice(0, 16))}</span>`;
  }

  // ---- 成交标记 --------------------------------------------------------
  let markerFrame = null;
  function scheduleMarkers() {
    if (markerFrame) return;
    markerFrame = requestAnimationFrame(() => { markerFrame = null; renderMarkers(); });
  }

  function buildMarkers() {
    markers = [];
    const code = contractCode(chartSymbol);
    trades.forEach(t => {
      if (t.symbol && contractCode(t.symbol) && code && contractCode(t.symbol) !== code) return;
      const isLong = ['long', '做多'].includes(t.side);
      const name = isLong ? '多' : '空';
      const entryTs = Date.parse(t.entryTime);
      if (Number.isFinite(entryTs) && +t.entryPrice > 0) {
        markers.push({ts: entryTs, value: +t.entryPrice, kind: isLong ? 'buy' : 'sell',
          label: `开${name} ${price(t.entryPrice)}`,
          tip: `开${name} @ ${price(t.entryPrice)}　${t.entryTime}　${t.strategyName || ''}`});
      }
      const exitTs = Date.parse(t.exitTime);
      if (Number.isFinite(exitTs) && +t.exitPrice > 0) {
        markers.push({ts: exitTs, value: +t.exitPrice, kind: isLong ? 'sell' : 'buy',
          label: `平${name} ${price(t.exitPrice)}`,
          tip: `平${name} @ ${price(t.exitPrice)}　${t.exitTime}　净盈亏 ${price(t.netPnl)}`});
      }
    });
    $('markerCount').textContent = `${markers.length} 个点`;
  }

  function renderMarkers() {
    const layer = $('markerLayer');
    if (!layer) return;
    if (!chart || !showMarkers || !markers.length) { layer.innerHTML = ''; updateAlignHint(0); return; }
    const list = chart.getDataList();
    if (!list.length) { layer.innerHTML = ''; updateAlignHint(0); return; }
    const times = list.map(b => b.timestamp);
    const idxOf = ts => {                      // 找最后一根 timestamp <= ts 的 bar
      let lo = 0, hi = times.length - 1, ans = -1;
      while (lo <= hi) {
        const mid = (lo + hi) >> 1;
        if (times[mid] <= ts) { ans = mid; lo = mid + 1; } else { hi = mid - 1; }
      }
      return ans;
    };
    const range = chart.getVisibleRange() || {};
    const width = layer.clientWidth || 0;
    const frag = document.createDocumentFragment();
    const placed = [];                 // 已占位的位置，用于避让
    let shown = 0;
    markers.forEach(m => {
      const index = idxOf(m.ts);
      if (index < 0) return;
      if (range.from != null && (index < range.from - 2 || index > range.to + 2)) return;
      const bar = list[index];
      // 买点挂在这根 bar 的最低点下方，卖点挂在最高点上方，避免压住蜡烛实体
      const anchor = m.kind === 'buy' ? Math.min(bar.low, m.value) : Math.max(bar.high, m.value);
      const point = chart.convertToPixel({dataIndex: index, value: anchor}, {paneId: 'candle_pane'});
      if (!point || point.x == null || point.y == null) return;
      let x = width ? Math.min(Math.max(point.x, 38), width - 74) : point.x;   // 收进画布，防右侧被裁
      let y = point.y + (m.kind === 'buy' ? 5 : -5);
      for (let guard = 0; guard < 6; guard += 1) {                            // 同位置错开，最多叠 6 层
        const clash = placed.find(p => Math.abs(p.x - x) < 62 && Math.abs(p.y - y) < 16);
        if (!clash) break;
        y += m.kind === 'buy' ? 15 : -15;
      }
      placed.push({x, y});
      const el = document.createElement('div');
      el.className = `tr-badge ${m.kind}`;
      el.style.left = `${x}px`;
      el.style.top = `${y}px`;
      el.textContent = m.label;
      el.title = m.tip;
      frag.appendChild(el);
      shown += 1;
    });
    layer.innerHTML = '';
    layer.appendChild(frag);
    updateAlignHint(shown);
  }

  function updateAlignHint(shown) {
    const box = $('alignHint');
    if (shown > 0 || !trades.length) { box.style.display = 'none'; return; }
    const runId = $('liveRun').value;
    const code = contractCode(chartSymbol);
    const other = trades.find(t => t.symbol && contractCode(t.symbol) !== code);
    const why = other
      ? `当前图表是 <b>${esc(chartSymbol)}</b>，而最近的成交属于 <b>${esc(other.symbol)}</b>，两者不是同一段行情。`
      : '当前图表的时间范围内没有成交点。';
    box.style.display = 'block';
    box.innerHTML = `${why}模拟盘的行情是一次性回放的区间数据（演示合成或 CTP 采集），与实时源各走各的。`
      + (runId ? `<br><button class="btn" id="alignBtn" style="margin-top:8px">载入该次运行的行情并对齐买卖点</button>` : '');
    const btn = $('alignBtn');
    if (btn) btn.addEventListener('click', () => alignRun(runId));
  }

  async function alignRun(runId) {
    if (!runId) return;
    try {
      const data = await api(`/api/trading/runs/${encodeURIComponent(runId)}/bars`);
      chartSymbol = data.symbol || chartSymbol;
      $('liveSymbol').value = chartSymbol;
      if (stream) { stream.close(); stream = null; }
      setState('回放行情（对齐成交点）', 'amber');
      buildMarkers();                    // 品种变了，先按新品种重建成交点
      applyBars(data.bars);
      $('liveMsg').textContent = `已载入 ${data.count} 根回放行情：${data.strategyName || runId}`;
    } catch (e) { $('liveMsg').textContent = e.message; }
  }

  // ---- 推送 ------------------------------------------------------------
  function reopenStream() {
    if (stream) { stream.close(); stream = null; }
    stream = new EventSource(`/api/live/stream?symbol=${encodeURIComponent(chartSymbol)}`);
    stream.addEventListener('hello', event => {
      const data = JSON.parse(event.data);
      applyStatus(data.status);
      const bars = data.bars || [];
      if (bars.length) applyBars(bars);
      if (bars.length < 5) hintThinHistory();
    });
    stream.addEventListener('bar', event => {
      const data = JSON.parse(event.data);
      if (data.symbol && data.symbol !== chartSymbol) return;
      onBar(data.bar);
    });
    stream.onerror = () => setState('连接中断，浏览器会自动重连', 'amber');
  }

  // 真实行情只有接入之后的数据（历史不回溯），刚启动时图上只有一两根，需要说清楚
  function hintThinHistory() {
    const box = $('liveNotice');
    if (!box) return;
    box.dataset.thin = '1';
    box.innerHTML = `<span>ⓘ</span><div><strong>正在从当前时刻开始累积</strong>：CTP 只推实时 tick，`
      + `不会回溯历史 K 线，所以图上先只有接入后的几根，此后每分钟新增 1 根。`
      + `想立刻看到完整形态，可点下方「启动内置模拟源」（模拟数据，仅供看效果）。</div>`;
  }

  function applyStatus(status) {
    if (!status) return;
    const running = status.running;
    const driver = status.driver;
    chartSymbol = status.symbol || chartSymbol;
    if (status.symbol) $('liveSymbol').value = status.symbol;
    $('liveMsg').textContent = status.message || '';
    if (running) setState(driver === 'ctp' ? 'CTP 实时行情' : `模拟源 ${status.speedup}×`, 'green');
    else setState('未启动', 'amber');
    const ctp = status.ctp || {};
    const notice = $('liveNotice');
    if (driver === 'sim') {
      notice.innerHTML = `<span>⚠</span><div><strong>当前是内置模拟源</strong>：价格由随机数生成，只用于验证「采集 → 聚合 → 推送 → 画图」链路，不能作为任何交易依据。</div>`;
    } else if (driver === 'ctp') {
      notice.innerHTML = `<span>ⓘ</span><div><strong>CTP 实时行情</strong>：来自 SimNow 柜台，只读订阅，不下单。行情仅在交易时段有效，非交易时段停在最后一个 tick。</div>`;
    } else {
      notice.innerHTML = `<span>ⓘ</span><div>这是<strong>只读行情</strong>：不发送任何委托、不改动模拟盘账本。点「连接 SimNow 实时行情」用 CTP 真实数据，或「启动内置模拟源」直接看效果。`
        + (ctp.credentialsConfigured ? '' : '　（.env 里 SimNow 账号还没填全，CTP 不可用）') + `</div>`;
    }
    if (driver === 'ctp' && notice.dataset.thin === '1') hintThinHistory();   // 状态轮询会重写提示，补回去
  }

  async function loadBars() {
    try {
      const data = await api(`/api/live/bars?symbol=${encodeURIComponent(chartSymbol)}&period=${period}&limit=500`);
      if (data.bars && data.bars.length) applyBars(data.bars);
      else if (!chart) ensureChart();
    } catch (e) { $('liveMsg').textContent = e.message; }
  }

  // ---- 启停 ------------------------------------------------------------
  async function startCtp() {
    const symbol = ($('liveSymbol').value || '').trim();
    if (!symbol) { $('liveMsg').textContent = '请先填写合约代码，例如 IF2612。'; return; }
    chartSymbol = symbol;
    const button = $('liveStartCtp');
    button.disabled = true;
    try {
      const ctp = await api('/api/simnow/status');
      if (!ctp.connected) {
        if (!ctp.available) throw Error('本机未安装 vnpy / vnpy_ctp，无法使用 CTP 行情。');
        setState('连接 SimNow 中…', 'amber');
        await post('/api/simnow/connect', {});
        for (let i = 0; i < 30; i += 1) {
          await sleep(1000);
          const state = await api('/api/simnow/status');
          if (state.connected) break;
          if (state.error) throw Error(state.error);
        }
        const after = await api('/api/simnow/status');
        if (!after.connected) throw Error(after.error || 'SimNow 连接超时，请检查账号或前置地址。');
      }
      const status = await post('/api/live/start', {driver: 'ctp', symbol, exchange: $('liveExchange').value});
      applyStatus(status);
      reopenStream();
      $('liveMsg').textContent = `已订阅 ${symbol}；非交易时段不会有新 tick。`;
    } catch (e) {
      $('liveMsg').textContent = e.message;
      setState('启动失败', 'amber');
    } finally {
      button.disabled = false;
    }
  }

  async function startSim() {
    const symbol = ($('liveSymbol').value || '').trim() || 'IF2612';
    chartSymbol = symbol;
    try {
      const status = await post('/api/live/start', {driver: 'sim', symbol, speedup: +$('liveSpeed').value || 20});
      applyStatus(status);
      period = '1m'; $('livePeriod').value = '1m';
      reopenStream();
    } catch (e) { $('liveMsg').textContent = e.message; }
  }

  async function stopFeed() {
    try {
      const status = await post('/api/live/stop', {});
      applyStatus(status);
      if (stream) { stream.close(); stream = null; }
    } catch (e) { $('liveMsg').textContent = e.message; }
  }

  // ---- 成交列表 --------------------------------------------------------
  async function loadTrades() {
    try {
      const all = await api('/api/trading/trades');
      trades = all || [];
      const runs = new Map();
      trades.forEach(t => { if (t.runId && !runs.has(t.runId)) runs.set(t.runId, t); });
      const select = $('liveRun');
      const current = select.value;
      select.innerHTML = runs.size
        ? [...runs.entries()].map(([id, t]) => `<option value="${esc(id)}">${esc(t.strategyName || '未命名')} · ${esc(t.symbol || '')} · ${esc((t.exitTime || '').replace('T', ' ').slice(5, 16))}</option>`).join('')
        : '<option value="">（暂无运行记录）</option>';
      if (current && [...runs.keys()].includes(current)) select.value = current;
      renderTradeList();
      buildMarkers();
      renderMarkers();
    } catch (e) { $('liveTradeList').innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
  }

  function renderTradeList() {
    const runId = $('liveRun').value;
    const rows = trades.filter(t => !runId || t.runId === runId).slice(0, 40);
    $('liveTradeList').innerHTML = rows.length ? rows.map(t => {
      const isLong = ['long', '做多'].includes(t.side);
      return `<div class="live-trade" data-ts="${esc(t.entryTime)}">
        <span class="side ${isLong ? 'buy' : 'sell'}">${isLong ? '多' : '空'}</span>
        <span>${esc(t.symbol || '')}<br><span class="t">${esc((t.entryTime || '').replace('T', ' ').slice(5, 16))} → ${esc((t.exitTime || '持仓中').replace('T', ' ').slice(5, 16))}</span></span>
        <span class="${t.netPnl >= 0 ? 'positive' : 'negative'}">${price(t.netPnl)}</span>
      </div>`;
    }).join('') : '<div class="empty">这次运行还没有成交</div>';
    document.querySelectorAll('.live-trade').forEach(row => row.addEventListener('click', () => {
      const ts = Date.parse(row.dataset.ts);
      const list = chart && chart.getDataList();
      if (!list || !list.length || !Number.isFinite(ts)) return;
      let index = 0;
      list.forEach((bar, i) => { if (bar.timestamp <= ts) index = i; });
      chart.scrollToDataIndex(Math.max(0, index - 30));
    }));
  }

  // ---- 事件绑定 --------------------------------------------------------
  $('liveStartCtp').addEventListener('click', startCtp);
  $('liveStartSim').addEventListener('click', startSim);
  $('liveStop').addEventListener('click', stopFeed);
  $('liveRefreshTrades').addEventListener('click', loadTrades);
  $('showMarkers').addEventListener('change', e => { showMarkers = e.target.checked; renderMarkers(); });
  $('liveRun').addEventListener('change', () => { renderTradeList(); });
  $('livePeriod').addEventListener('change', async e => {
    period = e.target.value;
    if (multiPeriodTimer) { clearInterval(multiPeriodTimer); multiPeriodTimer = null; }
    await loadBars();
    if (period !== '1m') multiPeriodTimer = setInterval(loadBars, 5000);   // 多分钟周期靠轮询刷新
  });
  $('liveSymbol').addEventListener('change', async e => {
    chartSymbol = (e.target.value || '').trim() || chartSymbol;
    if (stream) reopenStream();
    await loadBars();
    buildMarkers();
    renderMarkers();
  });
  window.addEventListener('qice-page-change', event => {
    if (event.detail === 'live') { ensureChart(); scheduleMarkers(); }
  });
  window.addEventListener('resize', () => { if (chart) { chart.resize(); scheduleMarkers(); } });

  // ---- 启动 ------------------------------------------------------------
  (async () => {
    try {
      await loadTrades();
      const status = await api('/api/live/status');
      applyStatus(status);
      if (status.running) {
        chartSymbol = status.symbol || chartSymbol;
        await loadBars();
        reopenStream();
      }
    } catch (e) { $('liveMsg').textContent = e.message; }
  })();
})();
