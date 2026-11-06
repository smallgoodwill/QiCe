/**
 * 策略模拟盘（区间一次跑完）
 *
 * 页面只做三件事：选策略 + 选时间段 → 服务端整套跑完 → 把买卖点和盈亏摊开。
 * 不再要求导入 CSV：行情要么由服务端按交易时段合成（演示），要么来自本机 CTP 采集库。
 * 绘图复用 kline-core，和「实时 K 线」「仪表盘」是同一份样式与坐标逻辑。
 */
(() => {
  const mount = document.getElementById('paperMount');
  const $ = id => document.getElementById(id);
  const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
  const cash = v => v == null ? '—' : (v < 0 ? '−' : v > 0 ? '+' : '') + '¥ ' + Math.abs(+v || 0).toLocaleString('zh-CN', {maximumFractionDigits: 2});
  const plain = v => v == null ? '—' : (v < 0 ? '−' : '') + '¥ ' + Math.abs(+v || 0).toLocaleString('zh-CN', {maximumFractionDigits: 2});
  const price = v => v == null ? '—' : Number(v).toLocaleString('zh-CN', {minimumFractionDigits: 2, maximumFractionDigits: 2});
  const pct = (v, d = 2) => v == null ? '—' : (+v).toFixed(d) + '%';
  const stamp = v => String(v || '').replace('T', ' ').slice(0, 16);
  const dayText = v => String(v || '').slice(0, 10);

  // 交易模式：paper=区间模拟（默认），live=实盘（通道未接入，仅展示前置条件）
  const MODE_KEY = 'qice.tradeMode';
  let mode = localStorage.getItem(MODE_KEY) === 'live' ? 'live' : 'paper';

  const REQUIREMENTS = [
    ['柜台接入', '交易前置/经纪商 API 授权、登录与结算确认，断线自动重连。'],
    ['委托与回报', '下单、撤单、部分成交、拒单与错单处理，回报逐笔落库。'],
    ['持仓与资金对账', '柜台持仓/资金与本地账本定时核对，差异即冻结交易。'],
    ['风控闸门', '单笔风险 ≤ 账户 1%、单日亏损停机、最大持仓与价格偏离校验，前置拦截。'],
    ['合约规则', '交易时段与夜盘、涨跌停、保证金、最小变动价位、换月处理。'],
    ['审计留痕', '每笔委托记录决策依据、参数快照与人工复核结果，可回放追溯。']
  ];

  mount.innerHTML = `
  <div class="heading"><div><h1>策略模拟盘</h1><p>选一条策略和一段时间，一次跑完：买卖点、逐笔盈亏、每笔持仓时长都在这一页看完。</p></div><div class="toolbar"><span class="tag amber" id="simStateTag">连接服务中</span><div class="mode-switch" role="tablist" aria-label="交易模式"><button type="button" role="tab" data-mode="paper"><i class="dot"></i>模拟盘</button><button type="button" role="tab" data-mode="live"><i class="dot"></i>实盘</button></div></div></div>

  <div class="mode-panel" id="modePaper">
    <div class="grid sim-layout">
      <div class="card">
        <div class="card-head"><h2>模拟设置</h2><span class="sub">一次算完，不用等</span></div>
        <div class="field"><label>策略</label><select id="simStrategy" class="select"><option value="">内置均线策略（EMA 9 / 21）</option></select><small id="simStrategyNote">不选策略时用内置 EMA 9/21 跑。</small></div>
        <div class="field" style="margin-top:13px"><label>行情来源</label><div class="seg" id="simSource"></div><small id="simSourceNote">演示行情用于验证策略与界面，不代表真实走势。</small><div id="simSymbolWrap" style="margin-top:10px"><label style="display:block;font-size:11px;color:#6f7b8e;margin-bottom:5px">交易品种</label><div style="display:flex;gap:6px"><select id="simSector" class="select" style="flex:0 0 74px" title="板块"></select><select id="simCode" class="select" style="flex:1" title="品种"></select><select id="simContract" class="select" style="flex:1" title="合约"></select></div><small id="simInstrumentNote">乘数、最小变动价位按品种自动套用。</small></div></div>
        <div class="field" style="margin-top:13px"><label>时间段</label><div class="quick" id="simQuick"><button data-days="1">最近 1 个交易日</button><button data-days="3">最近 3 个</button><button data-days="5">最近 5 个</button><button data-days="10">最近 10 个</button></div><div class="sim-dates"><input type="date" id="simStart"><span class="sub">至</span><input type="date" id="simEnd"></div><div class="sim-range" id="simRangeNote">选择交易日区间</div></div>
        <details class="sim-adv"><summary>合约与成本参数</summary><div class="form-grid"><div class="field"><label>初始资金</label><input id="simCapital" type="number" min="1" step="10000" value="1000000"></div><div class="field"><label>合约乘数</label><input id="simMult" type="number" min="0.01" step="0.1" value="300"></div><div class="field"><label>每笔手数</label><input id="simQty" type="number" min="1" step="1" value="1"></div><div class="field"><label>每边手续费 / 手</label><input id="simFee" type="number" min="0" step="0.1" value="23"></div><div class="field"><label>最小变动价位</label><input id="simTick" type="number" min="0.0001" step="0.1" value="0.2"></div><div class="field"><label>单边滑点（跳）</label><input id="simSlip" type="number" min="0" step="1" value="1"></div><div class="field"><label>ATR 波动上限（%）</label><input id="simAtrMax" type="number" min="0" step="0.05" value="0.4"><small>超过则不新开仓</small></div><label class="checkline"><input id="simBlackout" type="checkbox" checked> 跳过事件禁入 bar</label></div></details>
        <div class="toolbar" style="margin-top:15px"><button class="btn primary" id="simRun">▶　开始模拟</button><span class="sub" id="simHint"></span></div>
        <div id="simMessage" class="metric-tip"></div>
      </div>

      <div>
        <div class="grid sim-kpis">
          <div class="card"><div class="kpi-top">净盈亏</div><div class="value" id="simPnl">—</div><div class="metric-tip" id="simPnlTip">已扣双边费用与滑点</div></div>
          <div class="card"><div class="kpi-top">收益率</div><div class="value" id="simReturn">—</div><div class="metric-tip">相对初始资金</div></div>
          <div class="card"><div class="kpi-top">胜率</div><div class="value" id="simWin">—</div><div class="metric-tip" id="simWinTip">已平仓交易</div></div>
          <div class="card"><div class="kpi-top">盈亏因子</div><div class="value" id="simPf">—</div><div class="metric-tip">盈利总额 / 亏损总额</div></div>
          <div class="card"><div class="kpi-top">最大回撤</div><div class="value" id="simMdd">—</div><div class="metric-tip">逐 bar 权益估算</div></div>
          <div class="card"><div class="kpi-top">交易笔数</div><div class="value" id="simCount">—</div><div class="metric-tip" id="simCountTip">手续费合计 —</div></div>
        </div>
        <div class="card" style="margin-top:16px">
          <div class="card-head"><div><h2>K 线 + 买卖点</h2><div class="sub" id="simResultTitle">还没有结果 · 左侧设置好点「开始模拟」</div></div><div class="toolbar"><div class="seg" id="simZoom"><button type="button" data-zoom="all" class="active">全部</button><button type="button" data-zoom="day">近 1 日</button><button type="button" data-zoom="hour">近 1 小时</button></div><button class="btn" id="simMarkerToggle" title="切换是否在 K 线上标注买卖点">交易点：隐藏</button><span class="tag" id="simSourceTag" style="display:none">—</span></div></div>
          <div class="kline-wrap compact"><div id="simKline" class="kline-canvas"></div><div class="marker-layer" id="simMarkerLayer"></div><div class="kline-empty" id="simKlineEmpty"><b>买卖点会画在这里</b><span>红色为买入开仓，绿色为卖出平仓，鼠标悬停看成交价与时间</span></div></div>
          <div class="chart-note" id="simKlineNote">交易点默认隐藏，保持走势干净；点下方「逐笔明细」任意一行即可在图上定位该笔。</div>
        </div>
      </div>
    </div>

    <div class="card" style="margin-top:16px"><div class="card-head"><div><h2>权益曲线</h2><div class="sub" id="simEquityMeta">策略权益与买入持有基准对照</div></div><div class="chart-legend"><span><i class="key"></i>策略</span><span><i class="key gray"></i>基准</span></div></div><div class="chart-wrap" id="simEquity"><div class="empty">运行模拟后显示</div></div></div>

    <div class="card" style="margin-top:16px"><div class="card-head"><div><h2>逐笔成交明细</h2><div class="sub" id="simTradeMeta">开仓、平仓、持仓时长与每笔盈亏</div></div><div class="toolbar"><span class="tag" id="simTradeCount">0 笔</span><button class="btn" id="simExport">导出 CSV</button></div></div><div class="table-scroll sim-tbl"><table class="table"><thead><tr><th>#</th><th>方向</th><th>开仓时间 / 价</th><th>平仓时间 / 价</th><th>持仓</th><th>手数</th><th>毛盈亏</th><th>费用</th><th>净盈亏</th><th>收益率</th><th>浮盈 / 浮亏</th><th>退出原因</th></tr></thead><tbody id="simTrades"><tr><td colspan="12" class="empty">运行模拟后显示逐笔成交</td></tr></tbody></table></div></div>

    <div class="grid bottom-grid" style="margin-top:16px">
      <div class="card"><div class="card-head"><h2>按交易日盈亏</h2><span class="sub" id="simDailyMeta">—</span></div><div class="table-scroll sim-day"><table class="table"><thead><tr><th>交易日</th><th>成交</th><th>胜 / 负</th><th>手续费</th><th>当日盈亏</th><th>累计</th><th>日终权益</th></tr></thead><tbody id="simDaily"><tr><td colspan="7" class="empty">运行模拟后显示</td></tr></tbody></table></div></div>
      <div class="card"><div class="card-head"><h2>历史模拟记录</h2><span class="tag" id="simHistoryCount">0 条</span></div><div id="simHistory"><div class="metric-tip">还没有模拟记录</div></div><div class="note" id="simNotice">成本口径：信号在 bar 收盘确认，下一根开盘价加减滑点成交，手续费按每边每手计。</div></div>
    </div>
  </div>

  <div class="mode-panel" id="modeLive">
  <div class="card"><div class="live-hero"><div class="live-icon">⚠</div><div><h2>实盘通道未接入</h2><p>本版本不连接券商柜台，也不会发送任何真实委托。切到这个页签不影响模拟盘；下面列出接入实盘必须先补齐的环节，全部通过前不开放下单。</p></div><span class="tag amber">未接入</span></div>
  <div class="require-list">${REQUIREMENTS.map((r, i) => `<div class="require-row"><span class="mark">${i + 1}</span><div><b>${esc(r[0])}</b><span>${esc(r[1])}</span></div><span class="st">未就绪</span></div>`).join('')}</div>
  <div class="toolbar" style="margin-top:16px"><button class="btn primary" id="enableLive" disabled title="接入柜台并通过风控校验后才可启用">启用实盘交易</button><span class="sub" id="liveGate">0 / ${REQUIREMENTS.length} 项就绪 · 实盘开关处于关闭状态</span></div></div>
  <div class="risk-banner" style="margin-top:16px"><span>⚠</span><div><strong>真实资金风险。</strong>即使后续接入实盘，也请先以小资金或仿真账户验证；单笔风险按账户 1% 上限执行，触发单日亏损停机线后当日不再开新仓。</div></div>
  </div>`;

  let options = null, strategies = [], currentSource = 'demo', lastResult = null;
  let klineView = null, allMarkers = [], markers = [], markerFrame = null;
  // 交易点默认隐藏：图表先保持干净看走势；点「交易详情」里某笔，才把那一笔标出来
  let markerMode = 'off', focusTradeIndex = null;
  let currentZoom = 'all', currentBarSpace = 6;

  // K 线缩放：全部 / 近 1 日 / 近 1 小时。1 分钟 bar 下 1 个交易日 = 240 根、1 小时 = 60 根。
  const ZOOM_BARS = {all: Infinity, day: 240, hour: 60};

  function applyBarZoom(zoom) {
    currentZoom = zoom;
    const view = klineView;
    if (!view) return;
    const total = view.dataList().length;
    if (!total) return;
    const usable = (($('simKline').clientWidth || 800) - 90);
    const target = Math.min(total, ZOOM_BARS[zoom] || Infinity);
    // 下限放到 0.5：「全部」模式下宁可把蜡烛压成细线，也要让整段区间和所有买卖点同屏
    const space = Math.min(10, Math.max(0.5, usable / Math.max(1, target)));
    currentBarSpace = space;
    try {
      view.chart.setBarSpace(space);
      view.chart.scrollToDataIndex(total - 1);
    } catch (_) { /* 低版本没有该接口，退化为默认缩放 */ }
    document.querySelectorAll('#simZoom button').forEach(button => button.classList.toggle('active', button.dataset.zoom === zoom));
    scheduleMarkers();
  }

  // 只读诊断入口：排查"图只显示了一部分"这类布局问题时，可在控制台看实际 barSpace 与可见区间
  window.qiceSim = {
    zoom: () => ({mode: currentZoom, barSpace: currentBarSpace, bars: klineView ? klineView.dataList().length : 0,
                  width: $('simKline') ? $('simKline').clientWidth : 0,
                  range: klineView ? klineView.chart.getVisibleRange() : null})
  };

  async function api(path, init = {}) {
    const response = await fetch(path, init);
    let body = {};
    try { body = await response.json(); } catch (_) { /* 非 JSON 响应 */ }
    if (!response.ok) throw Error(body.error || `服务请求失败 (${response.status})`);
    return body;
  }

  // ---- 模式切换 --------------------------------------------------------
  function setMode(next, silent) {
    mode = next === 'live' ? 'live' : 'paper';
    localStorage.setItem(MODE_KEY, mode);
    document.querySelectorAll('.mode-switch button').forEach(button => {
      const on = button.dataset.mode === mode;
      button.classList.toggle('active', on);
      button.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    $('modePaper').classList.toggle('active', mode === 'paper');
    $('modeLive').classList.toggle('active', mode === 'live');
    if (mode === 'paper' && klineView) requestAnimationFrame(() => { klineView.resize(); scheduleMarkers(); });
    if (!silent) $('simMessage').textContent = '';
  }

  // ---- 日期工具 --------------------------------------------------------
  const isoDay = date => `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;

  function tradingDaysBack(count) {
    const anchor = new Date(`${(options && options.today) || isoDay(new Date())}T00:00:00`);
    const days = [];
    while (days.length < count) {
      const week = anchor.getDay();
      if (week !== 0 && week !== 6) days.push(isoDay(anchor));
      anchor.setDate(anchor.getDate() - 1);
    }
    return days.reverse();
  }

  function setRangeDays(count) {
    const days = tradingDaysBack(count);
    if (!days.length) return;
    $('simStart').value = days[0];
    $('simEnd').value = days[days.length - 1];
    updateRangeNote();
  }

  function countTradingDays(start, end) {
    if (!start || !end) return 0;
    const from = new Date(start + 'T00:00:00'), to = new Date(end + 'T00:00:00');
    if (Number.isNaN(from) || Number.isNaN(to) || to < from) return 0;
    let total = 0;
    for (const cursor = new Date(from); cursor <= to; cursor.setDate(cursor.getDate() + 1)) {
      const week = cursor.getDay();
      if (week !== 0 && week !== 6) total += 1;
    }
    return total;
  }

  function updateRangeNote() {
    const start = $('simStart').value, end = $('simEnd').value;
    const days = countTradingDays(start, end);
    if (!start || !end || !days) { $('simRangeNote').textContent = '请选择有效的起止日期（只算周一至周五）'; return; }
    $('simRangeNote').innerHTML = `<b>${esc(start)}</b> 至 <b>${esc(end)}</b> · ${days} 个交易日 · 约 ${(days * 240).toLocaleString('zh-CN')} 根 1 分钟 bar`
      + (days > (options?.maxDays || 20) ? '　<span style="color:#c4545c">超过单次上限</span>' : '');
    // 日期和某个快捷按钮对得上就高亮它，手动改日期则全部取消
    document.querySelectorAll('#simQuick button').forEach(button => {
      const span = tradingDaysBack(+button.dataset.days);
      button.classList.toggle('active', span[0] === start && span[span.length - 1] === end);
    });
  }

  // ---- 选项 / 策略 ------------------------------------------------------
  function renderSources() {
    const box = $('simSource');
    box.innerHTML = (options?.sources || []).map(source => {
      const live = source.id === 'live';
      const ready = (options?.contracts || []).some(row => row.count >= (options?.minBars || 30));
      const badge = live ? (ready ? '可用' : '未采够') : '可用';
      return `<button type="button" data-source="${source.id}" class="${source.id === currentSource ? 'active' : ''}">${esc(source.label)}<small>${esc(badge)}</small></button>`;
    }).join('');
    box.querySelectorAll('button').forEach(button => button.addEventListener('click', () => {
      currentSource = button.dataset.source;
      renderSources();
      updateSourceNote();
      updateSymbolPicker();
    }));
    updateSourceNote();
    updateSymbolPicker();
  }

  // ---- 交易品种：板块 → 品种 → 合约 --------------------------------------
  // 以前只有"真实采集"才能选合约，演示行情写死 IF-DEMO，导致换个品种根本没法试。
  // 现在任何来源都能选：演示行情按选中品种的乘数/最小变动/参考价位合成走势。
  let instrumentGroups = [], picks = {sector: '', code: '', contract: ''};

  function currentSymbol() { return $('simContract')?.value || null; }

  function fillSectors() {
    instrumentGroups = options?.instruments || [];
    if (!instrumentGroups.length) return;
    const select = $('simSector');
    select.innerHTML = instrumentGroups.map(group =>
      `<option value="${esc(group.sector)}">${esc(group.sector)}</option>`).join('');
    const wanted = picks.sector || '黑色';    // 默认停在黑色系：螺纹是最常被拿来试的品种
    select.value = instrumentGroups.some(g => g.sector === wanted) ? wanted
      : instrumentGroups[0].sector;
    fillCodes();
  }

  function fillCodes() {
    const group = instrumentGroups.find(g => g.sector === $('simSector').value) || instrumentGroups[0];
    const items = group?.items || [];
    const select = $('simCode');
    select.innerHTML = items.map(item =>
      `<option value="${esc(item.code)}">${esc(item.name)} · ${esc(item.code.toUpperCase())}</option>`).join('');
    if (items.some(item => item.code === picks.code)) select.value = picks.code;
    fillContracts();
  }

  async function fillContracts() {
    const select = $('simContract');
    if (!select) return;
    const minimum = options?.minBars || 30;

    if (currentSource === 'live') {
      // 真实采集只能选已经落库的合约，别的选了也没有数据
      const collected = (options?.contracts || []).filter(row => row.source === 'ctp');
      select.innerHTML = collected.length
        ? collected.map(row => {
            const low = row.count < minimum ? '（不足）' : '';
            return `<option value="${esc(row.symbol)}">${esc(row.symbol)} · ${row.count} 根${low}</option>`;
          }).join('')
        : '<option value="">尚未采集</option>';
      $('simInstrumentNote').textContent = collected.length
        ? '真实采集只能选已经攒够数据的合约；根数不足时跑不出成交。'
        : '还没有采集到真实行情。先到「实时 K 线」页保持采集，或把来源切回演示行情。';
      return;
    }

    const code = $('simCode').value;
    if (!code) { select.innerHTML = '<option value="">—</option>'; return; }
    try {
      const data = await (await fetch(`/api/instruments/${encodeURIComponent(code)}/contracts`)).json();
      const rows = data.contracts || [];
      select.innerHTML = rows.length
        ? rows.map(row => `<option value="${esc(row.symbol)}">${esc(row.symbol)}${row.isMain ? ' · 近月' : ''}</option>`).join('')
        : '<option value="">无可用合约</option>';
      const inst = data.instrument || {};
      $('simInstrumentNote').textContent =
        `乘数 ${inst.multiplier} · 最小变动 ${inst.tick} · 保证金参考 ${inst.marginPct}%；演示行情按该品种的价位量级合成。`;
      // 乘数与最小变动跟着品种走，手填容易和品种对不上，这里直接同步并置灰
      if (inst.multiplier && $('simMult')) { $('simMult').value = inst.multiplier; $('simMult').readOnly = true; }
      if (inst.tick && $('simTick')) { $('simTick').value = inst.tick; $('simTick').readOnly = true; }
    } catch (error) {
      select.innerHTML = '<option value="">—</option>';
      $('simInstrumentNote').textContent = '品种接口不可用：' + error.message;
    }
  }

  function updateSymbolPicker() {
    if (!instrumentGroups.length) fillSectors();
    else fillContracts();
  }

  function updateSourceNote() {
    const source = (options?.sources || []).find(item => item.id === currentSource);
    const contracts = (options?.contracts || []).filter(row => row.count >= (options?.minBars || 30));
    let text = source ? source.desc : '';
    if (currentSource === 'live') {
      text += contracts.length
        ? ` 可用合约：${contracts.map(row => `${row.symbol}（${row.count} 根）`).join('、')}`
        : ' 目前还没有采集够一根策略所需的 bar，先在「实时 K 线」页保持 CTP 采集。';
    }
    $('simSourceNote').textContent = text;
  }

  function fillStrategies(items) {
    strategies = items || [];
    const select = $('simStrategy'), selected = select.value;
    select.innerHTML = '<option value="">内置均线策略（EMA 9 / 21）</option>'
      + strategies.map(item => `<option value="${esc(item.id)}">${esc(item.name)}${item.symbol ? ' · ' + esc(item.symbol) : ''}</option>`).join('');
    if (strategies.some(item => item.id === selected)) select.value = selected;
    updateStrategyNote();
  }

  function chosenStrategy() { return strategies.find(item => item.id === $('simStrategy').value); }

  function updateStrategyNote() {
    const strategy = chosenStrategy();
    if (!strategy) {
      $('simStrategyNote').textContent = '内置策略：EMA(9) 上穿 EMA(21) 做多，ATR 过滤开启，RSI 关闭。';
      return;
    }
    const filters = [strategy.atr ? 'ATR 过滤' : null, strategy.rsi ? `RSI<${strategy.rsiUpper || 70}` : null].filter(Boolean);
    $('simStrategyNote').textContent = `${strategy.maType || 'EMA'}(${strategy.fast}/${strategy.slow})`
      + (filters.length ? ` · ${filters.join(' · ')}` : ' · 无过滤器')
      + (strategy.macd || strategy.bollinger ? ' · 该策略的 MACD / 布林带条件暂不参与撮合' : '');
  }

  // ---- 运行 ------------------------------------------------------------
  function collectConfig() {
    const tick = +$('simTick').value, slipTicks = +$('simSlip').value;
    return {
      capital: +$('simCapital').value,
      multiplier: +$('simMult').value,
      qty: +$('simQty').value,
      feePerSide: +$('simFee').value,
      tickSize: tick,
      slippageTicks: slipTicks,
      slippage: +(tick * slipTicks).toFixed(6),
      atrMaxPct: +$('simAtrMax').value,
      useBlackout: $('simBlackout').checked
    };
  }

  async function run() {
    const start = $('simStart').value, end = $('simEnd').value;
    if (!start || !end) { $('simMessage').textContent = '请先选择时间段。'; return; }
    const config = collectConfig();
    if ([config.capital, config.multiplier, config.qty, config.tickSize].some(v => !(v > 0)) || config.feePerSide < 0) {
      $('simMessage').textContent = '初始资金、合约乘数、手数和最小变动价位必须大于 0。';
      return;
    }
    const strategyId = $('simStrategy').value;
    $('simRun').disabled = true;
    $('simRun').textContent = '回放中…';
    $('simMessage').textContent = '正在回放这段行情…';
    try {
      const result = await api('/api/simulate', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          strategyId: strategyId || null, source: currentSource, start, end, config,
          symbol: currentSymbol()
        })
      });
      render(result);
      $('simMessage').textContent = `已跑完 ${result.barCount} 根 bar，产生 ${result.trades.length} 笔成交，结果已保存。`;
      loadHistory();
    } catch (error) {
      $('simMessage').textContent = error.message;
    } finally {
      $('simRun').disabled = false;
      $('simRun').textContent = '▶　开始模拟';
    }
  }

  // ---- 结果渲染 --------------------------------------------------------
  let markerBound = false;

  function ensureKlineView() {
    if (klineView) { klineView.resize(); return klineView; }
    const holder = $('simKline');
    if (!holder || !window.qiceKline) return null;
    klineView = window.qiceKline.create(holder, {onViewChange: scheduleMarkers});
    if (klineView && !markerBound) {
      markerBound = true;
      window.addEventListener('resize', () => { if (klineView) { klineView.resize(); scheduleMarkers(); } });
    }
    return klineView;
  }

  function scheduleMarkers() {
    if (markerFrame) return;
    markerFrame = requestAnimationFrame(() => { markerFrame = null; renderMarkers(); });
  }

  function buildMarkers(trades) {
    allMarkers = [];
    (trades || []).forEach((trade, index) => {
      const entryTs = Date.parse(trade.entryTime);
      if (Number.isFinite(entryTs) && +trade.entryPrice > 0) {
        allMarkers.push({ts: entryTs, value: +trade.entryPrice, kind: 'buy', tradeIndex: index,
          label: `买 ${price(trade.entryPrice)}`,
          tip: `第 ${index + 1} 笔 · 买入开仓 @ ${price(trade.entryPrice)}　${stamp(trade.entryTime)}`});
      }
      const exitTs = Date.parse(trade.exitTime);
      if (Number.isFinite(exitTs) && +trade.exitPrice > 0) {
        allMarkers.push({ts: exitTs, value: +trade.exitPrice, kind: 'sell', tradeIndex: index,
          label: `卖 ${price(trade.exitPrice)}`,
          tip: `第 ${index + 1} 笔 · 卖出平仓 @ ${price(trade.exitPrice)}　${stamp(trade.exitTime)}　净盈亏 ${plain(trade.netPnl)}`});
      }
    });
    applyMarkerMode();
  }

  // 三种状态：off=全部隐藏，all=全部显示，single=只显示选中的那一笔
  function applyMarkerMode() {
    if (markerMode === 'single' && focusTradeIndex != null) {
      markers = allMarkers.filter(marker => marker.tradeIndex === focusTradeIndex);
    } else if (markerMode === 'all') {
      markers = allMarkers.slice();
    } else {
      markers = [];
    }
    const button = $('simMarkerToggle');
    if (button) {
      button.textContent = markerMode === 'off' ? '交易点：隐藏'
        : markerMode === 'single' ? `交易点：第 ${focusTradeIndex + 1} 笔` : '交易点：全部';
      button.classList.toggle('primary', markerMode !== 'off');
    }
    const note = $('simKlineNote');
    if (note) {
      note.textContent = markerMode === 'off'
        ? '交易点已隐藏。点下方「逐笔明细」任意一行，即可在 K 线上定位那笔的开平仓。'
        : '红点 = 买入开仓 · 绿点 = 卖出平仓；K 线为 1 分钟 bar，已按北京时间对齐。';
    }
    scheduleMarkers();
  }

  function toggleMarkers() {
    const turningOn = markerMode === 'off';
    markerMode = turningOn ? 'all' : 'off';
    focusTradeIndex = null;
    document.querySelectorAll('#simTrades tr').forEach(row => row.classList.remove('selected'));
    applyMarkerMode();
    // 点开"全部"时把视野也拉回整段：否则刚才按单笔放大的视野里只看得到一两个点
    if (turningOn) applyBarZoom('all');
  }

  // 点某一笔：只标这一笔，并把图表滚到它的开仓位置
  function focusTrade(index) {
    const trade = lastResult?.trades?.[index];
    if (!trade) return;
    focusTradeIndex = index;
    markerMode = 'single';
    applyMarkerMode();
    document.querySelectorAll('#simTrades tr').forEach(row => {
      row.classList.toggle('selected', Number(row.dataset.index) === index);
    });
    if (klineView && Number.isFinite(+trade.entryIdx)) {
      try {
        const total = klineView.dataList().length;
        const usable = (($('simKline').clientWidth || 800) - 90);
        const space = Math.min(10, Math.max(2, usable / 120));   // 约 2 小时的视野
        currentBarSpace = space;
        klineView.chart.setBarSpace(space);
        klineView.chart.scrollToDataIndex(Math.max(0, Math.min(total - 1, +trade.entryIdx + 20)));
      } catch (_) { /* 低版本没有该接口，保持当前视野 */ }
      scheduleMarkers();
    }
  }

  function renderMarkers() {
    const layer = $('simMarkerLayer');
    if (!layer) return;
    if (!klineView || !markers.length) { layer.innerHTML = ''; return; }
    const list = klineView.dataList();
    if (!list.length) { layer.innerHTML = ''; return; }
    const times = list.map(bar => bar.timestamp);
    const indexOf = ts => {
      let lo = 0, hi = times.length - 1, ans = -1;
      while (lo <= hi) {
        const mid = (lo + hi) >> 1;
        if (times[mid] <= ts) { ans = mid; lo = mid + 1; } else { hi = mid - 1; }
      }
      return ans;
    };
    const range = klineView.chart.getVisibleRange() || {};
    const width = layer.clientWidth || 0;
    const frag = document.createDocumentFragment();
    const placed = [];
    markers.forEach(marker => {
      const index = indexOf(marker.ts);
      if (index < 0) return;
      if (range.from != null && (index < range.from - 2 || index > range.to + 2)) return;
      const bar = list[index];
      // 买点挂在这根 bar 的最低点下方、卖点挂在最高点上方，避免压住蜡烛实体
      const anchor = marker.kind === 'buy' ? Math.min(bar.low, marker.value) : Math.max(bar.high, marker.value);
      const point = klineView.chart.convertToPixel({dataIndex: index, value: anchor}, {paneId: 'candle_pane'});
      if (!point || point.x == null || point.y == null) return;
      const x = width ? Math.min(Math.max(point.x, 46), width - 74) : point.x;
      let y = point.y + (marker.kind === 'buy' ? 5 : -5);
      for (let guard = 0; guard < 6; guard += 1) {
        if (!placed.find(item => Math.abs(item.x - x) < 62 && Math.abs(item.y - y) < 16)) break;
        y += marker.kind === 'buy' ? 15 : -15;
      }
      placed.push({x, y});
      const node = document.createElement('div');
      // 一根 bar 不到 2px 时，带文字的标签必然互相压住，退化成圆点，细节交给悬停提示
      node.className = `tr-badge ${marker.kind}${currentBarSpace < 2 ? ' mini' : ''}`;
      node.style.left = `${x}px`;
      node.style.top = `${y}px`;
      node.textContent = marker.label;
      node.title = marker.tip;
      frag.appendChild(node);
    });
    layer.innerHTML = '';
    layer.appendChild(frag);
  }

  function drawEquity(curve) {
    const box = $('simEquity');
    if (!curve || !curve.length) { box.innerHTML = '<div class="empty">运行模拟后显示</div>'; return; }
    const W = 900, H = 235, pad = 46;
    const values = curve.flatMap(point => [point.equity, point.benchmark]);
    const low = Math.min(...values), high = Math.max(...values), span = high - low || 1;
    const x = index => pad + index / Math.max(1, curve.length - 1) * (W - pad * 2);
    const y = value => H - 30 - (value - low) / span * (H - 52);
    const path = key => curve.map((point, index) => `${index ? 'L' : 'M'}${x(index).toFixed(1)} ${y(point[key]).toFixed(1)}`).join(' ');
    box.innerHTML = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">`
      + [0, 0.5, 1].map(ratio => {
          const value = low + span * ratio;
          return `<line class="axis" x1="${pad}" y1="${y(value)}" x2="${W - pad}" y2="${y(value)}"/>`
            + `<text x="2" y="${y(value) + 3}" class="tick">${plain(value)}</text>`;
        }).join('')
      + `<path d="${path('benchmark')}" fill="none" stroke="#aeb9cb" stroke-width="1.6" stroke-dasharray="5 4"/>`
      + `<path d="${path('equity')}" fill="none" stroke="#5277ee" stroke-width="2.3"/>`
      + `<text x="${pad}" y="${H - 6}" class="tick">${esc(stamp(curve[0].timestamp))}</text>`
      + `<text x="${W - 132}" y="${H - 6}" class="tick">${esc(stamp(curve[curve.length - 1].timestamp))}</text></svg>`;
  }

  function renderRow(trade, index) {
    const positive = trade.netPnl >= 0;
    return `<tr data-index="${index}" title="点击在 K 线上定位这一笔：${esc(`${trade.id} · ${trade.entryReason || ''} → ${trade.exitReason || ''}`)}">`
      + `<td class="muted">${index + 1}</td><td>${esc(trade.side)}</td>`
      + `<td>${esc(stamp(trade.entryTime))}<br><span class="muted">${price(trade.entryPrice)}</span></td>`
      + `<td>${esc(stamp(trade.exitTime))}<br><span class="muted">${price(trade.exitPrice)}</span></td>`
      + `<td>${esc(trade.holdLabel || '—')}</td><td>${esc(trade.qty)}</td>`
      + `<td class="${trade.grossPnl >= 0 ? 'positive' : 'negative'}">${plain(trade.grossPnl)}</td>`
      + `<td class="muted">${plain(trade.fees)}</td>`
      + `<td class="${positive ? 'positive' : 'negative'}"><b>${cash(trade.netPnl)}</b></td>`
      + `<td class="${positive ? 'positive' : 'negative'}">${pct(trade.returnPct)}</td>`
      + `<td class="muted">${pct(trade.mfePct, 2)} / ${pct(trade.maePct, 2)}</td>`
      + `<td class="muted">${esc(trade.exitReason || '—')}</td></tr>`;
  }

  function render(result) {
    lastResult = result;
    const summary = result.summary || {};
    $('simPnl').textContent = cash(summary.netPnl);
    $('simPnl').className = 'value ' + (summary.netPnl >= 0 ? 'positive' : 'negative');
    $('simPnlTip').textContent = `期末权益 ${plain(summary.endEquity)}`;
    $('simReturn').textContent = pct(summary.returnPct, 2);
    $('simReturn').className = 'value ' + (summary.returnPct >= 0 ? 'positive' : 'negative');
    $('simWin').textContent = pct(summary.winRatePct, 1);
    $('simWinTip').textContent = `盈利 ${result.trades.filter(t => t.netPnl > 0).length} 笔 / 亏损 ${result.trades.filter(t => t.netPnl < 0).length} 笔`;
    $('simPf').textContent = summary.profitFactor === 'Infinity' ? '∞' : summary.profitFactor == null ? '—' : (+summary.profitFactor).toFixed(2);
    $('simMdd').textContent = pct(summary.maxDrawdownPct, 2);
    $('simCount').textContent = summary.tradeCount || 0;
    $('simCountTip').textContent = `手续费合计 ${plain(summary.feesTotal)}`;

    $('simResultTitle').textContent = `${result.strategyName} · ${result.symbol} · ${dayText(result.startTime)} → ${dayText(result.endTime)} · ${result.barCount} 根 bar`;
    $('simSourceTag').textContent = (result.source && result.source.label) || '—';
    $('simSourceTag').style.display = '';
    $('simSourceTag').className = 'tag ' + (result.source && result.source.kind === 'live' ? 'green' : 'amber');
    $('simEquityMeta').textContent = `策略 vs 买入持有 · 单笔期望 ${plain(summary.expectancy)} · 平均持仓 ${summary.avgHoldMinutes || 0} 分钟`;
    $('simTradeMeta').textContent = `开仓 → 平仓 → 持仓时长；浮盈/浮亏取持仓期间的最高/最低价`;
    $('simTradeCount').textContent = `${result.trades.length} 笔`;
    $('simNotice').textContent = result.notice || '结果只描述这段历史的模拟表现，不代表未来。';

    // K 线
    const view = ensureKlineView();
    if (view) {
      const clean = view.apply(result.bars || []);
      $('simKlineEmpty').style.display = clean.length ? 'none' : 'flex';
      markerMode = 'off';
      focusTradeIndex = null;
      buildMarkers(result.trades || []);
      // 图表首帧布局没完成时 setBarSpace 会被随后的内部重排覆盖，
      // 这里在几个时间点各设一次（幂等），确保不同机器速度下都能落到目标缩放
      [60, 220, 600].forEach(delay => setTimeout(() => {
        if (!klineView) return;
        klineView.resize();
        applyBarZoom(currentZoom);
      }, delay));
    } else {
      $('simKlineEmpty').style.display = 'flex';
      $('simKlineEmpty').innerHTML = '<b>图表容器还没准备好</b><span>切到本页后重新运行一次即可</span>';
    }

    drawEquity(result.curve || []);

    $('simTrades').innerHTML = result.trades.length
      ? result.trades.map(renderRow).join('')
      : '<tr><td colspan="12" class="empty">这段时间没有触发任何成交：可以放长区间，或在策略库把均线周期调小</td></tr>';
    $('simTrades').querySelectorAll('tr[data-index]').forEach(row => {
      row.addEventListener('click', () => focusTrade(Number(row.dataset.index)));
    });

    $('simDaily').innerHTML = (result.daily || []).length
      ? result.daily.map(day => `<tr><td>${esc(day.date)}</td><td>${day.tradeCount}</td><td>${day.wins} / ${day.losses}</td><td class="muted">${plain(day.fees)}</td><td class="${day.netPnl >= 0 ? 'positive' : 'negative'}">${cash(day.netPnl)}</td><td class="${day.cumPnl >= 0 ? 'positive' : 'negative'}">${plain(day.cumPnl)}</td><td>${plain(day.dayEndEquity)}</td></tr>`).join('')
      : '<tr><td colspan="7" class="empty">暂无按日数据</td></tr>';
    $('simDailyMeta').textContent = `${(result.daily || []).length} 个交易日 · 已实现盈亏按平仓日归集`;
  }

  // ---- 历史记录 --------------------------------------------------------
  async function loadHistory() {
    try {
      const runs = await api('/api/simulate/history');
      $('simHistoryCount').textContent = `${runs.length} 条`;
      $('simHistory').innerHTML = runs.length
        ? runs.map(run => `<button class="btn sim-hist" data-run="${esc(run.runId)}"><span>${esc(run.strategyName || '未命名')} · ${esc(run.symbol || '')}<br><span class="muted">${esc(run.startTime || '')} → ${esc(run.endTime || '')} · ${run.tradeCount} 笔</span></span><b class="${run.netPnl >= 0 ? 'positive' : 'negative'}">${cash(run.netPnl)}</b></button>`).join('')
        : '<div class="metric-tip">还没有模拟记录</div>';
      $('simHistory').querySelectorAll('[data-run]').forEach(button => button.addEventListener('click', () => loadRun(button.dataset.run)));
    } catch (error) {
      $('simHistory').innerHTML = `<div class="metric-tip">${esc(error.message)}</div>`;
    }
  }

  async function loadRun(runId) {
    $('simMessage').textContent = '正在读取历史模拟记录…';
    try {
      render(await api(`/api/simulate/${encodeURIComponent(runId)}`));
      $('simMessage').textContent = '已载入历史模拟记录。';
    } catch (error) {
      $('simMessage').textContent = error.message;
    }
  }

  // ---- 事件 ------------------------------------------------------------
  $('simStrategy').addEventListener('change', updateStrategyNote);
  $('simZoom').addEventListener('click', event => {
    const button = event.target.closest('button[data-zoom]');
    if (button) applyBarZoom(button.dataset.zoom);
  });
  $('simQuick').addEventListener('click', event => {
    const button = event.target.closest('button[data-days]');
    if (button) setRangeDays(+button.dataset.days);
  });
  [$('simStart'), $('simEnd')].forEach(input => input.addEventListener('change', updateRangeNote));
  $('simRun').addEventListener('click', run);
  document.querySelectorAll('.mode-switch button').forEach(button => button.addEventListener('click', () => setMode(button.dataset.mode)));
  $('simExport').addEventListener('click', () => {
    if (!lastResult || !lastResult.trades.length) return;
    const header = ['编号', '方向', '开仓时间', '开仓价', '平仓时间', '平仓价', '持仓分钟', '手数', '毛盈亏', '费用', '净盈亏', '收益率%', '最大浮盈%', '最大浮亏%', '退出原因'];
    const rows = [header, ...lastResult.trades.map(trade => [trade.id, trade.side, trade.entryTime, trade.entryPrice,
      trade.exitTime, trade.exitPrice, trade.holdMinutes, trade.qty, trade.grossPnl, trade.fees, trade.netPnl,
      trade.returnPct, trade.mfePct, trade.maePct, trade.exitReason])];
    const csv = '\ufeff' + rows.map(row => row.map(cell => `"${String(cell ?? '').replaceAll('"', '""')}"`).join(',')).join('\r\n');
    const link = document.createElement('a');
    link.href = URL.createObjectURL(new Blob([csv], {type: 'text/csv;charset=utf-8'}));
    link.download = `模拟成交_${lastResult.strategyName || '策略'}.csv`;
    link.click();
    URL.revokeObjectURL(link.href);
  });
  window.addEventListener('qice-page-change', event => {
    if (event.detail !== 'paper') return;
    if (klineView) { klineView.resize(); scheduleMarkers(); }
  });
  window.addEventListener('strategy-library-updated', event => fillStrategies(window.strategyLibrary?.all() || event.detail || []));

  // ---- 启动 ------------------------------------------------------------
  setMode(mode, true);
  (async () => {
    try {
      options = await api('/api/simulate/options');
      $('simStateTag').textContent = '服务已连接';
      $('simStateTag').className = 'tag green';
      const defaults = options.defaults || {};
      currentSource = defaults.source || 'demo';
      ['capital', 'multiplier', 'qty', 'feePerSide', 'tickSize', 'slippageTicks', 'atrMaxPct'].forEach(key => {
        const input = {capital: 'simCapital', multiplier: 'simMult', qty: 'simQty', feePerSide: 'simFee',
          tickSize: 'simTick', slippageTicks: 'simSlip', atrMaxPct: 'simAtrMax'}[key];
        if (defaults[key] != null && $(input)) $(input).value = defaults[key];
      });
      renderSources();
      setRangeDays(defaults.days || 3);
      $('simHint').textContent = `最多 ${options.maxDays} 个交易日`;
    } catch (error) {
      $('simStateTag').textContent = '服务未连接';
      $('simStateTag').className = 'tag amber';
      $('simMessage').textContent = error.message;
    }
    try {
      fillStrategies(window.strategyLibrary?.all() || await api('/api/strategies'));
    } catch (_) { fillStrategies(window.strategyLibrary?.all() || []); }
    $('simMarkerToggle').addEventListener('click', toggleMarkers);
  $('simSector').addEventListener('change', () => { picks.sector = $('simSector').value; fillCodes(); });
  $('simCode').addEventListener('change', () => { picks.code = $('simCode').value; fillContracts(); });
  $('simContract').addEventListener('change', () => { picks.contract = $('simContract').value; });

  loadHistory();
  })();
})();
