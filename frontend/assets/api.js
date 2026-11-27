// Cliente da API pública. No build estático (Cloudflare Pages) é substituído
// pelo simulador em mock-api.js — sem backend, sem recorte real.

export class ApiError extends Error {
  constructor(status, code, message) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

async function call(method, path, { token, body } = {}) {
  const headers = { Accept: 'application/json' };
  if (token) headers['X-Order-Token'] = token;
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  let res;
  try {
    res = await fetch(`/api/v1/public${path}`, {
      method,
      headers,
      body: body !== undefined ? JSON.stringify(body) : undefined,
      credentials: 'omit',
      cache: 'no-store',
    });
  } catch (e) {
    throw new ApiError(0, 'NETWORK', 'Sem conexão com o servidor. Verifique sua internet.');
  }
  let data = null;
  try { data = await res.json(); } catch (e) { /* corpo vazio */ }
  if (!res.ok) {
    const err = (data && data.error) || {};
    throw new ApiError(res.status, err.code || 'HTTP_' + res.status, err.message || 'Erro inesperado.');
  }
  return data;
}

export const realApi = {
  mode: 'server',
  config: () => call('GET', '/config'),
  demoSpots: () => call('GET', '/demo/spots'),
  spot: (code) => call('GET', `/spots/${encodeURIComponent(code)}`),
  trigger: (code) => call('POST', `/spots/${encodeURIComponent(code)}/replays`),
  claim: (code, shortCode) => call('POST', `/spots/${encodeURIComponent(code)}/claim`, { body: { code: shortCode || null } }),
  recover: (code) => call('POST', '/orders/recover', { body: { code } }),
  order: (id, token) => call('GET', `/orders/${encodeURIComponent(id)}`, { token }),
  pay: (id, token) => call('POST', `/orders/${encodeURIComponent(id)}/payment`, { token }),
  demoPay: (id, token, result) => call('POST', `/orders/${encodeURIComponent(id)}/demo-payment`, { token, body: { result } }),
  mediaLink: (id, token) => call('POST', `/orders/${encodeURIComponent(id)}/media-link`, { token }),
  privacy: (payload) => call('POST', '/privacy-requests', { body: payload }),
};

export async function getApi() {
  if (window.RS_STATIC_DEMO) {
    const m = await import('./mock-api.js');
    return m.mockApi;
  }
  return realApi;
}
