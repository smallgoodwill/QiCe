(() => {
  const $=id=>document.getElementById(id);
  const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const money=n=>n==null?'—':(n<0?'−':'')+'¥ '+Math.abs(n).toLocaleString('zh-CN',{maximumFractionDigits:2});
  const statusText={idle:'尚无模拟盘',running:'运行中',paused:'已暂停',completed:'已完成',stopped:'已停止'};
  document.getElementById('dashboardMount').innerHTML=`<div class="heading"><div><h1>工作台</h1><p>账户和策略统计仅从本地服务保存的回测/模拟盘记录读取。</p></div><button class="btn" id="refreshDashboard">刷新</button></div><div id="dashboardError"></div><div class="grid kpis"><div class="card"><div class="kpi-top">策略库</div><div class="value" id="dashStrategyCount">—</div><div class="metric-tip">已保存策略</div></div><div class="card"><div class="kpi-top">模拟权益</div><div class="value" id="dashEquity">—</div><div class="metric-tip" id="dashPaperStatus">尚无模拟盘</div></div><div class="card"><div class="kpi-top">模拟盘已实现盈亏</div><div class="value" id="dashPnl">—</div><div class="metric-tip">不含未平仓浮动盈亏</div></div><div class="card"><div class="kpi-top">最近回测</div><div class="value" id="dashBacktest">—</div><div class="metric-tip" id="dashBacktestMeta">尚无回测记录</div></div></div><div class="card dash-kline" style="margin-bottom:16px"><div class="card-head" style="flex-wrap:wrap;gap:10px"><div><h2>实时 K 线</h2><div class="sub" id="dashKlineMeta">未接入行情</div><div class="dash-kline-meta" id="dashKlineQuote"></div></div><div class="toolbar"><span class="tag amber" id="dashKlineState">未启动</span><button class="btn" id="goLive">打开完整图表</button></div></div><div class="kline-wrap compact"><div id="dashKlineChart" class="kline-canvas"></div><div class="kline-empty" id="dashKlineEmpty"><b>尚未接入实时行情</b><span>到「实时 K 线」页连接 SimNow 后，这里会自动显示</span></div></div></div><div class="grid dash-main"><div class="card"><div class="card-head"><div><h2>模拟盘权益曲线</h2><div class="sub" id="dashDataMeta">暂无模拟盘数据</div></div></div><div class="chart-wrap" id="dashChart"><div class="empty">在「交易」页跑一次模拟后显示权益。</div></div></div><div class="card"><div class="card-head"><h2>运行概况</h2><span class="tag" id="dashStatus">无运行记录</span></div><div id="dashRunInfo"><div class="empty">当前没有模拟盘。</div></div><div class="note">统计来自最近一次区间模拟。账户权益为模拟账户值，不代表真实账户。</div></div></div><div class="card"><div class="card-head"><h2>最近模拟成交</h2><button class="btn" id="goPaper">打开模拟盘</button></div><div class="table-scroll"><table class="table"><thead><tr><th>模拟 ID</th><th>品种</th><th>方向</th><th>开仓时间</th><th>平仓时间</th><th>净盈亏</th></tr></thead><tbody id="dashTrades"><tr><td colspan="6" class="empty">暂无成交</td></tr></tbody></table></div></div>`;
  document.getElementById('diagnosticsMount').innerHTML=`<div class="heading"><div><h1>品种与策略诊断</h1><p>根据当前模拟回放的实际行情和已成交记录计算；无数据时不显示推测结论。</p></div><button class="btn" id="refreshDiagnostics">刷新诊断</button></div><div id="diagnosticsBody"><div class="empty">暂无诊断数据。</div></div>`;
  document.getElementById('dailyMount').innerHTML=`<div class="heading"><div><h1>每日模拟盘诊断</h1><p>从当前模拟盘的行情、信号和虚拟成交生成运行诊断。</p></div><button class="btn" id="refreshDaily">刷新诊断</button></div><div id="dailyBody"><div class="empty">暂无诊断数据。</div></div>`;
  function draw(points){if(!points?.length){$('dashChart').innerHTML='<div class="empty">在「交易」页跑一次模拟后显示权益。</div>';return;}const W=700,H=210,pad=38,vals=points.map(x=>x.equity),lo=Math.min(...vals),hi=Math.max(...vals),range=hi-lo||1,x=i=>pad+i/Math.max(1,points.length-1)*(W-pad*2),y=v=>H-25-(v-lo)/range*(H-45),path=points.map((p,i)=>`${i?'L':'M'}${x(i).toFixed(1)} ${y(p.equity).toFixed(1)}`).join(' ');$('dashChart').innerHTML=`<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none"><line class="axis" x1="${pad}" y1="${y(lo)}" x2="${W-pad}" y2="${y(lo)}"/><line class="axis" x1="${pad}" y1="${y((lo+hi)/2)}" x2="${W-pad}" y2="${y((lo+hi)/2)}"/><path d="${path}" fill="none" stroke="#5277ee" stroke-width="2.5"/><text x="0" y="${y(hi)+3}" class="tick">${money(hi)}</text><text x="0" y="${y(lo)+3}" class="tick">${money(lo)}</text><text x="${pad}" y="${H-5}" class="tick">${esc(points[0].timestamp)}</text><text x="${W-130}" y="${H-5}" class="tick">${esc(points.at(-1).timestamp)}</text></svg>`;}
  async function load(){try{const r=await fetch('/api/dashboard');const d=await r.json();if(!r.ok)throw Error(d.error||'仪表盘接口不可用');$('dashboardError').textContent='';$('dashStrategyCount').textContent=d.strategyCount;$('dashPaperStatus').textContent=d.paper.active?`${statusText[d.paper.status]||d.paper.status} · ${d.paper.progressPct}%`:'尚无模拟盘';$('dashEquity').textContent=d.paper.active?money(d.paper.equity):'—';$('dashPnl').textContent=d.paper.active?money(d.paper.realizedPnl):'—';$('dashPnl').className='value '+(d.paper.active?(d.paper.realizedPnl>=0?'positive':'negative'):'');$('dashBacktest').textContent=d.latestBacktest?`${(+d.latestBacktest.summary.returnPct).toFixed(2)}%`:'—';$('dashBacktestMeta').textContent=d.latestBacktest?`${d.latestBacktest.summary.tradeCount} 笔 · ${d.latestBacktest.sourceBars} 根 bar`:'尚无回测记录';$('dashStatus').textContent=d.paper.active?statusText[d.paper.status]:'无运行记录';$('dashStatus').className='tag '+(d.paper.status==='running'?'green':'');if(d.paper.active){$('dashDataMeta').textContent=`${d.paper.symbol} · ${d.paper.strategyName} · ${d.paper.cursor}/${d.paper.totalBars} 根 · ${d.paper.currentTime||''}`;$('dashRunInfo').innerHTML=`<div class="summary-row"><span>策略</span><b>${esc(d.paper.strategyName)}</b></div><div class="summary-row"><span>品种</span><b>${esc(d.paper.symbol)}</b></div><div class="summary-row"><span>当前时间</span><b>${esc(d.paper.currentTime||'等待首根 bar')}</b></div><div class="summary-row"><span>未实现盈亏</span><b>${money(d.paper.unrealizedPnl)}</b></div><div class="summary-row"><span>最大回撤</span><b>${(+d.paper.maxDrawdownPct).toFixed(2)}%</b></div>`;draw(d.paper.equityCurve);}else{$('dashDataMeta').textContent='暂无模拟盘数据';$('dashRunInfo').innerHTML='<div class="empty">到「交易」页选一条策略和时间段，跑一次模拟就会显示。</div>';draw([]);}$('dashTrades').innerHTML=d.recentPaperTrades.length?d.recentPaperTrades.map(t=>`<tr><td>${esc(t.id)}</td><td>${esc(t.symbol)}</td><td>${esc(t.side)}</td><td>${esc(t.entryTime)}</td><td>${esc(t.exitTime)}</td><td class="${t.netPnl>=0?'positive':'negative'}">${money(t.netPnl)}</td></tr>`).join(''):'<tr><td colspan="6" class="empty">暂无成交</td></tr>';}catch(e){$('dashboardError').innerHTML=`<div class="note">${esc(e.message)}。请启动后端服务。</div>`;}}
  async function loadDiagnostics(){try{const r=await fetch('/api/diagnostics');const d=await r.json();if(!r.ok)throw Error(d.error||'诊断请求失败');if(!d.available){$('diagnosticsBody').innerHTML=`<div class="card empty">${esc(d.message)}</div>`;$('dailyBody').innerHTML=`<div class="card empty">${esc(d.message)}</div>`;return;}const card=(title,value,detail)=>`<div class="card diag-card"><h3>${esc(title)}</h3><div class="big">${esc(value)}</div><p>${esc(detail)}</p></div>`;$('diagnosticsBody').innerHTML=`<div class="grid diag-grid">${card('当前品种',d.symbol,d.strategyName)}${card('样本区间价格变化',d.priceChangePct==null?'—':`${d.priceChangePct}%`,`${d.startTime} 至 ${d.endTime}`)}${card('最近 20 根 ATR',d.atrLast20,`ATR/收盘价 ${d.atrPctLast20}%`)}</div><div class="grid diag-grid">${card('已平仓交易',d.tradeCount,'当前模拟回放')}${card('样本胜率',d.winRatePct==null?'—':`${d.winRatePct}%`,'仅对已平仓交易统计')}${card('最大回撤',`${d.maxDrawdownPct}%`,'逐 bar 模拟权益估算')}</div><div class="card"><div class="card-head"><h2>诊断范围与限制</h2><span class="tag amber">样本内统计</span></div><div class="status-line"><span>数据源</span><b>${esc(d.source)} · ${d.bars} 根 bar</b></div><div class="status-line"><span>成本</span><b>累计已记费用 ${money(d.costs)}</b></div><div class="status-line"><span>净盈亏</span><b>${money(d.closedNetPnl)}</b></div><div class="note">${esc(d.note)}</div></div>`;const daily=d.daily;$('dailyBody').innerHTML=`<div class="grid diag-grid">${card('诊断日期',daily.date||'—','来自当前回放的最后处理日期')}${card('当日已平仓交易',daily.closedTrades,'不含持仓中浮盈亏')}${card('当日已实现净盈亏',money(daily.netPnl),'按服务端记录计算')}</div><div class="card"><div class="card-head"><h2>当日信号事件</h2><span class="tag">${daily.signals.length} 条</span></div>${daily.signals.length?daily.signals.map(x=>`<div class="paper-signal"><b>${esc(x.timestamp)}</b><span>${esc(x.type)}</span><span>${esc(x.reason)}</span></div>`).join(''):'<div class="empty">当天暂无已记录信号事件。</div>'}<div class="note">${esc(d.note)}</div></div>`;}catch(e){$('diagnosticsBody').innerHTML=`<div class="card empty">${esc(e.message)}</div>`;$('dailyBody').innerHTML=`<div class="card empty">${esc(e.message)}</div>`;}}
  // ---- 实时 K 线组件 ---------------------------------------------------
  // 绘图复用 web/js/kline-core.js（与「实时 K 线」页同一份），这里只做状态与推送
  let klineView = null, klineStream = null, klineStreamSymbol = '', klineLast = null;
  const klineState = (text, tone) => { const el = $('dashKlineState'); el.textContent = text; el.className = 'tag ' + (tone || 'amber'); };
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

  function applyKlineBars(bars) {
    const view = ensureKlineView();
    if (!view || !bars || !bars.length) return;
    const clean = view.apply(bars);
    if (!clean.length) return;
    klineLast = clean[clean.length - 1];
    $('dashKlineEmpty').style.display = 'none';
    renderKlineQuote(klineLast);
  }

  function openKlineStream(symbol) {
    if (klineStream) { klineStream.close(); klineStream = null; }
    klineStreamSymbol = symbol;
    klineStream = new EventSource(`/api/live/stream?symbol=${encodeURIComponent(symbol)}`);
    klineStream.addEventListener('hello', event => {
      const data = JSON.parse(event.data);
      const bars = data.bars || [];
      if (bars.length) applyKlineBars(bars);
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

  async function syncKline() {
    if (!$('dashKlineChart')) return;
    try {
      const status = await (await fetch('/api/live/status')).json();
      if (!status.running) {
        if (klineStream) { klineStream.close(); klineStream = null; klineStreamSymbol = ''; }
        klineState('已停止', 'amber');
        // 停采后保留最后一次的图，不要用遮罩把已有数据盖掉
        if (klineView && klineLast) $('dashKlineMeta').textContent = '采集已停止 · 保留停止前的 K 线';
        else { $('dashKlineMeta').textContent = '未接入行情'; renderKlineQuote(null); }
        if (!klineLast) showKlineEmpty('尚未接入实时行情', '到「实时 K 线」页连接 SimNow 后，这里会自动显示');
        return;
      }
      const symbol = status.symbol || '';
      klineState(status.driver === 'ctp' ? 'CTP 实时行情' : `模拟源 ${status.speedup}×`, 'green');
      $('dashKlineMeta').textContent = `${symbol} · ${status.exchange || ''} · `
        + (status.driver === 'ctp' ? '只读订阅，不下单' : '模拟数据，不可用于决策');
      ensureKlineView();
      if (!klineStream || klineStreamSymbol !== symbol) openKlineStream(symbol);
    } catch (e) {
      klineState('行情不可用', 'amber');
    }
  }

  $('goLive').addEventListener('click', () => window.go?.('live'));
  window.addEventListener('qice-page-change', event => {
    if (event.detail !== 'overview') return;
    if (klineView) klineView.resize();
    if (klineStreamSymbol) { const symbol = klineStreamSymbol; klineStreamSymbol = ''; openKlineStream(symbol); }
    else syncKline();
  });
  window.addEventListener('resize', () => { if (klineView) klineView.resize(); });
  syncKline(); setInterval(syncKline, 5000);

  $('refreshDashboard').addEventListener('click',load);$('goPaper').addEventListener('click',()=>window.go?.('paper'));load();setInterval(load,5000);
  $('refreshDiagnostics').addEventListener('click',loadDiagnostics);$('refreshDaily').addEventListener('click',loadDiagnostics);document.querySelectorAll('.nav button').forEach(b=>b.addEventListener('click',()=>{if(b.dataset.page==='overview')load();if(['diagnostics','daily'].includes(b.dataset.page))loadDiagnostics();}));
})();
