/**
 * K 线图内核（live 页与仪表盘组件共用）
 *
 * 只负责「把 bar 数据画到 canvas 上」这一件事：样式、指标、时间单调性校验。
 * 取数、推送、成交标记、周期切换由调用方决定，避免两处各写一套绘图逻辑。
 */
(() => {
  if (typeof window === 'undefined') return;

  // 国内习惯：涨红跌绿
  const LIGHT_STYLES = {
    grid: {show: true, horizontal: {color: '#eef1f6'}, vertical: {color: '#f3f5f9'}},
    candle: {
      bar: {upColor: '#e06465', downColor: '#139875', noChangeColor: '#748198',
            upBorderColor: '#e06465', downBorderColor: '#139875',
            upWickColor: '#e06465', downWickColor: '#139875'},
      priceMark: {last: {upColor: '#e06465', downColor: '#139875', noChangeColor: '#748198'}}
    },
    xAxis: {axisLine: {color: '#e7ebf2'}, tickText: {color: '#8b96a8'}},
    yAxis: {axisLine: {color: '#e7ebf2'}, tickText: {color: '#8b96a8'}}
  };

  /** 服务端 bar（ISO 时间 + 字符串数字）→ 图表要求的结构（毫秒时间戳 + number） */
  function toChartBar(bar) {
    return {
      timestamp: Date.parse(bar.timestamp),
      open: +bar.open, high: +bar.high, low: +bar.low, close: +bar.close,
      volume: +(bar.volume || 0)
    };
  }

  /**
   * 图表要求时间严格递增：按时间升序、同一时间戳只保留最后一条。
   * 服务端已按数据源隔离并排序，这里再兜一层，防止任何来源把坐标轴顶歪。
   */
  function normalizeBars(bars) {
    const byTs = new Map();
    (bars || []).forEach(bar => {
      const ts = Date.parse(bar.timestamp);
      if (!Number.isFinite(ts)) return;
      byTs.set(ts, Object.assign({}, bar, {timestamp: bar.timestamp}));
    });
    return [...byTs.keys()].sort((a, b) => a - b).map(key => byTs.get(key));
  }

  /**
   * 在 holder 上创建一个 K 线视图。
   * @param {HTMLElement} holder 容器（必须已有宽度，否则初始化会得到 0×0）
   * @param {{volume?: boolean, onViewChange?: Function, indicators?: string[]}} options
   * @returns {{chart: object, apply: Function, update: Function, resize: Function,
   *            destroy: Function, dataList: Function} | null}
   */
  function create(holder, options = {}) {
    if (!holder || typeof klinecharts === 'undefined') return null;
    if (!holder.offsetWidth) return null;              // 页面/卡片还没显示出来，交给调用方稍后重试

    const chart = klinecharts.init(holder, {locale: 'zh-CN', timezone: 'Asia/Shanghai'});
    if (!chart) return null;
    chart.setStyles(LIGHT_STYLES);
    chart.createIndicator('MA', false, {id: 'candle_pane'});
    if (options.volume !== false) chart.createIndicator('VOL');
    (options.indicators || []).forEach(name => chart.createIndicator(name));

    const onViewChange = typeof options.onViewChange === 'function' ? options.onViewChange : null;
    if (onViewChange) {
      ['onVisibleRangeChange', 'onZoom', 'onScroll'].forEach(action => {
        try { chart.subscribeAction(action, onViewChange); } catch (_) { /* 低版本无此动作 */ }
      });
    }

    // 卡片刚插进 DOM 时高度往往还没算完（图片/字体/网格布局未收敛），
    // 首帧量到的高度会偏小，画布就按小尺寸画死了。下一帧再校准一次。
    if (typeof requestAnimationFrame === 'function') {
      requestAnimationFrame(() => { try { chart.resize(); } catch (_) { /* 已销毁 */ } });
    }

    return {
      chart,

      /** 整批替换（首屏、切周期、切品种）。返回清洗后的 bar，调用方可用它判断根数与末根 */
      apply(bars) {
        const clean = normalizeBars(bars);
        if (!clean.length) return [];
        chart.applyNewData(clean.map(toChartBar));
        return clean;
      },

      /**
       * 增量更新最后一根（或追加新的一根）。
       * 时间早于图上最后一根的 bar 直接丢弃——回插会让坐标轴非单调。
       * @returns {boolean} 是否真的写入了
       */
      update(bar) {
        const next = toChartBar(bar);
        if (!Number.isFinite(next.timestamp)) return false;
        const list = (chart.getDataList && chart.getDataList()) || [];
        const last = list.length ? Number(list[list.length - 1].timestamp) : null;
        if (last != null && next.timestamp < last) return false;
        chart.updateData(next);
        return true;
      },

      dataList() { return (chart.getDataList && chart.getDataList()) || []; },

      /** 容器尺寸变化后必须调用，否则画布是旧的像素尺寸 */
      resize() { try { chart.resize(); } catch (_) { /* 已销毁 */ } },

      /** 标记状态文本用不到，但保留给"是否已有数据"的判断 */
      isEmpty() { return this.dataList().length === 0; }
    };
  }

  window.qiceKline = {create, toChartBar, normalizeBars, styles: LIGHT_STYLES};
})();
