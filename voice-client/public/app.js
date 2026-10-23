const settingsForm = document.querySelector('#settingsForm');
const tasksCsv = document.querySelector('#tasksCsv');
const saveAllBtn = document.querySelector('#saveAllBtn');
const dryRunBtn = document.querySelector('#dryRunBtn');
const startBtn = document.querySelector('#startBtn');
const stopBtn = document.querySelector('#stopBtn');
const jobStatus = document.querySelector('#jobStatus');
const jobLog = document.querySelector('#jobLog');

await loadInitialData();
setInterval(refreshJob, 1200);
await refreshJob();

saveAllBtn.addEventListener('click', saveAll);
dryRunBtn.addEventListener('click', () => startJob('dry-run'));
startBtn.addEventListener('click', () => startJob('run'));
stopBtn.addEventListener('click', stopJob);

async function loadInitialData() {
  const [settings, tasks] = await Promise.all([request('/api/settings'), request('/api/tasks')]);
  for (const [name, value] of Object.entries(settings)) {
    const field = settingsForm.elements[name];
    if (field) field.value = value;
  }
  tasksCsv.value = tasks.csv;
}

async function saveAll() {
  await Promise.all([
    request('/api/settings', { method: 'POST', body: JSON.stringify(formToObject(settingsForm)) }),
    request('/api/tasks', { method: 'POST', body: JSON.stringify({ csv: tasksCsv.value }) })
  ]);
  flashStatus('已保存');
}

async function startJob(mode) {
  await saveAll();
  const result = await request('/api/job/start', { method: 'POST', body: JSON.stringify({ mode }) });
  if (!result.ok) flashStatus(result.error ?? '启动失败');
  await refreshJob();
}

async function stopJob() {
  await request('/api/job/stop', { method: 'POST', body: '{}' });
  await refreshJob();
}

async function refreshJob() {
  const job = await request('/api/job');
  jobStatus.textContent = job.running
    ? `运行中，开始于 ${formatTime(job.startedAt)}`
    : job.finishedAt ? `已结束，退出码 ${job.exitCode}` : '未运行';
  jobLog.textContent = job.log.map((line) => `${formatTime(line.time)} [${line.source}] ${line.text}`).join('\n');
  jobLog.scrollTop = jobLog.scrollHeight;
}

function formToObject(form) {
  return Object.fromEntries(new FormData(form).entries());
}

async function request(url, options = {}) {
  const response = await fetch(url, { headers: { 'Content-Type': 'application/json' }, ...options });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error ?? `HTTP ${response.status}`);
  return data;
}

function flashStatus(text) {
  jobStatus.textContent = text;
  setTimeout(refreshJob, 1000);
}

function formatTime(value) {
  if (!value) return '-';
  return new Date(value).toLocaleTimeString();
}
