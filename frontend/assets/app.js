// Replay Shape — app público (mobile-first, sem frameworks).
// Todo texto dinâmico passa por esc() antes de entrar no HTML (proteção XSS).
import { getApi, ApiError } from './api.js';

const $app = document.getElementById('app');
const BRL = new Intl.NumberFormat('pt-BR', { style: 'currency', currency: 'BRL' });
const TIME = new Intl.DateTimeFormat('pt-BR', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
const DATETIME = new Intl.DateTimeFormat('pt-BR', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });
let api;
let cfg = { demo_mode: false, simulated_payments: false };
let timers = [];
let clockSkew = 0; // servidor - cliente (s)

const esc = (s) => String(s ?? '').replace(/[&<>"'`]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;', '`': '&#96;' }[c]));
const money = (cents) => BRL.format((cents || 0) / 100);
const fmtTime = (ts) => (ts ? TIME.format(new Date(ts * 1000)) : '');
const fmtDateTime = (ts) => (ts ? DATETIME.format(new Date(ts * 1000)) : '');
const nowS = () => Date.now() / 1000 + clockSkew;

function clearTimers() { timers.forEach((t) => clearTimeout(t)); timers = []; }
function later(fn, ms) { const t = setTimeout(fn, ms); timers.push(t); return t; }

function toast(msg) {
  const el = document.createElement('div');
  el.className = 'toast';
  el.setAttribute('role', 'status');
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 2600);
}

const POCKETS = '<span class="pocket tl"></span><span class="pocket tr"></span><span class="pocket ml"></span><span class="pocket mr"></span><span class="pocket bl"></span><span class="pocket br"></span>';
function render(inner) {
  $app.innerHTML = `<div class="table"><div class="felt">${POCKETS}<div class="stack">${inner}</div></div></div>`;
  window.scrollTo({ top: 0 });
}

// --- tokens dos pedidos (somente neste aparelho) ----------------------------
const STORE_KEY = 'rs_orders_v1';
function loadTokens() { try { return JSON.parse(localStorage.getItem(STORE_KEY) || '{}'); } catch (e) { return {}; } }
function saveToken(orderId, token, shortCode) {
  const all = loadTokens();
  all[orderId] = { t: token, c: shortCode, at: Date.now() };
  for (const [k, v] of Object.entries(all)) if (Date.now() - (v.at || 0) > 3 * 86400e3) delete all[k];
  try { localStorage.setItem(STORE_KEY, JSON.stringify(all)); } catch (e) { /* modo privado */ }
}
function tokenFor(orderId) { return (loadTokens()[orderId] || {}).t; }

function go(path, replace = false) {
  if (replace) history.replaceState({}, '', path); else history.pushState({}, '', path);
  route();
}
window.addEventListener('popstate', route);
document.addEventListener('click', (e) => {
  const a = e.target.closest('a[data-nav]');
  if (a) { e.preventDefault(); go(a.getAttribute('href')); }
});

// --- telas ------------------------------------------------------------------
function errorScreen(title, msg, backHref = '/') {
  render(`
    <p class="eyebrow">Ops</p>
    <h1>${esc(title)}</h1>
    <p class="sub">${esc(msg)}</p>
    <div class="spacer"></div>
    <a class="btn btn-ghost" href="${esc(backHref)}" data-nav>Voltar</a>`);
}

async function homeScreen() {
  if (!cfg.demo_mode) {
    render(`
      <p class="eyebrow">Bem-vindo</p>
      <h1>Seu melhor momento.</h1>
      <p class="sub">Escaneie o QR Code fixado na sua quadra ou mesa para gerar o replay dos últimos segundos.</p>
      <div class="spacer"></div>
      <a class="btn btn-ghost" href="/recuperar" data-nav>Já tenho um código de pedido</a>`);
    return;
  }
  let spots = [];
  try { spots = (await api.demoSpots()).spots; } catch (e) { return errorScreen('Indisponível', e.message); }
  const items = spots.map((s, i) => `
    <a class="spot-item" href="/m/${esc(encodeURIComponent(s.code))}" data-nav>
      <span class="ball-num ${i % 3 === 1 ? 'b2' : i % 3 === 2 ? 'b3' : ''}">${i + 1}</span>
      <span class="grow"><strong>${esc(s.area_label)}</strong><br><span class="muted small">${esc(s.establishment)}</span></span>
      <span class="chip"><span class="dot ${s.camera_status === 'online' ? 'on' : s.camera_status === 'starting' ? 'warn' : 'off'}"></span>${s.camera_status === 'online' ? 'ao vivo' : s.camera_status === 'starting' ? 'iniciando' : 'offline'}</span>
    </a>`).join('');
  render(`
    <p class="eyebrow">Modo demonstração</p>
    <h1>Escolha a mesa</h1>
    <p class="sub">Na vida real, cada mesa tem um QR Code próprio. Aqui você escolhe uma câmera simulada.</p>
    <div class="spot-list">${items || '<p class="muted">Nenhuma câmera ativa.</p>'}</div>
    <div class="link-row"><a href="/recuperar" data-nav>Recuperar pedido</a></div>`);
  later(() => { if (location.pathname === '/') homeScreen(); }, 5000);
}

async function spotScreen(code) {
  let s;
  try { s = await api.spot(code); } catch (e) { return errorScreen('QR Code inválido', e.message); }
  const statusTxt = { online: 'Câmera ao vivo', starting: 'Câmera conectando…', offline: 'Câmera offline' }[s.camera_status];
  const dot = { online: 'on', starting: 'warn', offline: 'off' }[s.camera_status];
  const waiting = s.camera_status === 'online' && s.ready_in_s > 0;
  const disabled = s.camera_status !== 'online' || waiting;
  render(`
    <div><span class="chip"><span class="dot ${dot}"></span>${esc(statusTxt)}${s.simulated ? ' · simulada' : ''}</span></div>
    <div>
      <p class="eyebrow">${esc(s.establishment)}</p>
      <h1>Seu melhor momento.</h1>
      <p class="sub">${esc(s.area_label)} · ${esc(s.camera_name)}</p>
    </div>
    <div class="price-card">
      <span class="label">Replay de<br>${esc(s.duration_s)} segundos</span>
      <span class="price">${esc(money(s.price_cents))}</span>
    </div>
    <div>
      <button class="btn btn-primary" id="go" ${disabled ? 'disabled' : ''}>${waiting ? `Aquecendo… ${esc(s.ready_in_s)} s` : 'Gerar meu replay'}</button>
      <div class="cue" aria-hidden="true"></div>
    </div>
    <p class="muted small center">Recortamos os <strong>últimos ${esc(s.duration_s)} segundos</strong> a partir do momento do toque. Você só paga se o vídeo for gerado.</p>
    <div class="link-row">
      <button class="linklike" id="claim">Apertei o botão da mesa</button>
      <a href="/recuperar" data-nav>Tenho um código</a>
    </div>
    ${s.privacy_notice ? `<details class="notice"><summary>Aviso de gravação</summary><p>${esc(s.privacy_notice)}</p><a href="/privacidade" data-nav>Política de privacidade</a></details>` : ''}`);

  const btn = document.getElementById('go');
  btn.addEventListener('click', async () => {
    btn.disabled = true;
    btn.textContent = 'Solicitando…';
    try {
      const r = await api.trigger(code);
      saveToken(r.order_id, r.access_token, r.short_code);
      go(`/o/${r.order_id}`);
    } catch (e) {
      btn.disabled = false;
      btn.textContent = 'Gerar meu replay';
      toast(e.message);
    }
  });
  document.getElementById('claim').addEventListener('click', async () => {
    try {
      const r = await api.claim(code, null);
      saveToken(r.order_id, r.access_token, r.short_code);
      go(`/o/${r.order_id}`);
    } catch (e) { toast(e.message); }
  });
  if (disabled) later(() => { if (location.pathname === `/m/${code}`) spotScreen(code); }, waiting ? 1500 : 4000);
}

function processingView(o) {
  const st = o.replay.status;
  const order = ['queued', 'capturing', 'processing'];
  const idx = order.indexOf(st);
  const li = (i, txt) => `<li class="${idx > i ? 'done' : idx === i ? 'now' : ''}"><span class="mark">${idx > i ? '✓' : ''}</span>${txt}</li>`;
  render(`
    <p class="eyebrow">${esc(o.spot.establishment)} · ${esc(o.spot.area_label)}</p>
    <h1>Gerando seu replay</h1>
    <p class="sub">Acionado às <strong>${esc(fmtTime(o.replay.trigger_at))}</strong>. Vamos recortar os ${esc(o.replay.duration_s)} segundos anteriores.</p>
    <div class="rolling" aria-hidden="true"><span class="cue-ball"></span></div>
    <ul class="steps">
      ${li(0, 'Solicitação recebida')}
      ${li(1, 'Buscando o trecho na câmera')}
      ${li(2, 'Processando o vídeo')}
    </ul>
    <p class="muted small center">Pedido <span class="mono code-pill">${esc(o.short_code)}</span> — guarde este código.</p>`);
}

function paymentView(o, id, token) {
  const p = o.payment;
  const remaining = Math.max(0, Math.round((o.payment_deadline || 0) - nowS()));
  const mm = String(Math.floor(remaining / 60)).padStart(2, '0');
  const ss = String(remaining % 60).padStart(2, '0');
  const demo = o.simulated_payments;
  render(`
    <p class="eyebrow">${esc(o.spot.establishment)} · ${esc(o.spot.area_label)}</p>
    <h1>Seu replay está pronto!</h1>
    <p class="sub">Pague com Pix para liberar.</p>
    <span class="price big">${esc(money(o.amount_cents))}</span>
    ${p && p.qr_data_uri ? `
      <div class="qr-card">
        <img src="${esc(p.qr_data_uri)}" alt="QR Code Pix">
        ${demo ? '<span class="tag">QR ILUSTRATIVO — NÃO PAGUE</span>' : ''}
      </div>
      <div class="copy-box">
        <input id="pix" readonly value="${esc(p.pix_copy_paste)}" aria-label="Pix copia e cola">
      </div>
      <button class="btn btn-ghost" id="copy">Copiar código Pix</button>` : '<div class="loading short"><span class="cue-ball spin"></span>Gerando Pix…</div>'}
    <div class="status-line wait"><svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>Aguardando pagamento</div>
    <p class="muted center small">Liberação automática · expira em <strong id="countdown" class="mono">${mm}:${ss}</strong></p>
    ${demo ? `
      <div class="demo-box">
        <p><strong>Modo demonstração:</strong> nenhum dinheiro é cobrado. Simule o retorno do provedor de pagamento:</p>
        <div class="row">
          <button class="btn btn-demo-ok" id="demo-ok">Simular aprovado</button>
          <button class="btn btn-demo-no" id="demo-no">Simular recusa</button>
        </div>
      </div>` : ''}
    ${o.payment && o.payment.status === 'rejected' ? '<div class="notice err">Pagamento recusado. Você pode tentar novamente.</div>' : ''}
    <p class="muted small center">Pedido <span class="mono code-pill">${esc(o.short_code)}</span></p>`);
  const copy = document.getElementById('copy');
  if (copy) copy.addEventListener('click', async () => {
    const v = document.getElementById('pix').value;
    try { await navigator.clipboard.writeText(v); toast('Código Pix copiado'); }
    catch (e) { const i = document.getElementById('pix'); i.select(); document.execCommand && document.execCommand('copy'); toast('Código selecionado — copie manualmente'); }
  });
  const demoBtn = (elId, result) => {
    const b = document.getElementById(elId);
    if (!b) return;
    b.addEventListener('click', async () => {
      b.disabled = true;
      try { const r = await api.demoPay(id, token, result); showOrder(id, token, r); }
      catch (e) { toast(e.message); b.disabled = false; }
    });
  };
  demoBtn('demo-ok', 'approved');
  demoBtn('demo-no', 'rejected');
  const tick = () => {
    const el = document.getElementById('countdown');
    if (!el) return;
    const r = Math.max(0, Math.round((o.payment_deadline || 0) - nowS()));
    el.textContent = `${String(Math.floor(r / 60)).padStart(2, '0')}:${String(r % 60).padStart(2, '0')}`;
    later(tick, 1000);
  };
  later(tick, 1000);
}

async function readyView(o, id, token) {
  render(`
    <div class="ok-badge"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="m7.5 12.5 3 3 6-6"/></svg>Pagamento aprovado</div>
    <video class="player" id="player" controls playsinline preload="metadata"></video>
    <a class="btn btn-primary" id="dl" href="#" aria-disabled="true">Baixar vídeo</a>
    <button class="btn btn-ghost" id="share">Compartilhar</button>
    <p class="muted center">Disponível até <strong>${esc(fmtDateTime(o.available_until))}</strong></p>
    <p class="muted small center">${esc(o.spot.establishment)} · ${esc(o.spot.area_label)} · acionado às ${esc(fmtTime(o.replay.trigger_at))}<br>Pedido <span class="mono code-pill">${esc(o.short_code)}</span></p>`);
  const player = document.getElementById('player');
  const dl = document.getElementById('dl');
  let link = null;
  async function refreshLink() {
    try {
      link = await api.mediaLink(id, token);
      const t = player.currentTime || 0;
      const wasPlaying = !player.paused;
      player.src = link.url;
      if (t) { player.currentTime = t; if (wasPlaying) player.play().catch(() => {}); }
      dl.href = link.download_url;
      dl.setAttribute('download', `replay-shape-${o.short_code}.mp4`);
      dl.removeAttribute('aria-disabled');
      const ms = Math.max(30e3, (link.expires_at - nowS() - 60) * 1000);
      later(refreshLink, Math.min(ms, 2 ** 31 - 1));
    } catch (e) {
      if (e.code === 'EXPIRED' || e.status === 410) return showOrder(id, token);
      toast(e.message);
    }
  }
  await refreshLink();

  // Compartilhamento: arquivo via Web Share API (nível 2) quando suportado.
  // No Safari/iOS o share() precisa acontecer no toque — por isso preparamos o arquivo antes.
  const share = document.getElementById('share');
  let file = null;
  const canShareFiles = !!(navigator.canShare && window.File);
  share.addEventListener('click', async () => {
    if (!link) return;
    if (canShareFiles && !file) {
      share.disabled = true;
      share.textContent = 'Preparando vídeo…';
      try {
        const blob = await (await fetch(link.url)).blob();
        const f = new File([blob], `replay-shape-${o.short_code}.mp4`, { type: 'video/mp4' });
        if (navigator.canShare({ files: [f] })) {
          file = f;
          share.textContent = 'Compartilhar agora';
        } else {
          share.textContent = 'Compartilhar';
          fallbackShare();
        }
      } catch (e) { share.textContent = 'Compartilhar'; toast('Não foi possível preparar o vídeo.'); }
      share.disabled = false;
      return;
    }
    if (file) {
      try { await navigator.share({ files: [file], title: 'Meu replay', text: 'Replay Shape — Capture. Reviva. Compartilhe.' }); }
      catch (e) { if (e.name !== 'AbortError') fallbackShare(); }
      return;
    }
    fallbackShare();
  });
  function fallbackShare() {
    toast('Seu aparelho não compartilha arquivos daqui. Baixe o vídeo e envie pelo app de mensagens.');
  }
}

function finalView(o) {
  const map = {
    failed: ['Não foi possível gerar o replay', `${o.replay.error_message || ''} Nenhuma cobrança foi feita.`],
    expired: ['Prazo de pagamento encerrado', 'O replay não foi pago a tempo e será apagado automaticamente.'],
    closed: ['Replay expirado', 'O prazo de disponibilidade terminou e o arquivo foi apagado, como previsto.'],
    refunded: ['Pagamento estornado', 'O valor foi devolvido pelo provedor de pagamento.'],
    refund_pending: ['Estorno em andamento', 'Houve um problema com seu replay após o pagamento. O estorno foi solicitado automaticamente.'],
    paid_unfulfillable: ['Replay indisponível', 'O pagamento foi confirmado, mas o arquivo não estava mais disponível. O estorno é solicitado automaticamente.'],
    cancelled: ['Pedido cancelado', ''],
  };
  const [t, m] = map[o.status] || ['Pedido', o.status];
  render(`
    <p class="eyebrow">${esc(o.spot.establishment)} · ${esc(o.spot.area_label)}</p>
    <h1>${esc(t)}</h1>
    <p class="sub">${esc(m)}</p>
    <p class="muted small">Pedido <span class="mono code-pill">${esc(o.short_code)}</span></p>
    <div class="spacer"></div>
    <a class="btn btn-primary" href="/m/${esc(encodeURIComponent(o.spot.code))}" data-nav>Tentar de novo</a>`);
}

let payRequested = {};
let lastKey = '';
async function showOrder(id, token, preloaded) {
  clearTimers();
  let o = preloaded;
  if (!o) {
    try { o = await api.order(id, token); }
    catch (e) {
      if (e.code === 'NETWORK') { toast(e.message); later(() => showOrder(id, token), 3000); return; }
      return errorScreen('Pedido não encontrado', 'Abra o pedido pelo mesmo aparelho em que ele foi criado ou use o código do pedido.', '/recuperar');
    }
  }
  if (o.server_time) clockSkew = o.server_time - Date.now() / 1000;
  // Só redesenha quando o estado muda (evita piscar e perder o foco).
  const key = [id, o.status, o.replay.status, o.payment && o.payment.status, o.payment && o.payment.pix_copy_paste].join('|');
  const same = key === lastKey;
  lastKey = key;
  if (o.status === 'processing') {
    if (!same) processingView(o);
    later(() => showOrder(id, token), 1200);
  } else if (o.status === 'awaiting_payment') {
    if (!o.payment || !['pending'].includes(o.payment.status)) {
      if (!payRequested[id] || Date.now() - payRequested[id] > 8000) {
        payRequested[id] = Date.now();
        try { o = await api.pay(id, token); lastKey = ''; } catch (e) { toast(e.message); }
      }
    }
    if (!same || !lastKey) { paymentView(o, id, token); lastKey = key; }
    later(() => showOrder(id, token), 3000);
  } else if (o.status === 'paid') {
    await readyView(o, id, token);
  } else {
    finalView(o);
  }
}

function recoverScreen() {
  render(`
    <p class="eyebrow">Recuperar pedido</p>
    <h1>Tenho um código</h1>
    <p class="sub">Digite o código do pedido (ex.: K7P3-Q9XM).</p>
    <form id="f" class="stack">
      <div class="field"><label for="code">Código do pedido</label><input id="code" class="mono" autocomplete="off" autocapitalize="characters" maxlength="12" required></div>
      <button class="btn btn-primary" type="submit">Abrir pedido</button>
    </form>
    <a class="btn btn-ghost" href="/" data-nav>Voltar</a>`);
  document.getElementById('f').addEventListener('submit', async (e) => {
    e.preventDefault();
    try {
      const r = await api.recover(document.getElementById('code').value);
      saveToken(r.order_id, r.access_token, r.short_code);
      go(`/o/${r.order_id}`);
    } catch (err) { toast(err.message); }
  });
}

function privacyScreen() {
  render(`
    <p class="eyebrow">Privacidade e LGPD</p>
    <h1>Suas imagens</h1>
    <div class="notice">
      <p><strong>Finalidade:</strong> as câmeras gravam continuamente apenas para permitir que quem está jogando gere o replay dos últimos segundos.</p>
      <p><strong>Retenção:</strong> o buffer guarda cerca de 2 minutos e é sobrescrito automaticamente. Replays não pagos são apagados em até 30 minutos; replays pagos ficam disponíveis por 24 horas e depois são apagados.</p>
      <p><strong>Acesso:</strong> não existe galeria pública. Cada replay só pode ser aberto por quem tem o link/código do pedido.</p>
      <p class="small muted">Texto-modelo: a redação final, a base legal e o tratamento de menores devem ser validados pelo responsável jurídico do estabelecimento.</p>
    </div>
    <form id="pf" class="stack">
      <h2>Solicitar exclusão ou informação</h2>
      <div class="field"><label for="oc">Código do pedido (se tiver)</label><input id="oc" class="mono" maxlength="12"></div>
      <div class="field"><label for="ct">Contato (e-mail ou telefone)</label><input id="ct" maxlength="200"></div>
      <div class="field"><label for="msg">Mensagem</label><textarea id="msg" rows="4" required minlength="5" maxlength="2000"></textarea></div>
      <button class="btn btn-ghost" type="submit">Enviar solicitação</button>
    </form>`);
  document.getElementById('pf').addEventListener('submit', async (e) => {
    e.preventDefault();
    try {
      const r = await api.privacy({ order_code: document.getElementById('oc').value || null, contact: document.getElementById('ct').value || null, message: document.getElementById('msg').value });
      toast(r.message);
      e.target.reset();
    } catch (err) { toast(err.message); }
  });
}

function route() {
  clearTimers();
  lastKey = '';
  const path = location.pathname.replace(/\/+$/, '') || '/';
  let m;
  if ((m = path.match(/^\/m\/([A-Za-z0-9_-]{3,64})$/))) return spotScreen(decodeURIComponent(m[1]));
  if ((m = path.match(/^\/o\/([a-f0-9]{32})$/))) {
    const id = m[1];
    const frag = new URLSearchParams(location.hash.slice(1));
    if (frag.get('k')) { saveToken(id, frag.get('k'), ''); history.replaceState({}, '', `/o/${id}`); }
    const token = tokenFor(id);
    if (!token) return errorScreen('Pedido não encontrado neste aparelho', 'Use o código do pedido para recuperá-lo.', '/recuperar');
    return showOrder(id, token);
  }
  if (path === '/recuperar') return recoverScreen();
  if (path === '/privacidade') return privacyScreen();
  return homeScreen();
}

(async function init() {
  api = await getApi();
  try { cfg = await api.config(); } catch (e) { /* segue com padrão */ }
  const ribbon = document.getElementById('demo-ribbon');
  if (api.mode === 'static') {
    ribbon.textContent = 'DEMONSTRAÇÃO ESTÁTICA — sem servidor: câmera, recorte e pagamento são simulados no navegador.';
    ribbon.hidden = false;
  } else if (cfg.demo_mode) {
    ribbon.textContent = 'MODO DEMONSTRAÇÃO — câmera simulada e pagamento fictício. Nenhum valor é cobrado.';
    ribbon.hidden = false;
  } else if (cfg.simulated_payments) {
    ribbon.textContent = 'PILOTO — câmera real com pagamento de teste. Nenhum valor é cobrado.';
    ribbon.hidden = false;
  }
  route();
})();

export { ApiError };
