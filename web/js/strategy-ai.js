/* AI 调参：说一句话 → 模型给出改了哪些参数 → **人工确认**后才写策略库。
 * 这一层不做任何自动保存：只要用户没点「确认入库」，数据库里就什么都没变。
 */
(() => {
  const page = document.getElementById('page-strategy');
  if (!page) return;
  const toolbar = page.querySelector('.toolbar');
  if (!toolbar) return;
  toolbar.insertAdjacentHTML('beforeend', '<button class="btn" id="aiStrategyBtn">✳ AI 调参</button>');

  const $ = id => document.getElementById(id);
  const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));

  document.body.insertAdjacentHTML('beforeend', `
    <dialog id="aiStrategyModal" class="strategy-modal" style="width:min(860px,94vw)">
      <div class="modal-head">
        <div><h2>AI 调参</h2><div class="sub">描述你想怎么改，AI 给出参数提案；确认后才会写入策略库</div></div>
        <button class="btn" id="aiClose">关闭</button>
      </div>
      <!-- 表单和提案结果必须包在 .modal-body 里：dialog 本身是 overflow:hidden，
           提案表格+风险提示超出 92vh 会被裁掉，.modal-body 才是滚动容器 -->
      <div class="modal-body">
        <div class="form-grid" style="gap:12px">
          <div class="field"><label for="aiBase">以哪条策略为基准</label>
            <select class="select" id="aiBase"><option value="">不选（用默认 EMA 9/21）</option></select></div>
          <div class="field"><label for="aiSymbol">关注品种（可选）</label>
            <select class="select" id="aiSymbol"><option value="">不指定</option></select></div>
        </div>
        <div class="field" style="margin-top:12px"><label for="aiPrompt">你想怎么调整</label>
          <textarea id="aiPrompt" rows="3" maxlength="800"
            placeholder="例如：最近震荡太多，假信号频繁，想减少交易次数、只做趋势明确的行情，回撤控制在 3% 以内"></textarea>
          <small>4–800 字。只说需求即可，不用写具体参数——参数由 AI 决定，你再逐条确认。</small></div>
        <div class="toolbar" style="margin-top:12px">
          <button class="btn primary" id="aiGenerate">生成提案</button>
          <span class="sub" id="aiMessage"></span>
        </div>
        <div id="aiTuningResult" style="margin-top:14px"></div>
      </div>
    </dialog>`);

  let draft = null, busy = false;

  function fillSelects() {
    const all = window.strategyLibrary?.all?.() || [];
    $('aiBase').innerHTML = '<option value="">不选（用默认 EMA 9/21）</option>'
      + all.map(item => `<option value="${esc(item.id)}">${esc(item.name)} · ${esc(item.symbol || '')}</option>`).join('');
    fetch('/api/instruments').then(r => r.json()).then(data => {
      const flat = (data.groups || []).flatMap(group => group.items);
      $('aiSymbol').innerHTML = '<option value="">不指定</option>'
        + flat.map(item => `<option value="${esc(item.code)}">${esc(item.name)} · ${esc(item.code.toUpperCase())}</option>`).join('');
    }).catch(() => {});
  }

  function renderDraft(data) {
    const proposal = data.proposal || {};
    const changes = proposal.changes || [];
    const rows = changes.length ? changes.map(row => `
      <tr>
        <td><b>${esc(row.label)}</b><div class="sub">${esc(row.key)}</div></td>
        <td class="muted">${esc(row.fromText)}</td>
        <td>${row.deltaText ? `<span class="param-diff">${esc(row.fromText)} → ${esc(row.toText)}</span>` : `${esc(row.fromText)} → ${esc(row.toText)}`}</td>
        <td style="white-space:normal">${esc(row.reason || '（AI 未说明理由）')}</td>
      </tr>`).join('') : '<tr><td colspan="4" class="muted">AI 认为当前参数无需改动。</td></tr>';

    const warnings = (data.warnings || []).map(text => `<div class="sub">· ${esc(text)}</div>`).join('');
    const risks = (proposal.risks || []).map(text => `<li>${esc(text)}</li>`).join('');

    $('aiTuningResult').innerHTML = `
      <div class="card" style="box-shadow:none;border-color:#dfe7f5">
        <div class="card-head"><div><h2>${esc(proposal.name || 'AI 提案')}</h2>
          <div class="sub">${esc(proposal.summary || '')}</div></div><span class="tag amber">待确认</span></div>
        <table class="table"><thead><tr><th>参数</th><th>当前</th><th>建议</th><th>理由</th></tr></thead>
          <tbody>${rows}</tbody></table>
        ${proposal.expectedEffect ? `<div class="note">预计影响：${esc(proposal.expectedEffect)}</div>` : ''}
        ${risks ? `<div class="section-title">风险提示</div><ul>${risks}</ul>` : ''}
        ${warnings ? `<div class="section-title">自动修正</div>${warnings}` : ''}
        <div class="toolbar" style="margin-top:14px">
          <button class="btn primary" id="aiAccept" ${changes.length ? '' : 'disabled'}>确认入库</button>
          <button class="btn" id="aiDiscard">放弃</button>
          <span class="sub">入库会**另存为新策略**，不覆盖原策略；确认前数据库没有任何改动。</span>
        </div>
      </div>`;
    $('aiDiscard').addEventListener('click', () => { draft = null; $('aiTuningResult').innerHTML = ''; $('aiMessage').textContent = '已放弃，未写入任何改动。'; });
    if (changes.length) $('aiAccept').addEventListener('click', accept);
    else $('aiAccept')?.addEventListener('click', () => {});
    // 提案经常比表单长，生成后把结果区滚进视野，免得用户以为没反应
    $('aiTuningResult').scrollIntoView({block: 'nearest'});
  }

  async function generate() {
    if (busy) return;
    const prompt = $('aiPrompt').value.trim();
    if (prompt.length < 4) { $('aiMessage').textContent = '请先用一句话说明你想怎么调整。'; return; }
    busy = true;
    $('aiGenerate').disabled = true;
    $('aiGenerate').textContent = '生成中…';
    $('aiMessage').textContent = '模型正在推演参数，通常 10–40 秒。';
    $('aiTuningResult').innerHTML = '';
    try {
      const response = await fetch('/api/ai/strategy/draft', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({prompt, strategyId: $('aiBase').value || null, symbol: $('aiSymbol').value || null})
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || '生成失败');
      draft = data;
      $('aiMessage').textContent = `提案已生成（${data.model || ''}），请逐条确认。`;
      renderDraft(data);
    } catch (error) {
      $('aiMessage').textContent = error.message;
    } finally {
      busy = false;
      $('aiGenerate').disabled = false;
      $('aiGenerate').textContent = '生成提案';
    }
  }

  async function accept() {
    if (!draft) return;
    const payload = draft.savePayload;
    $('aiMessage').textContent = '正在写入策略库…';
    try {
      const response = await fetch('/api/strategies', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload)
      });
      const saved = await response.json();
      if (!response.ok) throw new Error(saved.error || '保存失败');
      $('aiMessage').textContent = `已保存为「${saved.name}」，可以到交易页跑一次模拟对比新旧参数。`;
      draft = null;
      $('aiTuningResult').innerHTML = '';
      window.strategyLibrary?.refresh?.();
      fillSelects();
    } catch (error) {
      $('aiMessage').textContent = error.message;
    }
  }

  $('aiStrategyBtn').addEventListener('click', () => { fillSelects(); $('aiMessage').textContent = ''; $('aiStrategyModal').showModal(); });
  $('aiClose').addEventListener('click', () => $('aiStrategyModal').close());
  $('aiGenerate').addEventListener('click', generate);
  fillSelects();
})();
