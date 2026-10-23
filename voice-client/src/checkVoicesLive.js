import http from 'node:http';
import { config } from './config.js';
import { loadLanguageVoiceRules } from './languageVoiceRules.js';

const rules = await loadLanguageVoiceRules(config.languageVoiceRulesFile);
const checks = [...rules].map(([language, voice], index) => ({
  id: `VOICE-${String(index + 1).padStart(2, '0')}`,
  language,
  voice,
  voiceCheckOnly: true
}));
checks.push({ id: 'VOICE-RESTORE', language: '英语', voice: rules.get('英语') || 'chand', voiceCheckOnly: true, restore: true });

let connected = false;
let index = 0;
let current = null;
const results = [];

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
      ok: true, active: true, checkOnly: false, voiceCheck: true,
      total: checks.length, completed: results.filter((item) => item.ok).length,
      failed: results.filter((item) => !item.ok).length,
      pending: checks.length - index, current: current?.id ?? null,
      done: index >= checks.length
    });
  }

  if (req.method === 'POST' && url.pathname === '/api/bridge/next') {
    if (current) return sendJson(res, 200, { ok: true, task: current, resumed: true });
    if (index >= checks.length) return sendJson(res, 200, { ok: true, done: true });
    current = checks[index];
    return sendJson(res, 200, { ok: true, task: current, resumed: false });
  }

  if (req.method === 'POST' && url.pathname === '/api/bridge/result') {
    const body = await readJson(req);
    if (!current || String(body.taskId ?? '') !== current.id) {
      return sendJson(res, 409, { ok: false, error: '音色检查任务编号不一致' });
    }
    results.push({
      id: current.id,
      language: current.language,
      voice: current.voice,
      restore: Boolean(current.restore),
      ok: Boolean(body.ok),
      error: String(body.error ?? '')
    });
    index += 1;
    current = null;
    return sendJson(res, 200, { ok: true, done: index >= checks.length });
  }

  if (req.method === 'POST' && url.pathname === '/api/bridge/diagnostics') {
    return sendJson(res, 200, { ok: true });
  }
  return sendJson(res, 404, { ok: false });
});

server.listen(config.bridgePort, '127.0.0.1');
await new Promise((resolve, reject) => {
  server.once('listening', resolve);
  server.once('error', reject);
});

console.log(`VOICE_CHECK_READY ${checks.length}`);
const startedAt = Date.now();
while (index < checks.length && Date.now() - startedAt < 6 * 60_000) {
  await new Promise((resolve) => setTimeout(resolve, 500));
}
await new Promise((resolve) => server.close(resolve));

if (!connected) {
  console.error('VOICE_CHECK_FAILED: Fish Audio 扩展未连接');
  process.exitCode = 1;
} else if (index < checks.length) {
  console.error(`VOICE_CHECK_TIMEOUT: ${index}/${checks.length}`);
  process.exitCode = 1;
} else {
  for (const result of results) {
    const label = result.restore ? '恢复' : result.language;
    console.log(`${result.ok ? 'PASS' : 'FAIL'} ${label} -> ${result.voice}${result.error ? ` | ${result.error}` : ''}`);
  }
  if (results.some((item) => !item.ok)) process.exitCode = 1;
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
