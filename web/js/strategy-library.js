(() => {
  const page=document.getElementById('page-strategy');
  const oldHeading=page.querySelector('.heading'),editorGrid=page.querySelector('.strategy-layout');
  oldHeading.hidden=true;editorGrid.hidden=true;
  page.insertAdjacentHTML('afterbegin',`<div class="heading"><div><h1>我的策略库</h1><p>策略保存在服务端数据库；点「详细」逐笔复盘成交与盈亏，编辑参数后再启用。</p></div><div class="toolbar"><div class="library-search"><input id="librarySearch" type="text" autocomplete="off" spellcheck="false" placeholder="搜索策略 / 品种 / 品类"></div><span class="tag amber" id="backendState">连接服务中</span><button class="btn primary" id="newStrategy">＋ 新建策略</button></div></div><div class="strategy-library-intro"><span class="tag">paper trading 策略</span><span class="sub">保存后可用于回测和模拟盘回放</span></div><div class="grid strategy-cards" id="strategyCards"></div><dialog id="strategyModal" class="strategy-modal"><div class="modal-head"><div><h2 id="modalTitle">新建策略</h2><div class="sub" id="modalSubtitle">填写参数后保存到策略库</div></div><button class="btn" id="modalClose">关闭</button></div><div class="modal-name field"><label for="libraryName">策略名称</label><input id="libraryName" type="text" maxlength="48" placeholder="例如：5分钟趋势跟随策略"></div><div class="modal-body" id="modalBody"></div><div class="modal-footer"><span class="sub" id="modalMessage"></span><div class="toolbar"><button class="btn" id="modalCancel">取消</button><button class="btn" id="saveAsNew">另存为新策略</button><button class="btn primary" id="saveChanges">保存策略</button></div></div></dialog><dialog id="tradesModal" class="strategy-modal"><div class="modal-head"><div><h2 id="tradesTitle">交易记录</h2><div class="sub" id="tradesSubtitle">读取中…</div></div><button class="btn" id="tradesClose">关闭</button></div><div class="trades-summary" id="tradesSummary"></div><div class="modal-body" id="tradesBody"></div><div class="modal-footer"><span class="sub" id="tradesNotice"></span><div class="toolbar"><button class="btn" id="tradesViewParams">查看策略参数</button><button class="btn primary" id="tradesCloseFooter">关闭</button></div></div></dialog>`);
  const $=id=>document.getElementById(id);
  $('modalBody').appendChild(editorGrid);editorGrid.hidden=false;
  let items=[],currentId=null,mode='new',ready=false,keyword='';
  window.strategyLibrary={get:id=>items.find(s=>s.id===id),all:()=>items.map(s=>({...s})),refresh:load};
  // 品类/品种搜索选择器：代码、中文名、板块、交易所都能搜（见 js/instrument-picker.js）
  const picker = window.qiceInstrumentPicker
    ? window.qiceInstrumentPicker.create({sectorInput:'assetSectorInput',sectorPanel:'assetSectorPanel',
        sectorHint:'assetSectorHint',codeInput:'assetInput',codePanel:'assetPanel',
        hidden:'asset',hint:'assetHint'})
    : null;
  const instOf = symbol => (picker && picker.lookup(symbol)) || null;
  function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
  // picker 还没加载完品种表时退回到 hidden 里的初始值，避免新建时把一个空品种存进去
  function capture(){return {symbol:(picker&&picker.symbol())||$('asset').value||'',timeframe:$('strategyTimeframe').value,maType:$('maType').value.startsWith('SMA')?'SMA':'EMA',fast:+$('fast').value,slow:+$('slow').value,rsi:$('rsiOn').checked,rsiParams:$('strategyRsiParams').value,macd:$('macdOn').checked,macdParams:$('strategyMacdParams').value,bollinger:$('bbOn').checked,bollingerParams:$('strategyBbParams').value,atr:$('atrOn').checked,atrPeriod:+$('strategyAtrPeriod').value,atrMaxPct:+(document.getElementById('btAtrMax')?.value||0.4)};}
  function dispatch(){window.dispatchEvent(new CustomEvent('strategy-library-updated',{detail:items.map(({id,name})=>({id,name}))}));}
  // ---- 常驻纸上交易：每条策略各自启停 ------------------------------------
  // 设计取舍：状态与按钮都放在卡片里，而不是页头放一个全局开关。
  // 全局开关在"某条策略在跑、另一条在暂停"时根本无法表达，只能二选一骗人。
  let trading = {};            // strategyId -> 运行态快照（服务端每 15 秒推进一次）
  let tradingTimer = null;

  const money = v => v == null ? '—'
    : (v >= 0 ? '+' : '−') + '¥' + Math.abs(Math.round(v)).toLocaleString('zh-CN');
  const price = v => v == null ? '—'
    : Number(v).toLocaleString('zh-CN', {minimumFractionDigits: 1, maximumFractionDigits: 2});
  // 交易记录用的精确格式（money 会把小数抹掉，盈亏要保留到分）
  const plain = v => v == null ? '—'
    : (v < 0 ? '−' : '') + '¥' + Math.abs(+v || 0).toLocaleString('zh-CN', {maximumFractionDigits: 2});
  const cash = v => v == null ? '—'
    : (v < 0 ? '−' : v > 0 ? '+' : '') + '¥' + Math.abs(+v || 0).toLocaleString('zh-CN', {maximumFractionDigits: 2});
  const pct = (v, d = 2) => v == null ? '—' : (+v).toFixed(d) + '%';
  const stamp = v => String(v || '').replace('T', ' ').slice(0, 16);

  // key 驱动卡片左侧状态色条与右上角徽章；short 是去掉符号后的文字（符号交给 CSS 圆点）
  function liveState(t) {
    if (!t) return {key: 'idle', cls: '', short: '未启动',
                    text: '点「启动策略」按当前参数开始纸上记账（不连柜台、不下单）'};
    if (t.status === 'running') return {key: 'running', cls: 'green', short: '交易中',
                    text: `${t.continuous || t.symbol} · ${t.periodLabel} · 每 ${t.tickSeconds} 秒跟一次行情`};
    if (t.status === 'paused') return {key: 'paused', cls: 'amber', short: '已暂停',
                    text: t.lastMessage || '已暂停：不再开平仓，持仓保留'};
    return {key: 'stopped', cls: '', short: '已停止', text: '已下线，历史成交仍保留在服务端'};
  }

  // 实时区只放「一行说明 + 三格数据 + 持仓」；状态徽章在卡片头部，由 paint() 同步
  function liveBlock(s) {
    const t = trading[s.id], st = liveState(t), pos = t && t.position;
    const caption = `<div class="live-caption" title="${esc(st.text)}">${esc(st.text)}</div>`;
    const cell = (label, value, cls = '', unit = '') =>
      `<div class="live-kpi"><span>${label}</span><b class="${cls}">${value}${unit ? `<i>${unit}</i>` : ''}</b></div>`;
    // 未启动也画三格占位，卡片高度不随状态跳动，列表才不会参差
    const kpis = `<div class="live-kpis">
        ${cell('成交', t ? t.tradeCount : '—', '', t ? '笔' : '')}
        ${cell('已实现', t ? money(t.netPnl) : '—', t ? (t.netPnl >= 0 ? 'positive' : 'negative') : '')}
        ${cell('浮动盈亏', t ? money(t.unrealizedPnl) : '—', t ? ((t.unrealizedPnl || 0) >= 0 ? 'positive' : 'negative') : '')}
      </div>`;
    const posLine = !t
      ? '<div class="live-pos"><span class="live-pos-empty">尚未启动 · 暂无持仓</span></div>'
      : pos
        ? `<div class="live-pos"><span class="live-pos-dir ${pos.side === '做空' ? 'short' : ''}">${esc(pos.side || '做多')} ${pos.qty} 手</span>
             <span class="live-pos-path">${price(pos.entryPrice)}<em>→</em>${price(pos.lastPrice)}</span>
             <span class="live-pos-bars">持有 ${pos.holdBars} 根</span></div>`
        : '<div class="live-pos"><span class="live-pos-empty">当前空仓</span></div>';
    const note = t && t.notice ? `<div class="live-note">${esc(t.notice)}</div>` : '';
    const err = t && t.error ? `<div class="live-note is-error">取数异常：${esc(t.error)}</div>` : '';
    return caption + kpis + posLine + note + err;
  }

  // 交易控制：只有这里会变（启动 ↔ 暂停 ↔ 恢复 ↔ 停止）
  function liveActions(s) {
    const t = trading[s.id], id = esc(s.id);
    if (!t || t.status === 'stopped') {
      return `<button class="btn primary" data-action="start" data-id="${id}">▶ 启动策略</button>`;
    }
    if (t.status === 'running') {
      return `<button class="btn" data-action="pause" data-id="${id}">⏸ 暂停交易</button>
              <button class="btn danger" data-action="stop" data-id="${id}">■ 停止</button>`;
    }
    return `<button class="btn primary" data-action="resume" data-id="${id}">▶ 恢复交易</button>
            <button class="btn danger" data-action="stop" data-id="${id}">■ 停止</button>`;
  }

  function bind(root) {
    root.querySelectorAll('[data-action]').forEach(button =>
      button.addEventListener('click', () => act(button.dataset.action, button.dataset.id, button)));
  }

  // 搜索：策略名 / 品种写法 / 品类都能命中，空关键词时原样展示
  function visible() {
    const q = keyword.trim().toLowerCase();
    if (!q) return items;
    return items.filter(s => {
      const inst = instOf(s.symbol);
      return [s.name, s.symbol, s.timeframe, inst && inst.name, inst && inst.code, inst && inst.sector, inst && inst.exchangeName]
        .filter(Boolean).some(v => String(v).toLowerCase().includes(q));
    });
  }

  function render() {
    const host = $('strategyCards');
    if (!items.length) {
      host.innerHTML = '<div class="card empty">策略库还没有策略。点击“新建策略”创建第一条。</div>';
      dispatch();
      return;
    }
    const rows = visible();
    if (!rows.length) {
      host.innerHTML = `<div class="card empty">没有匹配「${esc(keyword)}」的策略。
        <div style="margin-top:9px"><button class="btn" id="clearLibrarySearch">清空搜索</button></div></div>`;
      const clear = $('clearLibrarySearch');
      if (clear) clear.addEventListener('click', () => { keyword = ''; $('librarySearch').value = ''; render(); });
      dispatch();
      return;
    }
    host.innerHTML = rows.map(s => {
      const inst = instOf(s.symbol), st = liveState(trading[s.id]), id = esc(s.id);
      const where = inst ? `${inst.name} · ${inst.sector} · ${inst.exchangeName} · 乘数 ${inst.multiplier}` : '品种未识别';
      // 旧数据里存过「原油 · SC2503」这种整串写法，卡片上只显示短码，完整写法放 title
      const raw = String(s.symbol || '');
      const shortCode = ((raw.match(/[A-Za-z]+\d*/) || [])[0] || (inst ? inst.code : raw)).toUpperCase();
      const filters = [s.atr ? `ATR(${s.atrPeriod})` : '', s.rsi ? 'RSI' : '',
                       s.macd ? 'MACD' : '', s.bollinger ? '布林带' : ''].filter(Boolean);
      return `<article class="card strategy-card" data-id="${id}" data-status="${st.key}">
        <header class="sc-head">
          <div class="sc-title"><h2>${esc(s.name)}</h2>
            <div class="sc-subline">
              <span class="sc-code" title="${esc(raw)}">${esc(shortCode)}</span>
              <span>${esc(s.timeframe || '5 分钟')}</span>
              <span class="sc-chip">${esc(s.maType)} ${s.fast}/${s.slow}</span>
            </div></div>
          <span class="sc-status ${st.cls}" data-badge><i></i>${esc(st.short)}</span>
        </header>
        <div class="live-slot" data-live>${liveBlock(s)}</div>
        <div class="sc-meta">
          <div class="sc-meta-row"><span class="sc-meta-k">交易标的</span><span class="sc-meta-v">${esc(where)}</span></div>
          <div class="sc-meta-row"><span class="sc-meta-k">过滤条件</span><span class="sc-meta-v">${filters.length ? filters.map(f => `<span class="sc-chip">${esc(f)}</span>`).join('') : '<span>无</span>'}</span></div>
        </div>
        <footer class="sc-foot">
          <div class="strategy-card-actions" data-actions>${liveActions(s)}</div>
          <div class="strategy-card-manage">
            <button class="btn" data-action="detail" data-id="${id}">详细</button>
            <button class="btn" data-action="edit" data-id="${id}">编辑</button>
            <button class="btn danger" data-action="delete" data-id="${id}">删除</button>
          </div>
        </footer>
        <div class="sc-updated">更新于 ${new Date(s.updatedAt).toLocaleString('zh-CN')}</div>
      </article>`;
    }).join('');
    bind(host);
    dispatch();
  }

  // 只重画动态部分：整卡重建会让轮询里的按钮在鼠标下面闪，体验很差
  function paint() {
    document.querySelectorAll('#strategyCards .strategy-card').forEach(card => {
      const s = items.find(item => item.id === card.dataset.id);
      if (!s) return;
      const st = liveState(trading[s.id]);
      card.dataset.status = st.key;                 // 左侧状态色条跟着轮询变色
      const badge = card.querySelector('[data-badge]');
      if (badge) { badge.className = `sc-status ${st.cls}`; badge.innerHTML = `<i></i>${esc(st.short)}`; }
      const slot = card.querySelector('[data-live]');
      const actions = card.querySelector('[data-actions]');
      if (slot) slot.innerHTML = liveBlock(s);
      if (actions) { actions.innerHTML = liveActions(s); bind(actions); }
    });
  }

  async function loadTrading() {
    if (!ready) return;
    try {
      const data = await api('/api/strategies/trading');
      trading = {};
      (data.items || []).forEach(item => { trading[item.strategyId] = item; });
      paint();
    } catch (err) { /* 轮询失败不打扰用户，下一轮再试 */ }
  }

  function startPolling() { stopPolling(); loadTrading(); tradingTimer = setInterval(loadTrading, 5000); }
  function stopPolling() { if (tradingTimer) { clearInterval(tradingTimer); tradingTimer = null; } }
  async function api(path,options={}){const r=await fetch(path,options);let data={};try{data=await r.json();}catch{}if(!r.ok)throw Error(data.error||`服务异常 (${r.status})`);return data;}
  async function load(){try{items=await api('/api/strategies');ready=true;$('backendState').textContent='服务端数据库已连接';$('backendState').className='tag green';$('newStrategy').disabled=false;await loadTrading();render();}catch(err){ready=false;$('backendState').textContent='服务未连接';$('backendState').className='tag amber';$('newStrategy').disabled=true;$('strategyCards').innerHTML=`<div class="card empty">无法读取策略库：${esc(err.message)}。请启动本地服务后刷新。</div>`;}}
  function apply(s){$('libraryName').value=s.name||'';if(picker)picker.setSymbol(s.symbol||'');else $('asset').value=s.symbol||'';$('strategyTimeframe').value=s.timeframe||'5 分钟';$('maType').value=s.maType==='SMA'?'SMA 简单移动平均':'EMA 指数移动平均';$('fast').value=s.fast??9;$('slow').value=s.slow??21;$('rsiOn').checked=!!s.rsi;$('strategyRsiParams').value=s.rsiParams||'14 · 30 / 70';$('macdOn').checked=!!s.macd;$('strategyMacdParams').value=s.macdParams||'12 / 26 / 9';$('bbOn').checked=!!s.bollinger;$('strategyBbParams').value=s.bollingerParams||'20 · 2.0σ';$('atrOn').checked=!!s.atr;$('strategyAtrPeriod').value=s.atrPeriod??14;if(document.getElementById('btAtrMax'))document.getElementById('btAtrMax').value=s.atrMaxPct??0.4;if(document.getElementById('btFee'))document.getElementById('btFee').value=s.feePerSide??0;if(document.getElementById('btSlip'))document.getElementById('btSlip').value=s.slippage??0;if(document.getElementById('btMult'))document.getElementById('btMult').value=s.multiplier??1;if(document.getElementById('btQty'))document.getElementById('btQty').value=s.qty??1;}
  function readOnly(value){editorGrid.querySelectorAll('input,select,button').forEach(el=>el.disabled=value);$('libraryName').disabled=value;}
  function openEditor(nextMode,s){mode=nextMode;currentId=s?.id||null;if(s)apply(s);else{apply({...capture(),name:'',fast:9,slow:21,maType:'EMA',rsi:false,macd:false,bollinger:false,atr:true,atrPeriod:14});$('libraryName').value='';}const detail=mode==='detail';$('modalTitle').textContent=detail?'策略详情':mode==='edit'?'编辑策略':'新建策略';$('modalSubtitle').textContent=detail?'查看当前已保存参数':mode==='edit'?'修改参数并保存到服务端':'设置策略规则并保存到服务端';$('saveChanges').textContent=mode==='edit'?'保存修改':'保存策略';$('saveChanges').hidden=detail;$('saveAsNew').hidden=detail;$('modalMessage').textContent='';readOnly(detail);$('strategyModal').showModal();}
  function closeEditor(){$('strategyModal').close();readOnly(false);}
  // ---- 交易记录：点「详细」逐笔复盘这条策略的成交与盈亏 --------------------
  // 数据来自 /api/strategies/<id>/trades：常驻纸上交易 + 区间模拟/模拟盘 + 回测
  // 三处汇总，服务端已按平仓时间倒序、补好持仓时长与收益率（views.strategy_trades）。
  function tradeRow(trade, index) {
    const positive = (trade.netPnl || 0) >= 0;
    const segment = trade.segment ? ' · ' + trade.segment : '';
    return `<tr title="${esc(`${trade.id || ''} · ${trade.entryReason || ''} → ${trade.exitReason || ''}`)}">`
      + `<td class="muted">${index + 1}</td>`
      + `<td><span class="sc-chip">${esc(trade.sourceLabel || '—')}${esc(segment)}</span></td>`
      + `<td>${esc(trade.side || '做多')}</td>`
      + `<td>${esc(stamp(trade.entryTime))}<br><span class="muted">${price(trade.entryPrice)}</span></td>`
      + `<td>${esc(stamp(trade.exitTime))}<br><span class="muted">${price(trade.exitPrice)}</span></td>`
      + `<td>${esc(trade.holdLabel || '—')}</td><td>${esc(trade.qty)}</td>`
      + `<td class="${(trade.grossPnl || 0) >= 0 ? 'positive' : 'negative'}">${plain(trade.grossPnl)}</td>`
      + `<td class="muted">${plain(trade.fees)}</td>`
      + `<td class="${positive ? 'positive' : 'negative'}"><b>${cash(trade.netPnl)}</b></td>`
      + `<td class="${positive ? 'positive' : 'negative'}">${pct(trade.returnPct)}</td>`
      + `<td class="muted">${esc(trade.exitReason || '—')}</td></tr>`;
  }

  function renderTrades(data) {
    const trades = data.trades || [], summary = data.summary || {};
    $('tradesSubtitle').textContent =
      `${data.strategyName || ''} · 共 ${trades.length} 笔成交，按平仓时间倒序`;
    const pf = summary.profitFactor === 'Infinity' ? '∞'
      : summary.profitFactor == null ? '—' : (+summary.profitFactor).toFixed(2);
    const cells = [
      ['总成交', trades.length + ' 笔', ''],
      ['净盈亏', cash(summary.netPnl), (summary.netPnl || 0) >= 0 ? 'positive' : 'negative'],
      ['胜率', trades.length ? pct(summary.winRatePct, 1) : '—', ''],
      ['盈亏比', pf, ''],
      ['手续费合计', plain(summary.feesTotal), ''],
    ];
    const bySource = (summary.bySource || [])
      .map(group => `<span class="tag">${esc(group.label)} ${group.count} 笔 · ${esc(cash(group.netPnl))}</span>`).join('');
    $('tradesSummary').innerHTML =
      cells.map(([label, value, cls]) =>
        `<div class="tsum"><span>${label}</span><b class="${cls}">${value}</b></div>`).join('')
      + (bySource ? `<div class="tsum tsum-wide"><span>来源构成</span><div class="tsum-tags">${bySource}</div></div>` : '');
    $('tradesBody').innerHTML = trades.length
      ? `<div class="table-scroll"><table class="table"><thead><tr><th>#</th><th>来源</th><th>方向</th><th>开仓时间 / 价格</th><th>平仓时间 / 价格</th><th>持仓</th><th>手数</th><th>毛盈亏</th><th>费用</th><th>净盈亏</th><th>收益率</th><th>退出原因</th></tr></thead><tbody>${trades.map(tradeRow).join('')}</tbody></table></div>`
      : '<div class="card empty">这条策略还没有成交记录。启动常驻交易，或跑一次回测 / 模拟后，成交会自动汇总到这里。</div>';
    $('tradesNotice').textContent = data.notice || '';
  }

  function openTrades(s) {
    $('tradesTitle').textContent = `${s.name} · 交易记录`;
    $('tradesSubtitle').textContent = '正在读取交易记录…';
    $('tradesSummary').innerHTML = '';
    $('tradesNotice').textContent = '';
    $('tradesBody').innerHTML = '<div class="card empty">正在读取交易记录…</div>';
    $('tradesViewParams').onclick = () => { $('tradesModal').close(); openEditor('detail', s); };
    $('tradesModal').showModal();
    api('/api/strategies/' + encodeURIComponent(s.id) + '/trades')
      .then(renderTrades)
      .catch(err => {
        $('tradesBody').innerHTML = `<div class="card empty">读取交易记录失败：${esc(err.message)}</div>`;
      });
  }
  // 启动/暂停/恢复/停止：直接打服务端，成功后再刷新运行态
  async function control(action, id, button) {
    const label = {start: '启动', pause: '暂停', resume: '恢复', stop: '停止'}[action];
    if (action === 'stop' && !confirm('停止后这条策略的常驻交易会下线，已产生的成交记录保留。确定停止吗？')) return;
    const original = button ? button.textContent : '';
    if (button) {
      button.disabled = true;
      // 长提示放 title，不能塞进按钮文字：会把卡片底部操作栏撑到换行
      button.textContent = label + '中…';
      button.title = action === 'start' ? '首次启动要拉行情，约 3–8 秒' : '';
    }
    try {
      await api(`/api/strategies/${encodeURIComponent(id)}/${action}`, {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({})        // 空对象也要发：服务端靠它挡住跨站简单请求
      });
      await loadTrading();
    } catch (err) {
      alert(err.message);
      if (button) { button.disabled = false; button.textContent = original; }
    }
  }

  async function act(action, id, button) {
    if (action === 'start' || action === 'pause' || action === 'resume' || action === 'stop') {
      return control(action, id, button);
    }
    const s = items.find(x => x.id === id);
    if (!s) return;if(action==='detail'){openTrades(s);return;}if(action==='edit'){openEditor(action,s);return;}if(action==='delete'&&confirm(`是否确认删除策略“${s.name}”？此操作会从服务端策略库删除。`)){try{await api('/api/strategies/'+encodeURIComponent(id),{method:'DELETE'});await load();}catch(err){alert(err.message);}}}
  async function save(asNew){if(!ready){$('modalMessage').textContent='服务未连接，无法保存。';return;}const name=$('libraryName').value.trim();if(!name){$('modalMessage').textContent='请填写策略名称。';return;}const payload={...(mode==='edit'&&!asNew?{id:currentId}:{}),name,...capture()};if(!payload.symbol){$('modalMessage').textContent='请先选择交易品种（代码 / 中文名 / 板块都能搜）。';$('assetInput').focus();return;}if(payload.fast<2||payload.slow<=payload.fast){$('modalMessage').textContent='周期无效：快线必须大于等于 2 且小于慢线。';return;}try{const saved=await api('/api/strategies',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});currentId=saved.id;$('modalMessage').textContent='已保存到服务端数据库。';await load();closeEditor();}catch(err){$('modalMessage').textContent=err.message;}}
  $('newStrategy').addEventListener('click',()=>openEditor('new'));
  $('librarySearch').addEventListener('input', event => { keyword = event.target.value; render(); });
  $('saveChanges').addEventListener('click',()=>save(false));$('saveAsNew').addEventListener('click',()=>save(true));$('modalClose').addEventListener('click',closeEditor);$('modalCancel').addEventListener('click',closeEditor);
  $('tradesClose').addEventListener('click',()=>$('tradesModal').close());$('tradesCloseFooter').addEventListener('click',()=>$('tradesModal').close());
  $('applyParams').addEventListener('click',()=>{const type=$('maType').value.startsWith('SMA')?'SMA':'EMA';$('maSummary').textContent=`${type}(${$('fast').value}) / ${type}(${$('slow').value})`;});
  // 只在策略库页可见时轮询：切到别的页还在打接口是纯浪费
  window.addEventListener('qice-page-change', event => {
    if (event.detail === 'strategy') startPolling(); else stopPolling();
  });
  // 刷新后直接停在策略库页（#strategy 深链接）时，初始化那次 go() 可能早于本文件加载，
  // 事件收不到。这里补一次判断，否则页面停在这儿也不会自动刷新状态。
  load().then(() => {
    if (document.getElementById('page-strategy')?.classList.contains('active')) startPolling();
  });
})();
