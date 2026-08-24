'use strict';
/* ============================================================================
 * Damselfish Admin — 节点分组管理页 #/nodes  (fe-eng / t8)
 *
 * 契约（由 app.js 提供）：
 *   - 文件末尾 window.AdminPage.register('nodes', module)
 *   - ctx = { store, params, bus }；api(path) 相对 /admin/；401 由壳接管
 *   - bus 事件：'auth' 登录成功后刷新列表
 *
 * 数据源：
 *   GET api/nodes → { data:[ {...public_node, stats{...}} ] }
 *     public_node：id,label,base_url,model,enabled,available,local,free,priority,
 *       capabilities[],probe,max_concurrency,max_context,has_api_key,managed
 *     （provider-eng t6 后新增 provider/intelligence/api_key_count/api_key_hints，
 *        前端全部按可能缺失兜底，绝不回显完整 key）
 *   POST   api/nodes            新建（整体表单）
 *   PUT    api/nodes/{id}       编辑（api_key 列表=整体替换）
 *   DELETE api/nodes/{id}       删除（仅托管节点）
 *   POST   api/nodes/test       连通性测试（沿用旧页语义：保存前必须测试通过）
 *
 * 核心交互：按 base_url 分组的横向折叠列表。
 *   组头：健康聚合灯 · base_url · provider · N模型/M把Key · 请求数合计 · 展开箭头
 *   组内：模型行卡 flex-wrap（min-width≈320px），同组按 priority 升序。
 *   默认展开有流量的组；手动开合写入 localStorage 记忆（pg.nodes.groups.v1）。
 *
 * 自检：?mock=1 内置样例覆盖三种分组形态——
 *   ① 同 base 多模型异 key ② 同 base 单模型多 key(🔑×N 轮询)
 *   ③ 混合本地无 key（含停用灰灯 / 全异常红灯组）。
 * ========================================================================= */
(() => {
  const MOCK = new URLSearchParams(location.search).has('mock');
  const LS_KEY = 'pg.nodes.groups.v1';
  const CAPABILITIES = ['chat', 'chinese', 'multilingual', 'tools', 'coding', 'reasoning', 'creative', 'fast', 'vision'];

  /* ---------------- 工具 ---------------- */
  const esc = (v) => String(v ?? '').replace(/[&<>'"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[c]));
  const attr = (v) => esc(v).replace(/`/g, '&#96;').replace(/\n/g, ' ');
  const fmtInt = (v) => (Number.isFinite(Number(v)) ? Number(v).toLocaleString('zh-CN') : '-');
  const fmtMs = (v) => { if (v === null || v === undefined || v === '') return '-'; const n = Number(v); if (!Number.isFinite(n)) return '-'; if (n >= 1000) return (n / 1000).toFixed(2) + 's'; return Math.round(n) + 'ms'; };
  const intelBadge = (score) => {
    const n = Number(score);
    if (!Number.isFinite(n) || n <= 0) return '<span class="pg-muted" title="未配置智能分">智分 -</span>';
    const cls = n >= 85 ? 'cyan' : n >= 70 ? 'green' : n >= 50 ? 'warn' : 'gray';
    return `<span class="pg-badge pg-badge--${cls}" title="智能分 ${n}">智分 ${n}</span>`;
  };
  function readOverrides() {
    try {
      const raw = JSON.parse(localStorage.getItem(LS_KEY) || '{}');
      return raw && typeof raw === 'object' ? raw : {};
    } catch { return {}; }
  }
  function writeOverride(base, open) {
    try {
      const map = readOverrides();
      map[base] = open;
      localStorage.setItem(LS_KEY, JSON.stringify(map));
    } catch { /* 隐私模式等场景忽略 */ }
  }

  /* ---------------- 内置样例（?mock=1）：三种分组形态 ---------------- */
  const nowSec = Date.now() / 1000;
  const maskKey = (k) => (k.length >= 8 ? k.slice(0, 3) + '***' + k.slice(-4) : '***');
  const MOCK_NODES = [
    // 形态① 同 base 多模型、每把不同单 Key（Finna 平台）
    { id: 'finna-glm-5.2', label: 'Finna GLM-5.2', base_url: 'https://api.finna-platform.com/v1', model: 'glm-5.2', provider: 'finna', intelligence: 88, enabled: true, available: true, local: false, free: false, priority: 10, capabilities: ['chat', 'tools', 'chinese'], probe: true, max_concurrency: 8, max_context: 128000, has_api_key: true, managed: true, api_key_count: 1, api_key_hints: ['FIN***L0Rr'], stats: { requests: 412, failures: 5, rate_limits: 1, ewma_latency_ms: 918.2, circuit_open: false, last_success_at: nowSec - 40 } },
    { id: 'finna-kimi-k2', label: 'Finna Kimi K2', base_url: 'https://api.finna-platform.com/v1', model: 'kimi-k2-0905', provider: 'finna', intelligence: 81, enabled: true, available: true, local: false, free: false, priority: 20, capabilities: ['chat', 'creative', 'chinese'], probe: true, max_concurrency: 6, max_context: 256000, has_api_key: true, managed: true, api_key_count: 1, api_key_hints: ['FIN***9xQp'], stats: { requests: 187, failures: 2, rate_limits: 0, ewma_latency_ms: 1420.5, circuit_open: false, last_success_at: nowSec - 300 } },
    { id: 'finna-deepseek-v3', label: 'Finna DeepSeek V3', base_url: 'https://api.finna-platform.com/v1', model: 'deepseek-v3.1', provider: 'finna', intelligence: 86, enabled: true, available: true, local: false, free: false, priority: 30, capabilities: ['chat', 'coding', 'reasoning'], probe: true, max_concurrency: 6, max_context: 128000, has_api_key: true, managed: true, api_key_count: 1, api_key_hints: ['FIN***Tt3m'], stats: { requests: 96, failures: 14, rate_limits: 3, ewma_latency_ms: 2380.1, circuit_open: true, last_failure_at: nowSec - 120 } },
    // 形态② 同 base 单模型多 Key（StepFun 订阅 ×3 轮换）
    { id: 'stepfun-step-2-16k', label: 'StepFun Step 2 16K', base_url: 'https://api.stepfun.xyz/v1', model: 'step-2-16k', provider: 'stepfun', intelligence: 68, enabled: true, available: true, local: false, free: false, priority: 50, capabilities: ['chat', 'chinese'], probe: true, max_concurrency: 4, max_context: 16000, has_api_key: true, managed: true, api_key_count: 3, api_key_hints: ['STP***A1b2', 'STP***C3d4', 'STP***E5f6'], stats: { requests: 233, failures: 9, rate_limits: 6, ewma_latency_ms: 1210.7, circuit_open: false, last_success_at: nowSec - 15 } },
    // 形态③ 混合本地无 Key（Ollama）+ YAML 静态目标兜底字段缺失场景
    { id: 'ollama-qwen3-14b', label: '本地 Qwen3 14B', base_url: 'http://127.0.0.1:11434/v1', model: 'qwen3:14b', provider: null, intelligence: 42, enabled: true, available: true, local: true, free: true, priority: 90, capabilities: ['chat', 'chinese'], probe: false, max_concurrency: 2, max_context: 32000, has_api_key: false, managed: false, stats: { requests: 51, failures: 1, rate_limits: 0, ewma_latency_ms: 3450, circuit_open: false, last_success_at: nowSec - 600 } },
    { id: 'ollama-llama31-8b', label: '本地 Llama3.1 8B（停用）', base_url: 'http://127.0.0.1:11435/v1', model: 'llama3.1:8b', provider: null, intelligence: 0, enabled: false, available: false, local: true, free: true, priority: 95, capabilities: ['chat'], probe: false, max_concurrency: 2, max_context: null, has_api_key: false, managed: false, stats: { requests: 0, failures: 0, rate_limits: 0, ewma_latency_ms: null, circuit_open: false } },
    // 形态③补充：YAML 静态远端、无 env Key → 整组全异常红灯；且故意缺 api_key_count/hints 字段演练兜底
    { id: 'agnes-2.5-flash', label: 'Agnes 2.5 Flash', base_url: 'https://agnes.example.com/v1', model: 'agnes-2.5-flash', provider: 'agnes', intelligence: 55, enabled: true, available: false, local: false, free: true, priority: 100, capabilities: ['chat', 'fast'], probe: true, max_concurrency: 4, max_context: null, has_api_key: false, managed: false, stats: { requests: 0, failures: 0, rate_limits: 0, ewma_latency_ms: null, circuit_open: false } },
  ];

  /* ---------------- 状态 ---------------- */
  const state = {
    list: [], mockLoaded: false,
    overrides: readOverrides(),
    drawer: { open: false, mode: 'create', node: null, existingKeys: [], removedCount: 0, testing: false, saving: false },
  };

  /* ---------------- 数据获取与分组 ---------------- */
  async function fetchNodes() {
    if (MOCK) {
      await new Promise((r) => setTimeout(r, 120));
      if (!state.mockLoaded) { state.list = MOCK_NODES.map((n) => ({ ...n })); state.mockLoaded = true; }
      return { data: state.list.map((n) => ({ ...n })) };
    }
    const body = await api('api/nodes');
    // 兼容 {data:[...]} 与裸数组两种返回
    return Array.isArray(body) ? { data: body } : body;
  }

  function keyCountOf(n) {
    return Number.isFinite(Number(n.api_key_count)) ? Number(n.api_key_count) : (n.has_api_key ? 1 : 0);
  }
  function groupHealth(nodes) {
    if (!nodes.some((n) => n.enabled)) return 'off';                       // 全部未启用 → 灰
    const healthy = nodes.some((n) => n.enabled && n.available && !(n.stats && n.stats.circuit_open));
    return healthy ? 'ok' : 'bad';                                          // 有健康 → 绿，否则红
  }
  function buildGroups(list) {
    const map = new Map();
    for (const n of list) {
      const base = String(n.base_url || '(未知)').replace(/\/+$/, '');
      if (!map.has(base)) map.set(base, []);
      map.get(base).push(n);
    }
    const groups = [...map.entries()].map(([base, nodes]) => {
      const sorted = nodes.slice().sort((a, b) => Number(a.priority || 0) - Number(b.priority || 0));
      const reqSum = sorted.reduce((acc, n) => acc + Number(n.stats?.requests ?? 0), 0);
      const keyTotal = sorted.reduce((acc, n) => acc + keyCountOf(n), 0);
      return { base, nodes: sorted, health: groupHealth(sorted), reqSum, keyTotal };
    });
    groups.sort((a, b) => b.reqSum - a.reqSum || a.base.localeCompare(b.base));
    return groups;
  }
  function isOpen(group) {
    if (Object.prototype.hasOwnProperty.call(state.overrides, group.base)) return !!state.overrides[group.base];
    return group.reqSum > 0;                                                 // 默认：有流量展开
  }

  /* ---------------- 渲染：工具栏 + 分组 ---------------- */
  function keyInfoHtml(n) {
    const cnt = keyCountOf(n);
    const hints = Array.isArray(n.api_key_hints) ? n.api_key_hints.filter(Boolean) : [];
    if (cnt > 1) {
      const title = hints.length ? hints.join('\n') : `共 ${cnt} 把 Key 轮换`;
      return `<span class="pg-keychip" title="${attr(title)}">🔑×${cnt} 轮询</span>`;
    }
    if (cnt === 1) return `<span class="pg-keychip" title="已配置 1 把 Key">${esc(hints[0] || '已配置 Key')}</span>`;
    if (n.local) return '<span class="pg-badge pg-badge--gray" title="本地模型无需 API Key">本地</span>';
    return '<span class="pg-badge pg-badge--warn" title="尚未配置 API Key">未配 Key</span>';
  }
  function cardHtml(n) {
    const st = n.stats || {};
    const failRate = st.requests > 0 ? `${((Number(st.failures || 0) / Number(st.requests)) * 100).toFixed(1)}%` : '-';
    const dotCls = !n.enabled ? 'off' : n.available && !st.circuit_open ? 'ok' : 'bad';
    const dotText = !n.enabled ? '已停用' : st.circuit_open ? '熔断中' : n.available ? '可用' : '不可用';
    const srcBadge = n.managed
      ? '<span class="pg-badge pg-badge--cyan" title="托管节点（可在本页编辑/删除）">托管</span>'
      : '<span class="pg-badge pg-badge--gray" title="来自 config.yml 的静态目标">YAML 静态</span>';
    const caps = (n.capabilities || []).join(' · ');
    const circuitTag = st.circuit_open ? '<span class="pg-badge pg-badge--red">熔断中</span>' : '';
    const testBoxId = `pg-tr-${esc(n.id)}`;
    return `<article class="pg-node-card${n.enabled ? '' : ' is-disabled'}" data-node="${attr(n.id)}">
      <div class="pg-nc-top">
        <div style="flex:1;min-width:0">
          <div class="pg-nc-name">${esc(n.label)}</div>
          <code class="pg-nc-id">${esc(n.model)}</code>
        </div>
        <span class="pg-dot pg-dot--${dotCls}" title="${dotText}"></span>
      </div>
      <div class="pg-nc-badges">
        ${intelBadge(n.intelligence)}
        ${srcBadge}
        ${keyInfoHtml(n)}
        <span class="pg-chip" title="优先级（数字越小越优先）">优先级 ${fmtInt(n.priority)}</span>
        <span class="pg-chip" title="最大并发">并发 ${fmtInt(n.max_concurrency)}</span>
        ${n.max_context != null ? `<span class="pg-chip" title="上下文上限">ctx ${fmtInt(n.max_context)}</span>` : ''}
        ${circuitTag}
      </div>
      ${caps ? `<div class="pg-muted" style="font-size:11px">${esc(caps)}</div>` : ''}
      <div class="pg-nc-stats">
        <span>请求 <b>${fmtInt(st.requests)}</b></span>
        <span>失败率 <b>${failRate}</b></span>
        <span>EWMA <b>${fmtMs(st.ewma_latency_ms)}</b></span>
      </div>
      <div class="pg-nc-foot">
        <button type="button" class="pg-btn pg-btn--sm" data-act="edit">编辑</button>
        <button type="button" class="pg-btn pg-btn--sm" data-act="test">测试</button>
      </div>
      <div class="pg-test-result pg-hide" id="${testBoxId}"></div>
    </article>`;
  }
  function groupHtml(g) {
    const open = isOpen(g);
    const provider = g.nodes.find((n) => n.provider)?.provider;
    return `<section class="pg-group${open ? ' open' : ''}" data-base="${attr(g.base)}">
      <div class="pg-group-head" role="button" tabindex="0" aria-expanded="${open}" title="点击展开/折叠该组">
        <span class="pg-dot pg-dot--${g.health}" title="${g.health === 'ok' ? '组内有健康节点' : g.health === 'bad' ? '组内节点全部异常' : '组内节点未启用'}"></span>
        <span class="pg-g-url">${esc(g.base)}</span>
        ${provider ? `<span class="pg-chip">${esc(provider)}</span>` : ''}
        <span class="pg-g-sub">${g.nodes.length} 模型 · ${g.keyTotal} 把Key</span>
        <span class="pg-g-sub opt">请求数 ${fmtInt(g.reqSum)}</span>
        <button type="button" class="pg-btn pg-btn--sm pg-g-add" data-act="add-to-group" title="在此 Base URL 下新建模型节点">＋ 添加模型到该组</button>
        <span class="pg-g-arrow"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m6 9 6 6 6-6"/></svg></span>
      </div>
      <div class="pg-group-body"><div class="pg-cards">${g.nodes.map(cardHtml).join('')}</div></div>
    </section>`;
  }
  function renderList() {
    const wrap = document.getElementById('pg-groups');
    if (!wrap) return;
    const groups = buildGroups(state.list);
    wrap.innerHTML = groups.length
      ? groups.map(groupHtml).join('')
      : `<div class="pg-empty">还没有任何模型节点，点击右上角「新建节点」添加第一个上游。</div>`;
    const note = document.getElementById('pg-nodes-count');
    if (note) {
      const totalNodes = state.list.length;
      note.textContent = groups.length
        ? `共 ${groups.length} 组 · ${totalNodes} 个节点（按 base_url 聚合，同组按优先级升序）`
        : '';
    }
    bindListEvents(wrap);
  }

  /* ---------------- 卡片动作：编辑 / 测试 ---------------- */
  function findNode(id) { return state.list.find((n) => n.id === id); }

  function cardPayload(n) {
    return {
      id: n.id, label: n.label, base_url: n.base_url, model: n.model,
      api_key: '',                                   // 与旧页语义一致：留空=保留服务器已有 Key
      priority: n.priority, max_concurrency: n.max_concurrency,
      max_context: n.max_context ?? null, enabled: n.enabled,
      free: n.free !== false, probe: n.probe !== false,
      capabilities: n.capabilities || [],
    };
  }
  async function runCardTest(nodeId) {
    const card = document.querySelector(`.pg-node-card[data-node="${CSS.escape(nodeId)}"]`);
    const box = card && card.querySelector('.pg-test-result');
    const btn = card && card.querySelector('[data-act="test"]');
    if (!card || !box) return;
    const n = findNode(nodeId);
    if (!n) return;
    box.classList.remove('pg-hide', 'ok', 'bad');
    box.textContent = '正在测试连接…';
    btn.disabled = true;
    try {
      let res;
      if (MOCK) {
        await new Promise((r) => setTimeout(r, 450));
        res = nodeId.includes('agnes')
          ? { success: false, status: 401, latency_ms: 210.4, message: 'Missing environment variable AGNES_API_KEY' }
          : { success: true, status: 200, latency_ms: 812.3, message: '你好！OK' };
      } else {
        res = await api('api/nodes/test', { method: 'POST', body: JSON.stringify(cardPayload(n)) });
      }
      box.textContent = `${res.success ? '✓ 测试成功' : '✗ 测试失败'} · HTTP ${fmtInt(res.status)} · ${fmtMs(res.latency_ms)}\n${res.message || ''}`;
      box.classList.add(res.success ? 'ok' : 'bad');
    } catch (err) {
      if (!(err && err.status === 401)) {
        box.textContent = `✗ 测试请求失败：${err.message || err}`;
        box.classList.add('bad');
      } else { box.classList.add('pg-hide'); }
    } finally { btn.disabled = false; }
  }

  function toggleGroup(head) {
    const section = head.closest('.pg-group');
    const base = section.dataset.base;
    const willOpen = !section.classList.contains('open');
    section.classList.toggle('open', willOpen);
    head.setAttribute('aria-expanded', String(willOpen));
    writeOverride(base, willOpen);                       // 手动开合记忆
  }
  function bindListEvents(wrap) {
    wrap.querySelectorAll('.pg-group-head').forEach((head) => {
      head.addEventListener('click', (ev) => {
        if (ev.target.closest('.pg-g-add')) return;      // 添加按钮不触发展开切换
        toggleGroup(head);
      });
      head.addEventListener('keydown', (ev) => {
        if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); toggleGroup(head); }
      });
    });
    wrap.querySelectorAll('.pg-group-head [data-act="add-to-group"]').forEach((btn) => {
      btn.addEventListener('click', (ev) => {
        ev.stopPropagation();
        const base = btn.closest('.pg-group').dataset.base;
        openDrawer('create', { base_url: base });
      });
    });
    wrap.querySelectorAll('.pg-node-card [data-act="edit"]').forEach((btn) => {
      btn.addEventListener('click', () => openDrawer('edit', null, findNode(btn.closest('.pg-node-card').dataset.node)));
    });
    wrap.querySelectorAll('.pg-node-card [data-act="test"]').forEach((btn) => {
      btn.addEventListener('click', () => runCardTest(btn.closest('.pg-node-card').dataset.node));
    });
  }

  /* ---------------- 编辑抽屉 ---------------- */
  function drawerEl() { return document.getElementById('pg-drawer'); }
  function overlayEl() { return document.getElementById('pg-drawer-overlay'); }

  function setFormValue(sel, val) { const el = drawerEl().querySelector(sel); if (el) el.value = val; }
  function renderExistingKeyChips() {
    const d = state.drawer;
    const box = drawerEl().querySelector('#pg-exkeys');
    if (!box) return;
    box.innerHTML = d.existingKeys.map((k, i) => k.removed ? '' : `
      <span class="pg-exkey" data-i="${i}" title="${k.hint}（点击 × 移除这把 Key）">
        <span>🔑 ${esc(k.hint)}</span><button type="button" data-del="${i}" aria-label="移除该 Key">×</button>
      </span>`).join('') || '<span class="pg-keycount pg-muted">（该节点当前没有已保存的 Key）</span>';
    updateKeyMeta();
  }
  function updateKeyMeta() {
    const d = state.drawer;
    const ta = drawerEl().querySelector('#pg-newkeys');
    const lines = ta.value.split('\n').map((s) => s.trim()).filter(Boolean);
    const kept = d.existingKeys.filter((k) => !k.removed).length;
    const removed = d.existingKeys.filter((k) => k.removed).length;
    const meta = drawerEl().querySelector('#pg-keymeta');
    meta.textContent = d.mode === 'edit'
      ? `已有 ${d.existingKeys.length} 把 · 保留 ${kept} 把 · 移除 ${removed} 把 · 将新增 ${lines.length} 把`
      : `将新增 ${lines.length} 把`;
    const warn = drawerEl().querySelector('#pg-keywarn');
    warn.classList.toggle('pg-hide', !(d.mode === 'edit' && (removed > 0)));
    void lines; // 行数即新增数量
  }
  function buildApiKeyPayload() {
    const d = state.drawer;
    const newLines = (drawerEl().querySelector('#pg-newkeys').value.split('\n').map((s) => s.trim()).filter(Boolean));
    if (d.mode !== 'edit') return newLines.length ? { api_key: newLines } : {};
    const removed = d.existingKeys.filter((k) => k.removed).length;
    if (!removed && !newLines.length) return {};                            // 未改动：留空保留旧 Key（旧页语义）
    // 改动了 Key 列表：新键明文整体替换；同时携带保留掩码供后端做「掩码识别保留」（t6 若支持则无损保留）
    const keptHints = d.existingKeys.filter((k) => !k.removed).map((k) => k.hint);
    return { api_key: newLines, ...(keptHints.length ? { api_key_keep_masks: keptHints } : {}) };
  }
  function collectPayload() {
    const d = state.drawer;
    const q = (sel) => drawerEl().querySelector(sel);
    const mc = q('#f-maxcontext').value.trim();
    const payload = {
      id: q('#f-id').value.trim(),
      label: q('#f-label').value.trim(),
      base_url: q('#f-baseurl').value.trim(),
      model: q('#f-model').value.trim(),
      priority: Number(q('#f-priority').value || 100),
      max_concurrency: Number(q('#f-maxconc').value || 4),
      max_context: mc === '' ? null : Number(mc),
      intelligence: Number(q('#f-intel').value || 0),
      enabled: q('#f-enabled').checked,
      free: q('#f-free').checked,
      probe: q('#f-probe').checked,
      capabilities: [...drawerEl().querySelectorAll('#f-caps input:checked')].map((x) => x.value),
      scenarios: [], personas: [],
    };
    Object.assign(payload, buildApiKeyPayload());
    if (d.mode === 'edit') delete payload.id;                               // PUT 以路径 id 为准
    return payload;
  }
  function showDrawerResult(msg, cls) {
    const box = drawerEl().querySelector('#pg-dwresult');
    box.className = 'pg-dw-result' + (cls ? ' ' + cls : '');
    box.textContent = msg;
  }
  function openDrawer(mode, preset, node) {
    const d = state.drawer;
    d.open = true; d.mode = mode; d.node = node || null;
    d.existingKeys = []; d.testing = false; d.saving = false;
    const el = drawerEl();
    el.querySelector('#pg-dwtitle').textContent = mode === 'edit' ? `编辑节点 · ${node.label}` : '新建节点';
    el.querySelector('#pg-yaml-banner').classList.toggle('pg-hide', !(mode === 'edit' && !node.managed));
    el.querySelector('#f-idwrap').classList.toggle('pg-hide', mode === 'edit');
    setFormValue('#f-id', mode === 'create' ? '' : node.id);
    setFormValue('#f-label', mode === 'edit' ? node.label : '');
    setFormValue('#f-baseurl', mode === 'edit' ? node.base_url : (preset?.base_url || ''));
    setFormValue('#f-model', mode === 'edit' ? node.model : '');
    setFormValue('#f-intel', mode === 'edit' && Number.isFinite(Number(node.intelligence)) ? Number(node.intelligence) : 0);
    setFormValue('#f-priority', mode === 'edit' ? node.priority : 100);
    setFormValue('#f-maxconc', mode === 'edit' ? node.max_concurrency : 4);
    setFormValue('#f-maxcontext', mode === 'edit' && node.max_context != null ? node.max_context : '');
    el.querySelectorAll('#f-caps input').forEach((input) => {
      input.checked = mode === 'edit'
        ? (node.capabilities || []).includes(input.value)
        : ['chat', 'chinese'].includes(input.value);
    });
    el.querySelector('#f-enabled').checked = mode === 'edit' ? !!node.enabled : true;
    el.querySelector('#f-free').checked = mode === 'edit' ? node.free !== false : true;
    el.querySelector('#f-probe').checked = mode === 'edit' ? node.probe !== false : true;
    el.querySelector('#pg-newkeys').value = '';
    if (mode === 'edit') {
      const hints = Array.isArray(node.api_key_hints) ? node.api_key_hints.filter(Boolean) : [];
      if (hints.length) d.existingKeys = hints.map((hint) => ({ hint, removed: false }));
      else if (node.has_api_key) d.existingKeys = [{ hint: '已保存的 Key（不回显）', removed: false }];
    }
    renderExistingKeyChips();
    el.querySelector('#pg-dangerzone').classList.toggle('pg-hide', !(mode === 'edit'));
    const delBtn = el.querySelector('#pg-delete');
    if (mode === 'edit') {
      const deletable = !!node.managed;
      delBtn.disabled = !deletable;
      delBtn.title = deletable ? '删除该托管节点' : '来自 config.yml 的目标不可删除；可编辑保存为托管副本覆盖同名 id';
      el.querySelector('#pg-danger-note').textContent = deletable
        ? '删除后立即从路由移除，且不可恢复。'
        : 'YAML 静态目标不可删除；如需下线请在 config.yml 调整。';
    }
    showDrawerResult(mode === 'edit' ? '修改后先「测试连接」，通过才会保存。' : '填写完成后可先「获取模型列表」或直接「测试并保存」。');
    overlayEl().classList.add('show');
    el.classList.add('show');
    setTimeout(() => el.querySelector('#f-label').focus(), 220);
  }
  function closeDrawer() {
    const d = state.drawer;
    if (d.saving) return;                                                    // 保存中不可关闭
    d.open = false;
    drawerEl().classList.remove('show');
    overlayEl().classList.remove('show');
  }

  async function runDrawerTest(payload) {
    showDrawerResult('正在测试连接…');
    if (MOCK) {
      await new Promise((r) => setTimeout(r, 500));
      return payload.model.toLowerCase().includes('bad')
        ? { success: false, status: 404, latency_ms: 190.2, message: `model '${payload.model}' not found` }
        : { success: true, status: 200, latency_ms: 733.1, message: '仅回复 OK' };
    }
    return api('api/nodes/test', { method: 'POST', body: JSON.stringify(payload) });
  }
  async function saveFromDrawer() {
    const d = state.drawer;
    if (d.saving || d.testing) return;
    const payload = collectPayload();
    if (!payload.label) return showDrawerResult('✗ 显示名不能为空', 'bad');
    if (!payload.base_url) return showDrawerResult('✗ Base URL 不能为空', 'bad');
    if (!payload.model) return showDrawerResult('✗ 模型 ID 不能为空', 'bad');
    // 旧页语义：删除了已有 Key 且没有输入任何新键时，需要用户确认（服务器不回显完整 Key）
    const removedAll = d.mode === 'edit'
      && d.existingKeys.length > 0
      && d.existingKeys.every((k) => k.removed)
      && payload.api_key !== undefined && payload.api_key.length === 0
      && !payload.api_key_keep_masks;
    if (removedAll && !window.confirm('即将移除该节点全部已保存的 API Key，确定继续吗？')) return;

    d.saving = true;
    const saveBtn = drawerEl().querySelector('#pg-save');
    saveBtn.disabled = true;
    try {
      const test = await runDrawerTest({ ...payload, api_key: payload.api_key ?? '' });
      if (!test.success) throw new Error(`连接测试未通过（HTTP ${fmtInt(test.status)}）：${test.message || ''}`);
      showDrawerResult('测试通过，正在保存…');
      if (MOCK) { await new Promise((r) => setTimeout(r, 350)); mockUpsert(payload); }
      else if (d.mode === 'edit') await api(`api/nodes/${encodeURIComponent(d.node.id)}`, { method: 'PUT', body: JSON.stringify(payload) });
      else await api('api/nodes', { method: 'POST', body: JSON.stringify(payload) });
      showDrawerResult('✓ 已保存并立即生效。', 'ok');
      await refresh(false);
      closeDrawer();
    } catch (err) {
      if (!(err && err.status === 401)) showDrawerResult(`✗ ${err.message || '保存失败'}`, 'bad');
    } finally {
      d.saving = false;
      saveBtn.disabled = false;
    }
  }
  async function deleteFromDrawer() {
    const d = state.drawer;
    if (d.mode !== 'edit' || !d.node.managed) return;
    if (!window.confirm(`确定删除节点 ${d.node.id}？此操作不可恢复。`)) return;
    try {
      if (MOCK) { await new Promise((r) => setTimeout(r, 250)); state.list = state.list.filter((n) => n.id !== d.node.id); }
      else await api(`api/nodes/${encodeURIComponent(d.node.id)}`, { method: 'DELETE' });
      showDrawerResult('已删除。', 'ok');
      await refresh(false);
      closeDrawer();
    } catch (err) {
      if (!(err && err.status === 401)) showDrawerResult(`✗ 删除失败：${err.message}`, 'bad');
    }
  }
  /* mock 模式下的本地 upsert：让保存流程在演示中可见 */
  function mockUpsert(p) {
    const base = {
      enabled: true, local: false, free: p.free !== false, probe: p.probe !== false,
      has_api_key: Array.isArray(p.api_key) && p.api_key.length > 0,
      managed: true, stats: { requests: 0, failures: 0, rate_limits: 0, ewma_latency_ms: null, circuit_open: false },
    };
    const node = {
      ...base, ...p,
      api_key_count: Array.isArray(p.api_key) ? p.api_key.length : undefined,
      api_key_hints: Array.isArray(p.api_key) ? p.api_key.map(maskKey) : undefined,
    };
    const i = state.list.findIndex((n) => n.id === node.id);
    if (i >= 0) state.list[i] = node; else state.list.push(node);
  }
  async function discoverModels() {
    const payload = collectPayload();
    if (!payload.base_url || !payload.model) return showDrawerResult('✗ 请先填写 Base URL 与模型 ID', 'bad');
    showDrawerResult('正在获取模型列表…');
    try {
      let res;
      if (MOCK) { await new Promise((r) => setTimeout(r, 400)); res = { success: true, models: ['glm-5.2', 'glm-5-air', 'kimi-k2-0905'], latency_ms: 320.5, message: 'OK' }; }
      else res = await api('api/nodes/discover', { method: 'POST', body: JSON.stringify({ ...payload, api_key: payload.api_key ?? '' }) });
      if (!res.success) throw new Error(res.message || `HTTP ${fmtInt(res.status)}`);
      const dl = drawerEl().querySelector('#pg-modelist');
      dl.innerHTML = (res.models || []).map((m) => `<option value="${attr(m)}"></option>`).join('');
      showDrawerResult(`✓ 获取成功：${(res.models || []).length} 个模型，耗时 ${fmtMs(res.latency_ms)}（点击「模型 ID」输入框可选择）`, 'ok');
    } catch (err) {
      if (!(err && err.status === 401)) showDrawerResult(`✗ ${err.message || '获取模型列表失败'}`, 'bad');
    }
  }

  function buildDrawerMarkup() {
    const capChecks = CAPABILITIES.map((c) => `
      <label class="pg-check"><input type="checkbox" value="${c}">${c}</label>`).join('');
    return `
      <div class="pg-drawer-overlay" id="pg-drawer-overlay"></div>
      <aside class="pg-drawer" id="pg-drawer" role="dialog" aria-modal="true" aria-labelledby="pg-dwtitle">
        <div class="pg-dw-head">
          <h3 id="pg-dwtitle">新建节点</h3>
          <button type="button" class="pg-iconbtn" id="pg-dwclose" title="关闭" aria-label="关闭抽屉">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18M6 6l12 12"/></svg>
          </button>
        </div>
        <div class="pg-dw-body">
          <div class="pg-dw-banner pg-hide" id="pg-yaml-banner">来自 config.yml 的目标：此处修改将以托管副本覆盖同名 id。</div>
          <div class="pg-field pg-hide" id="f-idwrap"><span>节点 ID（唯一，字母/数字/._-）</span>
            <input type="text" id="f-id" placeholder="my-provider-model" autocomplete="off"></div>
          <div class="pg-field"><span>显示名</span><input type="text" id="f-label" placeholder="Finna GLM-5.2"></div>
          <div class="pg-field"><span>OpenAI 兼容 Base URL</span><input type="url" id="f-baseurl" placeholder="https://provider.example.com/v1"></div>
          <div class="pg-field"><span>模型 ID<input type="text" id="f-model" placeholder="glm-5.2" list="pg-modelist" style="margin-top:4px"></span>
            <datalist id="pg-modelist"></datalist></div>
          <div class="pg-dw-grid2">
            <label class="pg-field"><span>智能分 intelligence（0-100）</span><input type="number" id="f-intel" min="0" max="100" step="1" value="0"></label>
            <label class="pg-field"><span>优先级（越小越优先）</span><input type="number" id="f-priority" min="0" max="10000" value="100"></label>
            <label class="pg-field"><span>最大并发</span><input type="number" id="f-maxconc" min="1" max="100" value="4"></label>
            <label class="pg-field"><span>上下文上限 tokens（可空）</span><input type="number" id="f-maxcontext" min="0" placeholder="不限"></label>
          </div>
          <div class="pg-field"><span>能力标签</span><div class="pg-checks" id="f-caps">${capChecks}</div></div>
          <div class="pg-checks">
            <label class="pg-check"><input type="checkbox" id="f-enabled" checked>启用</label>
            <label class="pg-check"><input type="checkbox" id="f-free" checked>免费节点</label>
            <label class="pg-check"><input type="checkbox" id="f-probe" checked>后台探测</label>
          </div>
          <div class="pg-keys-section">
            <div class="pg-keys-title"><b>API Keys</b><span class="pg-keycount" id="pg-keymeta"></span></div>
            <div class="pg-existing-keys" id="pg-exkeys"></div>
            <textarea id="pg-newkeys" rows="3" placeholder="粘贴新的 API Key，一行一把&#10;留空表示保留服务器已有 Key" spellcheck="false"></textarea>
            <div class="pg-keywarn pg-hide" id="pg-keywarn">⚠ 服务器不回显完整 Key：一旦改动 Key 列表，未被移除的旧 Key 也需要在上方重新粘贴完整值，否则会随整体替换一并丢失。</div>
          </div>
          <div class="pg-dw-result" id="pg-dwresult"></div>
        </div>
        <div class="pg-dw-foot">
          <div style="display:flex;gap:8px;flex-wrap:wrap">
            <button type="button" class="pg-btn" id="pg-test">测试连接</button>
            <button type="button" class="pg-btn" id="pg-discover">获取模型列表</button>
            <span style="flex:1"></span>
            <button type="button" class="pg-btn pg-btn--primary" id="pg-save">测试并保存</button>
          </div>
          <div class="pg-danger-zone pg-hide" id="pg-dangerzone">
            <p id="pg-danger-note"></p>
            <button type="button" class="pg-btn pg-btn--danger" id="pg-delete">删除节点</button>
          </div>
        </div>
      </aside>`;
  }
  function bindDrawerEvents(busLike) {
    const el = drawerEl();
    el.querySelector('#pg-dwclose').addEventListener('click', closeDrawer);
    overlayEl().addEventListener('click', closeDrawer);
    document.addEventListener('keydown', onEscKey);
    el.querySelector('#pg-newkeys').addEventListener('input', updateKeyMeta);
    el.querySelector('#pg-exkeys').addEventListener('click', (ev) => {
      const btn = ev.target.closest('[data-del]');
      if (!btn) return;
      const i = Number(btn.dataset.del);
      if (state.drawer.existingKeys[i]) state.drawer.existingKeys[i].removed = true;
      renderExistingKeyChips();
    });
    el.querySelector('#pg-test').addEventListener('click', async () => {
      if (state.drawer.testing) return;
      state.drawer.testing = true;
      try { await runDrawerTest(collectPayload()); }
      finally { state.drawer.testing = false; }
    });
    el.querySelector('#pg-discover').addEventListener('click', discoverModels);
    el.querySelector('#pg-save').addEventListener('click', saveFromDrawer);
    el.querySelector('#pg-delete').addEventListener('click', deleteFromDrawer);
    void busLike;
  }
  function onEscKey(ev) { if (ev.key === 'Escape' && state.drawer.open) closeDrawer(); }

  /* ---------------- 加载 / 错误态 ---------------- */
  async function refresh(showLoading) {
    const root = document.getElementById('pg-nodes-page');
    if (!root) return;
    if (showLoading) root.querySelector('#pg-groups').innerHTML = '<div class="pg-loading">正在加载节点…</div>';
    try {
      const body = await fetchNodes();
      state.list = Array.isArray(body.data) ? body.data : [];
      renderList();
    } catch (err) {
      state.list = [];
      const box = root.querySelector('#pg-groups');
      if (err && err.status === 401) { box.innerHTML = ''; return; }        // 登录遮罩接管
      box.innerHTML = `<div class="pg-errorbox"><span>加载失败：${esc(err.message || '未知错误')}</span><button type="button" class="pg-btn" id="pg-retry">重试</button></div>`;
      box.querySelector('#pg-retry').addEventListener('click', () => refresh(true));
    }
  }

  /* ---------------- 模块注册 ---------------- */
  window.AdminPage.register('nodes', {
    title: '节点管理',
    mount(viewEl, ctx) {
      viewEl.innerHTML = `<div class="pg-root" id="pg-nodes-page">
        <div class="pg-toolbar">
          <span class="pg-count-note" id="pg-nodes-count"></span>
          <span class="spacer"></span>
          <button type="button" class="pg-btn" id="pg-refresh">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-2.64-6.36"/><path d="M21 3v6h-6"/></svg>刷新
          </button>
          <button type="button" class="pg-btn pg-btn--primary" id="pg-create">＋ 新建节点</button>
        </div>
        <div class="pg-groups" id="pg-groups"><div class="pg-loading">正在加载节点…</div></div>
        ${buildDrawerMarkup()}
      </div>`;
      bindDrawerEvents(ctx.bus);
      ctx.bus.on('auth', onAuth);
      state._bus = ctx.bus;
      const root = viewEl.querySelector('#pg-nodes-page');
      root.querySelector('#pg-refresh').addEventListener('click', () => refresh(true));
      root.querySelector('#pg-create').addEventListener('click', () => openDrawer('create'));
      refresh(true);
    },
    unmount() {
      if (state._bus) state._bus.off('auth', onAuth);
      state._bus = null;
      document.removeEventListener('keydown', onEscKey);
      state.drawer = { open: false, mode: 'create', node: null, existingKeys: [], removedCount: 0, testing: false, saving: false };
    },
  });
  function onAuth() { refresh(true); }
})();
