import fs from 'node:fs/promises';
import http from 'node:http';
import path from 'node:path';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const projectRoot = path.resolve(__dirname, '..');
const publicDir = path.join(projectRoot, 'public');
const taskFile = path.join(projectRoot, 'data', 'tasks.csv');
const envFile = path.join(projectRoot, '.env');
const port = Number.parseInt(process.env.CLIENT_PORT ?? '5177', 10);

let currentJob = null;
const jobLog = [];

const server = http.createServer(async (req, res) => {
  try {
    const url = new URL(req.url ?? '/', `http://${req.headers.host}`);

    if (req.method === 'GET' && url.pathname === '/') {
      return serveFile(res, path.join(publicDir, 'index.html'), 'text/html; charset=utf-8');
    }

    if (req.method === 'GET' && url.pathname.startsWith('/assets/')) {
      const safePath = path.normalize(url.pathname.replace('/assets/', ''));
      if (safePath.startsWith('..')) return sendJson(res, 400, { error: 'bad path' });
      return serveFile(res, path.join(publicDir, safePath), contentTypeFor(safePath));
    }

    if (req.method === 'GET' && url.pathname === '/api/tasks') {
      return sendJson(res, 200, { csv: await readText(taskFile, '') });
    }

    if (req.method === 'POST' && url.pathname === '/api/tasks') {
      const body = await readJson(req);
      await fs.mkdir(path.dirname(taskFile), { recursive: true });
      await fs.writeFile(taskFile, String(body.csv ?? ''), 'utf8');
      return sendJson(res, 200, { ok: true });
    }

    if (req.method === 'GET' && url.pathname === '/api/settings') {
      return sendJson(res, 200, await readSettings());
    }

    if (req.method === 'POST' && url.pathname === '/api/settings') {
      const body = await readJson(req);
      await writeSettings(body);
      return sendJson(res, 200, { ok: true });
    }

    if (req.method === 'GET' && url.pathname === '/api/job') {
      return sendJson(res, 200, jobSnapshot());
    }

    if (req.method === 'POST' && url.pathname === '/api/job/start') {
      const body = await readJson(req);
      return sendJson(res, 200, startJob(body.mode === 'dry-run' ? ['--dry-run'] : []));
    }

    if (req.method === 'POST' && url.pathname === '/api/job/stop') {
      if (currentJob?.process) {
        currentJob.process.kill();
        appendJobLog('CLIENT', 'stop requested');
      }
      return sendJson(res, 200, { ok: true });
    }

    return sendJson(res, 404, { error: 'not found' });
  } catch (error) {
    return sendJson(res, 500, { error: error instanceof Error ? error.message : String(error) });
  }
});

server.listen(port, '127.0.0.1', () => {
  console.log(`Client running at http://127.0.0.1:${port}`);
});

function startJob(args) {
  if (currentJob?.running) {
    return { ok: false, error: '已有任务正在运行' };
  }

  jobLog.length = 0;
  const child = spawn(process.execPath, ['src/runBatch.js', ...args], {
    cwd: projectRoot,
    shell: false
  });

  currentJob = {
    running: true,
    startedAt: new Date().toISOString(),
    finishedAt: null,
    exitCode: null,
    process: child
  };

  appendJobLog('CLIENT', `started: node src/runBatch.js ${args.join(' ')}`.trim());
  child.stdout.on('data', (chunk) => appendJobLog('OUT', chunk.toString()));
  child.stderr.on('data', (chunk) => appendJobLog('ERR', chunk.toString()));
  child.on('exit', (code) => {
    appendJobLog('CLIENT', `finished with exit code ${code}`);
    currentJob.running = false;
    currentJob.finishedAt = new Date().toISOString();
    currentJob.exitCode = code;
  });

  return { ok: true };
}

function jobSnapshot() {
  return {
    running: Boolean(currentJob?.running),
    startedAt: currentJob?.startedAt ?? null,
    finishedAt: currentJob?.finishedAt ?? null,
    exitCode: currentJob?.exitCode ?? null,
    log: jobLog.slice(-500)
  };
}

function appendJobLog(source, text) {
  for (const line of String(text).split(/\r?\n/)) {
    if (line.trim() === '') continue;
    jobLog.push({ time: new Date().toISOString(), source, text: line });
  }
}

async function readSettings() {
  const env = parseEnv(await readText(envFile, ''));
  return {
    targetUrl: env.TARGET_URL ?? 'https://example.com/voice',
    headless: env.HEADLESS ?? 'false',
    slowMoMs: env.SLOW_MO_MS ?? '0',
    taskFile: env.TASK_FILE ?? 'data/tasks.csv',
    downloadDir: env.DOWNLOAD_DIR ?? 'downloads',
    logDir: env.LOG_DIR ?? 'logs'
  };
}

async function writeSettings(settings) {
  const existing = parseEnv(await readText(envFile, ''));
  const merged = {
    ...existing,
    TARGET_URL: settings.targetUrl ?? '',
    HEADLESS: settings.headless ?? 'false',
    SLOW_MO_MS: settings.slowMoMs ?? '0',
    TASK_FILE: settings.taskFile ?? 'data/tasks.csv',
    DOWNLOAD_DIR: settings.downloadDir ?? 'downloads',
    LOG_DIR: settings.logDir ?? 'logs'
  };
  const lines = Object.entries(merged).map(([key, value]) => `${key}=${value}`);
  await fs.writeFile(envFile, `${lines.join('\n')}\n`, 'utf8');
}

function parseEnv(text) {
  const result = {};
  for (const line of text.split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith('#')) continue;
    const index = trimmed.indexOf('=');
    if (index < 0) continue;
    result[trimmed.slice(0, index)] = trimmed.slice(index + 1);
  }
  return result;
}

async function serveFile(res, filePath, contentType) {
  const content = await fs.readFile(filePath);
  res.writeHead(200, { 'Content-Type': contentType });
  res.end(content);
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

async function readText(filePath, fallback) {
  try {
    return await fs.readFile(filePath, 'utf8');
  } catch (error) {
    if (error?.code === 'ENOENT') return fallback;
    throw error;
  }
}

function contentTypeFor(filePath) {
  if (filePath.endsWith('.css')) return 'text/css; charset=utf-8';
  if (filePath.endsWith('.js')) return 'text/javascript; charset=utf-8';
  return 'application/octet-stream';
}
