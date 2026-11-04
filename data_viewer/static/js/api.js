// Thin wrappers around fetch() that surface the server's {"error": "..."} messages.

export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

async function handle(res) {
  const type = res.headers.get('content-type') || '';
  let body = null;
  if (type.includes('application/json')) {
    body = await res.json().catch(() => null);
  } else {
    body = await res.text().catch(() => '');
  }
  if (!res.ok) {
    const msg = (body && typeof body === 'object' && body.error) || (typeof body === 'string' && body) || `HTTP ${res.status}`;
    throw new ApiError(String(msg), res.status);
  }
  return body;
}

export async function get(path, params, opts = {}) {
  const url = new URL(path, location.origin);
  if (params) {
    for (const [k, v] of Object.entries(params)) {
      if (v !== undefined && v !== null && v !== '') url.searchParams.set(k, v);
    }
  }
  const res = await fetch(url, { headers: { accept: 'application/json' }, signal: opts.signal });
  return handle(res);
}

export async function post(path, body, opts = {}) {
  const res = await fetch(path, {
    method: 'POST',
    headers: { 'content-type': 'application/json', accept: 'application/json' },
    body: JSON.stringify(body || {}),
    signal: opts.signal,
  });
  return handle(res);
}

export async function getText(url, opts = {}) {
  const res = await fetch(url, { signal: opts.signal });
  if (!res.ok) throw new ApiError(`HTTP ${res.status}`, res.status);
  return res.text();
}
