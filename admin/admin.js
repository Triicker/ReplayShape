// Painel administrativo Replay Shape (vanilla JS). Todo conteúdo dinâmico passa por esc().
const esc = (s) => String(s ?? '').replace(/[&<>"'`]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;', '`': '&#96;' }[c]));
const BRL = new Intl.NumberFormat('pt-BR', { style: 'currency', currency: 'BRL' });
const NUM = new Intl.NumberFormat('pt-BR', { maximumFractionDigits: 1 });
const money = (c) => BRL.format((c || 0) / 100);
const dt = (ts) => (ts ? new Date(ts * 1000).toLocaleString('pt-BR') : '—');
const bytes = (b) => (b > 1e9 ? NUM.format(b / 1e9) + ' GB' : NUM.format((b || 0) / 1e6) + ' MB');
const $ = (id) => document.getElementById(id);
let me = null;
let csrf = '';
let refreshTimer = null;

class HttpError extends Error { constructor(s, c, m) { super(m); this.status = s; this.code = c; } }
async function api(method, path, body) {
  const headers = { Accept: 'application/json' };
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  if (method !== 'GET') headers['X-CSRF-Token'] = csrf;
  const r = await fetch('/api/v1/admin' + path, { method, headers, credentials: 'same-origin', body: body !== undefined ? JSON.stringify(body) : undefined });
  let data = null;
  try { data = await r.json(); } catch (e) { /* vazio */ }
  if (!r.ok) {
    const e = (data && data.error) || {};
    if (r.status === 401 && path !== '/auth/login') showLogin();
    throw new HttpError(r.status, e.code, e.message || 'Erro ' + r.status);
  }
  return data;
}
function toast(m) { const t = document.createElement('div'); t.className = 'toast'; t.textContent = m; document.body.appendChild(t); setTimeout(() => t.remove(), 3500); }
function pill(status) {
  const ok = ['online', 'paid', 'approved', 'ready', 'closed'];
  const warn = ['starting', 'awaiting_payment', 'pending', 'processing', 'queued', 'capturing', 'refund_pending', 'unknown'];
  const cls = ok.includes(status) ? 'ok' : warn.includes(status) ? 'warn' : 'err';
  return `<span class="pill ${cls}">${esc(status || '—')}</span>`;
}
function modal(html) { $('modal-body').innerHTML = html; $('modal').hidden = false; }
function closeModal() { $('modal').hidden = true; $('modal-body').innerHTML = ''; }
$('modal').addEventListener('click', (e) => { if (e.target.id === 'modal' || e.target.dataset.close !== undefined) closeModal(); });

// --- auth -------------------------------------------------------------------
function showLogin() { $('shell').hidden = true; $('login').hidden = false; }
$('login-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  $('login-error').hidden = true;
  try {
    const r = await api('POST', '/auth/login', { email: $('email').value, password: $('password').value });
    csrf = r.csrf_token;
    await boot();
  } catch (err) { $('login-error').textContent = err.message; $('login-error').hidden = false; }
});
$('logout').addEventListener('click', async () => { try { await api('POST', '/auth/logout'); } catch (e) { /* */ } showLogin(); });

async function boot() {
  try { me = await api('GET', '/me'); } catch (e) { showLogin(); return; }
  csrf = me.csrf_token;
  $('login').hidden = true; $('shell').hidden = false;
  $('me-email').textContent = `${me.email} (${me.role})`;
  $('demo-banner').hidden = !me.demo_mode;
  show(location.hash.slice(1) || 'dashboard');
}
document.querySelectorAll('#nav a').forEach((a) => a.addEventListener('click', () => show(a.dataset.tab)));
window.addEventListener('hashchange', () => me && show(location.hash.slice(1) || 'dashboard', true));

const VIEWS = {};
function show(tab, fromHash) {
  if (!VIEWS[tab]) tab = 'dashboard';
  clearInterval(refreshTimer);
  if (!fromHash) history.replaceState({}, '', '#' + tab);
  document.querySelectorAll('#nav a').forEach((a) => a.classList.toggle('active', a.dataset.tab === tab));
  VIEWS[tab]().catch((e) => { $('view').innerHTML = `<p class="error">${esc(e.message)}</p>`; });
}

// --- filtros comuns ------------------------------------------------------------
let cache = { est: [], cams: [] };
async function loadRefs() {
  cache.est = await api('GET', '/establishments');
  cache.cams = (await api('GET', '/cameras')).cameras;
}
function filterBar(id) {
  const today = new Date(); today.setHours(0, 0, 0, 0);
  return `<div class="row" id="${id}">
    <label>De<input type="date" name="start"></label>
    <label>Até<input type="date" name="end"></label>
    <label>Estabelecimento<select name="establishment_id"><option value="">Todos</option>${cache.est.map((e) => `<option value="${esc(e.id)}">${esc(e.name)}</option>`).join('')}</select></label>
    <label>Câmera<select name="camera_id"><option value="">Todas</option>${cache.cams.map((c) => `<option value="${esc(c.id)}">${esc(c.area_label)} — ${esc(c.name)}</option>`).join('')}</select></label>
    <button class="btn" data-apply>Aplicar</button>
  </div>`;
}
function filterQuery(id) {
  const box = $(id); const q = new URLSearchParams();
  if (!box) return '';
  const s = box.querySelector('[name=start]').value; const e = box.querySelector('[name=end]').value;
  if (s) q.set('start', new Date(s + 'T00:00:00').getTime() / 1000);
  if (e) q.set('end', new Date(e + 'T00:00:00').getTime() / 1000 + 86400);
  for (const k of ['establishment_id', 'camera_id']) { const v = box.querySelector(`[name=${k}]`).value; if (v) q.set(k, v); }
  return q.toString();
}

// --- dashboard -------------------------------------------------------------------
VIEWS.dashboard = async () => {
  await loadRefs();
  $('view').innerHTML = `<h1>Dashboard executivo</h1><p class="sub">Indicadores operacionais e financeiros. ${me.simulated_payments ? '<span class="tag-h">Inclui pedidos de demonstração (sem dinheiro real).</span>' : ''}</p>
    ${filterBar('f-dash')}<div id="dash" class="mt"></div>`;
  const load = async () => {
    const d = await api('GET', '/dashboard?' + filterQuery('f-dash'));
    const k = (l, v, n, lime) => `<div class="kpi"><div class="l">${l}</div><div class="v ${lime ? 'lime' : ''}">${v}</div>${n ? `<div class="n">${n}</div>` : ''}</div>`;
    $('dash').innerHTML = `
      <div class="grid kpis">
        ${k('Replays solicitados', d.requested)}
        ${k('Replays gerados', d.generated, `${d.failed} falhas`)}
        ${k('Replays vendidos', d.sold, '', true)}
        ${k('Conversão', NUM.format(d.conversion * 100) + '%', 'vendidos / gerados')}
        ${k('Faturamento bruto', money(d.gross_cents), '', true)}
        ${k('Taxas de pagamento', money(d.fees_cents), d.fees_estimated ? 'estimadas pela % configurada' : 'informadas pelo provedor')}
        ${k('Receita após taxas', money(d.net_cents), '', true)}
        ${k('Ticket médio', money(d.avg_ticket_cents))}
        ${k('Tempo médio de processamento', NUM.format(d.avg_processing_ms / 1000) + ' s')}
        ${k('Câmeras online / offline', `${d.cameras_online} / ${d.cameras_offline}`)}
        ${k('Armazenamento', bytes(d.storage_bytes.replays + d.storage_bytes.buffers), `replays ${bytes(d.storage_bytes.replays)} · buffers ${bytes(d.storage_bytes.buffers)}`)}
        ${k('Disco', d.disk_low ? '<span class="pill err">POUCO ESPAÇO</span>' : '<span class="pill ok">OK</span>')}
      </div>
      <div class="grid cols">
        <div class="card"><h2>Vendas por horário</h2><div class="bars" id="bars"></div><div class="bars-x">${d.by_hour.map((_, h) => `<span>${h % 3 === 0 ? h : ''}</span>`).join('')}</div></div>
        <div class="card"><h2>Vendas por câmera</h2><table><tr><th>Câmera</th><th>Vendas</th><th>Bruto</th></tr>${d.by_camera.map((r) => `<tr><td>${esc(r.label)}<br><span class="hint">${esc(r.establishment)}</span></td><td>${r.sold}</td><td>${money(r.gross)}</td></tr>`).join('') || '<tr><td colspan=3 class="hint">Sem vendas no período.</td></tr>'}</table></div>
        <div class="card"><h2>Vendas por estabelecimento</h2><table><tr><th>Estabelecimento</th><th>Vendas</th><th>Bruto</th></tr>${d.by_establishment.map((r) => `<tr><td>${esc(r.label)}</td><td>${r.sold}</td><td>${money(r.gross)}</td></tr>`).join('') || '<tr><td colspan=3 class="hint">Sem vendas no período.</td></tr>'}</table></div>
        <div class="card"><h2>Falhas operacionais</h2><table><tr><th>Código</th><th>Qtde</th></tr>${d.failures.map((r) => `<tr><td><code>${esc(r.code)}</code></td><td>${r.n}</td></tr>`).join('') || '<tr><td colspan=2 class="hint">Nenhuma falha.</td></tr>'}</table></div>
      </div>`;
    const max = Math.max(1, ...d.by_hour);
    const bars = $('bars');
    d.by_hour.forEach((v, h) => { const b = document.createElement('div'); b.style.height = (v / max * 100) + '%'; b.title = `${h}h: ${v}`; bars.appendChild(b); });
  };
  $('f-dash').querySelector('[data-apply]').addEventListener('click', load);
  await load();
  refreshTimer = setInterval(() => load().catch(() => {}), 15000);
};

// --- pedidos ---------------------------------------------------------------------
VIEWS.orders = async () => {
  await loadRefs();
  $('view').innerHTML = `<h1>Pedidos e pagamentos</h1><p class="sub">Acompanhe pendentes, aprovados, falhas e estornos.</p>
    ${filterBar('f-ord')}
    <div class="row mt"><label>Status<select id="ord-status"><option value="">Todos</option>${['processing', 'awaiting_payment', 'paid', 'closed', 'expired', 'failed', 'refund_pending', 'refunded', 'paid_unfulfillable'].map((s) => `<option>${s}</option>`).join('')}</select></label>
    <button class="btn" id="csv">Exportar CSV</button></div>
    <div class="table-wrap mt"><table id="ord"></table></div>`;
  const load = async () => {
    const q = filterQuery('f-ord'); const st = $('ord-status').value;
    const d = await api('GET', `/orders?limit=200&${q}${st ? '&status=' + st : ''}`);
    $('ord').innerHTML = `<tr><th>Código</th><th>Criado</th><th>Local</th><th>Origem</th><th>Replay</th><th>Pedido</th><th>Pagamento</th><th>Valor</th><th></th></tr>` +
      d.orders.map((o) => `<tr><td><code>${esc(o.short_code)}</code>${o.demo ? ' <span class="pill">demo</span>' : ''}</td><td>${dt(o.created_at)}</td>
        <td>${esc(o.establishment)}<br><span class="hint">${esc(o.area_label)}</span></td><td>${esc(o.source)}</td>
        <td>${pill(o.replay_status)}${o.error_code ? `<br><code>${esc(o.error_code)}</code>` : ''}${o.processing_ms ? `<br><span class="hint">${NUM.format(o.processing_ms / 1000)} s</span>` : ''}</td>
        <td>${pill(o.status)}</td><td>${o.payment_status ? pill(o.payment_status) : '—'}${o.refund_status ? `<br><span class="hint">estorno: ${esc(o.refund_status)}</span>` : ''}</td>
        <td>${money(o.amount_cents)}</td>
        <td>${o.status === 'paid' && me.role === 'superadmin' ? `<button class="btn small danger" data-refund="${esc(o.id)}">Estornar</button>` : ''}</td></tr>`).join('') +
      (d.orders.length ? '' : '<tr><td colspan=9 class="hint">Nenhum pedido.</td></tr>');
    $('ord').querySelectorAll('[data-refund]').forEach((b) => b.addEventListener('click', async () => {
      if (!confirm('Estornar este pagamento? O acesso ao vídeo será revogado.')) return;
      try { await api('POST', `/orders/${b.dataset.refund}/refund`); toast('Estorno solicitado.'); load(); } catch (e) { toast(e.message); }
    }));
  };
  $('f-ord').querySelector('[data-apply]').addEventListener('click', load);
  $('ord-status').addEventListener('change', load);
  $('csv').addEventListener('click', () => { window.location.href = '/api/v1/admin/orders/export.csv?' + filterQuery('f-ord'); });
  await load();
  refreshTimer = setInterval(() => load().catch(() => {}), 10000);
};

// --- câmeras -----------------------------------------------------------------------
let camMeta = { adapters: {}, quality_profiles: {} };
VIEWS.cameras = async () => {
  await loadRefs();
  const d = await api('GET', '/cameras');
  camMeta = d;
  $('view').innerHTML = `<h1>Câmeras</h1><p class="sub">Cadastro, credenciais (criptografadas), teste de conectividade, buffer e QR Codes.</p>
    <div class="row"><button class="btn primary" id="new-cam">+ Nova câmera</button></div>
    <div class="table-wrap mt"><table id="cams"></table></div>`;
  const render = (cams) => {
    $('cams').innerHTML = `<tr><th>Local</th><th>Adaptador</th><th>Origem</th><th>Status</th><th>Buffer</th><th>Replay</th><th></th></tr>` + cams.map((c) => {
      const est = cache.est.find((e) => e.id === c.establishment_id);
      const b = c.buffer;
      return `<tr><td><strong>${esc(c.area_label)}</strong> — ${esc(c.name)}<br><span class="hint">${esc(est ? est.name : '')}</span></td>
      <td>${esc(c.adapter)}<br><span class="hint">${esc(c.manufacturer || '')} ${esc(c.model || '')}</span></td>
      <td><code>${esc(c.source_url || '')}</code></td>
      <td>${c.active ? pill(c.status) : '<span class="pill">inativa</span>'}${c.status_detail ? `<br><span class="hint">${esc(c.status_detail).slice(0, 120)}</span>` : ''}</td>
      <td>${b ? `${NUM.format(b.seconds_buffered)} s · ${b.segments} seg.<br><span class="hint">reinícios: ${b.restarts}${b.locked ? ' · em uso: ' + b.locked : ''}</span>` : '—'}</td>
      <td>${c.replay_seconds} s · ${c.price_cents != null ? money(c.price_cents) : '<span class="hint">preço do estab.</span>'}</td>
      <td class="actions"><button class="btn small" data-edit="${esc(c.id)}">Editar</button><button class="btn small" data-test="${esc(c.id)}">Testar</button>
      <button class="btn small" data-qr="${esc(c.id)}">QR</button>${c.active ? `<button class="btn small" data-trigger="${esc(c.id)}">Replay teste</button>` : ''}<button class="btn small danger" data-del="${esc(c.id)}">Remover</button></td></tr>`;
    }).join('');
    const byId = Object.fromEntries(cams.map((c) => [c.id, c]));
    $('cams').querySelectorAll('[data-edit]').forEach((b) => b.addEventListener('click', () => cameraForm(byId[b.dataset.edit])));
    $('cams').querySelectorAll('[data-test]').forEach((b) => b.addEventListener('click', async () => {
      b.disabled = true; b.textContent = 'Testando…';
      try { const r = await api('POST', `/cameras/${b.dataset.test}/test`); modal(`<h2>Teste de conectividade</h2><p>${r.ok ? '<span class="pill ok">OK</span>' : '<span class="pill err">FALHOU</span>'} ${r.latency_ms != null ? r.latency_ms + ' ms' : ''}</p><p><code>${esc(r.url || '')}</code></p><pre class="secret">${esc(r.detail || '')}</pre><button class="btn" data-close>Fechar</button>`); }
      catch (e) { toast(e.message); }
      b.disabled = false; b.textContent = 'Testar';
    }));
    $('cams').querySelectorAll('[data-qr]').forEach((b) => b.addEventListener('click', () => {
      const c = byId[b.dataset.qr];
      modal(`<h2>QR Code — ${esc(c.area_label)}</h2><p class="hint">Imprima e fixe na quadra/mesa. Aponta para:</p><p><code>${esc(c.public_url)}</code></p>
        <img class="qr" src="/api/v1/admin/cameras/${esc(c.id)}/qr.svg" alt="QR Code"><div class="row mt"><a class="btn" href="/api/v1/admin/cameras/${esc(c.id)}/qr.svg" download="qr-${esc(c.public_code)}.svg">Baixar SVG</a><a class="btn" href="${esc(c.public_url)}" target="_blank" rel="noopener">Abrir página</a><button class="btn" data-close>Fechar</button></div>
        <p class="hint mt">O endereço usa RS_PUBLIC_BASE_URL. Ajuste-o para o domínio público antes de imprimir.</p>`);
    }));
    $('cams').querySelectorAll('[data-trigger]').forEach((b) => b.addEventListener('click', async () => {
      try { const r = await api('POST', `/cameras/${b.dataset.trigger}/trigger`); modal(`<h2>Replay de teste solicitado</h2><p>Pedido <code>${esc(r.short_code)}</code>. Abra a página do pedido (link válido neste navegador):</p><p><a href="/o/${esc(r.order_id)}#k=${esc(encodeURIComponent(r.access_token))}" target="_blank" rel="noopener">Abrir pedido</a></p><button class="btn" data-close>Fechar</button>`); }
      catch (e) { toast(e.message); }
    }));
    $('cams').querySelectorAll('[data-del]').forEach((b) => b.addEventListener('click', async () => {
      if (!confirm('Remover esta câmera? (se houver replays, ela será apenas desativada)')) return;
      try { const r = await api('DELETE', `/cameras/${b.dataset.del}`); toast(r.message); VIEWS.cameras(); } catch (e) { toast(e.message); }
    }));
  };
  render(d.cameras);
  $('new-cam').addEventListener('click', () => cameraForm(null));
  refreshTimer = setInterval(async () => { try { render((await api('GET', '/cameras')).cameras); } catch (e) { /* */ } }, 5000);
};

function cameraForm(c) {
  const v = (k, d = '') => esc(c && c[k] != null ? c[k] : d);
  const opt = (obj, cur) => Object.entries(obj).map(([k, l]) => `<option value="${esc(k)}" ${k === cur ? 'selected' : ''}>${esc(l)}</option>`).join('');
  modal(`<h2>${c ? 'Editar câmera' : 'Nova câmera'}</h2>
  <form id="cam-form" class="form-grid">
    <label>Estabelecimento<select name="establishment_id">${cache.est.map((e) => `<option value="${esc(e.id)}" ${c && c.establishment_id === e.id ? 'selected' : ''}>${esc(e.name)}</option>`).join('')}</select></label>
    <label>Nome<input name="name" value="${v('name')}" required></label>
    <label>Quadra / mesa<input name="area_label" value="${v('area_label')}" required></label>
    <label>Adaptador<select name="adapter">${opt(camMeta.adapters, c ? c.adapter : 'intelbras')}</select></label>
    <label>Fabricante<input name="manufacturer" value="${v('manufacturer')}"></label>
    <label>Modelo<input name="model" value="${v('model')}"></label>
    <label>IP / hostname (rede local)<input name="host" value="${v('host')}" placeholder="192.168.1.108"></label>
    <label>Porta RTSP<input name="port" type="number" value="${v('port', 554)}"></label>
    <label>Usuário<input name="username" value="${v('username')}" autocomplete="off"></label>
    <label>Senha ${c && c.has_password ? '<span class="hint">(salva — deixe vazio p/ manter)</span>' : ''}<input name="password" type="password" autocomplete="new-password"></label>
    <label>Canal DVR/NVR<input name="channel" type="number" min="1" value="${v('channel', 1)}"></label>
    <label>Stream (subtype)<select name="subtype"><option value="0" ${c && c.subtype === 1 ? '' : 'selected'}>0 — principal</option><option value="1" ${c && c.subtype === 1 ? 'selected' : ''}>1 — extra</option></select></label>
    <label class="full">Caminho RTSP (opcional, substitui o padrão)<input name="rtsp_path" value="${v('rtsp_path')}" placeholder="/cam/realmonitor?channel=1&subtype=0"></label>
    <label class="full">URL RTSP completa (opcional) ${c && c.has_rtsp_url ? '<span class="hint">(salva — vazio mantém)</span>' : ''}<input name="rtsp_url" type="password" autocomplete="off" placeholder="rtsp://usuario:senha@192.168.1.108:554/..."></label>
    <label>Resolução<input name="resolution" value="${v('resolution')}" placeholder="1280x720"></label>
    <label>FPS<input name="fps" type="number" value="${v('fps')}"></label>
    <label>Duração do replay (s)<input name="replay_seconds" type="number" min="5" max="300" value="${v('replay_seconds', 60)}"></label>
    <label>Preço (R$) <span class="hint">vazio = preço do estabelecimento</span><input name="price" type="number" step="0.01" min="0" value="${c && c.price_cents != null ? (c.price_cents / 100).toFixed(2) : ''}"></label>
    <label>Qualidade<select name="quality_profile"><option value="">Padrão global</option>${opt(camMeta.quality_profiles, c ? c.quality_profile : '')}</select></label>
    <label class="check"><input type="checkbox" name="watermark" ${c && c.watermark ? 'checked' : ''}> Marca d'água</label>
    <label class="check"><input type="checkbox" name="supports_playback" ${c && c.supports_playback ? 'checked' : ''}> Playback histórico validado (Estratégia B)</label>
    <label class="check"><input type="checkbox" name="active" ${!c || c.active ? 'checked' : ''}> Ativa</label>
    <p class="hint full">Por segurança, somente IPs de rede local são aceitos. Credenciais são criptografadas e nunca retornam pela API.</p>
    <div class="row full"><button class="btn primary" type="submit">Salvar</button><button class="btn" type="button" data-close>Cancelar</button></div>
  </form>`);
  $('cam-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const f = new FormData(e.target); const b = {};
    for (const k of ['establishment_id', 'name', 'area_label', 'adapter', 'manufacturer', 'model', 'host', 'username', 'rtsp_path', 'resolution', 'quality_profile']) { const val = f.get(k); if (val !== null && val !== '') b[k] = val; }
    for (const k of ['port', 'channel', 'subtype', 'fps', 'replay_seconds']) { const val = f.get(k); if (val !== null && val !== '') b[k] = Number(val); }
    for (const k of ['password', 'rtsp_url']) { const val = f.get(k); if (val) b[k] = val; }
    const price = f.get('price');
    if (price) b.price_cents = Math.round(Number(price) * 100); else if (c) b.clear_price = true;
    b.watermark = !!f.get('watermark'); b.supports_playback = !!f.get('supports_playback'); b.active = !!f.get('active');
    try { await api(c ? 'PATCH' : 'POST', c ? `/cameras/${c.id}` : '/cameras', b); closeModal(); toast('Câmera salva.'); VIEWS.cameras(); }
    catch (err) { toast(err.message); }
  });
}

// --- estabelecimentos ------------------------------------------------------------------
VIEWS.establishments = async () => {
  const list = await api('GET', '/establishments');
  $('view').innerHTML = `<h1>Estabelecimentos</h1><p class="sub">Preços, identidade e aviso de privacidade exibido ao público.</p>
    ${me.role === 'superadmin' ? '<div class="row"><button class="btn primary" id="new-est">+ Novo estabelecimento</button></div>' : ''}
    <div class="table-wrap mt"><table>${'<tr><th>Nome</th><th>Tipo</th><th>Preço padrão</th><th>Status</th><th></th></tr>' + list.map((e) => `<tr><td><strong>${esc(e.name)}</strong><br><span class="hint">${esc(e.slug)}</span></td><td>${esc(e.venue_type)}</td><td>${money(e.price_cents)}</td><td>${e.active ? '<span class="pill ok">ativo</span>' : '<span class="pill">inativo</span>'}</td><td class="actions"><button class="btn small" data-edit="${esc(e.id)}">Editar</button>${me.role === 'superadmin' ? `<button class="btn small danger" data-del="${esc(e.id)}">Remover</button>` : ''}</td></tr>`).join('')}</table></div>`;
  const byId = Object.fromEntries(list.map((e) => [e.id, e]));
  const form = (e) => {
    modal(`<h2>${e ? 'Editar' : 'Novo'} estabelecimento</h2><form id="est-form" class="form-grid">
      <label>Nome<input name="name" value="${esc(e ? e.name : '')}" required></label>
      <label>Tipo<select name="venue_type">${['sinuca', 'beach tennis', 'futebol', 'tênis', 'padel', 'academia', 'arena', 'outro'].map((t) => `<option ${e && e.venue_type === t ? 'selected' : ''}>${t}</option>`).join('')}</select></label>
      <label>Preço padrão (R$)<input name="price" type="number" step="0.01" min="0" value="${e ? (e.price_cents / 100).toFixed(2) : '5.00'}"></label>
      <label>Cor da marca<input name="brand_color" type="color" value="${esc(e ? e.brand_color : '#a3e635')}"></label>
      <label class="check"><input type="checkbox" name="active" ${!e || e.active ? 'checked' : ''}> Ativo</label>
      <label class="full">Aviso de gravação / privacidade (exibido ao público)<textarea name="privacy_notice" rows="4">${esc(e ? e.privacy_notice || '' : '')}</textarea></label>
      <div class="row full"><button class="btn primary">Salvar</button><button class="btn" type="button" data-close>Cancelar</button></div></form>`);
    $('est-form').addEventListener('submit', async (ev) => {
      ev.preventDefault();
      const f = new FormData(ev.target);
      const b = { name: f.get('name'), venue_type: f.get('venue_type'), price_cents: Math.round(Number(f.get('price') || 0) * 100), brand_color: f.get('brand_color'), active: !!f.get('active'), privacy_notice: f.get('privacy_notice') || null };
      try { await api(e ? 'PATCH' : 'POST', e ? `/establishments/${e.id}` : '/establishments', b); closeModal(); toast('Salvo.'); VIEWS.establishments(); } catch (err) { toast(err.message); }
    });
  };
  if ($('new-est')) $('new-est').addEventListener('click', () => form(null));
  document.querySelectorAll('[data-edit]').forEach((b) => b.addEventListener('click', () => form(byId[b.dataset.edit])));
  document.querySelectorAll('[data-del]').forEach((b) => b.addEventListener('click', async () => {
    if (!confirm('Remover este estabelecimento?')) return;
    try { const r = await api('DELETE', `/establishments/${b.dataset.del}`); toast(r.message || 'Removido.'); VIEWS.establishments(); } catch (e) { toast(e.message); }
  }));
};

// --- botões físicos ---------------------------------------------------------------------
VIEWS.devices = async () => {
  await loadRefs();
  const list = await api('GET', '/devices');
  $('view').innerHTML = `<h1>Botões físicos (ESP32)</h1><p class="sub">Cada botão tem identificador e segredo próprios e fica associado a uma câmera. Firmware em <code>firmware/esp32</code>.</p>
    <div class="row"><label>Câmera<select id="dev-cam">${cache.cams.map((c) => `<option value="${esc(c.id)}">${esc(c.area_label)} — ${esc(c.name)}</option>`).join('')}</select></label><label>Nome<input id="dev-name" placeholder="Botão Mesa 01"></label><button class="btn primary" id="dev-add">Cadastrar botão</button></div>
    <div class="table-wrap mt"><table><tr><th>Nome</th><th>Câmera</th><th>Conectividade</th><th>Último acionamento</th><th>Firmware</th><th></th></tr>${list.map((d) => `<tr><td>${esc(d.name)}<br><code class="hint">${esc(d.id)}</code></td><td>${esc(d.area_label)}</td><td>${d.online ? '<span class="pill ok">online</span>' : '<span class="pill">sem sinal</span>'}<br><span class="hint">${dt(d.last_seen_at)}</span></td><td>${dt(d.last_trigger_at)}</td><td>${esc(d.firmware_version || '—')}</td><td class="actions"><button class="btn small" data-rot="${esc(d.id)}">Novo segredo</button><button class="btn small" data-tog="${esc(d.id)}" data-on="${d.active}">${d.active ? 'Desativar' : 'Ativar'}</button><button class="btn small danger" data-del="${esc(d.id)}">Remover</button></td></tr>`).join('') || '<tr><td colspan=6 class="hint">Nenhum botão cadastrado.</td></tr>'}</table></div>`;
  const showSecret = (r) => modal(`<h2>Credenciais do botão</h2><p>Grave no <code>config.h</code> do firmware. <strong>O segredo não será exibido novamente.</strong></p>
    <p>DEVICE_ID</p><div class="secret">${esc(r.id)}</div><p>DEVICE_SECRET</p><div class="secret">${esc(r.secret)}</div><p>API_BASE</p><div class="secret">${esc(r.api_base || location.origin)}</div><button class="btn mt" data-close>Fechar</button>`);
  $('dev-add').addEventListener('click', async () => {
    try { const r = await api('POST', '/devices', { camera_id: $('dev-cam').value, name: $('dev-name').value || 'Botão' }); await VIEWS.devices(); showSecret(r); } catch (e) { toast(e.message); }
  });
  document.querySelectorAll('[data-rot]').forEach((b) => b.addEventListener('click', async () => {
    if (!confirm('Gerar novo segredo? O botão para de funcionar até ser regravado.')) return;
    try { showSecret({ ...(await api('POST', `/devices/${b.dataset.rot}/rotate-secret`)), api_base: location.origin }); } catch (e) { toast(e.message); }
  }));
  document.querySelectorAll('[data-tog]').forEach((b) => b.addEventListener('click', async () => {
    try { await api('PATCH', `/devices/${b.dataset.tog}`, { active: b.dataset.on !== '1' }); VIEWS.devices(); } catch (e) { toast(e.message); }
  }));
  document.querySelectorAll('[data-del]').forEach((b) => b.addEventListener('click', async () => {
    if (!confirm('Remover este botão?')) return;
    try { await api('DELETE', `/devices/${b.dataset.del}`); VIEWS.devices(); } catch (e) { toast(e.message); }
  }));
};

// --- configurações ---------------------------------------------------------------------
VIEWS.settings = async () => {
  const { settings: s, quality_profiles: qp } = await api('GET', '/settings');
  const ro = me.role !== 'superadmin' ? 'disabled' : '';
  const n = (k, label, hint, step = 1) => `<label>${label}<input name="${k}" type="number" step="${step}" value="${esc(s[k])}" ${ro}>${hint ? `<span class="hint">${hint}</span>` : ''}</label>`;
  $('view').innerHTML = `<h1>Configurações</h1><p class="sub">Retenção, qualidade e preço. Alterações valem para os próximos replays.</p>
  <form id="set-form" class="card form-grid">
    <h2 class="full">Retenção (exclusão automática)</h2>
    ${n('buffer_seconds', 'Buffer circular (s)', 'janela mantida em disco por câmera')}
    ${n('unpaid_minutes', 'Prazo para pagamento (min)', 'replay não pago é apagado depois')}
    ${n('late_payment_grace_minutes', 'Tolerância p/ Pix tardio (min)', 'mantém o arquivo após expirar')}
    ${n('paid_hours', 'Disponibilidade após pagamento (h)')}
    ${n('link_minutes', 'Validade do link temporário (min)', 'renovado automaticamente na página')}
    <h2 class="full">Vídeo</h2>
    ${n('replay_seconds', 'Duração padrão (s)')}
    <label>Perfil de qualidade<select name="quality_profile" ${ro}>${Object.entries(qp).map(([k, l]) => `<option value="${k}" ${k === s.quality_profile ? 'selected' : ''}>${esc(l)}</option>`).join('')}</select></label>
    <label class="check"><input type="checkbox" name="fast_copy" ${s.fast_copy ? 'checked' : ''} ${ro}> Corte sem recodificar (rápido; precisão por keyframe)</label>
    <label class="check"><input type="checkbox" name="watermark_enabled" ${s.watermark_enabled ? 'checked' : ''} ${ro} ${me.font_available ? '' : 'disabled'}> Marca d'água ${me.font_available ? '' : '(fonte não encontrada)'}</label>
    <h2 class="full">Comercial e operação</h2>
    <label>Preço padrão (R$)<input name="default_price" type="number" step="0.01" value="${(s.default_price_cents / 100).toFixed(2)}" ${ro}></label>
    ${n('payment_fee_percent', 'Taxa de pagamento (%)', 'HIPOTÉTICA — confirme no seu contrato', 0.01)}
    ${n('web_trigger_cooldown_seconds', 'Intervalo mínimo entre replays (s)', 'por câmera, botão virtual')}
    ${n('device_claim_window_seconds', 'Janela p/ reivindicar botão físico (s)')}
    ${n('min_free_disk_mb', 'Espaço livre mínimo (MB)', 'abaixo disso, novos replays são bloqueados')}
    <div class="row full"><button class="btn primary" ${ro}>Salvar</button></div>
  </form>
  <div class="card mt"><h2>Integrações</h2><p>Pagamento: <code>${esc(me.payment_provider)}</code> ${me.simulated_payments ? '<span class="pill warn">simulado</span>' : ''} · Armazenamento: <code>${esc(me.storage_backend)}</code></p><p class="hint">Credenciais de integrações são definidas por variáveis de ambiente (.env) e nunca aparecem no painel.</p></div>
  <div class="card mt"><h2>Trocar minha senha</h2><form id="pw" class="row"><label>Atual<input type="password" name="current" required></label><label>Nova (mín. 10)<input type="password" name="new" minlength="10" required></label><button class="btn">Alterar</button></form></div>`;
  $('set-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const f = new FormData(e.target); const b = {};
    for (const k of ['buffer_seconds', 'unpaid_minutes', 'late_payment_grace_minutes', 'paid_hours', 'link_minutes', 'replay_seconds', 'web_trigger_cooldown_seconds', 'device_claim_window_seconds', 'min_free_disk_mb']) b[k] = Number(f.get(k));
    b.payment_fee_percent = Number(f.get('payment_fee_percent'));
    b.quality_profile = f.get('quality_profile');
    b.fast_copy = !!f.get('fast_copy'); b.watermark_enabled = !!f.get('watermark_enabled');
    b.default_price_cents = Math.round(Number(f.get('default_price')) * 100);
    try { await api('PATCH', '/settings', b); toast('Configurações salvas.'); } catch (err) { toast(err.message); }
  });
  $('pw').addEventListener('submit', async (e) => {
    e.preventDefault(); const f = new FormData(e.target);
    try { await api('POST', '/me/password', { current: f.get('current'), new: f.get('new') }); toast('Senha alterada.'); e.target.reset(); } catch (err) { toast(err.message); }
  });
};

// --- simulador financeiro ------------------------------------------------------------------
VIEWS.simulator = async () => {
  const fields = [
    ['establishments', 'Estabelecimentos', 3, 1, 'H'], ['cameras', 'Câmeras por estabelecimento', 4, 1, 'H'],
    ['requests', 'Solicitações/dia por câmera', 12, 1, 'H'], ['conversion', 'Conversão (%)', 40, 1, 'H'],
    ['price', 'Preço por replay (R$)', 5, 0.5, 'H'], ['size', 'Tamanho médio do vídeo (MB)', 11.5, 0.5, 'C'],
    ['fee', 'Taxa de pagamento Pix (%)', 0.99, 0.01, 'H'], ['share', 'Repasse ao estabelecimento (%)', 30, 1, 'H'],
    ['retention', 'Retenção do pago (h)', 24, 1, 'H'], ['storage_usd', 'Armazenamento (US$/GB-mês)', 0.015, 0.001, 'R'],
    ['classA_usd', 'Operações classe A (US$/milhão)', 4.5, 0.1, 'R'], ['usd', 'Câmbio (R$/US$)', 5.5, 0.05, 'H'],
    ['edge', 'Edge por estabelecimento (R$/mês)', 120, 10, 'H'], ['cloud', 'Servidor central (R$/mês)', 60, 5, 'H'],
  ];
  $('view').innerHTML = `<h1>Simulador financeiro</h1>
  <p class="sub">Valores <span class="pill warn">H</span> = hipotéticos (ajuste à realidade). <span class="pill ok">R</span> = referência pública da Cloudflare R2 (armazenamento US$ 0,015/GB-mês, classe A US$ 4,50/milhão, egress sem cobrança) — confira a página oficial de preços antes de decidir. <span class="pill">C</span> = calculado (720p, 1,5 Mbps, 60 s ≈ 11,25 MB).</p>
  <div class="grid cols"><form id="sim" class="card form-grid">${fields.map(([k, l, d, st, t]) => `<label>${l} <span class="pill ${t === 'R' ? 'ok' : t === 'H' ? 'warn' : ''}">${t}</span><input name="${k}" type="number" step="${st}" value="${d}"></label>`).join('')}</form>
  <div class="card" id="sim-out"></div></div>
  <div class="card mt"><h2>Comparativo de infraestrutura (custo por replay vendido)</h2><div id="sim-cmp"></div>
  <p class="hint">Estratégia adotada: processar e manter o buffer no Edge (rede local) e só enviar ao armazenamento remoto o que foi pago. Valores não são benchmarks medidos.</p></div>`;
  const calc = () => {
    const f = Object.fromEntries([...new FormData($('sim')).entries()].map(([k, v]) => [k, Number(v)]));
    const cams = f.establishments * f.cameras;
    const reqMonth = cams * f.requests * 30;
    const sold = reqMonth * f.conversion / 100;
    const gross = sold * f.price;
    const fees = gross * f.fee / 100;
    const net = gross - fees;
    const share = net * f.share / 100;
    const uploadGB = sold * f.size / 1024;
    const avgStoredGB = (sold / 30) * (f.retention / 24) * f.size / 1024;
    const storage = avgStoredGB * f.storage_usd * f.usd;
    const ops = (sold * 2 / 1e6) * f.classA_usd * f.usd; // PUT + DELETE
    const infra = f.establishments * f.edge + f.cloud;
    const costs = infra + storage + ops;
    const margin = net - share - costs;
    const unitContrib = f.price * (1 - f.fee / 100) * (1 - f.share / 100);
    const breakEven = unitContrib > 0 ? Math.ceil(costs / unitContrib) : Infinity;
    const R = (v) => BRL.format(v);
    $('sim-out').innerHTML = `<h2>Resultado mensal estimado</h2><table>
      <tr><td>Replays solicitados</td><td>${NUM.format(reqMonth)}</td></tr>
      <tr><td>Replays vendidos</td><td><strong>${NUM.format(sold)}</strong></td></tr>
      <tr><td>Faturamento bruto</td><td><strong>${R(gross)}</strong></td></tr>
      <tr><td>Taxas de pagamento</td><td>− ${R(fees)}</td></tr>
      <tr><td>Receita após taxas</td><td>${R(net)}</td></tr>
      <tr><td>Repasse aos estabelecimentos</td><td>− ${R(share)}</td></tr>
      <tr><td>Infraestrutura (edge + central)</td><td>− ${R(infra)}</td></tr>
      <tr><td>Armazenamento remoto + operações</td><td>− ${R(storage + ops)}</td></tr>
      <tr><td><strong>Margem estimada</strong></td><td><strong class="${margin >= 0 ? '' : 'error'}">${R(margin)}</strong> (${gross ? NUM.format(margin / gross * 100) : 0}%)</td></tr>
      <tr><td>Volume mensal de upload</td><td>${NUM.format(uploadGB)} GB</td></tr>
      <tr><td>Armazenamento médio ocupado</td><td>${NUM.format(avgStoredGB)} GB</td></tr>
      <tr><td>Custo operacional por replay vendido</td><td>${sold ? R(costs / sold) : '—'}</td></tr>
      <tr><td>Ponto de equilíbrio</td><td>${isFinite(breakEven) ? NUM.format(breakEven) + ' replays/mês' : '—'}</td></tr></table>`;
    const alt = [
      ['Edge local + R2 só para pagos (adotado)', infra + storage + ops],
      ['Edge local, sem nuvem de vídeo (download direto do edge via túnel)', infra],
      ['Tudo na nuvem (stream contínuo p/ servidor)', f.cloud * Math.max(1, cams / 2) + cams * 90],
    ];
    $('sim-cmp').innerHTML = `<table><tr><th>Alternativa</th><th>Custo mensal</th><th>Por replay vendido</th></tr>${alt.map(([n, c]) => `<tr><td>${esc(n)}</td><td>${R(c)}</td><td>${sold ? R(c / sold) : '—'}</td></tr>`).join('')}</table>
    <p class="hint">"Tudo na nuvem" usa estimativa hipotética de R$ 90/câmera/mês (uplink dedicado + ingestão contínua + servidor maior). O custo dominante dessa opção é o upload contínuo de vídeo, que o Edge evita.</p>`;
  };
  $('sim').addEventListener('input', calc);
  calc();
};

// --- auditoria ----------------------------------------------------------------------------
VIEWS.audit = async () => {
  if (me.role !== 'superadmin') { $('view').innerHTML = '<p>Restrito.</p>'; return; }
  const [rows, priv] = await Promise.all([api('GET', '/audit?limit=200'), api('GET', '/privacy-requests')]);
  $('view').innerHTML = `<h1>Auditoria</h1><p class="sub">Eventos do sistema (sem senhas, tokens ou URLs com credenciais).</p>
  <div class="card"><h2>Solicitações de privacidade (LGPD)</h2><table><tr><th>Data</th><th>Pedido</th><th>Contato</th><th>Mensagem</th></tr>${priv.map((p) => `<tr><td>${dt(p.created_at)}</td><td>${esc(p.order_code || '—')}</td><td>${esc(p.contact || '—')}</td><td>${esc(p.message)}</td></tr>`).join('') || '<tr><td colspan=4 class="hint">Nenhuma.</td></tr>'}</table></div>
  <div class="table-wrap mt"><table><tr><th>Quando</th><th>Ator</th><th>Ação</th><th>Entidade</th><th>Detalhes</th></tr>${rows.map((r) => `<tr><td>${dt(r.at)}</td><td>${esc(r.actor_type)}</td><td><code>${esc(r.action)}</code></td><td>${esc(r.entity || '')} <span class="hint">${esc((r.entity_id || '').slice(0, 12))}</span></td><td class="hint">${esc(r.details || '')}</td></tr>`).join('')}</table></div>`;
};

boot();
