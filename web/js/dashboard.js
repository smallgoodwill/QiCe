(() => {
  const $=id=>document.getElementById(id);
  const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const money=n=>n==null?'—':(n<0?'−':'')+'¥ '+Math.abs(n).toLocaleString('zh-CN',{maximumFractionDigits:2});
  const statusText={idle:'尚无模拟盘',running:'运行中',paused:'已暂停',completed:'已完成',stopped:'已停止'};
  document.getElementById('dashboardMount').innerHTML=`<div class="heading"><div><h1>工作台</h1><p>账户和策略统计仅从本地服务保存的回测/模拟盘记录读取。</p></div><button class="btn" id="refreshDashboard">刷新</button></div><div id="dashboardError"></div><div class="grid kpis"><div class="card"><div class="kpi-top">策略库</div><div class="value" id="dashStrategyCount">—</div><div class="metric-tip">已保存策略</div></div><div class="card"><div class="kpi-top">模拟权益</div><div class="value" id="dashEquity">—</div><div class="metric-tip" id="dashPaperStatus">尚无模拟盘</div></div><div class="card"><div class="kpi-top">模拟盘已实现盈亏</div><div class="value" id="dashPnl">—</div><div class="metric-tip">不含未平仓浮动盈亏</div></div><div class="card"><div class="kpi-top">最近回测</div><div class="value" id="dashBacktest">—</div><div class="metric-tip" id="dashBacktestMeta">尚无回测记录</div></div></div><div class="card dash-kline" style="margin-bottom:16px"><div class="card-head" style="flex-wrap:wrap;gap:10px"><div><h2>实时 K 线</h2><div class="sub" id="dashKlineMeta">未接入行情</div><div class="dash-kline-meta" id="dashKlineQuote"></div></div><div class="toolbar"><select id="dashKlineSector" class="select" style="flex:0 0 82px" title="板块"></select><select id="dashKlineCode" class="select" title="品种"></select><select id="dashKlinePeriod" class="select" style="flex:0 0 88px" title="周期"><option value="1m">1 分钟</option><option value="5m">5 分钟</option><option value="15m">15 分钟</option><option value="30m">30 分钟</option><option value="60m">60 分钟</option><option value="4h">4 小时</option><option value="12h">12 小时</option><option value="1d">1 天</option><option value="1w">1 周</option></select><span class="tag amber" id="dashKlineState">未启动</span><button class="btn" id="goLive">打开完整图表</button></div></div><div class="kline-wrap compact"><div id="dashKlineChart" class="kline-canvas"></div><div class="kline-empty" id="dashKlineEmpty"><b>正在读取行情</b><span>选中品种后自动加载 K 线</span></div></div></div><div class="grid dash-main"><div class="card"><div class="card-head"><div><h2>模拟盘权益曲线</h2><div class="sub" id="dashDataMeta">暂无模拟盘数据</div></div></div><div class="chart-wrap" id="dashChart"><div class="empty">在「交易」页跑一次模拟后显示权益。</div></div></div><div class="card"><div class="card-head"><h2>运行概况</h2><span class="tag" id="dashStatus">无运行记录</span></div><div id="dashRunInfo"><div class="empty">当前没有模拟盘。</div></div><div class="note">统计来自最近一次区间模拟。账户权益为模拟账户值，不代表真实账户。</div></div></div><div class="card"><div class="card-head"><h2>最近模拟成交</h2><button class="btn" id="goPaper">打开模拟盘</button></div><div class="table-scroll"><table class="table"><thead><tr><th>模拟 ID</th><th>品种</th><th>方向</th><th>开仓时间</th><th>平仓时间</th><th>净盈亏</th></tr></thead><tbody id="dashTrades"><tr><td colspan="6" class="empty">暂无成交</td></tr></tbody></table></div></div>`;
  function draw(points){if(!points?.length){$('dashChart').innerHTML='<div class="empty">在「交易」页跑一次模拟后显示权益。</div>';return;}const W=700,H=210,pad=38,vals=points.map(x=>x.equity),lo=Math.min(...vals),hi=Math.max(...vals),range=hi-lo||1,x=i=>pad+i/Math.max(1,points.length-1)*(W-pad*2),y=v=>H-25-(v-lo)/range*(H-45),path=points.map((p,i)=>`${i?'L':'M'}${x(i).toFixed(1)} ${y(p.equity).toFixed(1)}`).join(' ');$('dashChart').innerHTML=`<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none"><line class="axis" x1="${pad}" y1="${y(lo)}" x2="${W-pad}" y2="${y(lo)}"/><line class="axis" x1="${pad}" y1="${y((lo+hi)/2)}" x2="${W-pad}" y2="${y((lo+hi)/2)}"/><path d="${path}" fill="none" stroke="#5277ee" stroke-width="2.5"/><text x="0" y="${y(hi)+3}" class="tick">${money(hi)}</text><text x="0" y="${y(lo)+3}" class="tick">${money(lo)}</text><text x="${pad}" y="${H-5}" class="tick">${esc(points[0].timestamp)}</text><text x="${W-130}" y="${H-5}" class="tick">${esc(points.at(-1).timestamp)}</text></svg>`;}
  async function load(){try{const r=await fetch('/api/dashboard');const d=await r.json();if(!r.ok)throw Error(d.error||'仪表盘接口不可用');$('dashboardError').textContent='';$('dashStrategyCount').textContent=d.strategyCount;$('dashPaperStatus').textContent=d.paper.active?`${statusText[d.paper.status]||d.paper.status} · ${d.paper.progressPct}%`:'尚无模拟盘';$('dashEquity').textContent=d.paper.active?money(d.paper.equity):'—';$('dashPnl').textContent=d.paper.active?money(d.paper.realizedPnl):'—';$('dashPnl').className='value '+(d.paper.active?(d.paper.realizedPnl>=0?'positive':'negative'):'');$('dashBacktest').textContent=d.latestBacktest?`${(+d.latestBacktest.summary.returnPct).toFixed(2)}%`:'—';$('dashBacktestMeta').textContent=d.latestBacktest?`${d.latestBacktest.summary.tradeCount} 笔 · ${d.latestBacktest.sourceBars} 根 bar`:'尚无回测记录';$('dashStatus').textContent=d.paper.active?statusText[d.paper.status]:'无运行记录';$('dashStatus').className='tag '+(d.paper.status==='running'?'green':'');if(d.paper.active){$('dashDataMeta').textContent=`${d.paper.symbol} · ${d.paper.strategyName} · ${d.paper.cursor}/${d.paper.totalBars} 根 · ${d.paper.currentTime||''}`;$('dashRunInfo').innerHTML=`<div class="summary-row"><span>策略</span><b>${esc(d.paper.strategyName)}</b></div><div class="summary-row"><span>品种</span><b>${esc(d.paper.symbol)}</b></div><div class="summary-row"><span>当前时间</span><b>${esc(d.paper.currentTime||'等待首根 bar')}</b></div><div class="summary-row"><span>未实现盈亏</span><b>${money(d.paper.unrealizedPnl)}</b></div><div class="summary-row"><span>最大回撤</span><b>${(+d.paper.maxDrawdownPct).toFixed(2)}%</b></div>`;draw(d.paper.equityCurve);}else{$('dashDataMeta').textContent='暂无模拟盘数据';$('dashRunInfo').innerHTML='<div class="empty">到「交易」页选一条策略和时间段，跑一次模拟就会显示。</div>';draw([]);}$('dashTrades').innerHTML=d.recentPaperTrades.length?d.recentPaperTrades.map(t=>`<tr><td>${esc(t.id)}</td><td>${esc(t.symbol)}</td><td>${esc(t.side)}</td><td>${esc(t.entryTime)}</td><td>${esc(t.exitTime)}</td><td class="${t.netPnl>=0?'positive':'negative'}">${money(t.netPnl)}</td></tr>`).join(''):'<tr><td colspan="6" class="empty">暂无成交</td></tr>';}catch(e){$('dashboardError').innerHTML=`<div class="note">${esc(e.message)}。请启动后端服务。</div>`;}}
  // ---- 实时 K 线组件 ---------------------------------------------------
  // 绘图复用 web/js/kline-core.js（与「实时 K 线」页同一份），这里只负责取数与状态。
  //
  // 数据来源按「周期 + 当前选中的品种」自动切换：
  //   1) 1 分钟周期 且 采集器正在跑的恰好就是这个品种 → 采集器 SSE，逐 tick 聚合，最准；
  //   2) 其他情况（含长周期）→ /api/kline/stream 秒级流：hello 给全量窗口，
  //      之后每秒推「正在形成的那根 bar」（真实 tick 合成），K 线按秒跳动；
  //      流不可用时退回 /api/kline 轮询兜底。
  // 有第 2 条，「跟随所选品种」才不用先把实时页连上 SimNow。
  let klineView = null, klineStream = null, klineStreamSymbol = '';
  let klineTick = null, klineTickFails = 0, klineTickKey = '';
  let klineMode = 'idle', klineLast = null, klineTimer = null, klineBusy = false;
  let klineGroups = [], klineCode = '', klinePeriod = '1m', klineLive = {running: false};
  const klineState = (text, tone) => { const el = $('dashKlineState'); if (!el) return; el.textContent = text; el.className = 'tag ' + (tone || 'amber'); };
  const klinePeriodText = p => ({'1m': '1 分钟', '5m': '5 分钟', '15m': '15 分钟', '30m': '30 分钟',
    '60m': '60 分钟', '4h': '4 小时', '12h': '12 小时', '1d': '1 天', '1w': '1 周'}[p] || p);
  /** 轮询兜底的间隔（正常情况走秒级流，只在流挂了才用）：日线一天才变一次 */
  const pollIntervalMs = p => ({'1d': 60000, '1w': 60000, '4h': 30000, '12h': 30000}[p] || 10000);
  /** 从 `rb2601` / `沪深300股指 · IF2503` 里取品种字母，用来判断「采集的是不是这个品种」 */
  const codeOfSymbol = symbol => (String(symbol || '').match(/[A-Za-z]+/) || [''])[0].toLowerCase();
  const priceText = v => v == null || v === '' ? '—' : Number(v).toLocaleString('zh-CN', {minimumFractionDigits: 2, maximumFractionDigits: 2});

  function ensureKlineView() {
    if (klineView) { klineView.resize(); return klineView; }
    const holder = $('dashKlineChart');
    if (!holder || !window.qiceKline) return null;
    klineView = window.qiceKline.create(holder, {});   // 容器没有宽度时返回 null，等切回来再建
    return klineView;
  }

  function showKlineEmpty(head, sub) {
    const box = $('dashKlineEmpty');
    if (!box) return;
    box.style.display = 'flex';
    box.innerHTML = `<b>${esc(head)}</b>${sub ? `<span>${esc(sub)}</span>` : ''}`;
  }

  function renderKlineQuote(bar) {
    const box = $('dashKlineQuote');
    if (!box) return;
    if (!bar) { box.innerHTML = ''; return; }
    const up = +bar.close >= +bar.open;
    box.innerHTML = `<b class="${up ? 'positive' : 'negative'}">${priceText(bar.close)}</b>`
      + `<span>高 ${priceText(bar.high)}</span><span>低 ${priceText(bar.low)}</span>`
      + `<span>量 ${Number(bar.volume || 0).toFixed(0)}</span>`
      + `<span>${esc(String(bar.timestamp || '').replace('T', ' ').slice(0, 16))}</span>`;
  }

  /** 整批换掉图上的数据：首屏、切品种、切周期、轮询刷新都走这里 */
  function paintKline(bars) {
    const view = ensureKlineView();
    if (!view || !bars || !bars.length) return false;
    const clean = view.apply(bars);
    if (!clean.length) return false;
    klineLast = clean[clean.length - 1];
    const box = $('dashKlineEmpty');
    if (box) box.style.display = 'none';
    renderKlineQuote(klineLast);
    return true;
  }

  /* ---- 品种选择：只选板块和品种，不选月份 ---------------------------------
   * 后端取的是主力连续合约（RB0 这种），让用户选 rb2601 再画 RB0 的价格
   * 等于给一个假精度——换月跳空、远近月价差都对不上。所以这里刻意不给月份，
   * 由界面明确标注「主力连续」。
   */
  async function loadKlinePicker() {
    try {
      const data = await (await fetch('/api/instruments')).json();
      klineGroups = data.groups || [];
      if (!klineGroups.length) return;
      const sectors = klineGroups.map(group => group.sector);
      $('dashKlineSector').innerHTML = sectors.map(sector =>
        `<option value="${esc(sector)}">${esc(sector)}</option>`).join('');
      $('dashKlineSector').value = sectors.includes('黑色') ? '黑色' : sectors[0];
      fillKlineCodes();
    } catch (_) { /* 品种表拿不到就保持空选择，不阻塞其他卡片 */ }
  }

  function fillKlineCodes() {
    const group = klineGroups.find(row => row.sector === $('dashKlineSector').value) || klineGroups[0];
    const items = (group && group.items) || [];
    const select = $('dashKlineCode');
    select.innerHTML = items.map(item =>
      `<option value="${esc(item.code)}">${esc(item.name)}</option>`).join('');
    if (items.some(item => item.code === klineCode)) select.value = klineCode;
    klineCode = select.value || '';
  }

  /* ---- 按需取数（采集器没在跑这个品种时用） ---- */
  async function fetchKlineByNeed() {
    if (klineBusy || !klineCode) return;
    const holder = $('dashKlineChart');
    if (!holder || !holder.offsetWidth) return;      // 页面没显示，等切回来再取
    klineBusy = true;
    try {
      const url = `/api/kline?symbol=${encodeURIComponent(klineCode)}`
        + `&period=${encodeURIComponent(klinePeriod)}&limit=320`;
      const response = await fetch(url);
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || '行情接口不可用');
      if (!paintKline(data.bars)) {
        showKlineEmpty('这个品种取不到 K 线', '可能已停牌或上游没有数据，换一个品种试试');
      } else {
        $('dashKlineMeta').textContent =
          `${data.continuous} 主力连续 · ${klinePeriodText(data.period)} · ${data.count} 根`;
        klineState('主力连续', 'green');
        $('dashKlineState').title = data.notice || '';
        $('dashKlineMeta').title = data.notice || '';
      }
    } catch (error) {
      klineState('行情不可用', 'amber');
      if (!klineLast || klineMode === 'poll') showKlineEmpty('取不到行情', error.message);
    } finally { klineBusy = false; }
  }

  function startPolling() {
    if (klineTimer) { clearInterval(klineTimer); klineTimer = null; }
    klineMode = 'poll';
    fetchKlineByNeed();
    klineTimer = setInterval(fetchKlineByNeed, pollIntervalMs(klinePeriod));
  }

  function stopPolling() {
    if (klineTimer) { clearInterval(klineTimer); klineTimer = null; }
  }

  /* ---- 秒级流（任意品种、任意周期）：/api/kline/stream -----------------
   * hello 给全量窗口一次画好；之后每秒一个 bar 事件 = tick 合成的「形成中 K 线」，
   * 增量 update 到最后一根上，图表按秒跳动。流挂了自动退回轮询。 */
  function openTickStream() {
    if (!klineCode) return;
    if (klineTick) { klineTick.close(); klineTick = null; }
    stopPolling();
    klineMode = 'tick';
    klineTickFails = 0;
    klineTickKey = klineCode + '|' + klinePeriod;
    klineState('秒级监听', 'green');
    klineTick = new EventSource(`/api/kline/stream?symbol=${encodeURIComponent(klineCode)}`
      + `&period=${encodeURIComponent(klinePeriod)}`);
    klineTick.addEventListener('hello', event => {
      const data = JSON.parse(event.data);
      if (klineTickKey !== klineCode + '|' + klinePeriod) return;   // 已切走
      if (!paintKline(data.bars)) {
        showKlineEmpty('这个品种取不到 K 线', '可能已停牌或上游没有数据，换一个品种试试');
        return;
      }
      $('dashKlineMeta').textContent =
        `${data.continuous} 主力连续 · ${klinePeriodText(data.period)} · ${data.bars.length} 根 · 按秒更新`;
      klineState('秒级监听', 'green');
      $('dashKlineState').title = data.notice || '';
      $('dashKlineMeta').title = data.notice || '';
    });
    klineTick.addEventListener('bar', event => {
      if (klineTickKey !== klineCode + '|' + klinePeriod) return;
      const data = JSON.parse(event.data);
      const view = ensureKlineView();
      if (!view || !data.bar) return;
      if (!view.update(data.bar)) return;             // 乱序/迟到 bar 由内核丢弃
      klineLast = data.bar;
      $('dashKlineEmpty').style.display = 'none';
      renderKlineQuote(klineLast);
    });
    klineTick.addEventListener('error', event => {
      // SSE 的 error 事件既可能是服务端主动报错，也可能是断流
      let message = '';
      try { message = JSON.parse(event.data).error || ''; } catch (_) { /* 断流没有 data */ }
      if (klineMode !== 'tick') return;
      klineTickFails += 1;
      if (klineTickFails >= 3) {
        fallbackToPolling(message || '实时流连接失败');
      } else {
        klineState('连接中断，自动重连', 'amber');
      }
    });
  }

  function closeTickStream() {
    if (klineTick) { klineTick.close(); klineTick = null; }
    klineTickKey = '';
    klineTickFails = 0;
  }

  /** 流不可用时退回轮询：图还能用，只是不再逐秒跳 */
  function fallbackToPolling(reason) {
    closeTickStream();
    klineMode = 'poll';
    klineState(reason ? '流不可用，已降级轮询' : '轮询行情', 'amber');
    if (reason) $('dashKlineState').title = reason;
    startPolling();
  }

  function openKlineStream(symbol) {
    stopPolling();
    closeTickStream();
    if (klineStream) { klineStream.close(); klineStream = null; }
    klineStreamSymbol = symbol;
    klineMode = 'stream';
    klineStream = new EventSource(`/api/live/stream?symbol=${encodeURIComponent(symbol)}`);
    klineStream.addEventListener('hello', event => {
      const data = JSON.parse(event.data);
      const bars = data.bars || [];
      if (bars.length) paintKline(bars);
      else { ensureKlineView(); showKlineEmpty('正在从当前时刻累积', 'CTP 只推实时 tick，不回溯历史 K 线，每分钟新增 1 根'); }
    });
    klineStream.addEventListener('bar', event => {
      const data = JSON.parse(event.data);
      if (data.symbol && data.symbol !== klineStreamSymbol) return;
      const view = ensureKlineView();
      if (!view || !data.bar) return;
      if (!view.update(data.bar)) return;             // 乱序/迟到 bar 由内核丢弃
      klineLast = data.bar;
      $('dashKlineEmpty').style.display = 'none';
      renderKlineQuote(klineLast);
    });
    klineStream.onerror = () => klineState('连接中断，自动重连', 'amber');
  }

  function closeKlineStream() {
    if (klineStream) { klineStream.close(); klineStream = null; }
    klineStreamSymbol = '';
  }

  /** 判断采集器正在跑的是不是当前选中的这个品种 */
  function liveMatchesPick() {
    return Boolean(klineLive && klineLive.running && klineCode
      && codeOfSymbol(klineLive.symbol) === klineCode.toLowerCase());
  }

  async function syncKline() {
    if (!$('dashKlineChart')) return;
    try {
      klineLive = await (await fetch('/api/live/status')).json();
    } catch (_) {
      klineLive = {running: false};
    }

    if (liveMatchesPick() && klinePeriod === '1m') {
      // 采集器正在跑这个品种：1 分钟周期直接用实时流，比轮询更及时。
      // 长周期即使采集器在跑也走轮询——SSE 只推 1 分钟 bar，画到 4h/1d 图上口径就错了。
      klineState(klineLive.driver === 'ctp' ? 'CTP 实时' : `模拟源 ${klineLive.speedup}×`, 'green');
      $('dashKlineMeta').textContent = `${klineLive.symbol} · ${klineLive.exchange || ''} · `
        + (klineLive.driver === 'ctp' ? '只读订阅，不下单' : '模拟数据，不可用于决策');
      ensureKlineView();
      if (klineStreamSymbol !== klineLive.symbol) openKlineStream(klineLive.symbol);
      return;
    }

    // 其他情况（含长周期）：秒级流——每秒推形成中 bar，K 线按秒跳动
    closeKlineStream();
    if (klineMode !== 'tick' || klineTickKey !== klineCode + '|' + klinePeriod) openTickStream();
  }

  /** 切品种/切周期：先停掉旧连接，再按新选择重新决定数据来源 */
  function restartKline() {
    closeKlineStream();
    closeTickStream();
    stopPolling();
    klineMode = 'idle';
    klineLast = null;
    renderKlineQuote(null);
    syncKline();
  }

  $('goLive').addEventListener('click', () => window.go?.('live'));
  $('dashKlineSector').addEventListener('change', () => { fillKlineCodes(); restartKline(); });
  $('dashKlineCode').addEventListener('change', () => { klineCode = $('dashKlineCode').value; restartKline(); });
  $('dashKlinePeriod').addEventListener('change', () => { klinePeriod = $('dashKlinePeriod').value; restartKline(); });

  window.addEventListener('qice-page-change', event => {
    if (event.detail !== 'overview') return;
    if (klineView) klineView.resize();
    if (klineMode === 'stream' && klineStreamSymbol) {
      const symbol = klineStreamSymbol;
      klineStreamSymbol = '';
      openKlineStream(symbol);
    } else if (klineMode === 'tick') {
      openTickStream();              // 切回来重建连接，立即补 hello 全量
    } else if (klineMode === 'poll') {
      fetchKlineByNeed();            // 切回来立刻补一次，不用干等下一个 10 秒
    } else {
      syncKline();
    }
  });
  window.addEventListener('resize', () => { if (klineView) klineView.resize(); });
  // 先拿到品种表再决定数据来源：否则第一次比较的是空品种，会白连一次
  loadKlinePicker().then(syncKline);
  setInterval(syncKline, 5000);

  $('refreshDashboard').addEventListener('click',load);$('goPaper').addEventListener('click',()=>window.go?.('paper'));load();setInterval(load,5000);
  document.querySelectorAll('.nav button').forEach(b=>b.addEventListener('click',()=>{if(b.dataset.page==='overview')load();}));
})();
