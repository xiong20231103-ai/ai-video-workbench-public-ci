// Keep the public entry/argv unchanged; no third-party import before this gate.
import { checkVoiceDependencies } from './dependencyPreflight.js';

const dependencies = await checkVoiceDependencies();
if (!dependencies.ok) {
  console.error(`[VOICE_DEPENDENCIES_MISSING] 配音已阻止：缺失或无法加载 ${dependencies.missing.join('、')}。请恢复依赖后重试；未启动浏览器。`);
  process.exitCode = 1;
} else {
  await import('./runBatchImpl.js');
}
