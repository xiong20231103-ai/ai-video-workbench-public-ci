import { config } from './config.js';
import { BatchLogger } from './logger.js';
import { runUserscriptBatch } from './userscriptBatch.js';

const logger = new BatchLogger(config.logDir);
await logger.init();

const task = {
  id: 'BRIDGE_TEST',
  project: '连接测试',
  language: '英语',
  voice: 'default',
  text: 'Seventh bridge test. Detect the new waveform, then download.',
  outputName: 'FishAudio_连接测试_7',
  selectionMode: 'manual',
  material: '',
  copyId: '',
  copyTitle: '',
  copySource: ''
};

const copySelector = {
  async select(source) {
    return { ...source };
  }
};

await runUserscriptBatch({
  tasks: [task],
  copySelector,
  logger,
  downloadDir: config.downloadDir,
  port: config.bridgePort
});
