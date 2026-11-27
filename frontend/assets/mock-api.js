// SIMULADOR para a versão estática (Cloudflare Pages) — NÃO há backend.
// Simula câmeras, processamento e pagamento no navegador. O vídeo exibido é um
// replay de exemplo gerado previamente pelo pipeline real (FFmpeg) do projeto.
// Dependem do backend (ver README): buffer RTSP, recorte real, Pix, webhooks,
// expiração/exclusão, painel administrativo e botões físicos.
import { ApiError } from './api.js';

const SPOTS = [
  { code: 'demo-mesa-01', area_label: 'Mesa 01', camera_name: 'Câmera 01' },
  { code: 'demo-mesa-02', area_label: 'Mesa 02', camera_name: 'Câmera 02' },
];
const EST = 'Shape Snooker Club (demo)';
const PRICE = 500;
const KEY = 'rs_static_orders_v1';
const now = () => Date.now() / 1000;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function load() { try { return JSON.parse(sessionStorage.getItem(KEY) || '{}'); } catch (e) { return {}; } }
function save(all) { try { sessionStorage.setItem(KEY, JSON.stringify(all)); } catch (e) { /* */ } }
function rid(n = 32) { const a = new Uint8Array(n / 2); crypto.getRandomValues(a); return [...a].map((b) => b.toString(16).padStart(2, '0')).join(''); }
function shortCode() {
  const al = 'ABCDEFGHJKMNPQRSTUVWXYZ23456789'; const a = new Uint8Array(8); crypto.getRandomValues(a);
  const s = [...a].map((b) => al[b % al.length]).join(''); return s.slice(0, 4) + '-' + s.slice(4);
}
function spotInfo(code) {
  const s = SPOTS.find((x) => x.code === code);
  if (!s) throw new ApiError(404, 'SPOT_NOT_FOUND', 'QR Code inválido ou câmera desativada.');
  return { ...s, establishment: EST, venue_type: 'sinuca', duration_s: 60, price_cents: PRICE, currency: 'BRL',
    camera_status: 'online', seconds_buffered: 120, ready_in_s: 0, simulated: true,
    privacy_notice: 'Demonstração estática: nenhuma câmera real é usada e nenhum dado é enviado a servidores.' };
}

function view(o) {
  const t = now();
  // Linha do tempo simulada do processamento (no backend real estes estados vêm do servidor).
  const age = t - o.created_at;
  let replayStatus = age < 1 ? 'queued' : age < 2.5 ? 'capturing' : age < 5 ? 'processing' : 'ready';
  let status = o.status;
  if (status === 'processing' && replayStatus === 'ready') {
    status = o.status = 'awaiting_payment';
    o.payment_deadline = t + 30 * 60;
  }
  if (status === 'awaiting_payment' && o.payment_deadline < t) status = o.status = 'expired';
  if (status === 'paid' && o.available_until < t) status = o.status = 'closed';
  return {
    id: o.id, short_code: o.short_code, status, amount_cents: PRICE, currency: 'BRL', created_at: o.created_at,
    payment_deadline: o.payment_deadline || null, available_until: o.available_until || null, demo: true, server_time: t,
    replay: { status: replayStatus, trigger_at: o.created_at, duration_s: 60, processing_ms: 4800, error_code: null, error_message: null },
    spot: { establishment: EST, area_label: o.area_label, code: o.code },
    payment: o.payment ? { ...o.payment } : null,
    simulated_payments: true,
  };
}

function get(id, token) {
  const all = load(); const o = all[id];
  if (!o || o.token !== token) throw new ApiError(404, 'ORDER_NOT_FOUND', 'Pedido não encontrado.');
  return { all, o };
}

export const mockApi = {
  mode: 'static',
  async config() { return { demo_mode: true, simulated_payments: true, env: 'static' }; },
  async demoSpots() { return { spots: SPOTS.map((s) => spotInfo(s.code)) }; },
  async spot(code) { return spotInfo(code); },
  async trigger(code) {
    const s = spotInfo(code); await sleep(300);
    const all = load(); const id = rid(); const token = rid(40);
    all[id] = { id, token, short_code: shortCode(), code, area_label: s.area_label, created_at: now(), status: 'processing' };
    save(all);
    return { order_id: id, access_token: token, short_code: all[id].short_code };
  },
  async claim() { throw new ApiError(404, 'NO_TRIGGER', 'Botão físico indisponível na demonstração estática.'); },
  async recover(code) {
    const all = load(); const o = Object.values(all).find((x) => x.short_code === String(code).toUpperCase().trim());
    if (!o) throw new ApiError(404, 'ORDER_NOT_FOUND', 'Código não encontrado (a demo estática guarda pedidos só nesta aba).');
    return { order_id: o.id, access_token: o.token, short_code: o.short_code };
  },
  async order(id, token) { const { all, o } = get(id, token); const v = view(o); save(all); return v; },
  async pay(id, token) {
    const { all, o } = get(id, token); view(o);
    if (o.status !== 'awaiting_payment') throw new ApiError(409, 'NOT_PAYABLE', 'Pedido não pode ser pago.');
    if (!o.payment || o.payment.status !== 'pending') {
      o.payment = { status: 'pending', expires_at: o.payment_deadline, pix_copy_paste: 'DEMO-REPLAYSHAPE-PIX-NAO-PAGAVEL-' + o.short_code, qr_data_uri: '/assets/demo-qr.svg' };
    }
    save(all); return view(o);
  },
  async demoPay(id, token, result) {
    const { all, o } = get(id, token); await sleep(500);
    if (!o.payment) throw new ApiError(409, 'NO_PENDING_PAYMENT', 'Gere o Pix antes.');
    if (result === 'approved') { o.status = 'paid'; o.payment.status = 'approved'; o.available_until = now() + 24 * 3600; }
    else o.payment.status = 'rejected';
    save(all); return view(o);
  },
  async mediaLink(id, token) {
    const { o } = get(id, token);
    if (o.status !== 'paid') throw new ApiError(402, 'NOT_PAID', 'O replay só é liberado após o pagamento.');
    return { url: '/assets/sample-replay.mp4', download_url: '/assets/sample-replay.mp4', expires_at: now() + 900 };
  },
  async privacy() { return { message: 'Demonstração estática: nada foi enviado.' }; },
};
