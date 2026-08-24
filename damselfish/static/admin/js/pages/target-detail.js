'use strict';
/* ============================================================================
 * Damselfish Admin — 目标详情页 #/targets/{id}  (fe-eng / t8)
 *
 * 契约（由 app.js 提供）：
 *   - 文件末尾 window.AdminPage.register('targets', module)
 *     module = { title, mount(viewEl, ctx), unmount() }
 *   - ctx = { store, params, bus }；#/targets/{id} 时 params.id 为目标 id
 *   - api(path, options)：path 相对 /admin/；401 由壳弹出登录遮罩
 *   - bus 事件：'window'(窗口切换) · 'autorefresh'(开关切换) · 'auth'(登录成功)
 *
 * 数据源：GET api/targets/{id}/detail?window=
 *   { target{target_id,label,model,provider,intelligence,priority,enabled,
 *            available,capabilities[],base_url},
 *     summary{requests,successes,failures,rate_limits,prompt_tokens,
 *             completion_tokens,total_tokens,avg_latency_ms,p95_latency_ms},
 *     series[{bucket_start,requests,errors,avg_latency_ms,prompt_tokens,completion_tokens}],
 *     recent[同 dashboard.recent],
 *     errors[{created_at,status,error}] }
 *
 * 自检：?mock=1 走内置样例常量，不发起任何请求。
 * ========================================================================= */
(() => {
  const MOCK = new URLSearchParams(location.search).has('mock');
  const AUTO_MS = 15000;

  /* ---------------- 本地工具（不依赖壳的全局） ---------------- */
  const esc = (v) => String(v ?? '').replace(/[&<>'"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[c]));
  const attr = (v) => esc(v).replace(/`/g, '&#96;').replace(/\n/g, ' ');
  const fmtInt = (v) => (Number.isFinite(Number(v)) ? Number(v).toLocaleString('zh-CN') : '-');
  const fmtTokens = (v) => { const n = Number(v); if (!Number.isFinite(n)) return '-'; if (Math.abs(n) >= 1e6) return (n / 1e6).toFixed(1).replace(/\.0$/, '') + 'M'; if (Math.abs(n) >= 1000) return (n / 1000).toFixed(1).replace(/\.0$/, '') + 'k'; return String(n); };
  const fmtMs = (v) => { if (v === null || v === undefined || v === '') return '-'; const n = Number(v); if (!Number.isFinite(n)) return '-'; if (n >= 60000) { const m = Math.floor(n / 60000), s = Math.round((n % 60000) / 1000); return `${m}m${s}s`; } if (n >= 1000) return (n / 1000).toFixed(2) + 's'; return Math.round(n) + 'ms'; };
  const two = (v) => String(v).padStart(2, '0');
  const fmtClock = (ts) => { const d = new Date(Number(ts) * 1000); return Number.isFinite(d.getTime()) && Number(ts) > 0 ? `${two(d.getMonth() + 1)}-${two(d.getDate())} ${two(d.getHours())}:${two(d.getMinutes())}:${two(d.getSeconds())}` : '-'; };

  function intelBadge(score) {
    const n = Number(score);
    if (!Number.isFinite(n) || n <= 0) return '<span class="pg-muted" title="未配置智能分">智分 -</span>';
    const cls = n >= 85 ? 'cyan' : n >= 70 ? 'green' : n >= 50 ? 'warn' : 'gray';
    return `<span class="pg-badge pg-badge--${cls}" title="智能分 ${n}">智分 ${n}</span>`;
  }

  /* ---------------- 内置样例（?mock=1） ---------------- */
  const MOCK_TARGET_META = {
    'finna-glm-5.2': { label: 'Finna GLM-5.2', model: 'glm-5.2', provider: 'finna', intelligence: 88, base_url: 'https://api.finna-platform.com/v1' },
    'stepfun-step-2-16k': { label: 'StepFun Step 2 16K', model: 'step-2-16k', provider: 'stepfun', intelligence: 68, base_url: 'https://api.stepfun.xyz/v1' },
    'openrouter-deepseek-v31-free': { label: 'DeepSeek V3.1 Free', model: 'deepseek/deepseek-chat-v3.1:free', provider: 'openrouter', intelligence: 58, base_url: 'https://openrouter.ai/api/v1' },
    'agnes-2.5-flash': { label: 'Agnes 2.5 Flash', model: 'agnes-2.5-flash', provider: null, intelligence: 0, base_url: 'https://agnes.example.com/v1' },
  };
  function buildMockDetail(id, windowSeconds) {
    const known = Object.prototype.hasOwnProperty.call(MOCK_TARGET_META, id);
    const meta = known ? MOCK_TARGET_META[id] : Object.values(MOCK_TARGET_META)[0];
    const targetId = known ? id : Object.keys(MOCK_TARGET_META)[0];
    const span = windowSeconds > 0 ? windowSeconds : 7 * 86400;
    const nb = 30, stepSec = Math.max(60, Math.ceil(span / nb));
    const now = Date.now() / 1000;
    const start = Math.floor((now - span) / stepSec) * stepSec;
    let reqSum = 0, errSum = 0, ptSum = 0, ctSum = 0;
    const lats = [];
    const series = [];
    for (let i = 0; i <= nb; i++) {
      const t = start + i * stepSec;
      const requests = Math.max(0, Math.round(Math.sin(i / 3.4) * 3 + 3.6));
      const errors = i % 9 === 5 ? 1 : 0;
      const avg = i % 8 === 3 ? null : Math.round(820 + Math.sin(i / 2.3) * 380);
      series.push({ bucket_start: t, requests, errors, avg_latency_ms: avg, prompt_tokens: requests * 260 + (i % 4) * 60, completion_tokens: requests * 140 + (i % 3) * 45 });
      reqSum += requests; errSum += errors;
      ptSum += series[i].prompt_tokens; ctSum += series[i].completion_tokens;
      if (avg != null) lats.push(avg);
    }
    lats.sort((a, b) => a - b);
    return {
      window_seconds: windowSeconds,
      target: {
        target_id: targetId, label: meta.label, model: meta.model, provider: meta.provider,
        intelligence: meta.intelligence, priority: 10, enabled: true, available: true,
        capabilities: ['chat', 'tools', 'chinese'], base_url: meta.base_url,
      },
      summary: {
        requests: reqSum, successes: reqSum - errSum, failures: errSum, rate_limits: 2,
        prompt_tokens: ptSum, completion_tokens: ctSum, total_tokens: ptSum + ctSum,
        avg_latency_ms: lats.length ? Math.round(lats.reduce((a, b) => a + b, 0) / lats.length) : null,
        p95_latency_ms: lats.length ? lats[Math.min(lats.length - 1, Math.floor(lats.length * 0.95))] : null,
      },
      series,
      recent: [
        { id: 512, created_at: now - 12, target_id: targetId, scenario: 'coding', persona: 'dev', project_id: 'damselfish', latency_ms: 812.3, success: 1, status: 200, error: null, prompt_tokens: 1200, completion_tokens: 640, total_tokens: 1840, stream: 1, first_token_ms: 310.5 },
        { id: 511, created_at: now - 61, target_id: targetId, scenario: 'reasoning', persona: null, project_id: null, latency_ms: 2310.9, success: 1, status: 200, error: null, prompt_tokens: 900, completion_tokens: 480, total_tokens: 1380, stream: 1, first_token_ms: 890.2 },
        { id: 510, created_at: now - 180, target_id: targetId, scenario: 'chat', persona: null, project_id: 'demo', latency_ms: null, success: 0, status: 502, error: '上游连接超时：upstream deadline exceeded while waiting for first byte from https://api.finna-platform.com/v1/chat/completions after 30s (proxy_read_timeout=30s, connect=ok, tls=ok, request sent=29.98s)', prompt_tokens: 0, completion_tokens: 0, total_tokens: 0, stream: 0, first_token_ms: null },
        { id: 509, created_at: now - 900, target_id: targetId, scenario: 'default', persona: null, project_id: 'demo', latency_ms: 654.1, success: 1, status: 200, error: null, prompt_tokens: 310, completion_tokens: 122, total_tokens: 432, stream: 0, first_token_ms: null },
      ],
      errors: [
        { created_at: now - 180, status: 502, error: '上游连接超时：upstream deadline exceeded while waiting for first byte from https://api.finna-platform.com/v1/chat/completions after 30s (proxy_read_timeout=30s, connect=ok, tls=ok, request sent=29.98s)' },
        { created_at: now - 620, status: 429, error: '限流：Too Many Requests，命中上游配额（retry-after: 17s），已按轮换策略冷却该 Key' },
        { created_at: now - 3600, status: 400, error: '上下文超限：prompt_tokens 138000 > max_context 128000，请求被拒绝' },
      ],
    };
  }

  /* ---------------- 模块状态 ---------------- */
  const state = { id: null, win: 86400, store: null, charts: [], timer: null, onResize: null, onVis: null, handlers: {}, seq: 0 };

  /* ---------------- 数据获取 ---------------- */
  async function fetchDetail() {
    if (MOCK) { await new Promise((r) => setTimeout(r, 120)); return buildMockDetail(state.id, state.win); }
    const body = await api(`api/targets/${encodeURIComponent(state.id)}/detail?window=${state.win}`);
    return body && body.target ? body : Promise.reject(new Error('响应格式无效'));
  }

  /* ---------------- 渲染 ---------------- */
  function skeleton(viewEl) {
    viewEl.innerHTML = `
      <div class="pg-root">
        <div class="pg-card pg-t-actions">
          <a class="pg-back" href="#/dashboard"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="m15 18-6-6 6-6"/></svg>返回看板</a>
          <span class="pg-count-note" id="pg-t-window"></span>
        </div>
        <div id="pg-t-body"><div class="pg-loading">正在加载目标详情…</div></div>
      </div>`;
    return viewEl.querySelector('#pg-t-body');
  }

  function renderHeader(el, d) {
    const t = d.target;
    const dotCls = t.available && t.enabled ? 'ok' : t.enabled ? 'bad' : 'off';
    const dotText = !t.enabled ? '已停用' : t.available ? '可用' : '不可用';
    el.querySelector('#pg-t-head').innerHTML = `
      <div class="pg-t-head-main">
        <span class="pg-dot pg-dot--${dotCls}" title="${dotText}"></span>
        <h2 class="pg-t-title">${esc(t.label)}</h2>
        ${intelBadge(t.intelligence)}
        ${t.enabled ? '' : '<span class="pg-badge pg-badge--gray">已停用</span>'}
        ${t.available ? '' : '<span class="pg-badge pg-badge--red">不可用</span>'}
      </div>
      <div class="pg-t-meta">
        <span>ID <b>${esc(t.target_id)}</b></span>
        ${t.provider ? `<span>Provider <span class="pg-chip">${esc(t.provider)}</span></span>` : ''}
        <span>模型 <code>${esc(t.model)}</code></span>
        <span class="pg-copywrap">Base URL <code>${esc(t.base_url)}</code>
          <button type="button" class="pg-iconbtn pg-copybtn" data-copy="${attr(t.base_url)}" title="复制 Base URL" aria-label="复制 Base URL">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/></svg>
          </button><span class="pg-copied pg-hide">已复制</span></span>
        <span>优先级 <b>${fmtInt(t.priority)}</b></span>
        ${(t.capabilities || []).map((c) => `<span class="pg-chip">${esc(c)}</span>`).join('')}
      </div>`;
    const btn = el.querySelector('.pg-copybtn');
    btn.addEventListener('click', async () => {
      try { await navigator.clipboard.writeText(btn.dataset.copy); }
      catch {
        const ta = document.createElement('textarea');
        ta.value = btn.dataset.copy; document.body.appendChild(ta); ta.select();
        try { document.execCommand('copy'); } catch { /* 忽略 */ }
        ta.remove();
      }
      const tip = el.querySelector('.pg-copied');
      tip.classList.remove('pg-hide'); btn.classList.add('copied');
      setTimeout(() => { tip.classList.add('pg-hide'); btn.classList.remove('copied'); }, 1300);
    });
  }

  function sumCard(k, v, sub, cls) {
    return `<div class="pg-sumcard"><div class="k">${k}</div><div class="v${cls ? ' ' + cls : ''}">${v}</div>${sub ? `<div class="s">${sub}</div>` : ''}</div>`;
  }
  function renderSummary(el, s) {
    const rate = s.requests > 0 ? `${((s.successes / s.requests) * 100).toFixed(1)}% 成功` : '';
    el.querySelector('#pg-t-summary').innerHTML =
      sumCard('请求数', fmtInt(s.requests)) +
      sumCard('成功', fmtInt(s.successes), rate, 'pg-ok-text') +
      sumCard('失败', fmtInt(s.failures), '', s.failures > 0 ? 'pg-bad-text' : '') +
      sumCard('限流', fmtInt(s.rate_limits), '', s.rate_limits > 0 ? 'pg-bad-text' : '') +
      sumCard('输入 Tokens', fmtTokens(s.prompt_tokens)) +
      sumCard('输出 Tokens', fmtTokens(s.completion_tokens)) +
      sumCard('总 Tokens', fmtTokens(s.total_tokens)) +
      sumCard('平均耗时', fmtMs(s.avg_latency_ms)) +
      sumCard('P95 耗时', fmtMs(s.p95_latency_ms));
  }

  function baseGridOption() {
    return {
      backgroundColor: 'transparent',
      animationDuration: 260,
      grid: { left: 46, right: 14, top: 26, bottom: 52 },
      tooltip: { trigger: 'axis', backgroundColor: '#18181b', borderColor: '#27272a', textStyle: { color: '#fafafa', fontSize: 12 } },
      xAxis: { type: 'time', axisLine: { lineStyle: { color: '#3f3f46' } }, axisLabel: { color: '#a1a1aa', fontSize: 11, hideOverlap: true }, splitLine: { show: false } },
      yAxis: { type: 'value', axisLabel: { color: '#a1a1aa', fontSize: 11 }, splitLine: { lineStyle: { color: '#202024' } } },
      dataZoom: [{ type: 'inside' }, { type: 'slider', height: 22, bottom: 10, borderColor: '#27272a', backgroundColor: '#141417', fillerColor: 'rgba(34,211,238,.12)', handleStyle: { color: '#22d3ee' }, textStyle: { color: '#a1a1aa', fontSize: 10 } }],
    };
  }

  function renderCharts(el, d) {
    const hostLatency = el.querySelector('#pg-chart-lat');
    const hostToken = el.querySelector('#pg-chart-tok');
    if (!hostLatency || !hostToken || typeof echarts === 'undefined') return;
    const mkChart = (host) => { const c = echarts.init(host); state.charts.push(c); return c; };
    const latChart = mkChart(hostLatency);
    const tokChart = mkChart(hostToken);

    const latOpt = baseGridOption();
    latOpt.yAxis.name = 'ms';
    latOpt.yAxis.nameTextStyle = { color: '#a1a1aa', fontSize: 11 };
    latOpt.series = [{
      name: '平均延迟', type: 'line', smooth: true, showSymbol: false, connectNulls: false,
      data: d.series.map((b) => [b.bucket_start * 1000, b.avg_latency_ms]),
      lineStyle: { color: '#22d3ee', width: 2 },
      itemStyle: { color: '#22d3ee' },
      areaStyle: { color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [{ offset: 0, color: 'rgba(34,211,238,.28)' }, { offset: 1, color: 'rgba(34,211,238,.02)' }]) },
    }];
    latChart.setOption(latOpt);

    const tokOpt = baseGridOption();
    tokOpt.yAxis.name = 'tokens';
    tokOpt.yAxis.nameTextStyle = { color: '#a1a1aa', fontSize: 11 };
    tokOpt.series = [
      { name: '输入 tokens', type: 'bar', stack: 'tok', barMaxWidth: 26, itemStyle: { color: '#22d3ee' }, data: d.series.map((b) => [b.bucket_start * 1000, b.prompt_tokens]) },
      { name: '输出 tokens', type: 'bar', stack: 'tok', itemStyle: { color: '#a78bfa' }, data: d.series.map((b) => [b.bucket_start * 1000, b.completion_tokens]) },
    ];
    tokOpt.legend = { top: 0, right: 0, textStyle: { color: '#a1a1aa', fontSize: 11 }, itemWidth: 14, itemHeight: 9 };
    tokChart.setOption(tokOpt);

    state.onResize = () => state.charts.forEach((c) => c.resize());
    window.addEventListener('resize', state.onResize);
  }

  function renderErrors(el, list) {
    const box = el.querySelector('#pg-t-errors');
    const rows = Array.isArray(list) ? list : [];
    if (!rows.length) {
      box.innerHTML = '<h2>错误明细</h2><div class="pg-empty">该窗口内无错误 🎉</div>';
      return;
    }
    const body = rows.map((r, ri) => {
      const msg = String(r.error ?? '');
      const long = msg.length > 120;
      const short = long ? esc(msg.slice(0, 120)) : esc(msg);
      return `<tr>
        <td class="pg-muted">${fmtClock(r.created_at)}</td>
        <td><span class="pg-chip">${fmtInt(r.status)}</span></td>
        <td class="pg-errcell"><span class="pg-errtext">${short}${long ? '…' : ''}</span>${long ? `<button type="button" class="pg-errtoggle" data-row="${ri}" aria-expanded="false">展开全文</button>` : ''}</td>
      </tr>`;
    }).join('');
    box.innerHTML = `<h2>错误明细（${rows.length} 条）</h2><div class="pg-tablewrap"><table class="pg-table">
      <thead><tr><th style="width:150px">时间</th><th style="width:70px">状态码</th><th>错误信息</th></tr></thead>
      <tbody>${body}</tbody></table></div>`;
    box.querySelectorAll('.pg-errtoggle').forEach((btn) => {
      btn.addEventListener('click', () => {
        const row = rows[Number(btn.dataset.row)] || { error: '' };
        const full = esc(String(row.error ?? ''));
        const open = btn.getAttribute('aria-expanded') === 'true';
        const cell = btn.closest('.pg-errcell');
        cell.querySelector('.pg-errtext').textContent = open ? full.slice(0, 120) + '…' : full;
        btn.textContent = open ? '展开全文' : '收起';
        btn.setAttribute('aria-expanded', String(!open));
      });
    });
  }

  function renderRecent(el, list) {
    const rows = (Array.isArray(list) ? list : []).slice().sort((a, b) => (Number(b.created_at) || 0) - (Number(a.created_at) || 0)).slice(0, 50);
    const body = rows.length ? rows.map((r) => {
      const okReq = Number(r.success) === 1;
      const isStream = Number(r.stream) === 1;
      const statusCell = okReq
        ? '<span class="pg-ok-text">✓ 成功</span>'
        : `<span class="pg-bad-text">✗ 失败</span>${r.status != null ? ` · HTTP ${fmtInt(r.status)}` : ''}${r.error
          ? `<div class="pg-muted" style="max-width:240px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${attr(r.error)}">${esc(String(r.error).slice(0, 80))}</div>`
          : ''}`;
      return `<tr>
        <td class="pg-muted">${fmtClock(r.created_at)}</td>
        <td>${r.scenario ? `<span class="pg-chip">${esc(r.scenario)}</span>` : '<span class="pg-muted">-</span>'}</td>
        <td>${r.project_id ? `<span class="pg-chip">${esc(r.project_id)}</span>` : '<span class="pg-muted">-</span>'}</td>
        <td>${isStream ? '<span class="pg-badge pg-badge--cyan">⚡ 流式</span>' : '<span class="pg-muted">-</span>'}</td>
        <td class="num">${isStream && r.first_token_ms != null ? fmtMs(r.first_token_ms) : '<span class="pg-muted">-</span>'}</td>
        <td class="num">${fmtMs(r.latency_ms)}</td>
        <td class="num">${fmtTokens(r.prompt_tokens)} / ${fmtTokens(r.completion_tokens)} / <b>${fmtTokens(r.total_tokens)}</b></td>
        <td>${statusCell}</td>
      </tr>`;
    }).join('') : '<tr><td colspan="8"><div class="pg-empty">窗口内暂无请求流水</div></td></tr>';
    el.querySelector('#pg-t-recent').innerHTML = `<h2>最近请求流水</h2><div class="pg-tablewrap"><table class="pg-table">
      <thead><tr><th>时间</th><th>场景</th><th>项目</th><th>模式</th><th style="text-align:right">首字延迟</th><th style="text-align:right">耗时</th><th style="text-align:right">Tokens（入/出/总）</th><th>状态</th></tr></thead>
      <tbody>${body}</tbody></table></div>`;
  }

  function renderAll(d) {
    const root = document.getElementById('pg-target-detail');
    if (!root) return;
    const note = root.querySelector('#pg-t-window');
    if (note) note.textContent = d.window_seconds > 0 ? `窗口：近${d.window_seconds >= 604800 ? '7天' : d.window_seconds >= 86400 ? '24小时' : '1小时'}` : '窗口：全部';
    const el = root.querySelector('#pg-t-body');
    el.innerHTML = `
      <div class="pg-card" id="pg-t-head"></div>
      <div class="pg-sumrow" id="pg-t-summary"></div>
      <div class="pg-chartgrid">
        <div class="pg-card"><h2>延迟历史（平均 ms）</h2><div class="pg-chartbox" id="pg-chart-lat"></div></div>
        <div class="pg-card"><h2>Token 消耗（堆叠）</h2><div class="pg-chartbox" id="pg-chart-tok"></div></div>
      </div>
      <div class="pg-card" id="pg-t-errors"></div>
      <div class="pg-card" id="pg-t-recent"></div>`;
    renderHeader(el, d);
    renderSummary(el, d.summary || {});
    renderCharts(el, d);
    renderErrors(el, d.errors);
    renderRecent(el, d.recent);
  }

  /* ---------------- 加载与自动刷新 ---------------- */
  async function load() {
    const root = document.getElementById('pg-target-detail');
    if (!root) return;
    const seq = ++state.seq;
    try {
      const d = await fetchDetail();
      if (seq !== state.seq) return;               // 过期响应丢弃
      renderAll(d);
    } catch (err) {
      if (seq !== state.seq) return;
      const el = root.querySelector('#pg-t-body');
      if (!el) return;
      if (err && err.status === 404) {
        el.innerHTML = `<div class="pg-errorbox">目标「${esc(state.id)}」不存在或已被移除。<a class="pg-back" href="#/dashboard">返回看板</a></div>`;
      } else if (!(err && err.status === 401)) {   // 401 已由壳的登录遮罩接管
        el.innerHTML = `<div class="pg-errorbox"><span>加载失败：${esc(err.message || '未知错误')}</span><button type="button" class="pg-btn" id="pg-retry">重试</button></div>`;
        el.querySelector('#pg-retry').addEventListener('click', load);
      }
    }
  }

  function setupAutoRefresh() {
    if (state.timer) clearInterval(state.timer);
    state.timer = setInterval(() => {
      if (!document.hidden && state.store && state.store.autoRefresh && !MOCK) load();
    }, AUTO_MS);
  }

  /* ---------------- 模块注册 ---------------- */
  window.AdminPage.register('targets', {
    title: '目标详情',
    mount(viewEl, ctx) {
      state.id = ctx.params.id;
      state.win = Number(ctx.store.windowSeconds) || 86400;
      state.store = ctx.store || {};
      state.charts = [];
      if (!state.id) {
        viewEl.innerHTML = `<div class="pg-root"><div class="pg-errorbox">缺少目标 ID。<a class="pg-back" href="#/dashboard">返回看板</a></div></div>`;
        return;
      }
      skeleton(viewEl).closest('.pg-root').id = 'pg-target-detail';
      // 全局窗口控件变化 → 重新拉数据；登录成功 → 刷新
      state.bus = ctx.bus;
      state.handlers.window = () => { state.win = Number(state.store.windowSeconds) || state.win; load(); };
      state.handlers.auth = () => load();
      ctx.bus.on('window', state.handlers.window);
      ctx.bus.on('auth', state.handlers.auth);
      state.onVis = () => { if (!document.hidden && state.store.autoRefresh && !MOCK) load(); };
      document.addEventListener('visibilitychange', state.onVis);
      load();
      setupAutoRefresh();
    },
    unmount() {
      state.seq++;                                  // 使在途响应失效
      if (state.bus) {
        if (state.handlers.window) state.bus.off('window', state.handlers.window);
        if (state.handlers.auth) state.bus.off('auth', state.handlers.auth);
      }
      state.handlers = {};
      state.bus = null;
      if (state.timer) { clearInterval(state.timer); state.timer = null; }
      if (state.onResize) { window.removeEventListener('resize', state.onResize); state.onResize = null; }
      if (state.onVis) { document.removeEventListener('visibilitychange', state.onVis); state.onVis = null; }
      state.charts.forEach((c) => { try { c.dispose(); } catch { /* 忽略 */ } });
      state.charts = [];
    },
  });
})();
