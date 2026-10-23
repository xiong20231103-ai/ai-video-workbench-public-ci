import http from 'node:http';
import { config } from './config.js';

let connected = false;
let diagnostics = null;

const server = http.createServer(async (req, res) => {
  setCors(res);
  if (req.method === 'OPTIONS') {
    res.writeHead(204);
    return res.end();
  }

  const url = new URL(req.url ?? '/', `http://${req.headers.host ?? '127.0.0.1'}`);
  if (req.method === 'GET' && url.pathname === '/api/bridge/status') {
    connected = true;
    return sendJson(res, 200, {
      ok: true,
      active: true,
      checkOnly: true,
      total: 0,
      completed: 0,
      failed: 0,
      pending: 0,
      current: null,
      done: true
    });
  }

  if (req.method === 'POST' && url.pathname === '/api/bridge/diagnostics') {
    diagnostics = await readJson(req);
    return sendJson(res, 200, { ok: true });
  }

  return sendJson(res, 404, { ok: false });
});

server.listen(config.bridgePort, '127.0.0.1');
await new Promise((resolve, reject) => {
  server.once('listening', resolve);
  server.once('error', reject);
});

const startedAt = Date.now();
while ((!connected || !diagnostics) && Date.now() - startedAt < 20_000) {
  await new Promise((resolve) => setTimeout(resolve, 200));
}

// A Chromium extension may keep its local HTTP connection alive.  Closing all
// sockets makes this check-only probe deterministic instead of hanging after a
// successful response.
server.closeAllConnections?.();
await new Promise((resolve) => server.close(resolve));

if (!connected || !diagnostics) {
  console.error('BRIDGE_CHECK_FAILED: Fish Audio 扩展未连接');
  process.exitCode = 1;
} else {
  console.log(`BRIDGE_CHECK_OK ${JSON.stringify(diagnostics ?? {})}`);
}

function setCors(res) {
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type');
  res.setHeader('Access-Control-Allow-Methods', 'GET, POST, OPTIONS');
  res.setHeader('Cache-Control', 'no-store');
}

function sendJson(res, status, payload) {
  res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8' });
  res.end(JSON.stringify(payload));
}

async function readJson(req) {
  const chunks = [];
  for await (const chunk of req) chunks.push(chunk);
  const raw = Buffer.concat(chunks).toString('utf8');
  return raw ? JSON.parse(raw) : {};
}
