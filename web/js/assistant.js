/* 平台问答。
 *
 * AI 服务**不在前端配置**：服务商、模型、Key 全部由服务端 .env 绑定
 * （AI_PROVIDER / DEEPSEEK_API_KEY / DEEPSEEK_MODEL），页面只读展示当前生效的模型，
 * 既不给输入框也不做"连接"动作——Key 永远不下发到浏览器。
 */
(() => {
  const mount = document.getElementById('assistantMount');
  if (!mount) return;

  mount.innerHTML = `
    <div class="heading">
      <div><h1>智能问答</h1>
        <p>结合当前平台状态解释策略、指标与统计口径；服务端已绑定模型，页面无需配置。</p></div>
      <span class="tag amber" id="aiStatus">检查服务中</span>
    </div>
    <div class="risk-banner"><span>ⓘ</span><div>问答用于解释策略、指标和本平台模拟数据，不提供具体买卖建议或收益承诺。模型回答可能有误，交易与统计口径请以系统记录为准。</div></div>

    <div class="grid qa-layout">
      <div class="card chat">
        <div class="card-head"><h2>平台问答</h2><button class="btn" id="clearChat">清空对话</button></div>
        <div class="messages" id="messages"><div class="msg">你好，可以询问均线规则、回测指标、模拟盘状态和费用口径。当前没有发送任何对话。</div></div>
        <form class="chat-form" id="chatForm">
          <input id="question" type="text" maxlength="3000" placeholder="例如：当前模拟盘的回撤是多少？" autocomplete="off">
          <button class="btn primary" id="sendQuestion">发送</button>
        </form>
        <div class="quick">
          <button data-q="金叉和死叉在当前策略中如何产生信号？">解释均线信号</button>
          <button data-q="最大回撤如何计算？">解释最大回撤</button>
          <button data-q="目前模拟盘运行状态和最新统计是什么？">查看当前模拟状态</button>
        </div>
      </div>

      <div class="card">
        <div class="card-head"><div><h2>模型与边界</h2><div class="sub">服务端绑定 · 页面不提供配置</div></div></div>
        <div class="status-line"><span>当前模型</span><b id="aiCurrentModel">—</b></div>
        <div class="status-line"><span>上下文来源</span><b>模拟盘 · 策略库 · 诊断报告</b></div>
        <div class="status-line"><span>密钥</span><b>仅存于服务端</b></div>
        <ul class="ai-scope">
          <li class="ok">解释均线、RSI、MACD、ATR 的口径与信号条件</li>
          <li class="ok">说明回测指标（胜率、盈亏比、最大回撤）怎么算</li>
          <li class="ok">分析某条策略在样本区间为什么盈利或亏损</li>
          <li class="no">不预测点位、不给买卖建议、不承诺收益</li>
          <li class="no">不生成策略、不下单、不改动任何参数</li>
        </ul>
        <div class="note">问答会消耗服务端账户额度。回答只引用平台上已发生的记录，没有的数字会直接说明缺失。</div>
      </div>
    </div>`;

  const $ = id => document.getElementById(id);
  let messages = [];
  let busy = false;

  /* AI 回答是 Markdown。早期用 textContent 直塞气泡，换行全被折叠、
   * `**`/`#`/表格竖线裸露成一堵文字墙。这里与诊断/报告页同口径渲染成
   * HTML（标题/列表/表格/代码块/引用），再套 .md-body 排版样式。 */
  const esc = text => String(text ?? '')
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

  function inline(text) {
    let out = esc(text);
    while (out.includes('**')) out = out.replace('**', '<strong>').replace('**', '</strong>');
    while (out.includes('`')) out = out.replace('`', '<code>').replace('`', '</code>');
    return out;
  }

  function renderMarkdown(markdown) {
    const blocks = [];
    let listTag = '';                       // '' / 'ul' / 'ol'，换类型先关旧列表
    const closeList = () => { if (listTag) { blocks.push(`</${listTag}>`); listTag = ''; } };
    const lines = String(markdown || '').split('\n');
    for (let i = 0; i < lines.length; i++) {
      const line = lines[i].trim();
      if (!line) { closeList(); continue; }
      // 围栏代码块：``` 开头到下一个 ``` 之间整段保留
      if (line.startsWith('```')) {
        closeList();
        const code = [];
        i++;
        while (i < lines.length && !lines[i].trim().startsWith('```')) { code.push(lines[i]); i++; }
        blocks.push(`<pre><code>${esc(code.join('\n'))}</code></pre>`);
        continue;
      }
      // 表格：连续的 | 开头行；|---| 分隔行跳过，首行作表头
      if (line.startsWith('|') && line.indexOf('|', 1) !== -1) {
        const rows = [];
        while (i < lines.length) {
          const t = lines[i].trim();
          if (!(t.startsWith('|') && t.indexOf('|', 1) !== -1)) break;
          if (!/^\|[\s:|-]+\|$/.test(t)) rows.push(t);
          i++;
        }
        i--;
        if (rows.length) {
          closeList();
          const cells = row => row.slice(1, row.endsWith('|') ? -1 : undefined).split('|').map(c => inline(c.trim()));
          const head = cells(rows[0]);
          const body = rows.slice(1)
            .map(r => `<tr>${cells(r).map(c => `<td>${c}</td>`).join('')}</tr>`).join('');
          blocks.push(`<div class="md-scroll"><table><thead><tr>${head.map(c => `<th>${c}</th>`).join('')}</tr></thead>` +
                      `<tbody>${body}</tbody></table></div>`);
          continue;
        }
      }
      if (line.startsWith('#')) {
        closeList();
        const level = Math.min(6, line.length - line.replace(/^#+/, '').length);
        blocks.push(`<h${level}>${inline(line.slice(level).trim())}</h${level}>`);
      } else if (/^[-*+] /.test(line)) {
        if (listTag !== 'ul') { closeList(); blocks.push('<ul>'); listTag = 'ul'; }
        blocks.push(`<li>${inline(line.slice(2))}</li>`);
      } else if (/^\d+[.)] /.test(line)) {
        if (listTag !== 'ol') { closeList(); blocks.push('<ol>'); listTag = 'ol'; }
        blocks.push(`<li>${inline(line.replace(/^\d+[.)] /, ''))}</li>`);
      } else if (line.startsWith('>')) {
        closeList();
        blocks.push(`<blockquote>${inline(line.replace(/^>\s*/, ''))}</blockquote>`);
      } else if (/^(-{3,}|\*{3,}|_{3,})$/.test(line)) {
        closeList();
        blocks.push('<hr>');
      } else {
        closeList();
        blocks.push(`<p>${inline(line)}</p>`);
      }
    }
    closeList();
    return blocks.join('\n');
  }

  function node(text, role) {
    const div = document.createElement('div');
    div.className = 'msg' + (role === 'user' ? ' user' : '');
    // 用户输入保持纯文本（防注入）；AI 回答走 Markdown 渲染（内部已转义）
    if (role === 'user') div.textContent = text;
    else div.innerHTML = `<div class="md-body">${renderMarkdown(text)}</div>`;
    return div;
  }

  function refreshMessages() {
    const box = $('messages');
    box.replaceChildren();
    if (!messages.length) {
      box.appendChild(node('你好，可以询问策略、回测和模拟盘统计。', 'assistant'));
      return;
    }
    for (const m of messages) box.appendChild(node(m.content, m.role));
    box.scrollTop = box.scrollHeight;
  }

  /* 只读状态：确认服务端把哪个模型绑上了，不暴露 Key 与来源细节 */
  async function status() {
    try {
      const response = await fetch('/api/ai/status');
      const data = await response.json();
      if (!response.ok) throw new Error();
      $('aiStatus').textContent = data.configured ? `${data.providerLabel} 已就绪` : '服务端未配置模型';
      $('aiStatus').className = 'tag ' + (data.configured ? 'green' : 'amber');
      $('aiCurrentModel').textContent = data.configured ? data.model : '未配置';
    } catch (_) {
      $('aiStatus').textContent = '本地服务未连接';
      $('aiStatus').className = 'tag amber';
      $('aiCurrentModel').textContent = '不可用';
    }
  }

  async function send(text) {
    if (busy || !text.trim()) return;
    const question = text.trim();
    messages.push({role: 'user', content: question});
    refreshMessages();
    $('question').value = '';
    busy = true;
    $('sendQuestion').disabled = true;
    const pending = node('正在请求 AI 服务…', 'assistant');
    $('messages').appendChild(pending);
    try {
      const response = await fetch('/api/ai/chat', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({messages: messages.slice(-12)})
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || '问答请求失败');
      messages.push({role: 'assistant', content: data.answer});
      pending.remove();
      refreshMessages();
      $('aiStatus').textContent = `${data.provider === 'deepseek' ? 'DeepSeek' : 'GLM'} 已就绪`;
      $('aiStatus').className = 'tag green';
      $('aiCurrentModel').textContent = data.model || $('aiCurrentModel').textContent;
    } catch (error) {
      pending.textContent = error.message || '问答服务暂不可用。';
      await status();
    } finally {
      busy = false;
      $('sendQuestion').disabled = false;
      $('question').focus();
    }
  }

  $('chatForm').addEventListener('submit', event => { event.preventDefault(); send($('question').value); });
  $('clearChat').addEventListener('click', () => { messages = []; refreshMessages(); });
  document.querySelectorAll('#assistantMount [data-q]').forEach(button =>
    button.addEventListener('click', () => send(button.dataset.q)));

  refreshMessages();
  status();
})();
