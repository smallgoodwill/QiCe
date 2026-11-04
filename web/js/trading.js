(() => {
  const mount=document.getElementById('paperMount');
  const $=id=>document.getElementById(id);
  const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const cash=v=>v==null?'—':(v<0?'−':'')+'¥ '+Math.abs(+v||0).toLocaleString('zh-CN',{maximumFractionDigits:2});
  const statusLabel={running:'运行中',completed:'已完成',stopped:'已取消',paused:'已暂停'};

  // 交易模式：paper=历史行情回放，live=实盘（通道未接入，仅展示前置条件）
  const MODE_KEY='qice.tradeMode';
  let mode=localStorage.getItem(MODE_KEY)==='live'?'live':'paper';

  const REQUIREMENTS=[
    ['柜台接入','交易前置/经纪商 API 授权、登录与结算确认，断线自动重连。'],
    ['委托与回报','下单、撤单、部分成交、拒单与错单处理，回报逐笔落库。'],
    ['持仓与资金对账','柜台持仓/资金与本地账本定时核对，差异即冻结交易。'],
    ['风控闸门','单笔风险 ≤ 账户 1%、单日亏损停机、最大持仓与价格偏离校验，前置拦截。'],
    ['合约规则','交易时段与夜盘、涨跌停、保证金、最小变动价位、换月处理。'],
    ['审计留痕','每笔委托记录决策依据、参数快照与人工复核结果，可回放追溯。']
  ];

  mount.innerHTML=`<div class="heading"><div><h1>策略交易</h1><p>模拟盘用历史行情逐 bar 回放；实盘通道尚未接入，当前不会发送任何真实委托。</p></div><div class="toolbar"><span class="tag amber" id="tradingApiState">连接服务中</span><div class="mode-switch" role="tablist" aria-label="交易模式"><button type="button" role="tab" data-mode="paper"><i class="dot"></i>模拟盘</button><button type="button" role="tab" data-mode="live"><i class="dot"></i>实盘</button></div></div></div>

  <div class="mode-panel" id="modePaper">
  <div class="risk-banner"><span>ⓘ</span><div>本页以导入的历史行情逐 bar 回放，不是实时行情。取消运行时如有持仓，会按最后处理 bar 收盘价模拟平仓并记录费用。</div></div>
  <div class="grid paper-layout"><div class="card"><div class="card-head"><h2>启动模拟交易</h2><span class="sub">最多同时运行 10 条策略</span></div>
  <div class="field"><label>行情 CSV</label><div class="file-drop">选择已授权的历史行情<input id="tradeFile" type="file" accept=".csv,text/csv"><button class="btn" id="tradeSample" type="button" style="margin-top:8px">载入内置样例行情</button><small id="tradeDataLabel">尚未载入行情</small></div></div>
  <div class="field" style="margin-top:12px"><label>策略库</label><select id="tradeStrategy" class="select"><option value="">选择已保存策略…</option></select><div id="tradeStrategyNote" class="metric-tip">策略必须先保存在“我的策略库”。</div></div>
  <div class="form-grid" style="margin-top:12px"><div class="field"><label>单策略初始资金</label><input id="tradeCapital" type="number" value="1000000" min="1" step="10000"></div><div class="field"><label>合约乘数</label><input id="tradeMultiplier" type="number" min="0.01" step="0.1" placeholder="按合约核实"></div><div class="field"><label>每笔手数</label><input id="tradeQty" type="number" value="1" min="1" step="1"></div><div class="field"><label>每边手续费 / 手</label><input id="tradeFee" type="number" min="0" step="0.1" placeholder="按费率核实"></div><div class="field"><label>最小变动价位</label><input id="tradeTick" type="number" min="0.0001" step="0.1" placeholder="按合约核实"></div><div class="field"><label>单边滑点（跳）</label><input id="tradeSlip" type="number" value="1" min="0" step="1"></div><div class="field"><label>单日已实现亏损停开线</label><input id="tradeDailyLoss" type="number" value="1000" min="0" step="100"></div><label class="checkline"><input id="tradeBlackout" type="checkbox" checked> 遵循 CSV 事件禁入标记</label></div>
  <label class="checkline contract-check"><input id="tradeContractChecked" type="checkbox"> 已核对该品种的乘数、手续费和最小变动价位</label><div class="toolbar paper-controls"><button class="btn primary" id="startStrategyTrade">▶　开始模拟交易</button><button class="btn" id="refreshTrading">刷新运行状态</button></div><div id="tradeMessage" class="metric-tip"></div></div>
  <div class="card"><div class="card-head"><h2>策略运行账户</h2><span class="tag" id="runningCount">0 / 10 运行中</span></div><div id="tradingRuns"><div class="empty">正在读取策略运行记录…</div></div><div class="note">每条运行记录独立保存。取消一条策略不会停止其他策略；已完成或已取消的记录仍保留在列表中。</div></div></div>
  <div class="card" style="margin-top:16px"><div class="card-head"><h2>策略模拟成交</h2><span class="sub">所有运行的模拟账本</span></div><div class="table-scroll"><table class="table"><thead><tr><th>运行 ID</th><th>策略</th><th>品种</th><th>方向</th><th>开仓时间 / 价格</th><th>平仓时间 / 价格</th><th>手数</th><th>费用</th><th>净盈亏</th><th>退出原因</th></tr></thead><tbody id="tradingTrades"><tr><td colspan="10" class="empty">暂无成交</td></tr></tbody></table></div></div>
  </div>

  <div class="mode-panel" id="modeLive">
  <div class="card"><div class="live-hero"><div class="live-icon">⚠</div><div><h2>实盘通道未接入</h2><p>本版本不连接券商柜台，也不会发送任何真实委托。切到这个页签不影响模拟盘运行；下面列出接入实盘必须先补齐的环节，全部通过前不开放下单。</p></div><span class="tag amber">未接入</span></div>
  <div class="require-list">${REQUIREMENTS.map((r,i)=>`<div class="require-row"><span class="mark">${i+1}</span><div><b>${esc(r[0])}</b><span>${esc(r[1])}</span></div><span class="st">未就绪</span></div>`).join('')}</div>
  <div class="toolbar" style="margin-top:16px"><button class="btn primary" id="enableLive" disabled title="接入柜台并通过风控校验后才可启用">启用实盘交易</button><span class="sub" id="liveGate">0 / ${REQUIREMENTS.length} 项就绪 · 实盘开关处于关闭状态</span></div></div>
  <div class="risk-banner" style="margin-top:16px"><span>⚠</span><div><strong>真实资金风险。</strong>即使后续接入实盘，也请先以小资金或仿真账户验证；单笔风险按账户 1% 上限执行，触发单日亏损停机线后当日不再开新仓。</div></div>
  </div>`;

  let bars=[],strategies=[],runs=[],polling=false,pollTimer=null;

  async function api(path,options={}){const response=await fetch(path,options);let body={};try{body=await response.json();}catch{}if(!response.ok)throw Error(body.error||`服务请求失败 (${response.status})`);return body;}

  function setMode(next,silent){
    mode=next==='live'?'live':'paper';
    localStorage.setItem(MODE_KEY,mode);
    document.querySelectorAll('.mode-switch button').forEach(b=>{
      const on=b.dataset.mode===mode;
      b.classList.toggle('active',on);
      b.setAttribute('aria-selected',on?'true':'false');
    });
    $('modePaper').classList.toggle('active',mode==='paper');
    $('modeLive').classList.toggle('active',mode==='live');
    if(mode==='paper'){ensurePolling();}
    else if(pollTimer){clearInterval(pollTimer);pollTimer=null;}   // 实盘页签不推进模拟盘
    if(!silent)$('tradeMessage').textContent='';
  }

  function fillStrategies(items){strategies=items||[];const select=$('tradeStrategy'),selected=select.value;select.innerHTML='<option value="">选择已保存策略…</option>'+strategies.map(s=>`<option value="${esc(s.id)}">${esc(s.name)} · ${esc(s.symbol||'未设品种')}</option>`).join('');if(strategies.some(s=>s.id===selected))select.value=selected;updateStrategyNote();}
  function chosen(){return strategies.find(s=>s.id===$('tradeStrategy').value);}
  function updateStrategyNote(){const s=chosen();$('tradeStrategyNote').textContent=s?`${s.maType}(${s.fast}/${s.slow}) · ATR ${s.atr?'开启':'关闭'} · RSI ${s.rsi?'开启':'关闭'}${s.macd||s.bollinger?'；当前交易引擎不支持该策略的 MACD / 布林带过滤':''}`:'策略必须先保存在“我的策略库”。';}
  async function loadRuns(){try{runs=await api('/api/trading/runs');$('tradingApiState').textContent='服务端已连接';$('tradingApiState').className='tag green';renderRuns();}catch(e){$('tradingApiState').textContent='服务未连接';$('tradingApiState').className='tag amber';$('tradeMessage').textContent=e.message;}}
  function renderRuns(){const active=runs.filter(r=>r.status==='running'||r.status==='paused').length;$('runningCount').textContent=`${active} / 10 运行中`;$('tradingRuns').innerHTML=runs.length?`<div class="table-scroll"><table class="table"><thead><tr><th>状态</th><th>策略 / 品种</th><th>进度</th><th>模拟权益</th><th>已实现盈亏</th><th>当前持仓</th><th>启动时间</th><th>操作</th></tr></thead><tbody>${runs.map(r=>`<tr><td><span class="tag ${r.status==='running'?'green':r.status==='stopped'?'amber':''}">${statusLabel[r.status]||esc(r.status)}</span></td><td><b>${esc(r.strategyName)}</b><br><span class="muted">${esc(r.symbol)}</span></td><td>${r.cursor}/${r.totalBars} bar (${(+r.progressPct).toFixed(1)}%)</td><td>${cash(r.equity)}</td><td class="${r.realizedPnl>=0?'positive':'negative'}">${cash(r.realizedPnl)}</td><td>${r.position?`持有 ${r.position.side==='LONG'?'多头':'空头'} ${r.position.qty||''} 手`:'空仓'}</td><td>${esc((r.createdAt||'').replace('T',' ').slice(0,16))}</td><td>${['running','paused'].includes(r.status)?`<button class="btn danger" data-cancel="${esc(r.runId)}">取消</button>`:'—'}</td></tr>`).join('')}</tbody></table></div>`:'<div class="empty">还没有策略运行记录。选择策略、导入行情并点击“开始模拟交易”。</div>';document.querySelectorAll('[data-cancel]').forEach(b=>b.addEventListener('click',()=>cancelRun(b.dataset.cancel)));}
  async function loadTrades(){try{const trades=await api('/api/trading/trades');$('tradingTrades').innerHTML=trades.length?trades.slice(0,100).map(t=>`<tr><td>${esc((t.runId||'').slice(0,8))}</td><td>${esc(t.strategyName)}</td><td>${esc(t.symbol)}</td><td>${esc(t.side)}</td><td>${esc(t.entryTime)} / ${(+t.entryPrice).toFixed(2)}</td><td>${esc(t.exitTime)} / ${(+t.exitPrice).toFixed(2)}</td><td>${esc(t.qty)}</td><td>${cash(t.fees)}</td><td class="${t.netPnl>=0?'positive':'negative'}">${cash(t.netPnl)}</td><td>${esc(t.exitReason)}</td></tr>`).join(''):'<tr><td colspan="10" class="empty">暂无模拟成交</td></tr>';}catch(e){$('tradingTrades').innerHTML=`<tr><td colspan="10" class="empty">${esc(e.message)}</td></tr>`;}}
  async function refresh(){if(polling)return;polling=true;try{const active=runs.filter(r=>r.status==='running');if(active.length){await Promise.all(active.map(r=>api(`/api/trading/runs/${encodeURIComponent(r.runId)}/step`,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}).catch(()=>null)));}await loadRuns();await loadTrades();ensurePolling();}finally{polling=false;}}
  async function startRun(){const strategy=chosen();if(!strategy){$('tradeMessage').textContent='请先从策略库选择一条已保存策略。';return;}if(!bars.length){$('tradeMessage').textContent='请先选择有效的历史行情 CSV。';return;}if(strategy.macd||strategy.bollinger){$('tradeMessage').textContent='当前模拟引擎不支持该策略的 MACD / 布林带过滤，请先在策略库调整。';return;}if(['tradeMultiplier','tradeFee','tradeTick'].some(id=>$(id).value==='')){$('tradeMessage').textContent='请填写合约乘数、每边手续费和最小变动价位。';return;}if(!$('tradeContractChecked').checked){$('tradeMessage').textContent='开始前请确认合约参数。';return;}const config={fast:strategy.fast,slow:strategy.slow,maType:strategy.maType,atr:!!strategy.atr,atrPeriod:strategy.atrPeriod||14,atrMaxPct:strategy.atrMaxPct??0.4,rsi:!!strategy.rsi,rsiPeriod:+((strategy.rsiParams||'14').match(/\d+/)||[14])[0],rsiUpper:+((strategy.rsiParams||'70').match(/\d+\s*$/)||[70])[0],capital:+$('tradeCapital').value,multiplier:+$('tradeMultiplier').value,qty:+$('tradeQty').value,feePerSide:+$('tradeFee').value,tickSize:+$('tradeTick').value,slippageTicks:+$('tradeSlip').value,slippage:+$('tradeTick').value*+$('tradeSlip').value,dailyLossLimit:+$('tradeDailyLoss').value,useBlackout:$('tradeBlackout').checked};$('startStrategyTrade').disabled=true;try{await api('/api/trading/runs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({strategyId:strategy.id,bars,config})});$('tradeMessage').textContent=`“${strategy.name}”模拟交易已启动；可继续选择其他策略并启动。`;await loadRuns();ensurePolling();}catch(e){$('tradeMessage').textContent=e.message;}finally{$('startStrategyTrade').disabled=false;}}
  async function cancelRun(id){if(!confirm('确认取消这条策略运行？如有持仓，将按当前 bar 收盘价模拟退出。'))return;try{await api(`/api/trading/runs/${encodeURIComponent(id)}/cancel`,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});$('tradeMessage').textContent='该策略已取消，其他策略继续运行。';await loadRuns();await loadTrades();ensurePolling();}catch(e){$('tradeMessage').textContent=e.message;}}
  $('tradeFile').addEventListener('change',async e=>{const file=e.target.files?.[0];if(!file)return;try{bars=window.qiceParseBars(await file.text());$('tradeDataLabel').textContent=`${file.name} · ${bars.length} 根`; $('tradeMessage').textContent='行情已载入。启动每条策略时会分别保存该行情样本。';}catch(e){bars=[];$('tradeDataLabel').textContent='文件无效';$('tradeMessage').textContent=e.message;}});
  $('tradeSample').addEventListener('click',async()=>{try{const d=await api('/api/market/sample');bars=d.bars;$('tradeDataLabel').textContent=`${d.name} · ${bars.length} 根 · 合成演示`;$('tradeMessage').textContent=d.notice||'';}catch(e){bars=[];$('tradeDataLabel').textContent='内置样例不可用';$('tradeMessage').textContent=e.message;}});
  $('tradeStrategy').addEventListener('change',updateStrategyNote);$('startStrategyTrade').addEventListener('click',startRun);$('refreshTrading').addEventListener('click',refresh);
  document.querySelectorAll('.mode-switch button').forEach(b=>b.addEventListener('click',()=>setMode(b.dataset.mode)));
  function ensurePolling(){if(mode!=='paper'){if(pollTimer){clearInterval(pollTimer);pollTimer=null;}return;}const active=runs.some(r=>r.status==='running');if(active&&!pollTimer)pollTimer=setInterval(refresh,1200);if(!active&&pollTimer){clearInterval(pollTimer);pollTimer=null;}}
  window.addEventListener('strategy-library-updated',e=>fillStrategies(window.strategyLibrary?.all()||e.detail||[]));
  setMode(mode,true);
  (async()=>{try{fillStrategies(window.strategyLibrary?.all()||[]);await loadRuns();await loadTrades();ensurePolling();}catch(e){$('tradeMessage').textContent=e.message;}})();
})();
