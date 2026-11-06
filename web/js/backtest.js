/* 策略回测：**选品种 + 选时间区间 → 服务端拉真实历史行情 → 回放撮合**。
 *
 * 不再有本地行情文件、也不再有内置合成样例：
 *   1) 探测  GET  /api/market/history?symbol=&start=&end=&period=
 *      只回「这段真行情能不能取、有多少根、可用窗口从哪天开始」，不传 bars。
 *   2) 运行  POST /api/backtests  {range, config, strategy}
 *      服务端 `history.py` 按时间区间取真实历史 K 线（日线支持任意历史日期；
 *      分钟线上游只给最近约 1023 根，超出窗口会明确报错、不静默截断），
 *      再走与模拟盘同一条 `engine.TradingSession` 撮合路径。
 *
 * 口径必须诚实：这是真实行情的**历史回放 + 模拟撮合**，不是实时实盘，
 * 不发送任何真实委托；行情是主力连续，存在换月跳空/价差。
 */
(() => {
  const mount = document.getElementById('backtestMount');
  if (!mount) return;

  // 周期 → ATR/价格 建议阈值（%）。分钟级默认 0.4%，日线波动更大，
  // 沿用 0.4% 会把几乎所有信号过滤掉，表现为「跑完 0 笔交易」。用户手改过就不覆盖。
  const ATR_HINT = {'1d': 3.0, '60m': 1.2, '30m': 0.9, '15m': 0.6, '5m': 0.4, '1m': 0.2};
  const PERIOD_NOTE = {
    '1d': '日线：支持任意历史区间（主力连续）',
    '60m': '60 分钟：上游约 9 个月窗口',
    '30m': '30 分钟：上游约 4~5 个月窗口',
    '15m': '15 分钟：上游约 2~3 个月窗口',
    '5m': '5 分钟：上游约 3 周窗口',
    '1m': '1 分钟：上游约 4 个交易日窗口'
  };

  mount.innerHTML = `
  <div class="heading"><div><h1>策略回测</h1><p>选品种和时间区间，服务端按时间拉取真实历史行情回放；结果连同数据源一起保存。</p></div><div class="toolbar"><select id="btStrategySelect" class="select"><option value="">从策略库选择…</option></select><button class="btn" id="loadBtStrategy">载入所选策略</button><span class="tag amber" id="backtestApiState">连接服务中</span><button class="btn" id="exportBacktest">导出交易明细</button></div></div>
  <div class="grid backtest-grid"><div class="card"><div class="card-head"><h2>回测设置</h2><span class="sub">真实历史行情 · 参数与结果一并记录</span></div>

  <div class="section-title">① 行情区间</div>
  <div class="bt-controls bt-range">
  <div class="field"><label for="btSectorInput">交易品类</label><div class="combo"><input id="btSectorInput" class="combo-input" type="text" autocomplete="off" spellcheck="false" value="全部品类" placeholder="搜索品类，如 黑色 / nh"><div class="combo-panel" id="btSectorPanel" hidden></div></div><small id="btSectorHint">点一下选品类，也可以直接搜</small></div>
  <div class="field"><label for="btCodeInput">交易品种</label><div class="combo"><input id="btCodeInput" class="combo-input" type="text" autocomplete="off" spellcheck="false" placeholder="搜索代码 / 中文名，如 rb、螺纹"><input id="btSymbol" type="hidden" value="螺纹钢 · RB"><div class="combo-panel" id="btCodePanel" hidden></div></div><small id="btCodeHint">品种代码 / 中文名 / 交易所都支持精准与模糊搜索</small></div>
  <div class="field"><label for="btStart">开始日期</label><input id="btStart" type="date"></div>
  <div class="field"><label for="btEnd">结束日期</label><input id="btEnd" type="date"></div>
  <div class="field"><label for="btPeriod">K 线周期</label><select id="btPeriod" class="select"><option value="1d">日线（任意历史区间）</option><option value="60m">60 分钟</option><option value="30m">30 分钟</option><option value="15m">15 分钟</option><option value="5m">5 分钟</option><option value="1m">1 分钟</option></select><small id="btPeriodNote">日线：支持任意历史区间（主力连续）</small></div>
  <div class="field"><label>快捷区间</label><div class="bt-quick"><button class="btn" type="button" data-days="30">近 1 月</button><button class="btn" type="button" data-days="90">近 3 月</button><button class="btn" type="button" data-days="180">近 6 月</button><button class="btn" type="button" data-days="365">近 1 年</button><button class="btn" type="button" data-days="ytd">今年</button></div></div>
  <div class="field full"><div class="bt-source busy" id="btSource">正在读取真实行情…</div><div class="bt-source-actions"><button class="btn" type="button" id="btUseWindow" hidden>按可用区间回测</button><button class="btn" type="button" id="btRefresh">刷新行情</button></div></div>
  </div>

  <div class="section-title">② 策略与成本</div>
  <div class="bt-controls">
  <div class="field"><label>均线类型</label><select id="btMaType" class="select"><option value="EMA">EMA</option><option value="SMA">SMA</option></select></div><div class="field"><label>快线周期</label><input id="btFast" type="number" min="2" value="9"></div><div class="field"><label>慢线周期</label><input id="btSlow" type="number" min="3" value="21"></div><div class="field"><label>ATR 周期</label><input id="btAtrPeriod" type="number" min="2" value="14"></div><div class="field"><label>最大 ATR / 价格（%）</label><input id="btAtrMax" type="number" min="0" step="0.05" value="3.0"><small>波动超过阈值时不新开仓，按周期给建议值</small></div><div class="field"><label>每边手续费 / 手</label><input id="btFee" type="number" min="0" step="0.1" value="2"><small>参考值，按你自己的费率表确认</small></div><div class="field"><label>单边滑点（价格点）</label><input id="btSlip" type="number" min="0" step="0.1" value="1"><small>默认取该品种最小变动价位</small></div><div class="field"><label>合约乘数</label><input id="btMult" type="number" min="0.01" step="0.1" value="10"><small>选品种后自动带出，可覆盖</small></div><div class="field"><label>每笔手数</label><input id="btQty" type="number" min="1" step="1" value="1"></div><div class="field"><label>初始权益</label><input id="btCapital" type="number" min="1" step="10000" value="1000000"></div><div class="field"><label>训练段 / 测试段</label><select id="btSplit" class="select"><option value="70">70% / 30%</option><option value="60">60% / 40%</option><option value="80">80% / 20%</option></select></div><button id="runBacktest" class="btn primary field full">▶　按所选区间运行回测</button></div>
  <div id="btError" class="metric-tip" style="color:#c4545c"></div><div class="note">信号在 bar 收盘确认、下一根开盘模拟成交；测试段边界强制清仓。真实行情为公开主力连续数据，分钟线有可得窗口限制；结果为模拟撮合，不含保证金、涨跌停、换月与真实成交规则。</div></div>

  <div><div class="grid bt-kpis"><div class="card bt-kpi"><div class="kpi-top">净收益率</div><div class="value" id="btReturn">—</div><div class="metric-tip">全样本，已扣模拟成本</div></div><div class="card bt-kpi"><div class="kpi-top">最大回撤</div><div class="value" id="btMdd">—</div><div class="metric-tip">逐 bar 权益估算</div></div><div class="card bt-kpi"><div class="kpi-top">胜率</div><div class="value" id="btWin">—</div><div class="metric-tip">已平仓交易</div></div><div class="card bt-kpi"><div class="kpi-top">盈亏因子</div><div class="value" id="btPf">—</div><div class="metric-tip">盈利总额 / 亏损总额</div></div><div class="card bt-kpi"><div class="kpi-top">交易数</div><div class="value" id="btCount">—</div><div class="metric-tip">已平仓</div></div></div>
  <div class="card"><div class="card-head"><div><h2>权益曲线与回撤</h2><div class="sub" id="btRange">等待运行 · 策略权益对照买入持有基准</div></div><div class="chart-legend"><span><i class="key"></i>策略</span><span><i class="key gray"></i>基准</span></div></div><div class="bt-chart" id="btChart"><div class="empty">选好品种和时间区间后运行回测，生成结果图表</div></div></div>
  <div class="grid bottom-grid" style="margin-top:15px"><div class="card"><div class="card-head"><h2>训练段 / 测试段</h2><span class="split-pill" id="btSplitLabel">未运行</span></div><div class="table-scroll"><table class="table"><thead><tr><th>区间</th><th>交易数</th><th>净收益</th><th>最大回撤</th><th>胜率</th><th>盈亏因子</th></tr></thead><tbody id="btSegments"><tr><td colspan="6" class="empty">等待回测结果</td></tr></tbody></table></div></div><div class="card"><div class="card-head"><h2>回测质量检查</h2><span class="tag amber">模拟撮合</span></div><div class="bt-stat"><span>行情来源</span><b>真实历史 · 主力连续</b></div><div class="bt-stat"><span>成交规则</span><b>信号后下一根开盘</b></div><div class="bt-stat"><span>交易成本</span><b>双边费用 + 固定滑点</b></div><div class="bt-stat"><span>分段方式</span><b>时间顺序训练 / 测试</b></div><div class="bt-stat"><span>未建模规则</span><b>保证金、涨跌停、换月</b></div><div class="section-title">最近回测</div><div id="btHistory" class="metric-tip">等待服务连接</div></div></div>
  <div class="card" style="margin-top:15px"><div class="card-head"><h2>逐笔回测交易</h2><span class="sub" id="btTradeCount">暂无记录</span></div><div class="table-scroll bt-table"><table class="table"><thead><tr><th>区间</th><th>方向</th><th>入场时间 / 价</th><th>退出时间 / 价</th><th>毛盈亏</th><th>费用</th><th>净盈亏</th><th>退出原因</th></tr></thead><tbody id="btTrades"><tr><td colspan="8" class="empty">运行回测后显示</td></tr></tbody></table></div></div><div class="footer-note">使用公开真实历史行情做模拟回放，不代表未来表现，也不构成投资建议。请核对品种乘数、费率、交易时段、换月和涨跌停规则。</div></div></div>`;

  const $ = id => document.getElementById(id);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
  const money = x => '¥' + Math.round(x).toLocaleString('zh-CN');
  let lastResult = null, lastTrades = [], activeStrategy = null,
      probeData = null, probeToken = 0, probeTimer = null;
  let costTouched = false, atrTouched = false;

  /* ---------- 日期：统一用北京时间，避免 UTC 差一天 ---------- */
  const today = () => new Date(Date.now() + 8 * 3600 * 1000).toISOString().slice(0, 10);
  const daysAgo = n => {
    const d = new Date(today() + 'T00:00:00Z');
    d.setUTCDate(d.getUTCDate() - n);
    return d.toISOString().slice(0, 10);
  };
  $('btEnd').value = today();
  $('btStart').value = daysAgo(365);

  /* ---------- 品种选择器：品类 + 品种都支持精准/模糊搜索 ---------- */
  const picker = window.qiceInstrumentPicker
    ? window.qiceInstrumentPicker.create({
        sectorInput: 'btSectorInput', sectorPanel: 'btSectorPanel', sectorHint: 'btSectorHint',
        codeInput: 'btCodeInput', codePanel: 'btCodePanel', hidden: 'btSymbol', hint: 'btCodeHint',
        onChange: inst => { if (inst) applyInstrument(inst); scheduleProbe(); }
      })
    : null;
  if (!picker) $('btCodeHint').textContent = '品种选择组件未加载，请刷新页面。';

  const currentSymbol = () => (picker && picker.symbol()) || $('btSymbol').value || '';

  function applyInstrument(inst) {
    if (!costTouched) {
      if (inst.multiplier) $('btMult').value = inst.multiplier;
      if (inst.tick) $('btSlip').value = inst.tick;
    }
    $('btCodeHint').textContent = `当前：${inst.name} · ${inst.code.toUpperCase()} · ${inst.exchangeName}/${inst.sector} · 乘数 ${inst.multiplier} / 最小变动 ${inst.tick}`;
    $('btCodeHint').className = '';
  }

  /* ---------- 行情可用性探测：改品种/日期/周期后自动刷新 ---------- */
  function paintSource(cls, html) {
    $('btSource').className = 'bt-source ' + cls;
    $('btSource').innerHTML = html;
    $('btUseWindow').hidden = !(probeData && !probeData.ok && probeData.availableFrom);
  }

  function scheduleProbe(delay = 350) {
    clearTimeout(probeTimer);
    probeTimer = setTimeout(() => probe(false), delay);
  }

  async function probe(force) {
    const symbol = currentSymbol();
    const token = ++probeToken;
    if (!symbol) { probeData = null; paintSource('bad', '请先从品种选择器里选一个交易品种。'); return; }
    const params = new URLSearchParams({symbol, start: $('btStart').value,
      end: $('btEnd').value, period: $('btPeriod').value});
    if (force) params.set('force', '1');
    paintSource('busy', '正在读取真实历史行情…');
    try {
      const response = await fetch('/api/market/history?' + params.toString());
      const data = await response.json();
      if (token !== probeToken) return;               // 旧请求回来时不能覆盖新选择
      probeData = data;
      if (!data.ok) {
        paintSource('bad', `<b>该区间暂不可用</b><span>${esc(data.reason || '行情源不可用')}</span>` +
          (data.availableFrom ? `<span>可得窗口：${esc(data.availableFrom)} ~ ${esc(data.availableTo)}，可点「按可用区间回测」。</span>` : ''));
        return;
      }
      const warning = (data.warnings || []).length ? `<span class="warn-line">${esc(data.warnings.join('；'))}</span>` : '';
      paintSource('ok', `<b>${esc(data.symbol || symbol)} · ${esc(data.periodLabel || '')} · ${data.count} 根真实 K 线</b>` +
        `<span>${esc(data.startTime.slice(0, 10))} ~ ${esc(data.endTime.slice(0, 10))} · ${esc(data.source || '')}</span>` +
        (data.period !== '1d' ? `<span>该周期可得窗口 ${esc(data.availableFrom)} ~ ${esc(data.availableTo)}（上游约 ${data.coverage?.windowBars || '—'} 根）</span>` : '') + warning);
    } catch {
      if (token !== probeToken) return;
      probeData = null;
      paintSource('bad', '本机服务未连接，无法读取真实行情；请启动服务后点「刷新行情」。');
    }
  }

  $('btStart').addEventListener('change', () => scheduleProbe(150));
  $('btEnd').addEventListener('change', () => scheduleProbe(150));
  $('btPeriod').addEventListener('change', () => {
    $('btPeriodNote').textContent = PERIOD_NOTE[$('btPeriod').value] || '';
    if (!atrTouched) $('btAtrMax').value = ATR_HINT[$('btPeriod').value] ?? 0.4;
    scheduleProbe(150);
  });
  $('btAtrMax').addEventListener('input', () => { atrTouched = true; });
  ['btFee', 'btSlip', 'btMult'].forEach(id => $(id).addEventListener('input', () => { costTouched = true; }));
  document.querySelectorAll('.bt-quick button').forEach(btn => btn.addEventListener('click', () => {
    const n = btn.dataset.days;
    $('btEnd').value = today();
    $('btStart').value = n === 'ytd' ? today().slice(0, 4) + '-01-01' : daysAgo(+n);
    scheduleProbe(100);
  }));
  $('btUseWindow').addEventListener('click', () => {
    if (!probeData?.availableFrom) return;
    $('btStart').value = probeData.availableFrom;
    $('btEnd').value = probeData.availableTo || today();
    scheduleProbe(100);
  });
  $('btRefresh').addEventListener('click', () => { clearTimeout(probeTimer); probe(true); });

  /* ---------- 运行回测：只发区间，行情由服务端按时间取 ---------- */
  async function run() {
    const symbol = currentSymbol(), start = $('btStart').value,
      end = $('btEnd').value, period = $('btPeriod').value;
    const fast = +$('btFast').value, slow = +$('btSlow').value, ap = +$('btAtrPeriod').value,
      atrMax = +$('btAtrMax').value / 100, fee = +$('btFee').value, slip = +$('btSlip').value,
      mult = +$('btMult').value, qty = +$('btQty').value, capital = +$('btCapital').value,
      type = $('btMaType').value;
    $('btError').textContent = '';
    if (!symbol) { $('btError').textContent = '请先选择交易品种。'; return; }
    if (!start || !end) { $('btError').textContent = '请选择回测开始和结束日期。'; return; }
    if (['btFee', 'btSlip', 'btMult'].some(id => $(id).value === '')) {
      $('btError').textContent = '请填写本合约每边手续费、单边滑点和合约乘数后再回测。'; return;
    }
    if (fast < 2 || slow <= fast || ap < 2 || [fee, slip, atrMax].some(x => x < 0) ||
        mult <= 0 || qty <= 0 || capital <= 0) {
      $('btError').textContent = '请检查参数：快线小于慢线，费用非负且乘数/手数/初始权益大于零。'; return;
    }
    const instrument = picker?.instrument?.();
    const cfg = {fast, slow, atrPeriod: ap, atrMaxPct: +$('btAtrMax').value,
      feePerSide: fee, slippage: slip, multiplier: mult, qty, capital,
      splitPct: +$('btSplit').value, maType: type,
      atr: activeStrategy ? !!activeStrategy.atr : true,
      rsi: !!activeStrategy?.rsi, rsiPeriod: activeStrategy?.rsiPeriod || 14,
      rsiUpper: activeStrategy?.rsiUpper || 70,
      tickSize: instrument?.tick || 1,
      useBlackout: false};       // 真实行情没有人工事件禁入标记
    const range = {symbol, start, end, period};
    $('runBacktest').disabled = true;
    $('runBacktest').textContent = '正在读取真实行情并回放…';
    try {
      const response = await fetch('/api/backtests', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({range, config: cfg,
          strategyId: activeStrategy?.id || null, strategy: activeStrategy || null})
      });
      let result;
      try { result = await response.json(); } catch { result = {error: '服务返回了无法识别的结果。'}; }
      if (!response.ok) {
        $('btError').textContent = result.error || '后端回测失败。';
        $('backtestApiState').textContent = '行情源异常';
        $('backtestApiState').className = 'tag amber';
        return;
      }
      $('backtestApiState').textContent = '本机服务端已连接';
      $('backtestApiState').className = 'tag green';
      renderBackendResult(result);
      loadHistory();
    } catch {
      $('btError').textContent = '本机服务未连接，回测未执行。请启动服务后重试。';
    } finally {
      $('runBacktest').disabled = false;
      $('runBacktest').textContent = '▶　按所选区间运行回测';
    }
  }

  /* ---------- 结果渲染：数据源随结果落库，回放旧记录不依赖本地 bars ---------- */
  function renderBackendResult(result) {
    lastResult = result;
    const s = result.summary || {};
    $('btReturn').textContent = (+s.returnPct).toFixed(2) + '%';
    $('btReturn').className = 'value ' + (s.netPnl >= 0 ? 'positive' : 'negative');
    $('btMdd').textContent = (+s.maxDrawdownPct).toFixed(2) + '%';
    $('btWin').textContent = (+s.winRatePct).toFixed(1) + '%';
    $('btPf').textContent = s.profitFactor === 'Infinity' ? '∞' : s.profitFactor == null ? '—' : (+s.profitFactor).toFixed(2);
    $('btCount').textContent = s.tradeCount || 0;
    $('btTradeCount').textContent = `回测编号 ${result.id.slice(0, 8)} · ${s.tradeCount || 0} 笔 · 服务端计算并保存`;
    $('btSplitLabel').textContent = `训练 ${result.splitIndex} 根 / 测试 ${result.sourceBars - result.splitIndex} 根`;
    const left = (result.startTime || result.requestedRange?.start || '').slice(0, 10);
    const right = (result.endTime || result.requestedRange?.end || '').slice(0, 10);
    const head = [result.strategyName ? '策略「' + result.strategyName + '」 · ' : '',
      result.symbol ? result.symbol + ' · ' : '',
      result.periodLabel ? result.periodLabel + ' · ' : ''].join('');
    $('btRange').textContent = `${head}${left} 至 ${right} · ${result.sourceBars} 根真实 K 线 · 净收益 ${s.netPnl >= 0 ? '+' : ''}${money(s.netPnl || 0)}`;
    if (result.dataSource) {
      paintSource('ok', `<b>本次回测：${esc(result.dataSource)}</b>` +
        `<span>${esc(result.symbol || '')} · ${esc(result.periodLabel || '')} · ${left} ~ ${right} · ${result.sourceBars} 根</span>` +
        (result.warnings?.length ? `<span class="warn-line">${esc(result.warnings.join('；'))}</span>` : ''));
    }
    drawChart(result.curve || [], result.splitIndex || 0, result.startTime, result.endTime);
    $('btSegments').innerHTML = (result.segments || []).map(m => `<tr><td>${esc(m.name)}</td><td>${m.tradeCount}</td><td class="${m.netPnl >= 0 ? 'positive' : 'negative'}">${m.netPnl >= 0 ? '+' : ''}${money(m.netPnl)} (${(+m.returnPct).toFixed(2)}%)</td><td>${(+m.maxDrawdownPct).toFixed(2)}%</td><td>${(+m.winRatePct).toFixed(1)}%</td><td>${m.profitFactor === 'Infinity' ? '∞' : m.profitFactor == null ? '—' : (+m.profitFactor).toFixed(2)}</td></tr>`).join('');
    lastTrades = result.trades || [];
    renderBtTrades(lastTrades);
  }

  async function loadHistory() {
    try {
      const res = await fetch('/api/backtests');
      if (!res.ok) throw Error();
      $('backtestApiState').textContent = '服务已连接';
      $('backtestApiState').className = 'tag green';
      const runs = await res.json();
      $('btHistory').innerHTML = runs.length ? runs.slice(0, 5).map(r => {
        const label = [r.strategyName || '未命名策略', r.symbol, r.periodLabel].filter(Boolean).join(' · ');
        return `<button class="btn history-run" data-id="${esc(r.id)}" style="display:flex;width:100%;justify-content:space-between;margin:5px 0"><span>${esc(label)} · ${new Date(r.createdAt).toLocaleString('zh-CN')}</span><b class="${r.summary?.netPnl >= 0 ? 'positive' : 'negative'}">${(+r.summary.returnPct).toFixed(2)}%</b></button>`;
      }).join('') : '尚无已保存回测';
      $('btHistory').querySelectorAll('[data-id]').forEach(b => b.addEventListener('click', async () => {
        try {
          const response = await fetch('/api/backtests/' + encodeURIComponent(b.dataset.id));
          const result = await response.json();
          if (response.ok) renderBackendResult(result);
        } catch { /* 旧记录读不到不影响页面其它部分 */ }
      }));
    } catch {
      $('backtestApiState').textContent = '后端未连接';
      $('backtestApiState').className = 'tag amber';
      $('btHistory').textContent = '后端未连接，运行记录暂不可用。';
    }
  }

  function drawChart(points, split, startTime, endTime) {
    if (!points.length) return;
    const W = 760, H = 255, pad = 32, vals = points.flatMap(p => [p.equity, p.benchmark]),
      lo = Math.min(...vals), hi = Math.max(...vals), range = hi - lo || 1,
      x = i => pad + i / (points.length - 1) * (W - pad * 2),
      y = v => H - 35 - (v - lo) / range * (H - 55),
      path = key => points.map((p, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)} ${y(p[key]).toFixed(1)}`).join(' '),
      markX = x(Math.max(0, Math.min(points.length - 1, split - 1))),
      left = (startTime || '').slice(0, 10) || '起点', right = (endTime || '').slice(0, 10) || '终点';
    $('btChart').innerHTML = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none"><line class="axis" x1="${pad}" y1="${y(lo)}" x2="${W - pad}" y2="${y(lo)}"/><line class="axis" x1="${pad}" y1="${y((lo + hi) / 2)}" x2="${W - pad}" y2="${y((lo + hi) / 2)}"/><line class="axis" x1="${pad}" y1="${y(hi)}" x2="${W - pad}" y2="${y(hi)}"/><path d="${path('benchmark')}" fill="none" stroke="#aeb9cb" stroke-width="1.8" stroke-dasharray="5 4"/><path d="${path('equity')}" fill="none" stroke="#5277ee" stroke-width="2.4"/><line x1="${markX}" y1="12" x2="${markX}" y2="${H - 28}" stroke="#d7a34e" stroke-dasharray="4 4"/><text x="${Math.min(markX + 5, W - 110)}" y="18" fill="#a97924" font-size="10">训练 / 测试切分</text><text x="2" y="${y(hi) + 3}" fill="#929db0" font-size="10">${money(hi)}</text><text x="2" y="${y(lo) + 3}" fill="#929db0" font-size="10">${money(lo)}</text><text x="${pad}" y="${H - 8}" fill="#929db0" font-size="10">${left}</text><text x="${W - 115}" y="${H - 8}" fill="#929db0" font-size="10">${right}</text></svg>`;
  }

  function renderBtTrades(ts) {
    $('btTrades').innerHTML = ts.length ? ts.slice().reverse().map(t => {
      const entry = t.entry ?? t.entryPrice, exit = t.exit ?? t.exitPrice,
        gross = t.gross ?? t.grossPnl, net = t.net ?? t.netPnl, reason = t.reason ?? t.exitReason;
      return `<tr><td>${esc(t.segment)}</td><td>${esc(t.side)}</td><td>${esc(t.entryTime.replace('T', ' ').slice(0, 16))} / ${(+entry).toFixed(2)}</td><td>${esc(t.exitTime.replace('T', ' ').slice(0, 16))} / ${(+exit).toFixed(2)}</td><td>${money(gross)}</td><td>${money(t.fees)}</td><td class="${net >= 0 ? 'positive' : 'negative'}">${net >= 0 ? '+' : ''}${money(net)}</td><td>${esc(reason)}</td></tr>`;
    }).join('') : '<tr><td colspan="8" class="empty">没有符合条件的交易；可尝试放宽 ATR 阈值或延长区间</td></tr>';
  }

  $('runBacktest').addEventListener('click', run);
  $('exportBacktest').addEventListener('click', () => {
    if (!lastTrades.length) return;
    // 结果导出用 JSON：连同数据源、区间、参数一起带走，方便事后复盘对齐口径。
    const r = lastResult || {};
    const payload = {
      exportedAt: new Date().toISOString(), id: r.id, strategyName: r.strategyName,
      dataSource: r.dataSource, symbol: r.symbol, continuous: r.continuous,
      period: r.period, periodLabel: r.periodLabel,
      requestedRange: r.requestedRange, coverage: r.coverage,
      startTime: r.startTime, endTime: r.endTime, sourceBars: r.sourceBars,
      summary: r.summary, segments: r.segments, parameters: r.parameters,
      notice: r.notice, trades: lastTrades
    };
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([JSON.stringify(payload, null, 2)],
      {type: 'application/json;charset=utf-8'}));
    a.download = '策略回测明细.json';
    a.click();
    URL.revokeObjectURL(a.href);
  });

  /* ---------- 与策略库联动 ---------- */
  window.applyStrategyToBacktest = strategy => {
    activeStrategy = {...strategy,
      rsiPeriod: +((strategy.rsiParams || '14').match(/\d+/) || [14])[0],
      rsiUpper: +((strategy.rsiParams || '14 · 30 / 70').match(/\d+\s*$/) || [70])[0]};
    if (strategy.fast) $('btFast').value = strategy.fast;
    if (strategy.slow) $('btSlow').value = strategy.slow;
    if (strategy.maType) $('btMaType').value = strategy.maType;
    if (strategy.atrPeriod) $('btAtrPeriod').value = strategy.atrPeriod;
    if (strategy.atrMaxPct !== undefined) { $('btAtrMax').value = strategy.atrMaxPct; atrTouched = true; }
    if (strategy.feePerSide !== undefined) { $('btFee').value = strategy.feePerSide; costTouched = true; }
    if (strategy.slippage !== undefined) { $('btSlip').value = strategy.slippage; costTouched = true; }
    if (strategy.multiplier !== undefined) { $('btMult').value = strategy.multiplier; costTouched = true; }
    if (strategy.qty !== undefined) $('btQty').value = strategy.qty;
    let symbolNote = '';
    if (strategy.symbol && picker) {
      const before = picker.symbol();
      picker.setSymbol(strategy.symbol);
      // 老策略里存过「黄金」这类名字却配着别的合约代码，选择器认不出时会把
      // 当前选择清空；这里退回原品种，别让「载入策略」把回测品种弄丢。
      if (!picker.symbol()) {
        picker.setSymbol(before);
        symbolNote = ` 策略记录的「${strategy.symbol}」无法识别，已沿用当前品种 ${before}。`;
      }
    }
    const unsupported = [strategy.macd && 'MACD', strategy.bollinger && '布林带'].filter(Boolean);
    $('btError').textContent = '已载入策略“' + strategy.name + '”；当前回测执行均线' +
      (strategy.atr ? '、ATR波动门槛' : '') + (strategy.rsi ? '、RSI 超买过滤' : '') + '。' +
      (unsupported.length ? '已保存的 ' + unsupported.join('、') + ' 条件暂不参与计算。' : '') +
      '选好品种与时间区间即可运行。' + symbolNote;
    scheduleProbe(100);
  };
  window.refreshBacktestStrategyOptions = strategies => {
    const sel = $('btStrategySelect'), prev = sel.value;
    sel.innerHTML = '<option value="">从策略库选择…</option>' +
      strategies.map(s => `<option value="${s.id}">${esc(s.name)}</option>`).join('');
    if (strategies.some(s => s.id === prev)) sel.value = prev;
  };
  $('loadBtStrategy').addEventListener('click', () => {
    const s = window.strategyLibrary?.get($('btStrategySelect').value);
    if (!s) { $('btError').textContent = '请先从策略库选择一个已保存策略。'; return; }
    window.applyStrategyToBacktest(s);
  });
  window.addEventListener('strategy-library-updated', e => window.refreshBacktestStrategyOptions(e.detail || []));
  window.addEventListener('qice-page-change', e => { if (e.detail === 'backtest' && !probeData) scheduleProbe(100); });

  loadHistory();
  scheduleProbe(100);
})();
