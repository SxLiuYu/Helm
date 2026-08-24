'use strict';
/* 用量看板页（#/dashboard）— 依赖 app.js 提供的契约：
   AdminPage.register / api / sse / Alpine store 'app' / AdminBus；图表用全局 echarts。 */
(() => {
  if (!window.AdminPage) { console.error('[dashboard] AdminPage 未就绪，请检查 defer 加载顺序'); return; }
  const { escapeHtml, fmtInt, fmtTokens, fmtMs, fmtCost, relTime, fmtClock } = window;

  let store = null, bus = null;
  let charts = [];            // echarts 实例
  let es = null;              // SSE 句柄
  let pollTimer = null;
  let lastSnapAt = 0;
  let data = null;            // 最近一次完整 dashboard 载荷
  let recentRows = [];        // 流水行（含 SSE 追加）
  let sortKey = 'requests', sortDir = 'desc';
  let busOffs = [];
  const COLORS = { cyan: '#22d3ee', green: '#34d399', red: '#f87171', warn: '#fbbf24', muted: '#a1a1aa', line: '#27272a' };

  /* ---------- KPI ---------- */
  function kpi(title, value, sub, valueCls = '') {
    return `<div class="card kpi"><div class="k-title">${escapeHtml(title)}</div><div class="k-value ${valueCls}">${value}</div><div class="k-sub">${sub}</div></div>`;
  }
  function renderKpis(s) {
    const cost = fmtCost(s.est_cost_usd);
    document.getElementById('kpiGrid').innerHTML = [
      kpi('总请求', fmtInt(s.requests), `成功率 ${s.success_rate == null ? '-' : escapeHtml(String(s.success_rate))}%`),
      kpi('Token 总量', fmtTokens(s.total_tokens), `输入 ${fmtTokens(s.prompt_tokens)} · 输出 ${fmtTokens(s.completion_tokens)}`),
      kpi('平均耗时', fmtMs(s.avg_latency_ms), '成功请求均值'),
      kpi('P95 耗时', fmtMs(s.p95_latency_ms), '95% 请求快于此值'),
      kpi('错误数', fmtInt(s.failures), (Number(s.failures) || 0) > 0 ? '<span class="t-red">存在失败请求</span>' : '窗口内无失败'),
      kpi('估算成本', cost === null ? '-' : escapeHtml(cost), cost === null ? '未配置 usage_cost_per_1m_tokens' : '按单价表估算'),
    ].join('');
  }

  /* ---------- 主图：时间序列（span-8） ---------- */
  function bucketLabel(ts, mode) {
    const d = new Date(ts * 1000);
    if (!Number.isFinite(d.getTime())) return '-';
    return mode === 'time' ? `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}` : `${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
  }
  function renderTsChart(series) {
    const el = document.getElementById('tsChart');
    if (!el) return;
    if (!window.echarts) { el.innerHTML = '<div class="empty-box">echarts 未加载，无法绘制主图</div>'; return; }
    let chart = charts[0] || (charts[0] = window.echarts.init(el));
    if (!series.length) { chart.clear(); el.innerHTML = ''; el.parentNode.querySelector('.chart-empty')?.remove(); el.insertAdjacentHTML('beforebegin', '<div class="empty-box chart-empty">当前范围内暂无请求数据</div>'); return; }
    el.parentNode.querySelector('.chart-empty')?.remove();
    const mode = (Number(store.windowSeconds) || 0) <= 86400 ? 'time' : 'date';
    const x = series.map((b) => bucketLabel(b.bucket_start, mode));
    const okBar = series.map((b) => Math.max(0, (Number(b.requests) || 0) - (Number(b.errors) || 0)));
    const errBar = series.map((b) => Number(b.errors) || 0);
    const latLine = series.map((b) => (b.avg_latency_ms == null ? null : Number(b.avg_latency_ms)));
    chart.setOption({
      backgroundColor: 'transparent',
      animationDuration: 300,
      grid: { left: 46, right: 56, top: 30, bottom: 64 },
      legend: { top: 0, textStyle: { color: COLORS.muted, fontSize: 11 }, itemWidth: 14, data: ['请求数', '错误', '平均耗时'] },
      tooltip: { trigger: 'axis', backgroundColor: '#18181b', borderColor: COLORS.line, textStyle: { color: '#fafafa', fontSize: 12 }, valueFormatter: (v) => (v == null ? '-' : v) },
      xAxis: { type: 'category', data: x, axisLine: { lineStyle: { color: COLORS.line } }, axisLabel: { color: COLORS.muted, fontSize: 10 }, axisTick: { show: false } },
      yAxis: [
        { type: 'value', minInterval: 1, splitLine: { lineStyle: { color: COLORS.line, opacity: .5 } }, axisLabel: { color: COLORS.muted, fontSize: 10 } },
        { type: 'value', splitLine: { show: false }, axisLabel: { color: COLORS.muted, fontSize: 10, formatter: (v) => (v >= 1000 ? (v / 1000).toFixed(1) + 's' : Math.round(v) + 'ms') } },
      ],
      dataZoom: [{ type: 'slider', height: 20, bottom: 12, borderColor: COLORS.line, backgroundColor: 'transparent', fillerColor: 'rgba(34,211,238,.08)', handleStyle: { color: '#232327' }, textStyle: { color: COLORS.muted, fontSize: 10 } }, { type: 'inside' }],
      series: [
        { name: '请求数', type: 'bar', stack: 'req', data: okBar, barMaxWidth: 18, itemStyle: { color: new window.echarts.graphic.LinearGradient(0, 0, 0, 1, [{ offset: 0, color: '#22d3ee' }, { offset: 1, color: 'rgba(34,211,238,.25)' }]), borderRadius: [0, 0, 2, 2] } },
        { name: '错误', type: 'bar', stack: 'req', data: errBar, barMaxWidth: 18, itemStyle: { color: COLORS.red, borderRadius: [3, 3, 0, 0] } },
        { name: '平均耗时', type: 'line', yAxisIndex: 1, data: latLine, smooth: true, symbolSize: 4, connectNulls: false, lineStyle: { color: COLORS.warn, width: 2 }, itemStyle: { color: COLORS.warn }, areaStyle: { color: new window.echarts.graphic.LinearGradient(0, 0, 0, 1, [{ offset: 0, color: 'rgba(251,191,36,.22)' }, { offset: 1, color: 'rgba(251,191,36,0)' }]) } },
      ],
    }, { notMerge: true });
  }

  /* ---------- 项目 Token Top10（span-4） ---------- */
  function renderProjectsChart(projects) {
    const el = document.getElementById('pjChart');
    if (!el) return;
    if (!window.echarts) { el.innerHTML = '<div class="empty-box">echarts 未加载</div>'; return; }
    let chart = charts[1] || (charts[1] = window.echarts.init(el));
    const list = (Array.isArray(projects) ? projects : []).slice().sort((a, b) => (Number(b.total_tokens) || 0) - (Number(a.total_tokens) || 0)).slice(0, 10);
    if (!list.length) { chart.clear(); el.innerHTML = '<div class="empty-box">窗口内无项目数据（project_id 为空的请求不计入）</div>'; return; }
    el.innerHTML = '';
    const names = list.map((p) => String(p.project_id || '-'));
    chart.setOption({
      backgroundColor: 'transparent',
      grid: { left: 8, right: 46, top: 10, bottom: 10, containLabel: true },
      tooltip: { trigger: 'item', backgroundColor: '#18181b', borderColor: COLORS.line, textStyle: { color: '#fafafa', fontSize: 12 }, formatter: (p) => `${escapeHtml(p.name)}<br/>Tokens：${fmtInt(p.value)}<br/>点击流水区可过滤` },
      xAxis: { type: 'value', splitLine: { lineStyle: { color: COLORS.line, opacity: .5 } }, axisLabel: { color: COLORS.muted, fontSize: 10, formatter: (v) => fmtTokens(v) } },
      yAxis: { type: 'category', data: names, inverse: true, axisLine: { show: false }, axisTick: { show: false }, axisLabel: { color: '#cfcfd6', fontSize: 11, width: 88, overflow: 'truncate' } },
      series: [{ type: 'bar', data: list.map((p) => Number(p.total_tokens) || 0), barMaxWidth: 14, itemStyle: { color: new window.echarts.graphic.LinearGradient(0, 0, 1, 0, [{ offset: 0, color: 'rgba(34,211,238,.85)' }, { offset: 1, color: 'rgba(8,145,178,.35)' }]), borderRadius: [0, 6, 6, 0] } }],
    }, { notMerge: true });
    chart.off('click');
    chart.on('click', (p) => { if (p.componentType === 'series') { store.projectFilter = p.name; renderRecent(); const sec = document.getElementById('recentSection'); if (sec) sec.scrollIntoView({ behavior: 'smooth', block: 'start' }); } });
  }

  /* ---------- 目标表格 ---------- */
  const iqBadge = (v) => { const n = Number(v); if (!Number.isFinite(n) || n <= 0) return '<span class="iq q0">-</span>'; const cls = n >= 85 ? 'q85' : n >= 70 ? 'q70' : n >= 50 ? 'q50' : 'q0'; return `<span class="iq ${cls}">${n}</span>`; };
  const statusLight = (t) => { const req = Number(t.requests) || 0; if (!req) return `<span class="light" style="background:#52525b" title="无流量"></span>`; return (Number(t.failures) || 0) / req >= 0.2 ? `<span class="light" style="background:${COLORS.red}" title="失败率较高"></span>` : `<span class="light" style="background:${COLORS.green}" title="运行正常"></span>`; };
  function sortedTargets() {
    const list = (data && Array.isArray(data.targets) ? data.targets.slice() : []);
    list.sort((a, b) => sortDir === 'desc' ? ((Number(b[sortKey]) || 0) - (Number(a[sortKey]) || 0)) : ((Number(a[sortKey]) || 0) - (Number(b[sortKey]) || 0)));
    return list;
  }
  const arrow = (key) => key === sortKey ? `<span class="arrow">${sortDir === 'desc' ? '▼' : '▲'}</span>` : '';
  function renderTargets() {
    const box = document.getElementById('targetsBox');
    if (!box) return;
    const grand = (data && Array.isArray(data.targets) ? data.targets : []).reduce((acc, t) => acc + (Number(t.total_tokens) || 0), 0);
    const rows = sortedTargets();
    box.innerHTML = rows.length ? `<table class="dt"><thead><tr>
      <th></th><th>目标</th><th>模型</th><th>智分</th><th>Provider</th>
      <th class="sortable" data-key="requests">请求${arrow('requests')}</th><th>成功/失败</th><th>限流</th>
      <th class="sortable" data-key="total_tokens">Tokens${arrow('total_tokens')}</th><th>输入</th><th>输出</th><th>平均</th><th>P95</th><th>最近使用</th><th>Token 占比</th>
    </tr></thead><tbody>${rows.map((t) => {
      const req = Number(t.requests) || 0, okN = Number(t.successes) || 0, failN = Number(t.failures) || 0;
      const pt = Number(t.prompt_tokens) || 0, ct = Number(t.completion_tokens) || 0, tt = Number(t.total_tokens) || 0;
      const share = grand > 0 ? Math.min(100, (tt / grand) * 100) : 0;
      return `<tr data-id="${escapeAttr(t.target_id)}">
        <td>${statusLight(t)}</td>
        <td><div class="cell-main">${escapeHtml(t.label || t.target_id || '-')}</div><div class="cell-id"><code>${escapeHtml(t.target_id || '-')}</code></div></td>
        <td><code>${escapeHtml(t.model || '-')}</code></td>
        <td>${iqBadge(t.intelligence)}</td>
        <td>${t.provider == null || t.provider === '' ? '<span class="cell-id">-</span>' : `<span class="chip">${escapeHtml(t.provider)}</span>`}</td>
        <td><strong>${fmtInt(req)}</strong></td>
        <td>${failN > 0 ? `<span class="t-green">${fmtInt(okN)}</span> / <span class="t-red">${fmtInt(failN)}</span>` : `${fmtInt(okN)} / ${fmtInt(failN)}`}</td>
        <td>${(Number(t.rate_limits) || 0) > 0 ? `<span class="t-warn">${fmtInt(t.rate_limits)}</span>` : fmtInt(t.rate_limits)}</td>
        <td><strong>${fmtTokens(tt)}</strong><div class="share-track"><div class="share-fill" style="width:${share.toFixed(1)}%"></div></div></td>
        <td>${fmtTokens(pt)}</td><td>${fmtTokens(ct)}</td>
        <td>${fmtMs(t.avg_latency_ms)}</td><td>${fmtMs(t.p95_latency_ms)}</td>
        <td class="cell-id">${escapeHtml(relTime(t.last_used_at))}</td>
        <td>${share.toFixed(1)}%</td>
      </tr>`;
    }).join('')}</tbody></table>` : '<div class="empty-box">当前范围内没有目标被调用</div>';
    box.querySelectorAll('th.sortable').forEach((th) => { th.onclick = () => { const k = th.dataset.key; if (sortKey === k) sortDir = sortDir === 'desc' ? 'asc' : 'desc'; else { sortKey = k; sortDir = 'desc'; } renderTargets(); }; });
    box.querySelectorAll('tbody tr').forEach((tr) => { tr.onclick = () => { location.hash = '#/targets/' + encodeURIComponent(tr.dataset.id); }; });
  }
  function escapeAttr(v) { return escapeHtml(v).replace(/`/g, '&#96;'); }

  /* ---------- 实时流水 ---------- */
  function recentRow(r, flash = false) {
    const okReq = Number(r.success) === 1;
    const isStream = Number(r.stream) === 1;
    const errText = r.error ? escapeHtml(String(r.error).slice(0, 90)) : '';
    const match = !store.projectFilter || r.project_id === store.projectFilter;
    if (!match) return null;
    return `<tr class="${!okReq ? 'err-row' : ''}${flash ? ' flash' : ''}">
      <td class="cell-id">${escapeHtml(fmtClock(Number(r.created_at)))}</td>
      <td><code>${escapeHtml(r.target_id || '-')}</code></td>
      <td>${r.scenario ? `<span class="chip">${escapeHtml(r.scenario)}</span>` : '<span class="cell-id">-</span>'}</td>
      <td>${r.project_id != null && r.project_id !== '' ? `<span class="chip">${escapeHtml(r.project_id)}</span>` : '<span class="cell-id">-</span>'}</td>
      <td>${isStream ? '<span class="stream-tag">⚡ 流式</span>' : '<span class="cell-id">-</span>'}</td>
      <td>${okReq ? '<span class="t-green">✓ 成功</span>' : `<span class="t-red">✗ 失败</span>${r.status != null ? ' · HTTP ' + fmtInt(r.status) : ''}${errText ? `<div class="cell-id err-text" title="${escapeAttr(r.error)}">${errText}</div>` : ''}`}</td>
      <td>${fmtMs(r.latency_ms)}</td>
      <td>${isStream && r.first_token_ms != null ? fmtMs(r.first_token_ms) : '<span class="cell-id">-</span>'}</td>
      <td>${fmtTokens(r.prompt_tokens)} / ${fmtTokens(r.completion_tokens)} / ${fmtTokens(r.total_tokens)}</td>
    </tr>`;
  }
  function renderRecent() {
    const box = document.getElementById('recentBox');
    if (!box) return;
    const filterBar = document.getElementById('filterBar');
    filterBar.innerHTML = store.projectFilter ? `仅显示项目 <span class="chip">${escapeHtml(store.projectFilter)}</span> 的请求 <button id="clearFilter" type="button">清除过滤</button>` : '';
    filterBar.querySelector('#clearFilter')?.addEventListener('click', () => { store.projectFilter = ''; renderRecent(); });
    const rows = recentRows.map((r) => recentRow(r)).filter(Boolean);
    box.innerHTML = rows.length ? `<table class="dt"><thead><tr><th>时间</th><th>目标</th><th>场景</th><th>项目</th><th>模式</th><th>状态</th><th>耗时</th><th>首字</th><th>Tokens 输入/输出/总</th></tr></thead><tbody>${rows.join('')}</tbody></table>` : '<div class="empty-box">窗口内暂无请求流水</div>';
  }

  /* ---------- 数据加载与渲染 ---------- */
  async function loadData() {
    try {
      data = await api('api/usage/dashboard?window=' + (store.windowSeconds | 0));
      recentRows = Array.isArray(data.recent) ? data.recent.slice(0, 50) : [];
      renderKpis(data.summary || {});
      renderTsChart(Array.isArray(data.series) ? data.series : []);
      renderProjectsChart(data.projects);
      renderTargets();
      renderRecent();
    } catch (e) {
      if (e && e.status === 401) return;   // 登录遮罩已弹出，登录成功后由 auth 事件重挂载
      const grid = document.getElementById('kpiGrid');
      if (grid) grid.innerHTML = `<div class="empty-box" style="grid-column:1/-1">加载失败：${escapeHtml(e.message)} <button id="retryBtn" type="button">重试</button></div>`;
      document.getElementById('retryBtn')?.addEventListener('click', loadData);
    }
  }
  function onSseEvent(e) {
    if (e.type === 'request') {
      let row;
      try { row = JSON.parse(e.data); } catch { return; }
      recentRows.unshift(row);
      recentRows = recentRows.slice(0, 50);
      const tr = recentRow(row, true);
      const tbody = document.querySelector('#recentBox tbody');
      if (tr && tbody) tbody.insertAdjacentHTML('afterbegin', tr);
    } else if (e.type === 'snapshot') {
      const now = Date.now();
      if (now - lastSnapAt < 500) return;   // 节流 500ms
      lastSnapAt = now;
      loadData();                            // 刷新 KPI 与主图
    }
  }
  function startRealtime() {
    stopRealtime();
    es = sse('api/usage/stream', onSseEvent);   // onerror 时封装内部置 store.degraded=true（顶栏黄标）
    pollTimer = setInterval(() => { if (store.autoRefresh || store.degraded) loadData(); }, 15000);
  }
  function stopRealtime() {
    if (es) { try { es.close(); } catch {} es = null; }
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  }

  /* ---------- 模块注册 ---------- */
  window.AdminPage.register('dashboard', {
    title: '用量看板',
    mount(viewEl, ctx) {
      store = ctx.store; bus = ctx.bus;
      viewEl.innerHTML = `
        <section class="card"><h2>核心指标</h2><div class="kpi-grid" id="kpiGrid"><div class="loading-page" style="grid-column:1/-1;padding:26px">正在加载…</div></div></section>
        <section class="chart-row">
          <div class="card span-8"><h2>请求与耗时时间序列</h2><div class="chart-box" id="tsChart"></div></div>
          <div class="card span-4"><h2>项目 Token Top10（点击过滤流水）</h2><div class="chart-box" id="pjChart"></div></div>
        </section>
        <section class="card"><h2>目标节点用量（点击行查看详情，表头点击排序）</h2><div class="table-scroll" id="targetsBox"><div class="loading-page" style="padding:26px">正在加载…</div></div></section>
        <section class="card" id="recentSection"><h2>实时请求流水（EventSource）</h2><div class="filter-bar" id="filterBar"></div><div class="table-scroll" id="recentBox"><div class="loading-page" style="padding:26px">正在加载…</div></div></section>`;
      loadData();
      startRealtime();
      const offWin = bus.on('window', () => loadData());
      busOffs = [offWin];
    },
    unmount() {
      stopRealtime();
      busOffs.forEach((off) => off());
      busOffs = [];
      charts.forEach((c) => { try { c.dispose(); } catch {} });
      charts = [];
      data = null; recentRows = [];
    },
  });
})();
