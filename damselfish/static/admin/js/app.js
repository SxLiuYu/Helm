'use strict';
/* ============================================================================
 * Damselfish Admin Shell — app.js
 * 提供给页面模块的契约（fe-eng 按此开发 pages/target-detail.js 与 pages/nodes.js）：
 *
 * 【模块注册】每个页面文件末尾：window.AdminPage.register(name, module)
 *   module = { title:'中文标题', mount(viewEl, ctx){}, unmount(){} }
 *   name：'dashboard' | 'targets'(目标详情，路由 #/targets/{id}) | 'nodes' | ...
 *   ctx  = { store, params, bus }
 *     - store：Alpine 全局 store 'app' 的状态对象
 *       { authed, windowSeconds(3600|86400|604800|0), autoRefresh, degraded, health, projectFilter }
 *     - params：路由参数（#/targets/{id} 时 params.id 可取）
 *     - bus：跨模块事件 AdminBus{on,off,emit}，事件：'window'(窗口切换)、'autorefresh'
 *
 * 【全局函数】
 *   api(path, options)  → Promise<any>；path 相对 /admin/（如 'api/usage/dashboard?window=86400'）
 *                         401 自动弹出登录遮罩；URL 带 ?mock=1 时全部走内置样例。
 *   sse(path, onEvent)  → EventSource；监听 event:request / event:snapshot 并回调 onEvent(e)；
 *                         onerror 置 store.degraded=true（顶栏黄标，页面应退化为 15s 轮询），
 *                         重连成功自动清除降级态。返回句柄有 close()。
 * ========================================================================= */
(() => {
  const MOCK = new URLSearchParams(location.search).has('mock');
  // 兼容 /admin、/admin/、/admin/{rest:path} 以及反代子路径部署
  const ADMIN_BASE = (() => {
    const m = location.pathname.match(/^(.*\/admin)(?:\/|$)/);
    return (m ? m[1] : '/admin') + '/';
  })();
  const HEALTH_URL = ADMIN_BASE.replace(/\/admin\/$/, '/') + 'health';

  /* ---------------- 全局应用状态 ---------------- */
  const appState = {
    authed: false,
    windowSeconds: 86400,
    autoRefresh: true,
    degraded: false,
    health: null,          // 'ok' | 'degraded' | null(未知)
    projectFilter: '',     // 流水按项目过滤（看板项目图点击设置）
  };

  /* ---------------- 轻量事件总线 ---------------- */
  const busListeners = {};
  const AdminBus = {
    on(evt, fn) { (busListeners[evt] ||= []).push(fn); return () => AdminBus.off(evt, fn); },
    off(evt, fn) { busListeners[evt] = (busListeners[evt] || []).filter((f) => f !== fn); },
    emit(evt, payload) { (busListeners[evt] || []).slice().forEach((fn) => { try { fn(payload); } catch (e) { console.error('[bus]', evt, e); } }); },
  };

  /* ---------------- 工具 ---------------- */
  const $ = (sel) => document.querySelector(sel);
  const escapeHtml = (v) => String(v).replace(/[&<>'"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[c]));
  const fmtInt = (v) => (Number.isFinite(Number(v)) ? Number(v).toLocaleString('zh-CN') : '-');
  const fmtTokens = (v) => { const n = Number(v); if (!Number.isFinite(n)) return '-'; if (Math.abs(n) >= 1e6) return (n / 1e6).toFixed(1).replace(/\.0$/, '') + 'M'; if (Math.abs(n) >= 1000) return (n / 1000).toFixed(1).replace(/\.0$/, '') + 'k'; return String(n); };
  const fmtMs = (v) => { if (v === null || v === undefined || v === '') return '-'; const n = Number(v); if (!Number.isFinite(n)) return '-'; if (n >= 60000) { const m = Math.floor(n / 60000), s = Math.round((n % 60000) / 1000); return `${m}m ${s}s`; } if (n >= 1000) return (n / 1000).toFixed(2) + 's'; return Math.round(n) + 'ms'; };
  const fmtCost = (v) => { if (v === null || v === undefined || v === '') return null; const n = Number(v); if (!Number.isFinite(n)) return null; if (n === 0) return '$0.00'; if (n < 0.01) return '$' + n.toFixed(4); return '$' + n.toFixed(2); };
  const relTime = (ts) => { const n = Number(ts); if (!Number.isFinite(n) || n <= 0) return '从未使用'; const diff = Date.now() / 1000 - n; if (diff < 60) return '刚刚'; if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`; if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`; const d = new Date(n * 1000); return `${d.getMonth() + 1}-${String(d.getDate()).padStart(2, '0')}`; };
  const two = (v) => String(v).padStart(2, '0');
  const fmtClock = (ts) => { const d = new Date(ts * 1000); return Number.isFinite(d.getTime()) ? `${two(d.getHours())}:${two(d.getMinutes())}:${two(d.getSeconds())}` : '-'; };
  Object.assign(window, { escapeHtml, fmtInt, fmtTokens, fmtMs, fmtCost, relTime, fmtClock });

  /* ================= 内置样例数据（?mock=1） ================= */
  function mockSeries(nb, spanSec) {
    const now = Date.now() / 1000, step = Math.max(60, Math.ceil(spanSec / nb));
    const start = Math.floor((now - spanSec) / step) * step;
    const out = [];
    for (let i = 0; i <= nb; i++) {
      const requests = Math.max(0, Math.round(Math.sin(i / 3.2) * 2.4 + Math.cos(i / 5.7) * 1.6 + 2.6));
      out.push({ bucket_start: start + i * step, requests, errors: i % 11 === 4 ? 1 : 0, prompt_tokens: requests * 210 + (i % 5) * 40, completion_tokens: requests * 130 + (i % 3) * 55, avg_latency_ms: i % 7 === 3 ? null : Math.round(700 + Math.sin(i / 2.1) * 420 + 320) });
    }
    return out;
  }
  const MOCK_TARGETS = [
    { target_id: 'finna-glm-5.2', label: 'Finna GLM-5.2', model: 'glm-5.2', intelligence: 85, provider: 'finna', requests: 28, successes: 27, failures: 1, rate_limits: 0, prompt_tokens: 18200, completion_tokens: 9400, total_tokens: 27600, avg_latency_ms: 902.4, p95_latency_ms: 2140, last_used_at: Date.now() / 1000 - 132 },
    { target_id: 'agnes-2.5-flash', label: 'Agnes 2.5 Flash', model: 'agnes-2.5-flash', intelligence: 0, provider: null, requests: 0, successes: 0, failures: 0, rate_limits: 0, prompt_tokens: 0, completion_tokens: 0, total_tokens: 0, avg_latency_ms: null, p95_latency_ms: null, last_used_at: null },
    { target_id: 'openrouter-deepseek-v31-free', label: 'DeepSeek V3.1 Free', model: 'deepseek/deepseek-chat-v3.1:free', intelligence: 58, provider: 'openrouter', requests: 17, successes: 14, failures: 3, rate_limits: 3, prompt_tokens: 8100, completion_tokens: 4300, total_tokens: 12400, avg_latency_ms: 1830.6, p95_latency_ms: 4210.2, last_used_at: Date.now() / 1000 - 900 },
    { target_id: 'stepfun-step-2-16k', label: 'StepFun Step 2 16K', model: 'step-2-16k', intelligence: 68, provider: 'stepfun', requests: 6, successes: 4, failures: 2, rate_limits: 1, prompt_tokens: 2400, completion_tokens: 1100, total_tokens: 3500, avg_latency_ms: 1210, p95_latency_ms: 3050, last_used_at: Date.now() / 1000 - 7200 },
  ];
  const MOCK_RECENT = [
    { id: 512, created_at: Date.now() / 1000 - 12, target_id: 'finna-glm-5.2', scenario: 'coding', persona: 'dev', project_id: 'damselfish', latency_ms: 812.3, success: 1, status: 200, error: null, prompt_tokens: 1200, completion_tokens: 640, total_tokens: 1840, stream: 1, first_token_ms: 310.5 },
    { id: 511, created_at: Date.now() / 1000 - 61, target_id: 'openrouter-deepseek-v31-free', scenario: 'reasoning', persona: null, project_id: 'demo', latency_ms: 2310.9, success: 1, status: 200, error: null, prompt_tokens: 900, completion_tokens: 480, total_tokens: 1380, stream: 1, first_token_ms: 890.2 },
    { id: 510, created_at: Date.now() / 1000 - 180, target_id: 'stepfun-step-2-16k', scenario: 'chat', persona: null, project_id: null, latency_ms: null, success: 0, status: 502, error: '上游连接超时：upstream deadline exceeded while waiting for first byte', prompt_tokens: 0, completion_tokens: 0, total_tokens: 0, stream: 0, first_token_ms: null },
    { id: 509, created_at: Date.now() / 1000 - 600, target_id: 'finna-glm-5.2', scenario: 'default', persona: null, project_id: 'demo', latency_ms: 654.1, success: 1, status: 200, error: null, prompt_tokens: 310, completion_tokens: 122, total_tokens: 432, stream: 0, first_token_ms: null },
  ];
  function buildMockDashboard(windowSeconds) {
    const span = windowSeconds > 0 ? windowSeconds : 7 * 86400;
    const series = mockSeries(36, span);
    const sum = series.reduce((a, b) => ({ req: a.req + b.requests, err: a.err + b.errors, pt: a.pt + b.prompt_tokens, ct: a.ct + b.completion_tokens }), { req: 0, err: 0, pt: 0, ct: 0 });
    const lats = series.map((b) => b.avg_latency_ms).filter((v) => v != null).sort((a, b) => a - b);
    return {
      window_seconds: windowSeconds, generated_at: Date.now() / 1000,
      summary: { requests: sum.req, successes: sum.req - sum.err, failures: sum.err, success_rate: sum.req ? ((sum.req - sum.err) / sum.req * 100).toFixed(1) : 0, prompt_tokens: sum.pt, completion_tokens: sum.ct, total_tokens: sum.pt + sum.ct, avg_latency_ms: lats.length ? Math.round(lats.reduce((a, b) => a + b, 0) / lats.length * 10) / 10 : null, p95_latency_ms: lats.length ? lats[Math.min(lats.length - 1, Math.floor(lats.length * 0.95))] : null, est_cost_usd: 0.4213, by_scenario: { coding: 34, default: 12, reasoning: 9 } },
      projects: [
        { project_id: 'damselfish', requests: 21, prompt_tokens: 9800, completion_tokens: 5200, total_tokens: 15000 },
        { project_id: 'demo', requests: 14, prompt_tokens: 5100, completion_tokens: 2600, total_tokens: 7700 },
        { project_id: 'agent-runner', requests: 9, prompt_tokens: 3300, completion_tokens: 1700, total_tokens: 5000 },
        { project_id: 'cli', requests: 4, prompt_tokens: 1200, completion_tokens: 600, total_tokens: 1800 },
      ],
      series, targets: JSON.parse(JSON.stringify(MOCK_TARGETS)), recent: JSON.parse(JSON.stringify(MOCK_RECENT)),
    };
  }
  function buildMockTargetDetail(id, windowSeconds) {
    const base = MOCK_TARGETS.find((t) => t.target_id === id) || MOCK_TARGETS[0];
    const span = windowSeconds > 0 ? windowSeconds : 7 * 86400;
    return { target: { target_id: base.target_id, label: base.label, model: base.model, provider: base.provider, intelligence: base.intelligence, priority: 100, enabled: true, available: true, capabilities: ['chat', 'tools'], base_url: 'https://www.finna.com.cn/v1' }, summary: { requests: base.requests, successes: base.successes, failures: base.failures, rate_limits: base.rate_limits, prompt_tokens: base.prompt_tokens, completion_tokens: base.completion_tokens, total_tokens: base.total_tokens, avg_latency_ms: base.avg_latency_ms, p95_latency_ms: base.p95_latency_ms }, series: mockSeries(24, span), recent: JSON.parse(JSON.stringify(MOCK_RECENT)), errors: [{ created_at: Date.now() / 1000 - 180, status: 502, error: '上游连接超时：upstream deadline exceeded while waiting for first byte (proxy_read_timeout=30s)' }] };
  }

  /* ---------------- 登录遮罩 ---------------- */
  let overlayDestroyed = false;
  function showLogin() {
    const ov = $('#loginOverlay');
    if (!ov || overlayDestroyed) return;
    ov.hidden = false;
    $('#loginKey').focus();
  }
  async function doLogin(event) {
    event.preventDefault();
    const key = $('#loginKey').value;
    const err = $('#loginError'), btn = $('#loginSubmit');
    err.textContent = ''; btn.disabled = true; btn.textContent = '登录中…';
    try {
      if (!MOCK) await api('login', { method: 'POST', body: JSON.stringify({ key }) });
      appState.authed = true;
      const ov = $('#loginOverlay');
      if (ov) ov.remove();          // 成功后销毁遮罩
      overlayDestroyed = true;
      AdminBus.emit('auth');
      renderRoute(true);            // 刷新当前页数据
    } catch (e) {
      err.textContent = e.status === 401 ? 'Key 不正确，请重试' : ('登录失败：' + e.message);
    } finally { btn.disabled = false; btn.textContent = '登录'; }
  }

  /* ---------------- 全局 api()/sse() 封装 ---------------- */
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  async function api(path, options = {}) {
    if (MOCK) {
      await sleep(150);
      if (/^api\/usage\/dashboard/.test(path)) return buildMockDashboard(appState.windowSeconds);
      if (/^api\/targets\/([^/?]+)/.test(path)) return buildMockTargetDetail(decodeURIComponent(path.match(/^api\/targets\/([^/?]+)/)[1]), appState.windowSeconds);
      if (/^api\/nodes/.test(path)) return [];
      if (/^(login|logout|health)$/.test(path)) return {};
      return {};
    }
    const response = await fetch(ADMIN_BASE + path, { credentials: 'same-origin', ...options, headers: { 'Content-Type': 'application/json', ...(options.headers || {}) } });
    if (response.status === 401) { showLogin(); throw Object.assign(new Error('未登录或会话过期'), { status: 401 }); }
    let body = null;
    try { body = await response.json(); } catch { /* 非 JSON 响应 */ }
    if (!response.ok) throw Object.assign(new Error((body && (body.detail || body.error && body.error.message)) || `HTTP ${response.status}`), { status: response.status });
    return body;
  }
  function sse(path, onEvent) {
    if (MOCK) {
      // 演示模式：1.2s 推一条 request（验证 flash），4s 推一帧 snapshot 后结束
      const timers = [];
      timers.push(setTimeout(() => { try { onEvent({ type: 'request', data: JSON.stringify({ ...MOCK_RECENT[0], id: 999, created_at: Date.now() / 1000, prompt_tokens: 88, completion_tokens: 45, total_tokens: 133 }) }); } catch {} }, 1200));
      timers.push(setTimeout(() => { try { onEvent({ type: 'snapshot', data: JSON.stringify(buildMockDashboard(appState.windowSeconds).summary) }); } catch {} }, 4000));
      return { closed: false, close() { timers.forEach(clearTimeout); this.closed = true; } };
    }
    const es = new EventSource(ADMIN_BASE + path);
    ['request', 'snapshot'].forEach((t) => es.addEventListener(t, (e) => { try { onEvent(e); } catch (err) { console.error('[sse]', err); } }));
    es.onopen = () => { appState.degraded = false; syncTopbar(); };
    es.onerror = () => { appState.degraded = true; syncTopbar(); };   // 自动降级：页面侧退化为轮询
    return es;
  }

  /* ================= 页面注册表 + hash 路由 ================= */
  const registry = {};
  let current = null;   // {name, module, params}
  const AdminPage = {
    register(name, module) {
      if (!name || typeof module !== 'object') return console.warn('[AdminPage] 非法注册', name);
      registry[name] = module;
      // 若壳已启动且该页正以占位形式展示，重挂载
      if (current && current.name === name && current.placeholder) { current.module = module; current.placeholder = false; renderRoute(true); }
    },
    registry,
  };
  AdminPage.register('memory', { title: '记忆库', mount(viewEl) { viewEl.innerHTML = '<div class="placeholder-page">数据库 / 记忆库模块即将上线</div>'; } });
  AdminPage.register('settings', { title: '设置', mount(viewEl) { viewEl.innerHTML = '<div class="placeholder-page">设置模块即将上线</div>'; } });

  function parseHash() {
    const h = location.hash.replace(/^#\/?/, '');
    const seg = h.split('/').filter(Boolean);
    if (!seg.length) return { name: 'dashboard', params: {} };
    if (seg[0] === 'targets' && seg[1]) return { name: 'targets', params: { id: decodeURIComponent(seg[1]) } };
    return { name: seg[0], params: {} };
  }
  function renderRoute(force = false) {
    const viewEl = $('#view');
    if (!viewEl) return;
    const { name, params } = parseHash();
    const sameRoute = current && current.name === name && JSON.stringify(current.params) === JSON.stringify(params);
    if (sameRoute && !force) return;
    if (current && current.module && current.module.unmount) { try { current.module.unmount(); } catch (e) { console.error(e); } }
    const module = registry[name];
    document.querySelectorAll('.nav a').forEach((a) => a.classList.toggle('active', a.dataset.nav === name));
    const title = module ? (module.title || name) : name;
    $('#pageTitle').textContent = title;
    document.title = `Damselfish 管理台 · ${title}`;
    current = { name, params, module, placeholder: !module };
    viewEl.innerHTML = '';
    if (!module) {
      viewEl.innerHTML = `<div class="placeholder-page">「${escapeHtml(name)}」模块尚未加载${name === 'nodes' || name === 'targets' ? '（页面文件缺失或尚未开发）' : ''}</div>`;
      return;
    }
    try { module.mount(viewEl, { store: appState, params, bus: AdminBus, refresh: () => renderRoute(true) }); }
    catch (e) { console.error(e); viewEl.innerHTML = `<div class="placeholder-page">模块渲染出错：${escapeHtml(e.message)}</div>`; }
  }

  /* ---------------- 顶栏同步与健康轮询 ---------------- */
  function syncTopbar() {
    const badge = $('#degradedBadge');
    if (badge) badge.hidden = !appState.degraded;
    const dot = $('#healthDot');
    if (dot) { dot.className = 'dot' + (appState.health === 'ok' ? ' ok' : appState.health ? ' bad' : ''); }
    const ws = $('#windowSelect');
    if (ws && Number(ws.value) !== appState.windowSeconds) ws.value = String(appState.windowSeconds);
    const at = $('#autoToggle');
    if (at && at.checked !== appState.autoRefresh) at.checked = appState.autoRefresh;
  }
  async function pollHealth() {
    if (MOCK) { appState.health = 'ok'; syncTopbar(); return; }
    try {
      const r = await fetch(HEALTH_URL, { credentials: 'same-origin' });
      const b = await r.json().catch(() => ({}));
      appState.health = r.ok && b.status === 'ok' ? 'ok' : 'bad';
    } catch { appState.health = 'bad'; }
    syncTopbar();
  }

  /* ---------------- 启动 ---------------- */
  document.addEventListener('alpine:init', () => { if (window.Alpine) window.Alpine.store('app', appState); });
  window.addEventListener('hashchange', () => renderRoute());
  document.addEventListener('DOMContentLoaded', () => {
    // 侧栏折叠记忆
    const sidebar = $('#sidebar'), cbtn = $('#collapseBtn');
    if (localStorage.getItem('admin.sidebar.collapsed') === '1') sidebar.classList.add('collapsed');
    cbtn.onclick = () => {
      const folded = sidebar.classList.toggle('collapsed');
      localStorage.setItem('admin.sidebar.collapsed', folded ? '1' : '0');
    };
    // 顶栏控件
    $('#windowSelect').onchange = (e) => { appState.windowSeconds = Number(e.target.value); AdminBus.emit('window', appState.windowSeconds); };
    $('#autoToggle').onchange = (e) => { appState.autoRefresh = e.target.checked; AdminBus.emit('autorefresh', appState.autoRefresh); };
    $('#loginForm').onsubmit = doLogin;
    // 健康灯：每 30 秒
    pollHealth(); setInterval(pollHealth, 30000);
    // 未登录先探测一次真实接口（mock 直接进入）
    if (MOCK) { appState.authed = true; const ov = $('#loginOverlay'); if (ov) ov.remove(); overlayDestroyed = true; }
    else {
      fetch(ADMIN_BASE + 'api/usage/dashboard?window=3600', { credentials: 'same-origin' })
        .then((r) => { if (r.ok) { appState.authed = true; const ov = $('#loginOverlay'); if (ov) ov.remove(); overlayDestroyed = true; } else showLogin(); })
        .catch(showLogin);
    }
    renderRoute(true);
    syncTopbar();
  });

  /* ---------------- 导出全局契约 ---------------- */
  window.AdminPage = AdminPage;
  window.AdminBus = AdminBus;
  window.api = api;
  window.sse = sse;
  window.APP_MOCK = MOCK;
  window.ADMIN_BASE = ADMIN_BASE;
})();
