import http from 'node:http';
import { config } from './config.js';
import { loadLanguageVoiceRules } from './languageVoiceRules.js';
import { verifyAndDeleteTestDownload } from './testDownloadCleanup.js';

const sampleTexts = new Map([
  ['英语', 'This is an English voice test.'],
  ['德语', 'Dies ist ein deutscher Sprachtest.'],
  ['西班牙语', 'Esta es una prueba de voz en español.'],
  ['法语', 'Ceci est un test de voix en français.'],
  ['印尼语', 'Ini adalah tes suara bahasa Indonesia.'],
  ['意大利语', 'Questa è una prova vocale in italiano.'],
  ['葡萄牙语', 'Este é um teste de voz em português.'],
  ['土耳其语', 'Bu bir Türkçe ses testidir.'],
  ['俄语', 'Это проверка русского голоса.'],
  ['韩语', '이것은 한국어 음성 테스트입니다.']
]);

const rules = await loadLanguageVoiceRules(config.languageVoiceRulesFile);
const languageArgIndex = process.argv.indexOf('--language');
const selectedLanguage = languageArgIndex >= 0 ? String(process.argv[languageArgIndex + 1] || '').trim() : '';
const skipLanguageArgIndex = process.argv.indexOf('--skip-language');
const skippedLanguage = skipLanguageArgIndex >= 0 ? String(process.argv[skipLanguageArgIndex + 1] || '').trim() : '';
const selectedRules = selectedLanguage
  ? [...rules].filter(([language]) => language === selectedLanguage)
  : [...rules].filter(([language]) => language !== skippedLanguage);
if (selectedLanguage && selectedRules.length === 0) {
  throw new Error(`找不到语言音色规则：${selectedLanguage}`);
}
if (skippedLanguage && !rules.has(skippedLanguage)) {
  throw new Error(`找不到要跳过的语言音色规则：${skippedLanguage}`);
}

const tasks = selectedRules.map(([language, voice], index) => ({
  id: `LIVE-VOICE-${String(index + 1).padStart(2, '0')}`,
  project: '音色实测',
  language,
  voice,
  text: sampleTexts.get(language),
  outputName: `FishAudio_voice_test_${String(index + 1).padStart(2, '0')}`
}));

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
      ok: true,
      active: true,
      checkOnly: false,
      total: tasks.length,
      completed: results.filter((item) => item.ok).length,
      failed: results.filter((item) => !item.ok).length,
      pending: tasks.length - index,
      current: current?.id ?? null,
      done: index >= tasks.length
    });
  }

  if (req.method === 'POST' && url.pathname === '/api/bridge/next') {
    if (current) return sendJson(res, 200, { ok: true, task: current, resumed: true });
    if (index >= tasks.length) return sendJson(res, 200, { ok: true, done: true });
    current = tasks[index];
    current.startedAt = Date.now();
    console.log(`START ${current.language} -> ${current.voice}`);
    return sendJson(res, 200, { ok: true, task: current, resumed: false });
  }

  if (req.method === 'POST' && url.pathname === '/api/bridge/result') {
    const body = await readJson(req);
    if (!current || String(body.taskId ?? '') !== current.id) {
      return sendJson(res, 409, { ok: false, error: '音色实测任务编号不一致' });
    }

    let ok = Boolean(body.ok);
    let error = String(body.error ?? '');
    let bytes = 0;
    let deleted = false;
    const download = String(body.download ?? '');
    if (ok) {
      try {
        ({ bytes, deleted } = await verifyAndDeleteTestDownload(
          download,
          current.outputName,
          current.startedAt,
          current.text
        ));
      } catch (verificationError) {
        ok = false;
        error = verificationError instanceof Error ? verificationError.message : String(verificationError);
      }
    }

    results.push({
      id: current.id,
      language: current.language,
      voice: current.voice,
      ok,
      error,
      bytes,
      deleted
    });
    console.log(`${ok ? 'PASS' : 'FAIL'} ${current.language} -> ${current.voice}${ok ? ` | ${bytes} bytes | deleted` : ` | ${error}`}`);
    index += 1;
    current = null;
    return sendJson(res, 200, { ok: true, done: index >= tasks.length });
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

console.log(`LIVE_VOICE_TEST_READY ${tasks.length}`);
const startedAt = Date.now();
while (index < tasks.length && Date.now() - startedAt < 30 * 60_000) {
  await new Promise((resolve) => setTimeout(resolve, 500));
}
await new Promise((resolve) => server.close(resolve));

if (!connected) {
  console.error('LIVE_VOICE_TEST_FAILED: Fish Audio 扩展未连接');
  process.exitCode = 1;
} else if (index < tasks.length) {
  console.error(`LIVE_VOICE_TEST_TIMEOUT: ${index}/${tasks.length}`);
  process.exitCode = 1;
} else if (results.some((item) => !item.ok)) {
  process.exitCode = 1;
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
