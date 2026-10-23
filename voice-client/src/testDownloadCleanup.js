import fs from 'node:fs/promises';
import path from 'node:path';

const AUDIO_EXTENSIONS = new Set(['.mp3', '.wav', '.m4a', '.ogg', '.flac', '.aac']);

export async function verifyAndDeleteTestDownload(filename, expectedBaseName, startedAt, expectedText = '', settleMs = 3000) {
  if (!filename || !path.isAbsolute(filename)) throw new Error('浏览器没有返回测试音频路径');

  let testFilename = filename;
  let stat = await fs.stat(testFilename);
  const expectedPrefix = expectedBaseName.toLowerCase();
  if (!path.basename(testFilename).toLowerCase().startsWith(expectedPrefix)) {
    const extension = path.extname(testFilename).toLowerCase();
    const recentEnough = Number.isFinite(startedAt) && stat.mtimeMs >= startedAt - 10_000;
    if (!recentEnough || !AUDIO_EXTENSIONS.has(extension)) {
      throw new Error(`拒绝重命名或删除非本次测试文件：${path.basename(testFilename)}`);
    }
    const renamed = path.join(path.dirname(testFilename), `${expectedBaseName}_${Date.now()}${extension}`);
    await fs.rename(testFilename, renamed);
    testFilename = renamed;
    stat = await fs.stat(testFilename);
  }

  if (!stat.isFile() || stat.size <= 0) throw new Error('下载的测试音频为空');
  if (!path.basename(testFilename).toLowerCase().startsWith(expectedPrefix)) {
    throw new Error(`拒绝删除非测试文件：${path.basename(testFilename)}`);
  }
  const downloadDir = path.dirname(testFilename);
  await fs.rm(testFilename, { force: true });
  let deletedCount = 1;

  const textMarker = filenameMarker(expectedText);
  if (textMarker.length >= 12) {
    const candidateDirs = [downloadDir];
    if (path.basename(downloadDir).toLowerCase() === 'batchvoiceclienttemp') {
      candidateDirs.push(path.dirname(downloadDir));
    }
    const cleanupUntil = Date.now() + Math.max(0, settleMs);
    do {
      for (const candidateDir of candidateDirs) {
        const entries = await fs.readdir(candidateDir, { withFileTypes: true });
        for (const entry of entries) {
          if (!entry.isFile() || !AUDIO_EXTENSIONS.has(path.extname(entry.name).toLowerCase())) continue;
          if (!filenameMarker(path.parse(entry.name).name).includes(textMarker)) continue;
          const sibling = path.join(candidateDir, entry.name);
          const siblingStat = await fs.stat(sibling);
          if (!Number.isFinite(startedAt) || siblingStat.mtimeMs < startedAt - 10_000) continue;
          await fs.rm(sibling, { force: true });
          deletedCount += 1;
        }
      }
      if (Date.now() < cleanupUntil) await new Promise((resolve) => setTimeout(resolve, 250));
    } while (Date.now() < cleanupUntil);
  }

  return { bytes: stat.size, deleted: true, deletedCount };
}

function filenameMarker(value) {
  return String(value)
    .normalize('NFKC')
    .toLowerCase()
    .replace(/[^\p{L}\p{N}]+/gu, '-')
    .replace(/^-+|-+$/g, '');
}
