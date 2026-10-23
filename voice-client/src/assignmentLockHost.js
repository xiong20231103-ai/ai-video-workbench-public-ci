import fs from 'node:fs/promises';

const lockDir = process.argv[2];
if (!lockDir) process.exit(2);

try {
  await fs.writeFile(
    `${lockDir}/owner.json`,
    JSON.stringify({ pid: process.pid, startedAt: new Date().toISOString() }),
    'utf8',
  );
  process.stdout.write('READY\n');
  process.stdin.resume();
  process.stdin.on('end', async () => {
    await fs.rm(lockDir, { recursive: true, force: true }).catch(() => {});
    process.exit(0);
  });
} catch {
  process.exit(1);
}
