/* 品种诊断页：**消息面 / 情绪为主体，技术面只作交叉校验**。
 *
 * 数据链路
 *   1) 消息面与情绪：GET  /api/diagnostics/context?symbol=
 *      服务端 `context.py` 抓 CBOE VIX、外盘避险资产、品种定向快讯、财经日历、
 *      资金行为与供需，算出可解释的「情绪温度」。选好品种后自动加载（15 分钟缓存）。
 *   2) 技术面速览：GET /api/diagnostics/instrument?symbol=&period=
 *      本地按主力连续分钟线算区间 / 均线 / ADX / ATR / 量能 / 关键位置，秒级返回，
 *      默认折叠，点「算技术面」才展开 —— 页面重心刻意留给消息面。
 *   3) AI 诊断：POST /api/diagnostics/instrument/analyze
 *      模型拿上面两份事实清单，按「消息面为主的六段骨架」写解读；返回值里带
 *      `context` 面板，直接用它刷新第 1 块，避免再抓一次。
 *
 * 口径必须对用户诚实：情绪温度是本地复合代理指标，不是 CBOE VIX；
 * 快讯倾向来自关键词词典，不是语义模型。AI 服务由服务端 .env 绑定，页面不提供配置入口。
 */
(() => {
  const mount = document.getElementById('diagnosticsMount');
  if (!mount) return;

  const $ = id => document.getElementById(id);
  const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
  const num = (v, digits = 2) => v == null || v === '' ? '—' : Number(v).toFixed(digits);
  // 先四舍五入再判符号，避免 +0.000% 被染色；口径与全站「红涨绿跌」一致
  const signed = v => {
    if (v == null || v === '') return '—';
    const text = Number(v).toFixed(3);
    return (Number(text) > 0 ? '+' : '') + text + '%';
  };
  const toneOf = v => {
    const n = Number(Number(v || 0).toFixed(3));
    return n > 0 ? 'positive' : n < 0 ? 'negative' : '';
  };
  const tag = (text, tone = '') => `<span class="tag ${tone}">${esc(text)}</span>`;
  const setMsg = text => { $('diagMsg').textContent = text; };
  const setState = (text, cls) => { $('diagState').textContent = text; $('diagState').className = `tag ${cls}`; };

  mount.innerHTML = `
    <div class="heading">
      <div><h1>品种诊断</h1>
        <p>以消息面与情绪为主线：VIX 恐慌指数、跨市场避险、品种快讯、财经事件与资金行为；
           技术面只作交叉校验，收在下方速览里。</p></div>
      <span class="tag amber" id="diagState">待选择品种</span>
    </div>

    <div class="risk-banner"><span>ⓘ</span><div>消息面来自公开第三方接口，可能延迟或缺失；
      情绪温度是本地按公开公式加权的复合代理指标（不是 CBOE VIX），快讯倾向由关键词词典判定。
      全部内容不构成品种推荐或买卖建议。</div></div>

    <div class="card">
      <div class="card-head"><div><h2>诊断对象</h2>
        <div class="sub">品类与品种都支持搜索：代码、中文名、板块、交易所（精准匹配置顶）</div></div>
        <div class="toolbar"><span class="tag" id="diagPeriodTag">—</span></div>
      </div>
      <div class="form-grid">
        <div class="field"><label for="diagSectorInput">交易品类</label>
          <div class="combo"><input id="diagSectorInput" class="combo-input" type="text" autocomplete="off" spellcheck="false" value="全部品类" placeholder="搜索品类，如 黑色 / nh"><div class="combo-panel" id="diagSectorPanel" hidden></div></div>
          <small id="diagSectorHint">点一下选品类，也可以直接搜</small></div>
        <div class="field"><label for="diagCodeInput">交易品种</label>
          <div class="combo"><input id="diagCodeInput" class="combo-input" type="text" autocomplete="off" spellcheck="false" placeholder="搜索代码 / 中文名，如 rb、螺纹"><input id="diagSymbol" type="hidden" value=""><div class="combo-panel" id="diagCodePanel" hidden></div></div>
          <small id="diagCodeHint">未选择品种</small></div>
        <div class="field"><label for="diagPeriod">K 线周期</label>
          <select class="select" id="diagPeriod">
            <option value="1m">1 分钟（约 3 个交易日）</option>
            <option value="5m">5 分钟</option>
            <option value="15m">15 分钟</option>
            <option value="60m">60 分钟</option>
          </select><small>只影响折叠的技术面速览</small></div>
        <div class="field"><label for="diagScope">AI 诊断范围</label>
          <select class="select" id="diagScope">
            <option value="both">消息面 + 技术面（默认）</option>
            <option value="news">只看消息面与情绪</option>
            <option value="tech">只看技术面</option>
          </select><small>默认让模型把消息、恐慌指数与资金串成叙事</small></div>
      </div>
      <div style="display:flex;gap:8px;margin-top:16px;flex-wrap:wrap;align-items:center">
        <button class="btn primary" id="runAi">✳ AI 诊断</button>
        <button class="btn" id="runSnapshot">算技术面</button>
        <button class="btn" id="runContext">↻ 刷新线索</button>
        <span class="sub" id="diagMsg">选好品种后自动加载消息面线索</span>
      </div>
    </div>

    <div id="contextPanel" class="ctx-panel" style="margin-top:16px">
      <div class="card empty">选择品种后，这里会显示 VIX 恐慌指数、跨市场避险、相关快讯、财经事件与情绪温度。</div>
    </div>

    <details class="tech-fold" id="techFold">
      <summary>技术面速览 <span>均线 / ADX / ATR / 量能 / 关键位置 · 默认收起</span></summary>
      <div class="tech-fold-body"><div id="diagResult"><div class="card empty">点「算技术面」按所选品种计算。</div></div></div>
    </details>

    <div id="aiResult"></div>`;

  /* ---------- 通用渲染 ---------- */
  function statCard(title, value, detail, tone) {
    const cls = tone == null ? '' : toneOf(tone);
    return `<div class="card diag-card"><h3>${esc(title)}</h3><div class="big ${cls}">${esc(value)}</div><p>${esc(detail)}</p></div>`;
  }

  function renderMarkdown(markdown) {
    const blocks = [];
    let listOpen = false;
    const inline = text => {
      let out = esc(text);
      while (out.includes('**')) out = out.replace('**', '<strong>').replace('**', '</strong>');
      return out;
    };
    for (const raw of String(markdown || '').split('\n')) {
      const line = raw.trim();
      if (!line) { if (listOpen) { blocks.push('</ul>'); listOpen = false; } continue; }
      if (line.startsWith('#')) {
        if (listOpen) { blocks.push('</ul>'); listOpen = false; }
        const level = Math.min(6, line.length - line.replace(/^#+/, '').length);
        blocks.push(`<h${level}>${inline(line.slice(level).trim())}</h${level}>`);
      } else if (/^[-*+] /.test(line)) {
        if (!listOpen) { blocks.push('<ul>'); listOpen = true; }
        blocks.push(`<li>${inline(line.slice(2))}</li>`);
      } else if (line.startsWith('>')) {
        if (listOpen) { blocks.push('</ul>'); listOpen = false; }
        blocks.push(`<blockquote>${inline(line.replace(/^>\s*/, ''))}</blockquote>`);
      } else {
        if (listOpen) { blocks.push('</ul>'); listOpen = false; }
        blocks.push(`<p>${inline(line)}</p>`);
      }
    }
    if (listOpen) blocks.push('</ul>');
    return blocks.join('\n');
  }

  const trendTone = text => text === '多头排列' ? 'green' : text === '空头排列' ? 'red' : '';

  /* ---------- 品种选择器（选好后自动加载消息面） ---------- */
  const picker = window.qiceInstrumentPicker
    ? window.qiceInstrumentPicker.create({
        sectorInput: 'diagSectorInput', sectorPanel: 'diagSectorPanel', sectorHint: 'diagSectorHint',
        codeInput: 'diagCodeInput', codePanel: 'diagCodePanel', hidden: 'diagSymbol', hint: 'diagCodeHint',
        onChange: inst => { if (inst) loadContext(false); }
      })
    : null;
  if (!picker) $('diagCodeHint').textContent = '品种选择组件未加载，请刷新页面。';

  const currentSymbol = () => (picker && picker.symbol()) || $('diagSymbol').value || '';

  /* ---------- 消息面与情绪面板 ---------- */
  let contextPanel = null;
  let contextBusy = false;
  let snapshot = null;
  let busy = false;

  function spark(values) {
    const arr = (values || []).map(Number).filter(Number.isFinite);
    if (arr.length < 2) return '';
    const min = Math.min(...arr), max = Math.max(...arr), span = (max - min) || 1;
    const points = arr.map((v, i) =>
      `${(i / (arr.length - 1) * 100).toFixed(1)},${(28 - (v - min) / span * 24).toFixed(1)}`).join(' ');
    return `<svg class="ctx-spark" viewBox="0 0 100 32" preserveAspectRatio="none" aria-hidden="true">
      <polyline points="${points}" fill="none" stroke="#e0a63c" stroke-width="1.6" vector-effect="non-scaling-stroke"/></svg>`;
  }

  function kv(label, value, tone = '') {
    return `<div class="ctx-kv"><span>${esc(label)}</span><b class="${tone}">${value}</b></div>`;
  }

  function renderContext(p) {
    const temp = p.temperature || {}, vix = p.vix, news = p.news || {};
    const score = temp.score;
    const comps = (temp.components || []).map(c => `
      <div class="ctx-comp" title="${esc(c.display || '')} · 权重 ${num(c.weight * 100, 0)}%">
        <span>${esc(c.label)}</span>
        <div class="ctx-bar"><i style="width:${Math.max(0, Math.min(100, Number(c.value) || 0))}%"></i></div>
        <b>${num(c.value, 0)}</b></div>`).join('') || '<div class="ctx-meta">线索不足，无法计算分量。</div>';

    const hero = `<div class="ctx-hero">
      <div class="ctx-score-box">
        <div class="ctx-score">${score == null ? '—' : num(score, 1)}<small>/100</small></div>
        <span class="ctx-level ${esc(temp.tone || '')}">${esc(temp.level || '数据不足')}</span>
        <div class="ctx-sub" style="margin-top:7px">情绪温度 · 越高越紧张</div>
      </div>
      <div class="ctx-comp-list">${comps}</div>
      <div class="ctx-formula">${esc(temp.formula || '')}${temp.missing && temp.missing.length
        ? ` 未获取分量：${esc(temp.missing.join('、'))}` : ''}</div>
    </div>`;

    // VIX 卡：真实指数，与上面的复合温度明确分开
    const vixCard = `<div class="ctx-card">
      <h3><span>VIX 恐慌指数</span>${vix ? tag(vix.level, vix.tone) : ''}</h3>
      ${vix ? `<div class="ctx-big">${num(vix.close)}</div>
        <div class="ctx-sub">CBOE 官方日线 · 收盘 ${esc(vix.date)} · 近 ${vix.sampleDays} 个交易日样本</div>
        ${spark(vix.spark)}
        ${kv('近 1 日', `<span class="${toneOf(vix.change1dPct)}">${signed(vix.change1dPct)}</span>`)}
        ${kv('近 5 日', `<span class="${toneOf(vix.change5dPct)}">${signed(vix.change5dPct)}</span>`)}
        ${kv('近 20 日', `<span class="${toneOf(vix.change20dPct)}">${signed(vix.change20dPct)}</span>`)}
        ${kv('近一年分位', `${num(vix.percentile1y, 1)}%`)}`
        : '<div class="ctx-meta">VIX 数据未获取，情绪温度里的相关分量已剔除。</div>'}
    </div>`;

    // 跨市场避险线索：外盘商品 + 股指 / 美元
    const cross = [
      ...(p.overseas || []).map(x => ({name: x.name, value: x.changePct})),
      ...(p.global || []).filter(x => /美元指数|标普500|纳斯达克|道琼斯|上证指数|恒生指数/.test(String(x.name || '')))
        .map(x => ({name: x.name, value: x.changePct}))
    ];
    const crossRows = cross.length
      ? cross.slice(0, 11).map(x => kv(x.name, `<span class="${toneOf(x.value)}">${signed(x.value)}</span>`)).join('')
      : '<div class="ctx-meta">跨市场行情未获取。</div>';
    const riskClues = (temp.riskClues || []).length
      ? `<div class="ctx-sub" style="margin-top:8px">判定线索：${esc(temp.riskClues.join('；'))}</div>` : '';
    const riskCard = `<div class="ctx-card">
      <h3><span>跨市场避险</span><span class="sub" style="font-weight:400">红涨绿跌</span></h3>
      ${crossRows}${riskClues}
    </div>`;

    // 资金与供需：quote / basis / inventory 都来自 macro 的 15 分钟缓存
    const q = p.quote || {}, b = p.basis, inv = p.inventory;
    const fundRows = [
      q.changePct1d != null ? kv('近 1 日涨跌', `<span class="${toneOf(q.changePct1d)}">${signed(q.changePct1d)}</span>`) : '',
      q.changePct5d != null ? kv('近 5 日涨跌', `<span class="${toneOf(q.changePct5d)}">${signed(q.changePct5d)}</span>`) : '',
      q.changePct20d != null ? kv('近 20 日涨跌', `<span class="${toneOf(q.changePct20d)}">${signed(q.changePct20d)}</span>`) : '',
      q.openInterestChangePct != null ? kv('持仓量变化', `<span class="${toneOf(q.openInterestChangePct)}">${signed(q.openInterestChangePct)}</span>`) : '',
      q.volumeChangePct != null ? kv('量能偏离均值', `<span class="${toneOf(q.volumeChangePct)}">${signed(q.volumeChangePct)}</span>`) : '',
      q.vol20Pct != null ? kv('20 日年化波动率', `${num(q.vol20Pct)}%`) : '',
      q.vol20Percentile != null ? kv('波动率一年分位', `${num(q.vol20Percentile, 1)}%`) : '',
    ].join('');
    const supplyRows = [
      b ? kv('现货基差', `${num(b.basis)}（${esc(b.date || '')}）`) : kv('现货基差', '未获取'),
      b && b.basisRatePct != null ? kv('基差率', `${num(b.basisRatePct * 100, 3)}%`) : '',
      inv ? kv('库存', `${num(inv.value)}（${esc(inv.date || '')}）`) : kv('库存', '未获取'),
      inv && inv.changePct5d != null ? kv('库存近 5 期变化', `<span class="${toneOf(inv.changePct5d)}">${signed(inv.changePct5d)}</span>`) : '',
    ].join('');
    const fundCard = `<div class="ctx-card">
      <h3><span>资金行为与供需</span></h3>
      ${fundRows || '<div class="ctx-meta">品种行情未获取。</div>'}
      <div style="border-top:1px dashed #e9edf5;margin-top:7px;padding-top:5px">${supplyRows}</div>
    </div>`;

    // 相关快讯 + 宏观背景 + 财经日历
    const toneCls = t => t === '偏多' ? 'positive' : t === '偏空' ? 'negative' : '';
    const newsRow = item => `<div class="ctx-news-item">
      <span class="ctx-news-time">${esc(String(item.time || '').slice(5, 16))}</span>
      <span class="ctx-tone ${toneCls(item.tone)}">${esc(item.tone || '中性')}</span>
      <span class="ctx-scope" title="${esc(item.scope || '')}">${esc(item.scope || '')}</span>
      <span class="ctx-text" title="${esc(item.text || '')}">${esc(item.text || '')}</span></div>`;
    const items = news.items || [];
    const mainNews = items.slice(0, 8).map(newsRow).join('');
    const moreNews = items.length > 8
      ? `<details style="margin-top:4px"><summary class="sub" style="cursor:pointer;padding:6px 0">还有 ${items.length - 8} 条相关快讯，点开查看</summary>${items.slice(8).map(newsRow).join('')}</details>` : '';
    const background = (news.marketItems || []).slice(0, 5).map(item =>
      `<div class="ctx-news-item" style="grid-template-columns:72px minmax(0,1fr)">
        <span class="ctx-news-time">${esc(String(item.time || '').slice(5, 16))}</span>
        <span class="ctx-text">${esc(item.text || '')}</span></div>`).join('');
    const events = (p.calendar || []).slice(0, 8).map(e => `<div class="ctx-event">
      <span class="ctx-news-time">${esc(e.time || '')}</span>
      <span class="ctx-scope">${esc(e.region || '')}</span>
      <span class="ctx-text">${esc(e.event || '')}${e.forecast != null ? ` · 预期 ${esc(e.forecast)}` : ''}${e.stars ? ` · ${'★'.repeat(Math.min(3, Number(e.stars)))}` : ''}</span></div>`).join('');

    const newsCard = `<div class="card">
      <div class="card-head"><div><h2>消息面线索</h2>
        <div class="sub">${esc(news.category || '—')} 分类 · 相关 ${news.relatedCount || 0} 条
          · 偏多 <b class="positive">${news.bullCount || 0}</b> / 偏空 <b class="negative">${news.bearCount || 0}</b>
          · 倾向分 ${num(news.toneScore, 2)}</div></div>
        ${tag('关键词词典', '')}</div>
      <div class="ctx-warn" style="margin-bottom:9px">${esc(news.method || '关键词规则判定')}；
        标「宏观线索」的条目是通用宏观消息，不等于该品种的直接供需变化。</div>
      ${mainNews || '<div class="ctx-meta">本次没有抓到与品种关键词相关的快讯。</div>'}
      ${moreNews}
      ${background ? `<details style="margin-top:6px"><summary class="sub" style="cursor:pointer;padding:6px 0">宏观背景快讯（${(news.marketItems || []).length} 条）</summary>${background}</details>` : ''}
      ${events ? `<div class="section-title">今日高重要性财经事件 · ${esc(p.calendarDate || '')}</div>${events}` : ''}
    </div>`;

    const missing = (p.unavailable || []).map(x => `${esc(x.key)}（${esc(x.reason || '')}）`).join('；');
    const meta = `<div class="ctx-meta">
      数据抓取于 ${esc(String(p.generatedAt || '').replace('T', ' ').slice(0, 19))}
      ${p.cached ? ' · 命中 15 分钟缓存' : ' · 本次实时抓取'}
      ${p.elapsedMs != null ? ` · 抓取耗时 ${esc(p.elapsedMs)} ms` : ''}
      ${missing ? `<div class="ctx-warn" style="margin-top:7px">未获取：${missing}</div>` : ''}
      <div style="margin-top:5px">${esc(p.notice || '')}</div>
    </div>`;

    $('contextPanel').innerHTML = hero
      + `<div class="ctx-grid">${vixCard}${riskCard}${fundCard}</div>`
      + newsCard + meta;
  }

  async function loadContext(force) {
    const symbol = currentSymbol();
    if (!symbol) {
      $('contextPanel').innerHTML = '<div class="card empty">先在上面选一个品种，消息面线索会自动加载。</div>';
      return;
    }
    if (contextBusy) return;
    contextBusy = true;
    const button = $('runContext');
    button.disabled = true;
    button.textContent = '抓取中…';
    setMsg(force ? '正在重新抓取公开消息面数据（约 5–10 秒）…' : '正在读取消息面线索…');
    setState('读取线索中', 'amber');
    try {
      const url = `/api/diagnostics/context?symbol=${encodeURIComponent(symbol)}${force ? '&force=1' : ''}`;
      const response = await fetch(url);
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || '消息面读取失败');
      contextPanel = data;
      renderContext(data);
      const temp = data.temperature || {};
      setMsg(`消息面已就绪：情绪温度 ${temp.score == null ? '—' : num(temp.score, 1)}（${temp.level || '—'}）· VIX ${data.vix ? num(data.vix.close) : '未获取'} · 相关快讯 ${(data.news || {}).relatedCount || 0} 条。`);
      setState('线索已就绪', 'green');
    } catch (error) {
      $('contextPanel').innerHTML = `<div class="card empty">${esc(error.message)}</div>`;
      setMsg(error.message);
      setState('线索读取失败', 'amber');
    } finally {
      contextBusy = false;
      button.disabled = false;
      button.textContent = '↻ 刷新线索';
    }
  }

  /* ---------- 技术面速览（折叠区） ---------- */
  function renderSnapshot(data) {
    const r = data.range, t = data.trend, v = data.volatility, lv = data.levels, vol = data.volume;
    const inst = data.instrument || {};
    const streakText = t.streak && t.streak.bars
      ? `${t.streak.direction === 'up' ? '连涨' : t.streak.direction === 'down' ? '连跌' : '平盘'} ${t.streak.bars} 根`
      : '—';
    $('diagResult').innerHTML = `
      <div class="card" style="margin-bottom:16px">
        <div class="card-head">
          <div><h2>${esc(inst.name || data.symbol)} · ${esc(String(inst.code || '').toUpperCase())}</h2>
            <div class="sub">${esc(inst.exchangeName || '')} · ${esc(inst.sector || '')} · 主力连续 ${esc(data.continuous || '')}
              · 乘数 ${esc(inst.multiplier ?? '—')} · 最小变动 ${esc(inst.tick ?? '—')}</div></div>
          <div class="toolbar">${tag(data.periodLabel || data.period, 'amber')}${tag(t.alignment, trendTone(t.alignment))}${tag(t.strength)}</div>
        </div>
        <div class="status-line"><span>样本区间</span><b>${esc(String(r.startTime || '').replace('T', ' ').slice(0, 16))} 至 ${esc(String(r.endTime || '').replace('T', ' ').slice(0, 16))} · ${r.bars} 根</b></div>
        <div class="status-line"><span>行情来源</span><b>${esc(data.source || '—')}</b></div>
      </div>
      <div class="grid diag-grid">
        ${statCard('区间涨跌', signed(r.changePct), `首 ${num(r.firstClose)} → 末 ${num(r.lastClose)}`, r.changePct)}
        ${statCard('当前价区间位置', r.positionPct == null ? '—' : `${r.positionPct}%`, `区间最低 ${num(r.low)} / 最高 ${num(r.high)} · 振幅 ${num(r.amplitudePct)}%`)}
        ${statCard('趋势强度', t.strength, `ADX14 ${num(t.adx)} · +DI ${num(t.plusDi)} / −DI ${num(t.minusDi)}`)}
      </div>
      <div class="grid diag-grid">
        ${statCard('波动（ATR/价格）', v.atrPct == null ? '—' : `${num(v.atrPct)}%`, `ATR14 ${num(v.atr14, 4)} · 平均单根振幅 ${num(v.avgBarRangePct)}%`)}
        ${statCard('量能比（近20/前60）', vol.ratio == null ? '—' : `${num(vol.ratio)}×`, `近 20 根均量 ${num(vol.avg20, 0)} · 前 60 根 ${num(vol.avgBase, 0)}`)}
        ${statCard('跳空次数（≥0.2%）', `${v.gapCount} 次`, `最大单根 +${num(v.maxUpBarPct)}% / ${num(v.maxDownBarPct)}%`)}
      </div>
      <div class="grid" style="grid-template-columns:1fr 1fr;gap:16px;margin-bottom:12px">
        <div class="card">
          <div class="card-head"><h2>趋势结构</h2><span class="tag">${esc(t.alignment)}</span></div>
          <div class="status-line"><span>MA5 / MA10</span><b>${num(t.ma5)} / ${num(t.ma10)}</b></div>
          <div class="status-line"><span>MA20 / MA60</span><b>${num(t.ma20)} / ${num(t.ma60)}</b></div>
          <div class="status-line"><span>MA20 斜率</span><b class="${toneOf(t.ma20SlopePct)}">${signed(t.ma20SlopePct)}</b></div>
          <div class="status-line"><span>末端连续</span><b>${esc(streakText)}</b></div>
          <div class="status-line"><span>近 40 根涨跌</span><b class="${toneOf(t.last40ChangePct)}">${signed(t.last40ChangePct)}</b></div>
        </div>
        <div class="card">
          <div class="card-head"><h2>关键位置</h2><span class="tag">距当前价</span></div>
          <div class="status-line"><span>样本最高 / 最低</span><b>${num(lv.windowHigh)} ${signed(lv.toWindowHighPct)} / ${num(lv.windowLow)} ${signed(lv.toWindowLowPct)}</b></div>
          <div class="status-line"><span>近 40 根高 / 低</span><b>${num(lv.recentHigh)} ${signed(lv.toRecentHighPct)} / ${num(lv.recentLow)} ${signed(lv.toRecentLowPct)}</b></div>
          <div class="status-line"><span>MA20</span><b>${num(lv.ma20)} ${signed(lv.toMa20Pct)}</b></div>
          <div class="status-line"><span>MA60</span><b>${num(lv.ma60)} ${signed(lv.toMa60Pct)}</b></div>
          <div class="status-line"><span>跳空明细</span><b>${(v.gaps || []).length ? v.gaps.map(g => `${String(g.timestamp).slice(5, 16).replace('T', ' ')} ${signed(g.pct)}`).join('；') : '无'}</b></div>
        </div>
      </div>`;
  }

  function renderAi(data) {
    $('aiResult').innerHTML = `
      <div class="card" style="margin-top:16px">
        <div class="card-head">
          <div><h2>AI 诊断</h2>
            <div class="sub">模型 ${esc(data.model || '')} · ${esc(String(data.analyzedAt || '').replace('T', ' ').slice(0, 16))}
              · ${esc(data.scope || '')} ${data.contextAttached ? ` · 已附消息面（${esc(data.contextSource || '')}）` : ' · 未附消息面'}</div></div>
          ${tag(data.contextAttached ? '消息面为主' : '纯技术面', data.contextAttached ? 'green' : '')}
        </div>
        <article class="md-body">${renderMarkdown(data.analysis)}</article>
        <div class="note">${esc(data.notice || '')}</div>
      </div>`;
  }

  async function loadSnapshot() {
    const symbol = currentSymbol();
    if (!symbol) { setMsg('请先选择品种。'); return; }
    if (busy) return;
    busy = true;
    const button = $('runSnapshot');
    button.disabled = true;
    button.textContent = '计算中…';
    setMsg('正在拉取主力连续 K 线…');
    $('techFold').open = true;
    try {
      const response = await fetch(`/api/diagnostics/instrument?symbol=${encodeURIComponent(symbol)}&period=${encodeURIComponent($('diagPeriod').value)}`);
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || '诊断失败');
      snapshot = data;
      renderSnapshot(data);
      $('diagPeriodTag').textContent = `${data.periodLabel} · ${data.range.bars} 根`;
      setMsg(`技术面已算完（${data.range.bars} 根 bar）；页面主体仍是上方消息面。`);
    } catch (error) {
      $('diagResult').innerHTML = `<div class="card empty">${esc(error.message)}</div>`;
      setMsg('技术面未完成');
    } finally {
      busy = false;
      button.disabled = false;
      button.textContent = '算技术面';
    }
  }

  async function runAi() {
    const symbol = currentSymbol();
    if (!symbol) { setMsg('请先选择品种。'); return; }
    if (busy) return;
    const scope = $('diagScope').value;
    busy = true;
    const button = $('runAi');
    button.disabled = true;
    button.textContent = '诊断中…';
    setMsg(scope === 'tech' ? '正在让模型写技术面解读（约 20–40 秒）…'
      : '正在读消息面并让模型写六段诊断（约 30–60 秒）…');
    setState('AI 诊断中', 'amber');
    try {
      const response = await fetch('/api/diagnostics/instrument/analyze', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({symbol, period: $('diagPeriod').value,
                              includeMacro: scope !== 'tech', includeTech: scope !== 'news'})
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || '诊断失败');
      snapshot = data;
      renderSnapshot(data);
      renderAi(data);
      if (data.context) { contextPanel = data.context; renderContext(data.context); }
      $('diagPeriodTag').textContent = `${data.periodLabel} · ${data.range.bars} 根`;
      setMsg(data.contextAttached ? '诊断完成，已按消息面为主的六段骨架解读。' : '诊断完成（本次未附消息面）。');
      setState('AI 诊断完成', 'green');
    } catch (error) {
      setMsg(error.message);
      setState('AI 诊断失败', 'amber');
    } finally {
      busy = false;
      button.disabled = false;
      button.textContent = '✳ AI 诊断';
    }
  }

  $('runSnapshot').addEventListener('click', loadSnapshot);
  $('runAi').addEventListener('click', runAi);
  $('runContext').addEventListener('click', () => loadContext(true));
  $('diagPeriod').addEventListener('change', () => {
    $('diagPeriodTag').textContent = '—';
    if (snapshot) setMsg('周期已改，重新点「算技术面」。');
  });
  window.addEventListener('qice-page-change', event => {
    if (event.detail === 'diagnostics' && picker) picker.reload();
  });
})();
