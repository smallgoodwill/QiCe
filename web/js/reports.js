/* 每日 AI 诊断报告：生成、查看历史、导出 Markdown / HTML。
 * 与「品种诊断」页的分工：那边是算出来的统计表，这里是模型写的复盘文字。
 * 同一天的同一段行情只会保留最新一份（后端按 date+runId+symbol 做唯一约束）。
 */
(() => {
  const mount = document.getElementById('dailyMount');
  if (!mount) return;

  const $ = id => document.getElementById(id);
  const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
  const money = n => n == null ? '—' : (n < 0 ? '−' : '') + '¥ ' + Math.abs(n).toLocaleString('zh-CN', {maximumFractionDigits: 2});

  mount.innerHTML = `
    <div class="heading">
      <div><h1>每日 AI 诊断</h1>
        <p>基于模拟盘成交记录生成复盘：今日概览、妙手、失误点、改进建议、明日观察。</p></div>
      <div class="toolbar">
        <select class="select" id="reportRun"><option value="">最近一次模拟</option></select>
        <button class="btn primary" id="genReport">生成今日报告</button>
      </div>
    </div>
    <div class="risk-banner"><span>ⓘ</span><div>报告由 AI 依据模拟数据生成，只评价策略表现，不构成投资建议；关键数字请以页面统计为准。</div></div>
    <div class="grid" style="grid-template-columns:minmax(0,1.6fr) minmax(280px,.7fr);align-items:start">
      <div class="card" id="reportBody"><div class="empty">还没有报告。点右上角「生成今日报告」，会用最近一次模拟的成交跑一份复盘。</div></div>
      <div class="card"><div class="card-head"><h2>历史报告</h2><span class="tag" id="reportCount">—</span></div>
        <div id="reportList"><div class="empty">暂无记录</div></div>
        <div class="note">报告保存在本地数据库（ai_reports 表），同一天同一段行情重新生成会覆盖旧的那份。</div>
      </div>
    </div>`;

  let reports = [], currentId = null;

  /* ---- 极简 Markdown 渲染（标题/列表/引用/粗体/分隔线） ---- */
  function inline(text) {
    let out = esc(text);
    while (out.includes('**')) out = out.replace('**', '<strong>').replace('**', '</strong>');
    while (out.includes('`')) out = out.replace('`', '<code>').replace('`', '</code>');
    return out;
  }
  function renderMarkdown(markdown) {
    const blocks = [];
    let listOpen = false;
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
      } else if (/^(-{3,}|\*{3,}|_{3,})$/.test(line)) {
        if (listOpen) { blocks.push('</ul>'); listOpen = false; }
        blocks.push('<hr>');
      } else {
        if (listOpen) { blocks.push('</ul>'); listOpen = false; }
        blocks.push(`<p>${inline(line)}</p>`);
      }
    }
    if (listOpen) blocks.push('</ul>');
    return blocks.join('\n');
  }

  /* ---- 历史列表 ---- */
  async function loadList(autoOpen) {
    try {
      const response = await fetch('/api/reports');
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || '读取报告列表失败');
      reports = data.items || [];
      $('reportCount').textContent = `${reports.length} 篇`;
      $('reportList').innerHTML = reports.length ? reports.map(item => `
        <div class="report-item" data-id="${esc(item.id)}">
          <div class="report-item-top">
            <b>${esc(item.date)}</b>
            <span class="tag ${item.id === currentId ? 'green' : ''}">${esc(item.symbol || '—')}</span>
          </div>
          <div class="sub">${esc(item.strategyName || '—')}</div>
          <div class="sub">${esc(item.summary || '')}</div>
        </div>`).join('') : '<div class="empty">暂无记录</div>';
      document.querySelectorAll('#reportList .report-item').forEach(node => {
        node.addEventListener('click', () => loadReport(node.dataset.id));
      });
      // 进页面直接摊开最新一份，省得用户还要在右边点一下
      if (autoOpen && !currentId && reports.length) loadReport(reports[0].id);
    } catch (error) {
      $('reportList').innerHTML = `<div class="empty">${esc(error.message)}</div>`;
    }
  }

  /* ---- 模拟记录下拉（选哪次模拟来复盘） ---- */
  async function loadRuns() {
    try {
      const list = await (await fetch('/api/simulate/history')).json();
      const select = $('reportRun');
      const options = (Array.isArray(list) ? list : []).slice(0, 20).map(row =>
        `<option value="${esc(row.runId)}">${esc(row.startTime || '')}~${esc(row.endTime || '')} · ${esc(row.strategyName || '')} · ${esc(row.symbol || '')}</option>`);
      select.innerHTML = '<option value="">最近一次模拟</option>' + options.join('');
    } catch (_) { /* 下拉只是便利，失败不影响生成 */ }
  }

  /* ---- 报告详情 ---- */
  function statsCards(stats) {
    if (!stats) return '';
    const ratio = v => v == null ? '—' : `${v}%`;
    return `<div class="grid diag-grid" style="margin-bottom:14px">
      <div class="card diag-card"><h3>当日成交</h3><div class="big">${esc(stats.tradeCount ?? '—')}</div><p>${esc(stats.wins ?? 0)} 胜 / ${esc(stats.losses ?? 0)} 负 · 胜率 ${esc(ratio(stats.winRatePct))}</p></div>
      <div class="card diag-card"><h3>净盈亏</h3><div class="big ${(stats.netPnl || 0) >= 0 ? 'positive' : 'negative'}">${money(stats.netPnl)}</div><p>手续费 ${money(stats.fees)}${stats.feeShareOfGrossPct != null ? ` · 占毛利 ${esc(stats.feeShareOfGrossPct)}%` : ''}</p></div>
      <div class="card diag-card"><h3>最大回撤</h3><div class="big">${esc(stats.maxDrawdownPct ?? '—')}%</div><p>平均持仓 ${esc(stats.avgHoldMinutes ?? 0)} 分钟</p></div>
    </div>`;
  }

  async function loadReport(id) {
    try {
      const response = await fetch('/api/reports/' + encodeURIComponent(id));
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || '读取报告失败');
      currentId = data.id;
      const stats = (data.meta || {}).stats || {};
      $('reportBody').innerHTML = `
        <div class="card-head" style="flex-wrap:wrap;gap:10px">
          <div><h2>${esc(data.date)} 复盘</h2>
            <div class="sub">${esc(data.symbol || '')} · ${esc(data.strategyName || '')} · 生成于 ${esc(String(data.createdAt || '').replace('T', ' ').slice(0, 16))}</div></div>
          <div class="toolbar">
            <button class="btn" id="exportMd">导出 Markdown</button>
            <button class="btn" id="exportHtml">导出 HTML</button>
            <button class="btn danger" id="deleteReport">删除</button>
          </div>
        </div>
        ${statsCards(stats)}
        <article class="md-body">${renderMarkdown(data.content)}</article>
        <div class="note">${esc(data.notice || '')}</div>`;
      $('exportMd').addEventListener('click', () => download(data.id, 'md'));
      $('exportHtml').addEventListener('click', () => download(data.id, 'html'));
      $('deleteReport').addEventListener('click', async () => {
        if (!confirm('确定删除这份报告？删除后不可恢复。')) return;
        await fetch('/api/reports/' + encodeURIComponent(data.id), {method: 'DELETE'});
        currentId = null;
        $('reportBody').innerHTML = '<div class="empty">已删除。可以重新生成一份。</div>';
        loadList();
      });
      loadList();
    } catch (error) {
      $('reportBody').innerHTML = `<div class="card empty">${esc(error.message)}</div>`;
    }
  }

  function download(id, format) {
    // 直接交给浏览器下载：后端会带 Content-Disposition，文件名含中文已按 RFC 5987 编码
    const link = document.createElement('a');
    link.href = `/api/reports/${encodeURIComponent(id)}/export?format=${format}`;
    link.rel = 'noopener';
    document.body.appendChild(link);
    link.click();
    link.remove();
  }

  /* ---- 生成 ---- */
  let busy = false;
  async function generate() {
    if (busy) return;
    busy = true;
    const button = $('genReport');
    button.disabled = true;
    button.textContent = '正在生成…';
    $('reportBody').innerHTML = '<div class="empty">模型正在读成交记录写复盘，通常需要 20–60 秒（推理模型会先思考）。</div>';
    try {
      const response = await fetch('/api/reports', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({runId: $('reportRun').value || null})
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || '生成失败');
      await loadReport(data.id);
    } catch (error) {
      $('reportBody').innerHTML = `<div class="card empty">${esc(error.message)}</div>`;
    } finally {
      busy = false;
      button.disabled = false;
      button.textContent = '生成今日报告';
    }
  }

  $('genReport').addEventListener('click', generate);
  window.addEventListener('qice-page-change', event => {
    if (event.detail === 'daily') { loadList(true); loadRuns(); }
  });
  loadList();
  loadRuns();
})();
