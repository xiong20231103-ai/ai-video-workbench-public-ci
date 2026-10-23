import fs from 'node:fs/promises';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { parse } from 'csv-parse/sync';
import { assignmentTransaction } from './assignmentTransaction.js';
import { isoWeekTag, loadWeeklyLibrary } from './weeklyCopy.js';

const EXPECTED_MATERIALS = [
  '净白板', '碳岩板', '金属板', '镜面板', '木饰面', 'PU石', 'PU软石', '塑木地板', '晶岩墙板'
];
const BASE_LANGUAGES = [
  '英语', '德语', '西班牙语', '法语', '印尼语', '意大利语', '葡萄牙语', '土耳其语', '俄语'
];
const SCOPED_LANGUAGE_MATERIALS = {
  韩语: ['净白板', '碳岩板', '金属板', '镜面板', '木饰面', 'PU石', 'PU软石']
};
const ExampleBrand_MATERIALS = ['清淤泵', '潜污泵', '离心泵', '轴流泵', '粉碎格栅', '一体化预制泵站', '大型泵站', 'MaterialPump'];
const ExampleBrand_LANGUAGES = ['英语', '西班牙语'];
const ExampleBrand_COPY_LIBRARY_VERSION = 'ExampleBrand_Pump_Reels_Copy_FINAL_100_EN_ES_v1';
const SUPPORTED_COPY_VERSIONS = new Set([ExampleBrand_COPY_LIBRARY_VERSION, 'legacy', 'legacy_unverified']);

export class CopySelector {
  constructor({
    libraryFile, rulesFile, usageFile, legacyLibraryFile = '', random = Math.random,
    weeklyDir = '', weeklyRulesFile = '', now = () => new Date()
  }) {
    this.libraryFile = libraryFile;
    this.weeklyDir = weeklyDir || path.join(path.dirname(libraryFile), 'weekly');
    this.weeklyRulesFile = weeklyRulesFile || path.join(path.dirname(libraryFile), '..', 'weekly-copy', 'rules.json');
    this.now = now;
    this.weekly = { week: '', active: false, reason: '' };
    this.legacyLibraryFile = legacyLibraryFile || path.join(path.dirname(libraryFile), 'copy_library_legacy.csv');
    this.rulesFile = rulesFile;
    this.usageFile = usageFile;
    this.assignmentFile = path.join(path.dirname(usageFile), 'copy_assignments.json');
    this.random = random;
    this.library = [];
    this.pools = new Map();
    this.legacyLibrary = [];
    this.legacyPools = new Map();
    this.rules = new Map();
    this.state = emptyState();
    this.previewState = emptyState();
    this.assignments = {};
  }

  async init() {
    const [libraryText, rulesText, legacyLibraryText] = await Promise.all([
      fs.readFile(this.libraryFile, 'utf8'),
      fs.readFile(this.rulesFile, 'utf8'),
      fs.readFile(this.legacyLibraryFile, 'utf8').catch((error) => {
        if (error?.code === 'ENOENT') return '';
        throw error;
      })
    ]);
    const csvOptions = { columns: true, skip_empty_lines: true, trim: true, bom: true };
    this.library = parse(libraryText.replace(/\r\n?/g, '\n'), csvOptions);
    this.legacyLibrary = legacyLibraryText
      ? parse(legacyLibraryText.replace(/\r\n?/g, '\n'), csvOptions)
      : [];
    const ruleRows = parse(rulesText.replace(/\r\n?/g, '\n'), csvOptions);

    for (const record of this.library) {
      const key = poolKey(record.material, record.language);
      if (!this.pools.has(key)) this.pools.set(key, []);
      this.pools.get(key).push(record);
    }
    for (const record of this.legacyLibrary) {
      const key = poolKey(record.material, record.language);
      if (!this.legacyPools.has(key)) this.legacyPools.set(key, []);
      this.legacyPools.get(key).push(record);
    }
    for (const rule of ruleRows) {
      const materials = String(rule.materials ?? '').split('|').map((item) => item.trim()).filter(Boolean);
      if (!rule.project || !['rotation', 'fixed'].includes(rule.mode) || materials.length === 0) {
        throw new Error(`无效的项目文案规则：${JSON.stringify(rule)}`);
      }
      this.rules.set(rule.project, { mode: rule.mode, materials });
    }

    this.validateLibrary();
    await this.applyWeeklyLibrary();
    this.state = await readState(this.usageFile);
    this.assignments = await readAssignments(this.assignmentFile);
    this.previewState = structuredClone(this.state);
    return this;
  }

  validateLibrary() {
    if (this.library.length !== 640) {
      throw new Error(`文案库应有正式440条＋ExampleBrand 200条，实际为 ${this.library.length} 条`);
    }
    for (const material of EXPECTED_MATERIALS) {
      for (const language of BASE_LANGUAGES) {
        const pool = this.pools.get(poolKey(material, language)) ?? [];
        if (pool.length !== 5) {
          throw new Error(`文案池 ${material}/${language} 应有 5 条，实际为 ${pool.length} 条`);
        }
      }
    }
    for (const [language, materials] of Object.entries(SCOPED_LANGUAGE_MATERIALS)) {
      for (const material of materials) {
        const pool = this.pools.get(poolKey(material, language)) ?? [];
        if (pool.length !== 5) {
          throw new Error(`文案池 ${material}/${language} 应有 5 条，实际为 ${pool.length} 条`);
        }
      }
    }
    for (const material of ExampleBrand_MATERIALS) {
      for (const language of ExampleBrand_LANGUAGES) {
        const pool = this.pools.get(poolKey(material, language)) ?? [];
        if (pool.length < 1 || new Set(pool.map((copy) => copy.copyId)).size !== pool.length ||
            pool.some((copy) => !String(copy.title ?? '').trim() || !String(copy.text ?? '').trim())) {
          throw new Error(`ExampleBrand文案池 ${material}/${language} 必须有非空且唯一的文案，实际为${pool.length}条`);
        }
      }
    }
  }

  // The week that is current when this batch starts replaces the regular
  // material pools (never ExampleBrand). Copy already bound to a task keeps its saved
  // text, so a batch that spans Monday is not changed half-way.
  async applyWeeklyLibrary() {
    const week = isoWeekTag(this.now());
    try {
      this.weekly = await loadWeeklyLibrary({ weeklyDir: this.weeklyDir, rulesFile: this.weeklyRulesFile, week });
    } catch (error) {
      this.weekly = { week, active: false, reason: `周文案读取失败：${error.message}` };
    }
    if (!this.weekly.active) return;
    const weeklyPools = new Map();
    for (const record of this.weekly.rows) {
      const key = poolKey(record.material, record.language);
      if (!weeklyPools.has(key)) weeklyPools.set(key, []);
      weeklyPools.get(key).push(record);
    }
    for (const [key, pool] of weeklyPools) this.pools.set(key, pool);
  }

  async select(task, { consume = true } = {}) {
    // Selection and persistence are one transaction.  Two voice clients may
    // start at the same time; the second one must observe and reuse the first
    // committed assignment instead of selecting a new copy and overwriting it.
    return assignmentTransaction(this.assignmentFile, this.usageFile, async (transaction) => {
      this.assignments = transaction.assignments;
      this.state = transaction.usage;
      return this._selectLocked(task, { consume });
    }, consume);
  }

  async _selectLocked(task, { consume = true } = {}) {
    task = { ...task };
    for (const field of ['material', 'language', 'copyId', 'copySource', 'copyLibraryVersion', 'copyTextHash']) {
      task[field] = String(task[field] ?? '').trim();
    }
    const assignmentKeys = stableAssignmentKeys(task);
    const exactSavedKey = assignmentKeys.find((key) => this.assignments[key]) || '';
    let assignmentKey = exactSavedKey || assignmentKeys[0] || '';
    let savedAssignment = exactSavedKey ? this.assignments[exactSavedKey] : null;
    if (!savedAssignment) {
      const stableBase = String(task.stableTaskKey || task.canonicalTaskId || [
        task.productionDate || task.date, task.runGroupId, task.batchId, task.localTaskId || task.id,
      ].map((value) => String(value || '').trim()).filter(Boolean).join('|')).trim();
      const requestedProject = String(task.project || '').trim();
      const requestedMaterial = String(task.material || '').trim();
      const requestedLanguage = String(task.language || '').trim();
      const requestedAudioTaskKey = audioTaskKey(task);
      const candidates = Object.entries(this.assignments).filter(([, value]) => {
        if (!value || typeof value !== 'object') return false;
        const savedStable = String(value.stableTaskKey || '').trim();
        if (savedStable && savedStable !== stableBase) return false;
        if (!savedStable && assignmentKeys.length && !assignmentKeys.includes(String(value.assignmentKey || ''))) return false;
        if (requestedProject && String(value.project || '').trim() !== requestedProject) return false;
        // A parent canonicalTaskId is shared by all language rows in a Fish
        // batch.  It is not an audio-task identity.  Never let a Spanish
        // assignment become a Portuguese (or sibling-language) candidate.
        if (requestedLanguage && String(value.language || '').trim() !== requestedLanguage) return false;
        if (requestedMaterial && String(value.material || '').trim() !== requestedMaterial) return false;
        const savedAudioTaskKey = String(value.audioTaskKey || '').trim();
        if (requestedAudioTaskKey && savedAudioTaskKey && savedAudioTaskKey !== requestedAudioTaskKey) return false;
        return Boolean(savedStable || assignmentKeys.includes(String(value.assignmentKey || '')));
      });
      if (candidates.length > 1) {
        throw new Error(`稳定任务存在多个文案分配，无法安全选择：${stableBase}`);
      }
      if (candidates.length === 1) {
        assignmentKey = candidates[0][0];
        savedAssignment = candidates[0][1];
      }
    }
    if (savedAssignment) {
      const expectedStable = String(task.stableTaskKey || task.canonicalTaskId || [
        task.productionDate || task.date, task.runGroupId, task.batchId, task.localTaskId || task.id,
      ].map(value => String(value || '').trim()).filter(Boolean).join('|')).trim();
      if (!savedAssignment.stableTaskKey || savedAssignment.stableTaskKey !== expectedStable) {
        throw new Error('COPY_LEGACY_IDENTITY_UNVERIFIED');
      }
      const expectedAudioTaskKey = audioTaskKey(task);
      const savedAudioTaskKey = String(savedAssignment.audioTaskKey || '').trim();
      if (savedAudioTaskKey && expectedAudioTaskKey && savedAudioTaskKey !== expectedAudioTaskKey) {
        throw new Error('COPY_ASSIGNMENT_CONFLICT: audioTask');
      }
      // Migrate a legacy parent-scoped assignment only after its language,
      // material and task ownership have passed the checks above.  The
      // migration is committed by the same assignment transaction as the
      // normal selection; preview mode never mutates persistent state.
      if (consume && expectedAudioTaskKey && !savedAudioTaskKey) {
        savedAssignment.audioTaskKey = expectedAudioTaskKey;
        savedAssignment.audioTaskId = String(task.id || task.localTaskId || '').trim();
        this.assignments[assignmentKey] = savedAssignment;
      }
      for (const field of ['project', 'material', 'language', 'copyId', 'copySource', 'copyLibraryVersion']) {
        const requested = String(task[field] ?? '').trim();
        const saved = String(savedAssignment[field] ?? '').trim();
        if (requested && requested !== saved) throw new Error(`COPY_ASSIGNMENT_CONFLICT: ${field === 'copyLibraryVersion' ? '版本冲突' : field}`);
        if (!requested) task[field] = saved;
      }
      const actualHash = sha256Text(String(savedAssignment.text ?? ''));
      if (savedAssignment.copyTextHash && savedAssignment.copyTextHash !== actualHash) {
        throw new Error('COPY_SAVED_TEXT_HASH_MISMATCH: 保存正文已被修改');
      }
      if (task.copyTextHash && task.copyTextHash !== actualHash) throw new Error('COPY_TEXT_HASH_CONFLICT');
    }
    if (task.language === '韩语' && task.project !== 'ProjectD') {
      throw new Error('韩语文案和音色仅允许用于ProjectD项目');
    }
    if (task.project === 'ExampleBrand' && !ExampleBrand_LANGUAGES.includes(task.language)) {
      throw new Error('ExampleBrand只允许英语和西班牙语文案');
    }
    // Older submitters know this product but omit the library version. It
    // debuted in FINAL_100; only a pristine, unbound task can adopt that
    // introduction version. Never reinterpret saved/manual copy identities
    // or redirect any of the seven legacy products to today's library.
    if (task.project === 'ExampleBrand' && task.material === 'MaterialPump'
        && assignmentKey && !savedAssignment
        && !String(task.text ?? '').trim()
        && !task.copyLibraryVersion && !task.copyId && !task.copySource && !task.copyTextHash) {
      task.copyLibraryVersion = ExampleBrand_COPY_LIBRARY_VERSION;
    }
    const requestedVersion = String(
      task.copyLibraryVersion
      || (task.copySource === ExampleBrand_COPY_LIBRARY_VERSION ? ExampleBrand_COPY_LIBRARY_VERSION : '')
    ).trim();
    if (requestedVersion && !SUPPORTED_COPY_VERSIONS.has(requestedVersion)) {
      throw new Error(`不支持的文案库版本：${requestedVersion}`);
    }
    if (!String(task.text ?? '').trim() && savedAssignment && savedAssignment.text) {
      const assignedVersion = String(savedAssignment.copyLibraryVersion || 'legacy_unverified').trim();
      if (!SUPPORTED_COPY_VERSIONS.has(assignedVersion)) {
        throw new Error(`已绑定的文案库版本无法识别：${assignedVersion}`);
      }
      const requestedCopyId = String(task.copyId ?? '').trim();
      const savedText = String(savedAssignment.text ?? '');
      const savedTextHash = sha256Text(savedText);
      if (requestedVersion && requestedVersion !== assignedVersion) {
        throw new Error(`稳定任务文案版本冲突：已绑定=${assignedVersion}，请求=${requestedVersion}`);
      }
      if (requestedCopyId && requestedCopyId !== String(savedAssignment.copyId || '').trim()) {
        throw new Error(`稳定任务文案ID冲突：已绑定=${savedAssignment.copyId || ''}，请求=${requestedCopyId}`);
      }
      if (task.copyTextHash && String(task.copyTextHash).trim() !== savedTextHash) {
        throw new Error(`稳定任务文案正文哈希冲突：${assignmentKey}`);
      }
      if (savedAssignment.copyTextHash && String(savedAssignment.copyTextHash).trim() !== savedTextHash) {
        throw new Error(`已绑定文案保存正文已被修改：${assignmentKey}`);
      }
      const assignedMaterial = String(task.material || savedAssignment.material || '').trim();
      const assignedPool = assignedVersion === ExampleBrand_COPY_LIBRARY_VERSION ? this.pools : this.legacyPools;
      const assignedPoolRows = assignedPool.get(poolKey(assignedMaterial, task.language || savedAssignment.language)) ?? [];
      const assignedCopy = assignedPoolRows.find((item) => String(item.copyId ?? '').trim() === String(savedAssignment.copyId ?? '').trim());
      if (task.project === 'ExampleBrand' && !assignedCopy) {
        throw new Error(`已绑定的ExampleBrand文案不存在或版本冲突：${savedAssignment.copyId || assignmentKey}`);
      }
      if (assignedCopy && sha256Text(assignedCopy.text) !== String(savedAssignment.copyTextHash || sha256Text(assignedCopy.text))) {
        throw new Error(`已绑定文案正文哈希不一致：${assignmentKey}`);
      }
      return {
        ...task,
        text: savedText,
        outputName: String(task.outputName ?? '').trim() || safeOutputName(
          [task.project, task.material, task.language, task.id, savedAssignment.copyId].join('_')
        ),
        selectionMode: 'persisted',
        material: assignedMaterial,
        copyId: String(savedAssignment.copyId || '').trim(),
        copyTitle: String(savedAssignment.copyTitle || assignedCopy?.title || '').trim(),
        copySource: String(savedAssignment.copySource || '').trim(),
        copyLibraryVersion: assignedVersion,
         copyTextHash: savedTextHash
      };
    }
    if (String(task.text ?? '').trim()) {
      const text = String(task.text ?? '');
      const requestedTextHash = sha256Text(text);
      if (savedAssignment && savedAssignment.copyTextHash && savedAssignment.copyTextHash !== requestedTextHash) {
        throw new Error(`稳定任务文案正文已变化，拒绝覆盖既有分配：${assignmentKey}`);
      }
      if (savedAssignment) {
        const requestedVersion = String(task.copyLibraryVersion ?? '').trim();
        const requestedCopyId = String(task.copyId ?? '').trim();
        const requestedMaterial = String(task.material ?? '').trim();
        const requestedLanguage = String(task.language ?? '').trim();
        if (requestedVersion && requestedVersion !== String(savedAssignment.copyLibraryVersion || '').trim()) {
          throw new Error(`稳定任务文案版本冲突：已绑定=${savedAssignment.copyLibraryVersion || ''}，请求=${requestedVersion}`);
        }
        if (requestedCopyId && requestedCopyId !== String(savedAssignment.copyId || '').trim()) {
          throw new Error(`稳定任务文案ID冲突：已绑定=${savedAssignment.copyId || ''}，请求=${requestedCopyId}`);
        }
        if (requestedMaterial && requestedMaterial !== String(savedAssignment.material || '').trim()) {
          throw new Error(`稳定任务文案材料冲突：已绑定=${savedAssignment.material || ''}，请求=${requestedMaterial}`);
        }
        if (requestedLanguage && requestedLanguage !== String(savedAssignment.language || '').trim()) {
          throw new Error(`稳定任务文案语言冲突：已绑定=${savedAssignment.language || ''}，请求=${requestedLanguage}`);
        }
      }
       const persistedVersion = String(task.copyLibraryVersion ?? '').trim();
      const persistedHash = String(task.copyTextHash ?? '').trim();
      const copyTextHash = persistedHash || sha256Text(text);
      if (persistedHash && persistedHash !== sha256Text(text)) {
        throw new Error(`已绑定文案正文哈希不一致：${task.id || 'unknown-task'}`);
      }
      const resolved = {
        ...task,
        text,
        selectionMode: 'manual',
        material: String(task.material || savedAssignment?.material || '').trim(),
        language: String(task.language || savedAssignment?.language || '').trim(),
        // A manually supplied body is still an existing task fact.  Never
        // erase its copy identity just because it bypassed the selector.
        copyId: String(task.copyId || savedAssignment?.copyId || '').trim(),
        copyTitle: String(task.copyTitle || savedAssignment?.copyTitle || '').trim(),
        copySource: String(task.copySource || savedAssignment?.copySource || '').trim(),
        copyLibraryVersion: persistedVersion || String(savedAssignment?.copyLibraryVersion ?? '').trim() || 'legacy_unverified',
        copyTextHash
      };
      if (consume && assignmentKey && !savedAssignment) {
        this.assignments[assignmentKey] = assignmentFromTask(resolved);
      }
      return resolved;
    }

    const isExamplebrand = task.project === 'ExampleBrand';
    if (isExamplebrand && requestedVersion &&
      requestedVersion !== ExampleBrand_COPY_LIBRARY_VERSION &&
      requestedVersion !== 'legacy' && requestedVersion !== 'legacy_unverified') {
      throw new Error(`不支持的ExampleBrand文案库版本：${requestedVersion}`);
    }
    const poolMap = isExamplebrand && requestedVersion !== ExampleBrand_COPY_LIBRARY_VERSION
      ? this.legacyPools
      : this.pools;
    const existingCopyId = String(task.copyId ?? '').trim();
    if (isExamplebrand && existingCopyId) {
      const existingPool = poolMap.get(poolKey(task.material, task.language)) ?? [];
      const existingCopy = existingPool.find((item) => String(item.copyId ?? '').trim() === existingCopyId);
      if (!existingCopy) {
        throw new Error(`找不到已绑定的ExampleBrand文案：${existingCopyId}`);
      }
      if (task.copyTextHash && task.copyTextHash !== sha256Text(existingCopy.text)) {
        throw new Error('COPY_TEXT_HASH_CONFLICT');
      }
      const resolved = {
        ...task,
        text: existingCopy.text,
        copyTextHash: sha256Text(existingCopy.text),
        outputName: String(task.outputName ?? '').trim() || safeOutputName(
          [task.project, task.material, task.language, task.id, existingCopy.copyId].join('_')
        ),
        selectionMode: 'persisted',
        material: String(task.material ?? '').trim(),
        copyId: existingCopy.copyId,
        copyTitle: existingCopy.title,
        copySource: existingCopy.source,
        copyLibraryVersion: requestedVersion || 'legacy'
      };
      if (consume && assignmentKey) {
        this.assignments[assignmentKey] = assignmentFromTask(resolved);
      }
      return resolved;
    }

    const rule = this.rules.get(task.project);
    if (!rule) throw new Error(`项目“${task.project}”没有配置随机文案规则`);
    const state = consume ? this.state : this.previewState;
    const requestedMaterial = String(task.material ?? '').trim();
    if (requestedMaterial && !rule.materials.includes(requestedMaterial)) {
      throw new Error(`项目“${task.project}”不能使用材料“${requestedMaterial}”`);
    }
    const material = requestedMaterial || this.takeMaterial(state, task.project, task.language, rule);
    const copy = this.takeCopy(state, task.project, task.language, material, poolMap);
    const outputName = String(task.outputName ?? '').trim() || safeOutputName(
      [task.project, material, task.language, task.id, copy.copyId].join('_')
    );

    const resolved = {
      ...task,
      text: copy.text,
      outputName,
      selectionMode: 'library',
      material,
      copyId: copy.copyId,
      copyTitle: copy.title,
      copySource: copy.source,
      copyTextHash: sha256Text(copy.text),
      copyLibraryVersion: task.project === 'ExampleBrand'
        ? (requestedVersion || 'legacy')
        : ''
    };
    if (consume && assignmentKey) {
      this.assignments[assignmentKey] = assignmentFromTask(resolved);
    }
    return resolved;
  }

  takeMaterial(state, project, language, rule) {
    if (rule.mode === 'fixed') return rule.materials[0];
    const key = `${project}|${language}`;
    let deck = state.materialDecks[key];
    if (!Array.isArray(deck) || deck.length === 0 || deck.some((item) => !rule.materials.includes(item))) {
      deck = shuffle([...rule.materials], this.random);
      state.materialDecks[key] = deck;
    }
    return deck.shift();
  }

  takeCopy(state, project, language, material, poolMap = this.pools) {
    const pool = poolMap.get(poolKey(material, language)) ?? [];
    if (pool.length === 0) throw new Error(`找不到 ${material}/${language} 的文案`);
    const key = project === 'ExampleBrand' && poolMap === this.pools
      ? `${project}|${language}|${material}|${ExampleBrand_COPY_LIBRARY_VERSION}`
      : `${project}|${language}|${material}`;
    const validIds = new Set(pool.map((copy) => copy.copyId));
    let deck = state.copyDecks[key];
    if (!Array.isArray(deck) || deck.length === 0 || deck.some((id) => !validIds.has(id))) {
      deck = shuffle([...validIds], this.random);
      state.copyDecks[key] = deck;
    }
    const copyId = deck.shift();
    return pool.find((copy) => copy.copyId === copyId);
  }
}

function poolKey(material, language) {
  return `${material}|${language}`;
}

function emptyState() {
  return { version: 1, materialDecks: {}, copyDecks: {} };
}

function sha256Text(value) {
  return createHash('sha256').update(String(value ?? ''), 'utf8').digest('hex');
}

function stableAssignmentKeys(task) {
  const explicitStable = String(task.stableTaskKey || task.canonicalTaskId || '').trim();
  const fallbackStable = [
    task.productionDate || task.date, task.runGroupId, task.batchId, task.localTaskId || task.id
  ].map((value) => String(value || '').trim()).filter(Boolean).join('|');
  const stable = explicitStable || fallbackStable;
  if (!stable) return [];
  const current = [stable, task.project, task.material, task.language]
    .map((value) => String(value || '').trim()).join('|');
  // Keep reading the pre-v0.6 key for old tasks, but never write to it.
  const legacyBase = String(task.canonicalTaskId || task.id || '').trim();
  const legacy = legacyBase && legacyBase !== stable
    ? [legacyBase, task.project, task.material, task.language].map((value) => String(value || '').trim()).join('|')
    : '';
  return legacy ? [current, legacy] : [current];
}

function stableAssignmentKey(task) {
  return stableAssignmentKeys(task)[0] || '';
}

function assignmentFromTask(task) {
  return {
    stableTaskKey: String(task.stableTaskKey || task.canonicalTaskId || [
      task.productionDate || task.date, task.runGroupId, task.batchId, task.localTaskId || task.id,
    ].map((value) => String(value || '').trim()).filter(Boolean).join('|')).trim(),
    canonicalTaskId: String(task.canonicalTaskId || '').trim(),
    productionDate: String(task.productionDate || task.date || '').trim(),
    runGroupId: String(task.runGroupId || '').trim(),
    batchId: String(task.batchId || '').trim(),
    taskId: String(task.id || '').trim(),
    audioTaskId: String(task.id || task.localTaskId || '').trim(),
    audioTaskKey: audioTaskKey(task),
    project: String(task.project || '').trim(),
    material: String(task.material || '').trim(),
    language: String(task.language || '').trim(),
    copyId: String(task.copyId || '').trim(),
    copyTitle: String(task.copyTitle || '').trim(),
    copySource: String(task.copySource || '').trim(),
    copyLibraryVersion: String(task.copyLibraryVersion || '').trim(),
    copyTextHash: String(task.copyTextHash || sha256Text(task.text || '')).trim(),
    text: String(task.text || ''),
    assignedAt: new Date().toISOString()
  };
}

// The parent canonical task is shared by every language in a Fish batch.
// Keep the old assignment map key for compatibility, but persist this
// language-task identity so sibling rows cannot inherit one another's copy.
function audioTaskKey(task) {
  const parent = String(task.stableTaskKey || task.canonicalTaskId || [
    task.productionDate || task.date, task.runGroupId, task.batchId
  ].map((value) => String(value || '').trim()).filter(Boolean).join('|')).trim();
  const child = String(task.audioTaskId || task.localTaskId || task.id || '').trim();
  const project = String(task.project || '').trim();
  const material = String(task.material || '').trim();
  const language = String(task.language || '').trim();
  if (!parent || !child || !language) return '';
  return ['audio-v1', parent, child, project, material, language].join('|');
}

async function readState(file) {
  try {
    const parsed = JSON.parse(await fs.readFile(file, 'utf8'));
    if (!parsed || parsed.version !== 1) return emptyState();
    return {
      version: 1,
      materialDecks: parsed.materialDecks && typeof parsed.materialDecks === 'object' ? parsed.materialDecks : {},
      copyDecks: parsed.copyDecks && typeof parsed.copyDecks === 'object' ? parsed.copyDecks : {}
    };
  } catch (error) {
    if (error?.code === 'ENOENT' || error instanceof SyntaxError) return emptyState();
    throw error;
  }
}


async function readAssignments(file) {
  try {
    const parsed = JSON.parse(await fs.readFile(file, 'utf8'));
    if (!parsed || parsed.version !== 1 || !parsed.assignments || typeof parsed.assignments !== 'object') {
      throw new Error(`文案分配状态格式无效：${file}`);
    }
    return parsed.assignments;
  } catch (error) {
    if (error?.code === 'ENOENT') return {};
    throw error;
  }
}


function shuffle(items, random) {
  for (let index = items.length - 1; index > 0; index -= 1) {
    const target = Math.floor(random() * (index + 1));
    [items[index], items[target]] = [items[target], items[index]];
  }
  return items;
}

function safeOutputName(value) {
  return String(value).replace(/[<>:"/\\|?*\x00-\x1F]/g, '_').replace(/\s+/g, '_');
}
