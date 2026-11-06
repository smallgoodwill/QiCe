/* 品类 / 品种搜索选择器（策略创建时选交易标的用）
 *
 * 为什么不用原生控件
 * ------------------
 * <select> 在 Windows 上没法输入过滤，68 个品种一路滚到底很难受；
 * <datalist> 不能分组、不能显示板块/交易所、也区分不出"精准"和"模糊"。
 * 所以这里自己画一个：一个输入框 + 一个 fixed 面板。
 *
 * 搜索口径（两档，界面上会标出来）
 * --------------------------------
 *   精准（绿标）：代码完全相等、中文名完全相等
 *                 归一化后比较，所以 rb2610 / RB0 / rb 都算命中"螺纹钢"
 *   模糊：代码前缀、中文名包含、板块名、交易所名、板块英文与拼音首字母
 * 品种选择不受品类限制（搜 au 会跨品类命中），同品类结果只是排在同类前面；
 * 品类选择支持"含某品种"的反查，输入"螺纹"能看到"黑色"。
 *
 * 面板定位为什么是 fixed
 * ----------------------
 * 它挂在一个 <dialog> 里的 .modal-body 内，而 .modal-body 是 overflow:auto。
 * absolute 面板会被这个滚动容器裁掉一半，只能 fixed + 按输入框 rect 算位置。
 */
(() => {
  const esc = value => String(value ?? '').replace(/[&<>"']/g,
    ch => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[ch]));

  /** 归一化：小写、去掉数字与符号。
   *  让 "rb2610"/"RB0"/"rb" 都变成 "rb"，"沪深300股指" 变成 "沪深股指"，
   *  用户从行情软件里复制过来的写法基本都能对上。 */
  const norm = value => String(value ?? '')
    .toLowerCase()
    .replace(/\d+/g, '')
    .replace(/[^a-z\u4e00-\u9fa5]/g, '');

  // 板块别名：拼音首字母（py）+ 拼音全拼/英文（en）。
  // 英文词只做"全等"或"3 个字母以上前缀"，否则输入 ag（白银代码）会把 agri 名下的
  // 16 个农产品全带出来，把真正的白银挤在一堆噪声里。
  const SECTOR_ALIAS = {
    金融: {py: 'jr', en: 'jinrong finance financial index bond'},
    黑色: {py: 'hs', en: 'heise ferrous steel coal coke'},
    有色: {py: 'ys', en: 'youse nonferrous metal base copper'},
    贵金属: {py: 'gjs', en: 'guijinshu precious gold silver'},
    能化: {py: 'nh', en: 'nenghua energy chemical chem oil'},
    农产品: {py: 'ncp', en: 'nongchanpin agri agriculture farm'},
    航运: {py: 'hy', en: 'hangyun shipping freight container'},
    新能源: {py: 'xny', en: 'xinnengyuan newenergy lithium silicon'}
  };
  const ALL_SECTOR = '全部品类';

  function aliasHit(sector, qn) {
    const alias = SECTOR_ALIAS[sector];
    if (!alias || !qn) return false;
    if (alias.py === qn) return true;
    return alias.en.split(' ').some(word => word === qn || (qn.length >= 3 && word.startsWith(qn)));
  }

  /** 在原文里找 qn 对应的连续片段（跳过数字/符号），用于高亮 */
  function findRange(text, qn) {
    const source = String(text ?? '');
    if (!qn) return null;
    for (let start = 0; start < source.length; start++) {
      let acc = '', end = start;
      while (end < source.length && acc.length < qn.length) {
        acc += norm(source[end]);
        end++;
      }
      if (acc === qn) return [start, end];
    }
    return null;
  }

  function mark(text, qn) {
    const range = findRange(text, qn);
    if (!range) return esc(text);
    const [start, end] = range;
    return esc(text.slice(0, start)) + '<mark>' + esc(text.slice(start, end)) + '</mark>'
      + esc(text.slice(end));
  }

  /** 品种打分：>=980 视为精准命中 */
  function scoreInstrument(item, qn) {
    if (!qn) return 1;
    const code = norm(item.code), name = norm(item.name);
    const sector = String(item.sector || '');
    const exchange = norm(item.exchangeName);
    const exchangeCode = norm(item.exchange);
    if (code === qn) return 1000;
    if (name === qn) return 980;
    if (code.startsWith(qn)) return 900;
    if (name.startsWith(qn)) return 860;
    if (name.includes(qn)) return 760;
    if (sector === qn) return 740;
    if (sector.includes(qn)) return 700;
    if (aliasHit(sector, qn)) return 680;
    if (exchange === qn) return 660;
    if (exchange.includes(qn)) return 640;
    if (exchangeCode === qn) return 620;
    if (norm(item.label).includes(qn)) return 500;
    return 0;
  }

  function placePanel(input, panel, minWidth) {
    const rect = input.getBoundingClientRect();
    const gap = 4;
    const width = Math.max(minWidth || 0, rect.width);
    const viewWidth = document.documentElement.clientWidth || window.innerWidth;
    const viewHeight = window.innerHeight;
    let left = rect.left;
    if (left + width > viewWidth - 8) left = Math.max(8, viewWidth - width - 8);

    const below = viewHeight - rect.bottom - gap - 8;
    const above = rect.top - gap - 8;
    // 下方放不下 220px、且上方更宽裕时才往上翻，否则一直向下更符合直觉
    const useBelow = below >= 220 || below >= above;
    const maxHeight = Math.min(340, Math.max(150, useBelow ? below : above));

    panel.style.width = width + 'px';
    panel.style.left = left + 'px';
    panel.style.maxHeight = maxHeight + 'px';
    panel.style.top = (useBelow ? rect.bottom + gap : Math.max(8, rect.top - maxHeight - gap)) + 'px';
  }

  function createPicker(config) {
    const sectorInput = document.getElementById(config.sectorInput);
    const sectorPanel = document.getElementById(config.sectorPanel);
    const codeInput = document.getElementById(config.codeInput);
    const codePanel = document.getElementById(config.codePanel);
    const hidden = document.getElementById(config.hidden);
    const hint = config.hint ? document.getElementById(config.hint) : null;
    const sectorHint = config.sectorHint ? document.getElementById(config.sectorHint) : null;
    // 可选回调：选定品种变化时通知宿主页面（诊断页靠它自动加载消息面线索）。
    // 不传就完全不影响策略库等既有调用方。
    const onChange = typeof config.onChange === 'function' ? config.onChange : null;
    if (!codeInput || !codePanel || !hidden) return null;

    let groups = [];                 // [{sector, items:[instrument]}]
    let flat = [];                   // 扁平化后的全部品种，带 order 保留板块顺序
    let loaded = false, loading = null, failed = '';
    let sector = ALL_SECTOR;
    let selected = null;
    let pendingSymbol = '';
    let lastEmitted = '';        // 避免同一次 sync 反复通知宿主页面
    let activeIndex = -1;
    let openTarget = '';             // 'sector' | 'code' | ''

    const fetchAll = () => {
      if (loading) return loading;
      loading = fetch('/api/instruments').then(response => response.json()).then(data => {
        groups = (data.groups || []).map(group => ({sector: group.sector, items: group.items || []}));
        flat = [];
        groups.forEach(group => group.items.forEach(item => flat.push({...item, order: flat.length})));
        loaded = true;
        failed = '';
        renderHints();
        // 新建表单里 HTML 写了个默认品种，先认它；否则保持"未选择"
        const want = pendingSymbol || (!selected ? hidden.value : '');
        pendingSymbol = '';
        if (want) setSymbol(want); else sync();
      }).catch(error => {
        failed = error.message || '品种清单读取失败';
        loading = null;
        if (sectorHint) sectorHint.textContent = '品种清单读取失败，稍后重试';
      });
      return loading;
    };

    // ---- 从库里存的 symbol 反解出品种 ----------------------------------
    // 历史数据长这样："沪深300股指 · IF2503"、"螺纹钢 · RB2505"，新数据是 "螺纹钢 · RB"。
    function bySymbol(symbol) {
      const text = String(symbol || '').trim();
      if (!text || !flat.length) return null;
      const lower = text.toLowerCase();
      const exact = flat.find(item => item.label.toLowerCase() === lower || item.code === lower);
      if (exact) return exact;
      // 合约代码基本都在后半段，取最后一段字母更稳（"PVC · V" 里前面的 PVC 是名字）
      const runs = text.match(/[A-Za-z]+/g) || [];
      const tail = (runs[runs.length - 1] || '').toLowerCase();
      for (let size = tail.length; size >= 1; size--) {
        const found = flat.find(item => item.code === tail.slice(0, size));
        if (found) return found;
      }
      const joined = runs.join('').toLowerCase();
      for (let size = joined.length; size >= 1; size--) {
        const found = flat.find(item => item.code === joined.slice(0, size));
        if (found) return found;
      }
      return flat.find(item => text.includes(item.name)) || null;
    }

    function renderHints() {
      if (sectorHint) {
        const counted = flat.length;
        sectorHint.textContent = sector === ALL_SECTOR
          ? `${groups.length} 个品类 · ${counted} 个品种`
          : `${sector} · ${(groups.find(g => g.sector === sector) || {}).items?.length || 0} 个品种`;
      }
      if (hint) {
        hint.textContent = selected
          ? `当前：${selected.name} · ${selected.code.toUpperCase()} · ${selected.exchangeName}/${selected.sector}`
          : '还没选品种，保存时会拦下来';
        hint.className = selected ? '' : 'warn';
      }
    }

    function sync() {
      sectorInput.value = sector;
      codeInput.value = selected ? `${selected.name} · ${selected.code.toUpperCase()}` : '';
      hidden.value = selected ? selected.label : '';
      renderHints();
      // 只在「选中的品种真的变了」时通知宿主（初始化 / 重复 sync 不打扰）
      const key = selected ? selected.label : '';
      if (onChange && key !== lastEmitted) {
        lastEmitted = key;
        onChange(selected);
      }
    }

    function setSector(name, keepCode) {
      sector = name;
      sectorInput.value = name;
      if (!keepCode && selected && name !== ALL_SECTOR && selected.sector !== name) {
        selected = null;
        codeInput.value = '';
        hidden.value = '';
      }
      renderHints();
    }

    function setSymbol(symbol) {
      if (!loaded) { pendingSymbol = symbol; fetchAll(); return; }
      const found = bySymbol(symbol);
      selected = found || null;
      if (found) sector = found.sector;
      sync();
    }

    // ---- 面板渲染 ------------------------------------------------------
    function closePanels() {
      openTarget = '';
      activeIndex = -1;
      sectorPanel.hidden = true;
      codePanel.hidden = true;
    }

    function paintSectorPanel() {
      // 输入框里显示的就是当前品类时不算"在搜索"，否则一聚焦就把自己过滤没了
      const text = sectorInput.value.trim();
      const qn = (!text || text === ALL_SECTOR || text === sector) ? '' : norm(text);
      const rows = [];
      const allScore = !qn || norm(ALL_SECTOR).includes(qn) ? 1 : 0;
      if (allScore) rows.push({name: ALL_SECTOR, items: flat, score: 1, exact: false});
      groups.forEach(group => {
        const score = scoreSector(group, qn);
        if (score > 0) rows.push({name: group.sector, items: group.items, score, exact: score >= 1000});
      });
      rows.sort((a, b) => b.score - a.score);

      if (!rows.length) {
        sectorPanel.innerHTML = `<div class="combo-empty">没有匹配「${esc(sectorInput.value)}」的品类<br>试试：黑色 / 有色 / nh / agri</div>`;
        return;
      }
      sectorPanel.innerHTML = `<div class="combo-list">${rows.map((row, index) => {
        const count = row.items.length;
        const hitName = qn ? (row.items.find(i => norm(i.name).includes(qn) || norm(i.code) === qn)?.name || '') : '';
        const hintText = row.name === ALL_SECTOR ? '按板块浏览全部'
          : row.score >= 1000 ? '精准'
          : row.score === 800 ? '名称包含'
          : row.score === 620 ? '拼音 / 英文'
          : hitName ? `含 ${hitName}` : '';
        return `<div class="combo-opt ${row.name === sector ? 'on' : ''} ${index === activeIndex ? 'active' : ''}"
            data-index="${index}" data-sector="${esc(row.name)}">
            <span>${mark(row.name, qn)}</span>
            <span class="code">${count} 个品种</span>
            <span class="meta">${esc(hintText)}</span></div>`;
      }).join('')}</div>`;
      bindSectorOptions(rows);
    }

    function scoreSector(group, qn) {
      if (!qn) return 1;
      const name = group.sector.toLowerCase();
      if (name === qn) return 1000;
      if (name.includes(qn)) return 800;
      if (aliasHit(group.sector, qn)) return 620;
      if (group.items.some(item => norm(item.name) === qn || norm(item.code) === qn)) return 480;
      if (group.items.some(item => norm(item.name).includes(qn))) return 300;
      return 0;
    }

    function paintCodePanel() {
      const raw = codeInput.value === (selected ? `${selected.name} · ${selected.code.toUpperCase()}` : '')
        ? '' : codeInput.value;
      const qn = norm(raw);

      let rows = flat
        .map(item => ({item, score: scoreInstrument(item, qn), inSector: item.sector === sector}))
        .filter(row => row.score > 0);
      if (!qn && sector !== ALL_SECTOR) rows = rows.filter(row => row.inSector);
      rows.sort((a, b) => b.score - a.score || (b.inSector - a.inSector) || (a.item.order - b.item.order));
      rows = rows.slice(0, 120);          // 全量 68 条，留点余量防止板块加品种后变卡

      const chips = [`<button type="button" data-sector="${ALL_SECTOR}" class="${sector === ALL_SECTOR ? 'on' : ''}">全部</button>`]
        .concat(groups.map(group =>
          `<button type="button" data-sector="${esc(group.sector)}" class="${sector === group.sector ? 'on' : ''}">${esc(group.sector)}</button>`))
        .join('');

      if (!rows.length) {
        codePanel.innerHTML = `<div class="combo-empty">没有匹配「${esc(raw)}」的品种<br>
          可以试：代码 <b>rb</b> · 中文 <b>螺纹</b> · 板块 <b>黑色</b> · 交易所 <b>上期所</b></div>
          <div class="combo-foot">${chips}</div>`;
        bindCodePanel();
        return;
      }

      // 按板块分组，但「精准 / 代码前缀」命中必须先跨板块拎到最前面：
      // 否则输入 j 时，焦炭（代码完全相等）会被「金融组排在黑色组之前」挤到第三屏去。
      const order = groups.map(group => group.sector);
      const precise = rows.filter(row => row.score >= 900);
      const rest = rows.filter(row => row.score < 900);
      const buckets = new Map();
      rest.forEach(row => {
        const key = row.item.sector;
        if (!buckets.has(key)) buckets.set(key, []);
        buckets.get(key).push(row);
      });

      let index = 0;
      const chunk = (title, list) => {
        if (!list.length) return '';
        const head = `<div class="combo-group"><span>${esc(title)}</span><span>${list.length}</span></div>`;
        return head + list.map(row => {
          const exact = row.score >= 980;
          const meta = exact ? `精准 · ${row.item.exchangeName}` : row.item.exchangeName;
          const html = `<div class="combo-opt ${row.item.code === selected?.code ? 'on' : ''} ${index === activeIndex ? 'active' : ''} ${exact ? 'is-exact' : ''}"
              data-index="${index}" data-code="${esc(row.item.code)}">
              <span>${mark(row.item.name, qn)}</span>
              <span class="code">${mark(row.item.code.toUpperCase(), qn)}</span>
              <span class="meta">${esc(meta)}</span></div>`;
          index += 1;
          return html;
        }).join('');
      };

      const body = [
        chunk(precise.length ? '精准匹配' : '', precise),
        ...[...buckets.keys()]
          .sort((a, b) => order.indexOf(a) - order.indexOf(b))
          .map(key => chunk(key, buckets.get(key)))
      ].join('');

      codePanel.innerHTML = `<div class="combo-list">${body}</div><div class="combo-foot">${chips}</div>`;
      bindCodePanel();
    }

    function bindSectorOptions(rows) {
      sectorPanel.querySelectorAll('.combo-opt').forEach(node => {
        node.addEventListener('mousedown', event => event.preventDefault());   // 别让输入框先失焦
        node.addEventListener('click', () => {
          setSector(rows[+node.dataset.index].name, false);
          closePanels();
          // 换品类后直接把焦点交给品种框，少点一次
          if (!selected) codeInput.focus();
        });
      });
    }

    function bindCodePanel() {
      codePanel.querySelectorAll('.combo-opt').forEach(node => {
        node.addEventListener('mousedown', event => event.preventDefault());
        node.addEventListener('click', () => {
          const found = flat.find(item => item.code === node.dataset.code);
          if (!found) return;
          selected = found;
          sector = found.sector;
          sync();
          closePanels();
        });
      });
      codePanel.querySelectorAll('.combo-foot button').forEach(node => {
        node.addEventListener('mousedown', event => event.preventDefault());
        node.addEventListener('click', () => {
          setSector(node.dataset.sector, false);
          paintCodePanel();
          placePanel(codeInput, codePanel, 320);
          codeInput.focus();
        });
      });
    }

    function openCode() {
      fetchAll();
      if (codeInput.disabled) return;
      openTarget = 'code';
      sectorPanel.hidden = true;
      activeIndex = -1;
      paintCodePanel();
      codePanel.hidden = false;
      placePanel(codeInput, codePanel, 320);
      // 已选中的那个品种可能在第 5 屏之外（全部品类视图下），打开时先滚到它。
      // 用 scrollTop 而不是 scrollIntoView：后者会顺带滚动整个文档，弹窗会跳。
      const list = codePanel.querySelector('.combo-list');
      const chosen = codePanel.querySelector('.combo-opt.on');
      if (list && chosen) {
        list.scrollTop = Math.max(0, chosen.offsetTop - list.clientHeight / 2 + chosen.offsetHeight / 2);
      }
      codeInput.select();
    }

    function openSector() {
      fetchAll();
      if (sectorInput.disabled) return;
      openTarget = 'sector';
      codePanel.hidden = true;
      activeIndex = -1;
      paintSectorPanel();
      sectorPanel.hidden = false;
      placePanel(sectorInput, sectorPanel, 240);
      sectorInput.select();
    }

    // ---- 事件 ----------------------------------------------------------
    sectorInput.addEventListener('focus', openSector);
    sectorInput.addEventListener('click', () => { if (openTarget !== 'sector') openSector(); });
    sectorInput.addEventListener('input', () => {
      if (openTarget !== 'sector') { openSector(); return; }
      activeIndex = -1;
      paintSectorPanel();
    });
    sectorInput.addEventListener('blur', () => {
      // 失焦时把输入框恢复成真实状态，避免"看起来改了其实没改"
      setTimeout(() => { if (openTarget !== 'sector') sectorInput.value = sector; }, 120);
    });

    codeInput.addEventListener('focus', openCode);
    codeInput.addEventListener('click', () => { if (openTarget !== 'code') openCode(); });
    codeInput.addEventListener('input', () => {
      if (openTarget !== 'code') { openCode(); return; }
      activeIndex = -1;
      paintCodePanel();
    });
    codeInput.addEventListener('blur', () => {
      setTimeout(() => {
        if (openTarget === 'code') return;
        codeInput.value = selected ? `${selected.name} · ${selected.code.toUpperCase()}` : '';
      }, 120);
    });

    function move(step) {
      const panel = openTarget === 'sector' ? sectorPanel : codePanel;
      const options = panel.querySelectorAll('.combo-opt');
      if (!options.length) return;
      activeIndex = (activeIndex + step + options.length) % options.length;
      options.forEach((node, index) => node.classList.toggle('active', index === activeIndex));
      options[activeIndex].scrollIntoView({block: 'nearest'});
    }

    function commit() {
      const panel = openTarget === 'sector' ? sectorPanel : codePanel;
      const options = panel.querySelectorAll('.combo-opt');
      if (activeIndex < 0 || !options[activeIndex]) {
        // 没高亮时按 Enter：如果只有一条结果就直接选它，符合"打字—回车"的直觉
        if (options.length === 1) { options[0].click(); return true; }
        return false;
      }
      options[activeIndex].click();
      return true;
    }

    [sectorInput, codeInput].forEach(input => {
      input.addEventListener('keydown', event => {
        if (event.key === 'ArrowDown') { event.preventDefault(); move(1); }
        else if (event.key === 'ArrowUp') { event.preventDefault(); move(-1); }
        else if (event.key === 'Enter') { if (commit()) event.preventDefault(); }
        else if (event.key === 'Escape') { event.preventDefault(); closePanels(); input.blur(); }
        else if (event.key === 'Tab') { closePanels(); }
      });
    });

    // 点空白处关闭：面板在 .combo 内，所以只要不在面板里就关
    document.addEventListener('mousedown', event => {
      if (!openTarget) return;
      if (sectorPanel.contains(event.target) || codePanel.contains(event.target)) return;
      if (event.target === sectorInput || event.target === codeInput) return;
      closePanels();
    }, true);
    // 模态框内部滚动 / 窗口尺寸变化时，面板要跟着输入框走。
    // 这里不能直接关闭：focus() 本身就会触发一次轻微滚动，关了会表现成"点了没反应"。
    document.addEventListener('scroll', event => {
      if (!openTarget) return;
      if (event.target === sectorInput || event.target === codeInput) return;
      const panel = openTarget === 'sector' ? sectorPanel : codePanel;
      if (panel.contains(event.target)) return;         // 面板内列表自己在滚，不用管
      const input = openTarget === 'sector' ? sectorInput : codeInput;
      placePanel(input, panel, openTarget === 'sector' ? 240 : 320);
    }, true);
    window.addEventListener('resize', () => {
      if (!openTarget) return;
      const panel = openTarget === 'sector' ? sectorPanel : codePanel;
      const input = openTarget === 'sector' ? sectorInput : codeInput;
      placePanel(input, panel, openTarget === 'sector' ? 240 : 320);
    });
    window.addEventListener('qice-page-change', () => { if (openTarget) closePanels(); });

    fetchAll();

    return {
      element: codeInput,
      get ready() { return loaded; },
      get error() { return failed; },
      /** 存库用的字符串，例如「螺纹钢 · RB」 */
      symbol: () => selected ? selected.label : '',
      /** 当前品种元数据 */
      instrument: () => selected,
      /** 用库里存的 symbol 回填 */
      setSymbol,
      /** 只查询不改变状态，列表卡片显示品类用 */
      lookup: symbol => bySymbol(symbol),
      close: closePanels,
      reload: fetchAll
    };
  }

  window.qiceInstrumentPicker = {create: createPicker};
})();
