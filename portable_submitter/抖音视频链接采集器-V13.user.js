// ==UserScript==
// @name         抖音视频链接采集器 V13
// @namespace    http://tampermonkey.net/
// @version      2.2
// @description  自动采集抖音视频链接，支持分类、日期折叠、搜索、备份、批量复制、批量移动和V13任务导入
// @match        *://www.douyin.com/*
// @grant        GM_setClipboard
// @grant        GM_getValue
// @grant        GM_setValue
// @grant        GM_addValueChangeListener
// @grant        GM_xmlhttpRequest
// @connect      127.0.0.1
// @connect      localhost
// @connect      192.0.2.20
// @connect      example-pc
// @run-at       document-idle
// ==/UserScript==

(function () {
  'use strict';

  const VIDEO_URL_RE = /https?:\/\/(?:www\.)?douyin\.com\/video\/(\d+)/;
  const DEFAULT_CAT = '未分类';
  const BRAND = '#FE2C55';
  const BRAND_DARK = '#c41c3e';
  const COLOR_PAUSE = '#f39c12';
  const COLOR_RESUME = '#27ae60';

  const KEY_COLLECTED = 'douyin_collected';
  const KEY_DELETED = 'douyin_deleted';
  const KEY_CATEGORIES = 'douyin_categories';
  const KEY_CURRENT_CAT = 'douyin_current_cat';
  const KEY_PAUSED = 'douyin_paused';
  const KEY_UI = 'douyin_ui_state_v1';
  const KEY_V12_SENT = 'douyin_v13_sent_v1';
  const KEY_V12_MAPPINGS = 'douyin_v13_category_mappings_v1';
  const KEY_V12_SETTINGS = 'douyin_v13_settings_v1';
  const KEY_V13_PENDING_IMPORTS = 'douyin_v13_pending_imports_v1';
  const KEY_LEGACY_V12_SENT = 'douyin_v12_sent_v1';
  const KEY_LEGACY_V12_MAPPINGS = 'douyin_v12_category_mappings_v1';
  const KEY_LEGACY_V12_SETTINGS = 'douyin_v12_settings_v1';
  const KEY_V12_RESET_20260818 = 'douyin_v12_reset_mistaken_20260818_v1';
  const V13_LOCAL_BRIDGE = 'http://127.0.0.1:18766';
  const V13_BRIDGE_PROTOCOL = 'v13.3';
  const V13_FORMAL_BATCH_KIND = 'formal';
  const V13_ExampleBrand_BATCH_KIND = 'examplebrand-natural-flow';
  const V12_PROJECTS = ['ProjectD', 'ProjectA', 'ProjectB', 'SPC地板', 'SPC墙板', 'ProjectC'];
  const V13_PROJECT_EXPECTED_COUNTS = {
    ProjectA: 24,
    ProjectB: 13,
    ProjectC: 8,
    ProjectD: 26,
    'SPC地板': 4,
    'SPC墙板': 4,
  };
  const V13_ExampleBrand_PRODUCTS = ['清淤泵', '潜污泵', '离心泵', '轴流泵', '粉碎格栅', '一体化预制泵站', '大型泵站'];
  const V13_ExampleBrand_PRODUCT_SLUGS = {
    清淤泵: 'dredging-pump',
    潜污泵: 'submersible-sewage-pump',
    离心泵: 'centrifugal-pump',
    轴流泵: 'axial-flow-pump',
    粉碎格栅: 'grinder-screen',
    一体化预制泵站: 'prefabricated-pump-station',
    大型泵站: 'large-pump-station',
  };
  const V13_ExampleBrand_ACCOUNT_SLOTS = [
    'EN-01', 'EN-01', 'EN-01',
    'EN-02', 'EN-02', 'EN-02',
    'EN-03', 'EN-03', 'EN-03',
    'EN-04', 'EN-04', 'EN-04',
    'EN-05', 'EN-05', 'EN-05',
    'EN-06', 'EN-06', 'EN-06',
    'ES-01', 'ES-01', 'ES-01',
  ];
  const V12_MATERIALS = ['碳岩板', '净白板', '金属板', '镜面板', '木饰面', 'PU石', 'PU软石', '塑木地板', '晶岩墙板'];
  const V12_TEMPLATES = {
    'ProjectA': 'cloud-carbon',
    'ProjectB': 'physical-default',
    'ProjectC': 'huang-default',
    'ProjectD': 'real-huang-default',
    'SPC地板': 'spc-floor',
    'SPC墙板': 'spc-wall',
  };
  const V12_SPECIAL_MAPPINGS = {
    'SPC地板': { project: 'SPC地板', material: '塑木地板', templateId: 'spc-floor' },
    'SPC墙板': { project: 'SPC墙板', material: '晶岩墙板', templateId: 'spc-wall' },
  };

  const collapsedCats = new Set();
  const dateCollapseOverrides = new Map();
  const sessionNew = new Set();
  const collected = new Map();
  const deleted = new Set();
  let v12Sent = {
    ...safeParseObject(GM_getValue(KEY_LEGACY_V12_SENT, '{}')),
    ...safeParseObject(GM_getValue(KEY_V12_SENT, '{}')),
  };
  let v12Mappings = {
    ...safeParseObject(GM_getValue(KEY_LEGACY_V12_MAPPINGS, '{}')),
    ...safeParseObject(GM_getValue(KEY_V12_MAPPINGS, '{}')),
  };
  let pendingImports = safeParseObject(GM_getValue(KEY_V13_PENDING_IMPORTS, '{}'));

  let currentVideoId = '';
  let categories = [DEFAULT_CAT];
  let currentCat = DEFAULT_CAT;
  let paused = true;
  let panel;
  let bubble;
  let headerEl;
  let countEl;
  let statusEl;
  let statusDot;
  let btnPauseEl;
  let btnMinEl;
  let listEl;
  let catSelectEl;
  let filterSelectEl;
  let searchInput;
  let viewTodayBtn;
  let viewAllBtn;
  let copyScopeSelect;
  let batchScopeSelect;
  let batchTargetSelect;
  let batchSelectionInfo;
  let toolsDrawer;
  let toolStatusEl;
  let deletedManagerEl;
  let newCatInput;
  let blackInput;
  let renderFrame = 0;
  let queuedAutoFocus = false;
  let restoreScrollTimer = 0;
  let suppressAutoScrollUntil = 0;
  let uiSaveTimer = 0;
  let toastTimer = 0;
  // Batch selection is intentionally transient: it never changes the
  // collected-link store until the user confirms a move.
  const selectedUrls = new Set();

  const UI_DEFAULTS = {
    top: 80,
    left: null,
    right: 16,
    width: 420,
    height: Math.round(Math.max(360, Math.min(900, window.innerHeight * 0.7))),
    minimized: false,
    viewMode: 'today',
    categoryFilter: 'all',
  };

  function clamp(value, min, max) {
    return Math.max(min, Math.min(max, Number(value) || min));
  }

  function safeParseArray(raw, fallback = []) {
    if (Array.isArray(raw)) return raw;
    if (typeof raw !== 'string') return fallback;
    try {
      const parsed = JSON.parse(raw);
      return Array.isArray(parsed) ? parsed : fallback;
    } catch (e) {
      return fallback;
    }
  }

  function safeParseObject(raw, fallback = {}) {
    if (!raw || typeof raw !== 'string') return fallback;
    try {
      const parsed = JSON.parse(raw);
      return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : fallback;
    } catch (e) {
      return fallback;
    }
  }

  function readUiState() {
    const saved = safeParseObject(GM_getValue(KEY_UI, '{}'));
    return {
      ...UI_DEFAULTS,
      ...saved,
      width: clamp(saved.width == null ? UI_DEFAULTS.width : saved.width, 340, 680),
      height: clamp(saved.height == null ? UI_DEFAULTS.height : saved.height, 360, 900),
      top: Number.isFinite(Number(saved.top)) ? Number(saved.top) : UI_DEFAULTS.top,
      left: Number.isFinite(Number(saved.left)) ? Number(saved.left) : null,
      right: Number.isFinite(Number(saved.right)) ? Number(saved.right) : UI_DEFAULTS.right,
      minimized: saved.minimized === true,
      viewMode: saved.viewMode === 'all' ? 'all' : 'today',
      categoryFilter: typeof saved.categoryFilter === 'string' ? saved.categoryFilter : 'all',
    };
  }

  let uiState = readUiState();

  function saveUiState(patch = {}, immediate = false) {
    uiState = { ...uiState, ...patch };
    clearTimeout(uiSaveTimer);
    const write = () => GM_setValue(KEY_UI, JSON.stringify(uiState));
    if (immediate) write();
    else uiSaveTimer = setTimeout(write, 250);
  }

  function readCollectedArray(rawValue) {
    const raw = rawValue == null ? GM_getValue(KEY_COLLECTED, '[]') : rawValue;
    return safeParseArray(raw)
      .map(item => {
        if (typeof item === 'string') return { url: item, ts: 0, cat: DEFAULT_CAT };
        if (item && typeof item.url === 'string') {
          return { url: item.url, ts: Number(item.ts) || 0, cat: item.cat || DEFAULT_CAT };
        }
        return null;
      })
      .filter(Boolean);
  }

  function readDeletedArray(rawValue) {
    const raw = rawValue == null ? GM_getValue(KEY_DELETED, '[]') : rawValue;
    return safeParseArray(raw).filter(url => typeof url === 'string');
  }

  function mergeCollectedArray(items) {
    let changed = false;
    items.forEach(({ url, ts, cat }) => {
      if (!url || deleted.has(url)) return;
      const local = collected.get(url);
      if (!local) {
        collected.set(url, { ts: ts || 0, cat: cat || DEFAULT_CAT });
        changed = true;
        return;
      }
      if ((ts || 0) > (local.ts || 0)) {
        local.ts = ts || 0;
        local.cat = cat || local.cat || DEFAULT_CAT;
        changed = true;
        return;
      }
      if (cat && local.cat === DEFAULT_CAT && cat !== local.cat) {
        local.cat = cat;
        changed = true;
      }
    });
    return changed;
  }

  function applyDeletedArray(urls) {
    let changed = false;
    urls.forEach(url => {
      if (typeof url !== 'string') return;
      if (!deleted.has(url)) {
        deleted.add(url);
        changed = true;
      }
      if (collected.delete(url)) changed = true;
      sessionNew.delete(url);
    });
    return changed;
  }

  function serializeCollected() {
    return [...collected.entries()].map(([url, value]) => ({
      url,
      ts: Number(value.ts) || 0,
      cat: value.cat || DEFAULT_CAT,
    }));
  }

  function saveCollected(immediate = false) {
    const write = () => {
      mergeCollectedArray(readCollectedArray());
      GM_setValue(KEY_COLLECTED, JSON.stringify(serializeCollected()));
    };
    clearTimeout(saveCollected.timer);
    if (immediate) write();
    else saveCollected.timer = setTimeout(write, 500);
  }

  function saveDeleted() {
    GM_setValue(KEY_DELETED, JSON.stringify([...deleted]));
  }

  function saveCategories() {
    GM_setValue(KEY_CATEGORIES, JSON.stringify(categories));
  }

  applyDeletedArray(readDeletedArray());
  const savedCats = safeParseArray(GM_getValue(KEY_CATEGORIES, '[]'));
  if (savedCats.length > 0) {
    categories = savedCats.filter(cat => typeof cat === 'string' && cat.trim());
    if (!categories.includes(DEFAULT_CAT)) categories.unshift(DEFAULT_CAT);
  }
  currentCat = GM_getValue(KEY_CURRENT_CAT, DEFAULT_CAT);
  if (!categories.includes(currentCat)) currentCat = DEFAULT_CAT;
  mergeCollectedArray(readCollectedArray());
  paused = GM_getValue(KEY_PAUSED, '1') === '1';

  function todayKey() {
    return tsToDateKey(Date.now());
  }

  function yesterdayKey() {
    return tsToDateKey(Date.now() - 86400000);
  }

  function tsToDateKey(ts) {
    if (!ts) return '更早';
    return new Date(ts).toLocaleDateString('zh-CN', {
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
    }).replace(/\//g, '-');
  }

  function tsToTimeStr(ts) {
    if (!ts) return '';
    return new Date(ts).toLocaleTimeString('zh-CN', {
      hour: '2-digit',
      minute: '2-digit',
    });
  }

  function friendlyDateLabel(key) {
    if (key === todayKey()) return `今天 ${key}`;
    if (key === yesterdayKey()) return `昨天 ${key}`;
    if (key === '更早') return '更早（无时间记录）';
    return key;
  }

  function updateCurrentVideoId() {
    try {
      const mid = new URL(location.href).searchParams.get('modal_id');
      if (mid && /^\d+$/.test(mid)) {
        currentVideoId = mid;
        return;
      }
    } catch (e) {}
    const match = location.href.match(VIDEO_URL_RE);
    currentVideoId = match ? match[1] : '';
  }

  function videoIdFromUrl(url) {
    const match = url.match(/\/video\/(\d+)/);
    return match ? match[1] : url;
  }

  function addUrl(url) {
    if (collected.has(url) || deleted.has(url)) return false;
    collected.set(url, { ts: Date.now(), cat: currentCat });
    sessionNew.add(url);
    saveCollected();
    return true;
  }

  function collectUrl(rawUrl) {
    if (paused) return false;
    const match = rawUrl.match(VIDEO_URL_RE);
    return match ? addUrl(`https://www.douyin.com/video/${match[1]}`) : false;
  }

  function collectFromModalId(urlStr) {
    if (paused) return false;
    try {
      const modalId = new URL(urlStr).searchParams.get('modal_id');
      return modalId && /^\d+$/.test(modalId)
        ? addUrl(`https://www.douyin.com/video/${modalId}`)
        : false;
    } catch (e) {
      return false;
    }
  }

  function showStatus(text, timeout = 1800) {
    if (toolStatusEl) toolStatusEl.textContent = text;
    if (statusEl && text) {
      statusEl.textContent = text;
      clearTimeout(toastTimer);
      toastTimer = setTimeout(() => applyPauseStyle(), timeout);
    }
  }

  function copyUrls(urls, label = '链接') {
    if (!urls.length) {
      showStatus('当前没有可复制的链接');
      return;
    }
    GM_setClipboard(urls.join('\n'));
    showStatus(`已复制 ${urls.length} 条${label}`);
  }

  function normalizeMappingText(value) {
    return String(value || '').replace(/\s+/g, '').replace(/pu/ig, 'PU');
  }

  function isReplenishmentCategory(category) {
    return /补链|补充链接|replacement/i.test(String(category || ''));
  }

  function inferV12Mapping(category, allowPrompt = true) {
    const normalized = normalizeMappingText(category);
    const specialKey = Object.keys(V12_SPECIAL_MAPPINGS).find(key => normalizeMappingText(key) === normalized);
    if (specialKey) {
      const mapping = { ...V12_SPECIAL_MAPPINGS[specialKey] };
      v12Mappings[category] = mapping;
      GM_setValue(KEY_V12_MAPPINGS, JSON.stringify(v12Mappings));
      return mapping;
    }
    if (v12Mappings[category]) return v12Mappings[category];
    const project = V12_PROJECTS.find(name => normalized.startsWith(normalizeMappingText(name)));
    const material = V12_MATERIALS.find(name => normalized.endsWith(normalizeMappingText(name)));
    if (project && material) {
      const mapping = { project, material, templateId: V12_TEMPLATES[project] };
      v12Mappings[category] = mapping;
      GM_setValue(KEY_V12_MAPPINGS, JSON.stringify(v12Mappings));
      return mapping;
    }
    if (!allowPrompt) return null;
    const answer = prompt(
      `分类“${category}”无法自动识别。\n请输入：项目|材料\n例如：ProjectB|碳岩板`,
      ''
    );
    if (!answer) return null;
    const [rawProject, rawMaterial] = answer.split('|').map(item => String(item || '').trim());
    const exactProject = V12_PROJECTS.find(item => normalizeMappingText(item) === normalizeMappingText(rawProject));
    const exactMaterial = V12_MATERIALS.find(item => normalizeMappingText(item) === normalizeMappingText(rawMaterial));
    if (!exactProject || !exactMaterial || !V12_TEMPLATES[exactProject]) {
      alert(`映射无效。\n支持项目：${V12_PROJECTS.join('、')}\n支持材料：${V12_MATERIALS.join('、')}`);
      return null;
    }
    const mapping = { project: exactProject, material: exactMaterial, templateId: V12_TEMPLATES[exactProject] };
    v12Mappings[category] = mapping;
    GM_setValue(KEY_V12_MAPPINGS, JSON.stringify(v12Mappings));
    return mapping;
  }

  function inferV13Mapping(category, allowPrompt = true) {
    const normalized = normalizeMappingText(category);
    const examplebrandProduct = V13_ExampleBrand_PRODUCTS.find(
      product => normalizeMappingText(product) === normalized
        || normalized === `ExampleBrand${normalizeMappingText(product)}`
        || normalized === `自然流${normalizeMappingText(product)}`
    );
    if (examplebrandProduct) {
      return {
        batchKind: V13_ExampleBrand_BATCH_KIND,
        project: 'ExampleBrand',
        material: examplebrandProduct,
        product: examplebrandProduct,
        templateId: 'examplebrand-natural-flow',
      };
    }
    const formal = inferV12Mapping(category, allowPrompt);
    return formal ? { ...formal, batchKind: V13_FORMAL_BATCH_KIND } : null;
  }

  function isV12Sent(url) {
    return Boolean(v12Sent[videoIdFromUrl(url)]);
  }

  function saveV12Sent() {
    GM_setValue(KEY_V12_SENT, JSON.stringify(v12Sent));
  }

  function savePendingImports() {
    GM_setValue(KEY_V13_PENDING_IMPORTS, JSON.stringify(pendingImports));
  }

  function pendingImportForUrl(url) {
    const key = videoIdFromUrl(url);
    if (!key) return null;
    return Object.values(pendingImports).find(entry =>
      entry && Array.isArray(entry.items) && entry.items.some(item => videoIdFromUrl(item.url) === key)
    ) || null;
  }

  function resetV12SentForDate(dateKey, askFirst = true) {
    const matched = Object.entries(v12Sent).filter(([, value]) => {
      const sentAt = Number(value && value.sentAt) || 0;
      return sentAt && tsToDateKey(sentAt) === dateKey;
    });
    if (!matched.length) {
      if (askFirst) alert(`${dateKey} 没有可重置的V13已导入标记。`);
      return 0;
    }
    if (askFirst && !confirm(`将重置 ${dateKey} 的 ${matched.length} 条“已导入V13”标记。\n不会删除链接，也不会撤销家里工作台任务。继续吗？`)) return 0;
    matched.forEach(([id]) => delete v12Sent[id]);
    saveV12Sent();
    if (panel && listEl) renderPanel();
    if (askFirst) alert(`已重置 ${matched.length} 条。它们现在可以重新导入V13。`);
    return matched.length;
  }

  function receiverSettings() {
    const current = safeParseObject(GM_getValue(KEY_V12_SETTINGS, '{}'));
    const legacy = safeParseObject(GM_getValue(KEY_LEGACY_V12_SETTINGS, '{}'));
    const merged = { ...legacy, ...current };
    return {
      bridgeUrl: V13_LOCAL_BRIDGE,
      // Kept only for backwards-compatible settings migration.  The V13.3
      // collector never sends a token or a direct request to the home receiver.
      receiverUrl: V13_LOCAL_BRIDGE,
      accessToken: '',
    };
  }

  function bridgeRequest(path, body = null, method = 'POST') {
    const target = receiverSettings();
    const maxAttempts = 3;
    const requestAttempt = attempt => new Promise((resolve, reject) => {
      GM_xmlhttpRequest({
        method,
        url: `${target.bridgeUrl}${path}`,
        headers: {
          'Content-Type': 'application/json',
        },
        data: body == null ? undefined : JSON.stringify(body),
        timeout: 8000,
        onload(response) {
          let payload = {};
          try { payload = JSON.parse(response.responseText || '{}'); } catch (e) {}
          if (response.status >= 200 && response.status < 300 && payload.ok !== false) {
            resolve(payload);
            return;
          }
          // HTTP validation/auth/contract failures are deterministic and must
          // not be repeated.  Only transport failures below are retried.
          reject(new Error(payload.error || `桥接器返回 HTTP ${response.status}`));
        },
        onerror() {
          if (attempt < maxAttempts) {
            showStatus(`统一递交器导入桥暂时无响应，正在重试（${attempt + 1}/${maxAttempts}）…`, 5000);
            setTimeout(() => requestAttempt(attempt + 1).then(resolve, reject), attempt * 1200);
            return;
          }
          reject(new Error(
            `无法连接统一任务递交器导入桥：${target.bridgeUrl}。` +
            '请先在当前电脑打开V13.3统一任务递交器，然后重试。'
          ));
        },
        ontimeout() {
          if (attempt < maxAttempts) {
            showStatus(`统一递交器导入桥响应超时，正在重试（${attempt + 1}/${maxAttempts}）…`, 5000);
            setTimeout(() => requestAttempt(attempt + 1).then(resolve, reject), attempt * 1200);
            return;
          }
          reject(new Error(`统一递交器导入桥响应超时：${target.bridgeUrl}`));
        },
      });
    });
    // /api/imports is idempotent by importId, so retrying a lost response
    // cannot duplicate rows in either child submitter.
    return requestAttempt(1);
  }

  function localBridgeSession() {
    return bridgeRequest('/api/session', null, 'GET');
  }

  function defaultProductionDate() {
    const now = new Date();
    return `${now.getMonth() + 1}.${now.getDate()}`;
  }

  function openV12SettingsDialog(groups) {
    const saved = {
      ...safeParseObject(GM_getValue(KEY_LEGACY_V12_SETTINGS, '{}')),
      ...safeParseObject(GM_getValue(KEY_V12_SETTINGS, '{}')),
    };
    return new Promise(resolve => {
      const overlay = document.createElement('div');
      overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:2147483646;display:flex;align-items:center;justify-content:center;padding:18px;';
      const card = document.createElement('div');
      card.style.cssText = 'width:min(620px,96vw);max-height:88vh;overflow:auto;background:#fff;border-radius:12px;box-shadow:0 18px 60px rgba(0,0,0,.35);padding:18px;font:13px system-ui;color:#333;';
      const rows = groups.map(group => {
        const fixed = group.mapping.batchKind === V13_ExampleBrand_BATCH_KIND
          ? 'ExampleBrand 21条合计'
          : `${V13_PROJECT_EXPECTED_COUNTS[group.mapping.project] || '?'}条固定合同`;
        return `<tr><td>${escapeHtml(group.category)}</td><td>${escapeHtml(group.mapping.project)}</td><td>${escapeHtml(group.mapping.material)}</td><td style="text-align:right">${group.items.length} / ${fixed}</td></tr>`;
      }).join('');
      card.innerHTML = `
        <h3 style="margin:0 0 6px;font-size:18px">传入 V13.3 统一任务递交器</h3>
        <div style="color:#777;margin-bottom:10px">只把合同传入公司本机递交器任务框，不直接提交家里接收器，也不会启动工作台或下载；正式项目和 ExampleBrand 会分成独立子批次。</div>
        <table style="width:100%;border-collapse:collapse;margin-bottom:12px"><thead><tr><th>采集分类</th><th>项目</th><th>产品/材料</th><th>链接/合同</th></tr></thead><tbody>${rows}</tbody></table>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px">
          <label>生产日期<input data-v12="date" value="${escapeHtml(defaultProductionDate())}" style="display:block;width:100%;box-sizing:border-box;padding:7px;margin-top:4px"></label>
          <label>音频批次<input data-v12="audio" value="${escapeHtml(defaultProductionDate())}" style="display:block;width:100%;box-sizing:border-box;padding:7px;margin-top:4px"></label>
          <label>语音方案<select data-v12="voice" style="display:block;width:100%;padding:7px;margin-top:4px"><option value="reuse_existing">复用已有语音</option><option value="create_new">创建新语音</option></select></label>
          <label>发送范围<select data-v12="scope" style="display:block;width:100%;padding:7px;margin-top:4px"><option value="today_unsent">今天且尚未发送（推荐）</option><option value="visible_unsent">当前筛选且尚未发送</option></select></label>
          <label style="grid-column:1 / -1">本地导入桥<input data-v12="receiver" value="${V13_LOCAL_BRIDGE}" readonly style="display:block;width:100%;box-sizing:border-box;padding:7px;margin-top:4px;background:#f4f6f8;color:#667085"></label>
        </div>
        <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:16px"><button data-v12="cancel">取消</button><button data-v12="ok" style="background:${BRAND};color:#fff;border:0;border-radius:6px;padding:7px 16px">下一步对账</button></div>`;
      overlay.appendChild(card);
      document.body.appendChild(overlay);
      const voice = card.querySelector('[data-v12="voice"]');
      voice.value = saved.voicePlan === 'create_new' ? 'create_new' : 'reuse_existing';
      const scope = card.querySelector('[data-v12="scope"]');
      scope.value = saved.scope === 'visible_unsent' ? 'visible_unsent' : 'today_unsent';
      const finish = value => { overlay.remove(); resolve(value); };
      card.querySelector('[data-v12="cancel"]').onclick = () => finish(null);
      card.querySelector('[data-v12="ok"]').onclick = () => {
        const settings = {
          date: card.querySelector('[data-v12="date"]').value.trim(),
          audioBatch: card.querySelector('[data-v12="audio"]').value.trim(),
          voicePlan: voice.value,
          scope: scope.value,
          receiverUrl: V13_LOCAL_BRIDGE,
          accessToken: '',
        };
        if (!settings.date || !settings.audioBatch) return alert('生产日期和音频批次不能为空');
        GM_setValue(KEY_V12_SETTINGS, JSON.stringify(settings));
        finish(settings);
      };
    });
  }

  function escapeHtml(value) {
    return String(value || '').replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));
  }

  function candidateItems(category, scope) {
    const base = scope === 'visible_unsent'
      ? allFilteredItems()
      : [...collected.entries()].map(([url, value]) => ({ url, ts: value.ts, cat: value.cat || DEFAULT_CAT })).filter(item => tsToDateKey(item.ts) === todayKey());
    return base.filter(item => item.cat === category && !isV12Sent(item.url) && !pendingImportForUrl(item.url));
  }

  function buildV12Groups(categoriesToSend, scope) {
    const groups = [];
    for (const category of categoriesToSend) {
      if (category === DEFAULT_CAT || isReplenishmentCategory(category)) continue;
      const items = candidateItems(category, scope);
      if (!items.length) continue;
      const mapping = inferV13Mapping(category, true);
      if (!mapping) throw new Error(`分类“${category}”尚未完成项目/材料映射`);
      groups.push({ category, mapping, items });
    }
    return groups;
  }

  function newImportId(prefix) {
    const random = globalThis.crypto && typeof globalThis.crypto.randomUUID === 'function'
      ? globalThis.crypto.randomUUID().replace(/-/g, '').slice(0, 10)
      : Math.random().toString(16).slice(2, 12);
    return `${prefix}-${Date.now()}-${random}`;
  }

  function rememberPendingImport(importId, groups, payloads, runGroupId) {
    const batchIds = Object.fromEntries(payloads.map(payload => [String(payload.batchKind || ''), String(payload.batchId || '')]));
    pendingImports[importId] = {
      importId,
      runGroupId,
      expectedKinds: payloads.map(payload => String(payload.batchKind || '')).filter(Boolean),
      items: groups.flatMap(group => group.items.map(item => ({
        url: item.url,
        batchKind: String(group.mapping.batchKind || V13_FORMAL_BATCH_KIND),
        batchId: batchIds[String(group.mapping.batchKind || V13_FORMAL_BATCH_KIND)] || '',
      }))),
      createdAt: Date.now(),
    };
    savePendingImports();
  }

  function markPendingImportSent(entry) {
    if (!entry || !Array.isArray(entry.items)) return;
    entry.items.forEach(item => {
      const id = videoIdFromUrl(item.url);
      if (!id) return;
      v12Sent[id] = {
        batchId: String(item.batchId || ''),
        runGroupId: String(entry.runGroupId || ''),
        sentAt: Date.now(),
      };
    });
    saveV12Sent();
  }

  function removePendingImport(importId) {
    if (!pendingImports[importId]) return;
    delete pendingImports[importId];
    savePendingImports();
  }

  function wait(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
  }

  async function waitForImportLoaded(importId, expectedKinds, timeoutMs = 15000) {
    const deadline = Date.now() + timeoutMs;
    let latest = null;
    while (Date.now() <= deadline) {
      try {
        latest = await bridgeRequest(`/api/imports/${encodeURIComponent(importId)}`, null, 'GET');
        const childStatus = latest && latest.childStatus && typeof latest.childStatus === 'object' ? latest.childStatus : {};
        const failedKind = expectedKinds.find(kind => ['blocked', 'error'].includes(String(childStatus[kind] && childStatus[kind].state || '')));
        if (failedKind) {
          const detail = childStatus[failedKind] && childStatus[failedKind].error;
          return { loaded: false, failed: true, status: latest, error: `${failedKind}任务框未加载${detail ? `：${detail}` : ''}` };
        }
        if (expectedKinds.length && expectedKinds.every(kind => String(childStatus[kind] && childStatus[kind].state || '') === 'loaded')) {
          return { loaded: true, status: latest };
        }
      } catch (error) {
        latest = { error: error && error.message ? error.message : String(error) };
      }
      await wait(Math.min(900, Math.max(150, deadline - Date.now())));
    }
    return { loaded: false, timedOut: true, status: latest };
  }

  async function resumePendingImports() {
    const now = Date.now();
    const entries = Object.values(pendingImports).filter(entry => entry && entry.importId && now - Number(entry.createdAt || now) < 7 * 24 * 3600 * 1000);
    for (const entry of entries) {
      const expectedKinds = Array.isArray(entry.expectedKinds) ? entry.expectedKinds : [];
      if (!expectedKinds.length) { removePendingImport(entry.importId); continue; }
      try {
        const result = await waitForImportLoaded(entry.importId, expectedKinds, 2500);
        if (result.loaded) {
          markPendingImportSent(entry);
          removePendingImport(entry.importId);
          if (panel && listEl) renderPanel();
          showStatus(`导入记录 ${entry.importId} 已由递交器确认加载`, 5000);
        } else if (result.failed) {
          removePendingImport(entry.importId);
        }
      } catch (error) {
        // The bridge may be closed while the collector page is open; retain
        // the durable import and retry on the next page refresh/focus.
      }
    }
    const stale = Object.values(pendingImports).filter(entry => entry && now - Number(entry.createdAt || now) >= 7 * 24 * 3600 * 1000);
    stale.forEach(entry => removePendingImport(entry.importId));
  }

  function assertUniqueGroupUrls(groups) {
    const seen = new Map();
    groups.forEach(group => group.items.forEach(item => {
      const identity = videoIdFromUrl(item.url);
      const previous = seen.get(identity);
      if (previous) throw new Error(`链接重复：${group.category} 中的视频与 ${previous} 重复`);
      seen.set(identity, group.category);
    }));
  }

  function assertFormalProjectTotals(groups) {
    const totals = {};
    groups.forEach(group => {
      const project = group.mapping.project;
      totals[project] = (totals[project] || 0) + group.items.length;
    });
    const issues = [];
    Object.entries(totals).forEach(([project, actual]) => {
      const expected = V13_PROJECT_EXPECTED_COUNTS[project];
      if (!expected) issues.push(`不支持的正式项目：${project}`);
      else if (actual !== expected) issues.push(`${project} 固定需要 ${expected} 条，当前只有 ${actual} 条`);
    });
    if (issues.length) throw new Error(`正式项目固定合同未满足：\n${issues.join('\n')}`);
    return totals;
  }

  function formalTaskPayload(groups, settings, runGroupId) {
    const batchId = newImportId('formal-batch');
    const selectedProjects = [...new Set(groups.map(group => group.mapping.project))];
    const tasks = groups.map((group, index) => {
      const taskId = `${batchId}-${String(index + 1).padStart(2, '0')}`;
      const isPhysical = group.mapping.project === 'ProjectB';
      return {
        schemaVersion: 2,
        batchKind: V13_FORMAL_BATCH_KIND,
        runGroupId,
        selectedProjects,
        batchId,
        batchSequence: index + 1,
        batchSize: groups.length,
        taskId,
        canonicalTaskId: taskId,
        status: 'pending_confirmation',
        date: settings.date,
        productionDate: settings.date,
        templateId: group.mapping.templateId,
        project: group.mapping.project,
        material: group.mapping.material,
        urls: group.items.map(item => item.url),
        expectedCount: group.items.length,
        audioBatch: isPhysical ? settings.date : settings.audioBatch,
        voicePlan: isPhysical ? 'create_new' : settings.voicePlan,
        coverTemplateMode: 'auto_balanced',
        sourceCategory: group.category,
        source: 'douyin_collector_v2',
      };
    });
    return {
      schemaVersion: 2,
      batchKind: V13_FORMAL_BATCH_KIND,
      runGroupId,
      batchId,
      productionDate: settings.date,
      selectedProjects,
      projectExpectedCount: selectedProjects.reduce((sum, project) => sum + V13_PROJECT_EXPECTED_COUNTS[project], 0),
      startMode: 'pending_confirmation',
      voicePlanIntent: settings.voicePlan,
      audioBatchIntent: settings.audioBatch,
      tasks,
    };
  }

  function examplebrandOutputName(product, language, account, outputIndex, shortId) {
    return `ExampleBrand_${V13_ExampleBrand_PRODUCT_SLUGS[product]}_${language}_${account}_${String(outputIndex).padStart(2, '0')}_${shortId.slice(0, 10)}.mp4`;
  }

  function examplebrandTaskPayload(groups, settings, runGroupId) {
    const total = groups.reduce((sum, group) => sum + group.items.length, 0);
    if (total !== 21) throw new Error(`ExampleBrand每批必须正好21条，当前为${total}条`);
    const batchId = newImportId('examplebrand-batch');
    let globalIndex = 0;
    const tasks = groups.map((group, index) => {
      const product = group.mapping.product;
      const taskId = `${batchId}-${V13_ExampleBrand_PRODUCT_SLUGS[product]}`;
      const languageCounts = { en: 0, es: 0 };
      const outputPlan = group.items.map((item, localIndex) => {
        const account = V13_ExampleBrand_ACCOUNT_SLOTS[globalIndex];
        const language = account === 'ES-01' ? 'es' : 'en';
        const canonicalTaskId = `${taskId}:out-${String(localIndex + 1).padStart(2, '0')}`;
        const outputIndex = globalIndex + 1;
        globalIndex += 1;
        languageCounts[language] += 1;
        return {
          sourceUrl: item.url,
          product,
          language,
          languageName: language === 'es' ? '西班牙语' : '英语',
          channelAccount: account,
          outputIndex,
          canonicalTaskId,
          outputName: examplebrandOutputName(product, language, account, outputIndex, canonicalTaskId),
        };
      });
      return {
        schemaVersion: 2,
        batchKind: V13_ExampleBrand_BATCH_KIND,
        runGroupId,
        selectedProjects: ['ExampleBrand'],
        batchId,
        batchSequence: index + 1,
        batchSize: groups.length,
        taskId,
        canonicalTaskId: taskId,
        status: 'pending_confirmation',
        date: settings.date,
        productionDate: settings.date,
        project: 'ExampleBrand',
        material: product,
        product,
        urls: group.items.map(item => item.url),
        expectedCount: group.items.length,
        languagePlan: [
          languageCounts.en ? { code: 'en', name: '英语', audioCount: 1, outputCount: languageCounts.en, voice: 'chand' } : null,
          languageCounts.es ? { code: 'es', name: '西班牙语', audioCount: 1, outputCount: languageCounts.es, voice: 'Farid Dieck' } : null,
        ].filter(Boolean),
        outputPlan,
        voicePlan: 'create_new',
        audioBatch: settings.date,
        pipelineProfile: {
          kind: V13_ExampleBrand_BATCH_KIND,
          logo: 'ExampleBrand',
          finalFolder: 'ExampleBrand',
          requiresExamplebrandAssets: true,
          remoteDirectoryRule: `AI视频生产线/${settings.date}/ExampleBrand`,
          outputDirectoryRule: `E:/生成视频/${settings.date}/ExampleBrand`,
        },
        sourceCategory: group.category,
        source: 'douyin_collector_v2',
      };
    });
    return {
      schemaVersion: 2,
      batchKind: V13_ExampleBrand_BATCH_KIND,
      runGroupId,
      selectedProjects: ['ExampleBrand'],
      batchId,
      productionDate: settings.date,
      startMode: 'pending_confirmation',
      examplebrandSummary: {
        total: 21,
        english: 18,
        spanish: 3,
        accounts: Object.fromEntries([...new Set(V13_ExampleBrand_ACCOUNT_SLOTS)].map(account => [account, 3])),
      },
      tasks,
    };
  }

  function buildImportPayloads(groups, settings, runGroupId) {
    assertUniqueGroupUrls(groups);
    if (!runGroupId) throw new Error('统一递交器没有返回当前运行组ID');
    const formalGroups = groups.filter(group => group.mapping.batchKind === V13_FORMAL_BATCH_KIND);
    const examplebrandGroups = groups.filter(group => group.mapping.batchKind === V13_ExampleBrand_BATCH_KIND);
    const payloads = [];
    if (formalGroups.length) {
      assertFormalProjectTotals(formalGroups);
      payloads.push(formalTaskPayload(formalGroups, settings, runGroupId));
    }
    if (examplebrandGroups.length) payloads.push(examplebrandTaskPayload(examplebrandGroups, settings, runGroupId));
    if (!payloads.length) throw new Error('没有可导入的正式项目或ExampleBrand产品');
    return { runGroupId, payloads };
  }

  function preflightSummary(payload, result) {
    const total = payload.tasks.reduce((sum, task) => sum + Number(task.expectedCount || task.urls.length || 0), 0);
    if (payload.batchKind === V13_ExampleBrand_BATCH_KIND) {
      return `ExampleBrand：${result.totalExpected || total}/21 条，英语18、西语3，7个账号各3条`;
    }
    const projects = Object.entries(result.projectTotals || {}).map(([project, count]) => `${project} ${count}条`);
    return `正式项目：${projects.join('、')}（合计${result.totalExpected || total}条）`;
  }

  async function sendCategoriesToV12(categoriesToSend) {
    try {
      let initialGroups = buildV12Groups(categoriesToSend, 'today_unsent');
      if (!initialGroups.length) initialGroups = buildV12Groups(categoriesToSend, 'visible_unsent');
      if (!initialGroups.length) throw new Error('今天或当前筛选中没有尚未发送的有效分类链接');
      const settings = await openV12SettingsDialog(initialGroups);
      if (!settings) return;
      const groups = buildV12Groups(categoriesToSend, settings.scope);
      if (!groups.length) throw new Error('所选范围没有尚未发送的链接');
      const pending = groups.flatMap(group => group.items.map(item => pendingImportForUrl(item.url))).filter(Boolean);
      if (pending.length) {
        const ids = [...new Set(pending.map(entry => entry.importId).filter(Boolean))];
        throw new Error(`已有导入正在等待递交器确认：${ids.join('、')}。请等待任务框加载或刷新页面后再试。`);
      }
      showStatus('正在连接 V13.3 统一递交器…', 5000);
      const session = await localBridgeSession();
      if (String(session.protocolVersion || '') !== V13_BRIDGE_PROTOCOL) {
        throw new Error(`统一递交器协议不兼容：需要 ${V13_BRIDGE_PROTOCOL}，实际为 ${session.protocolVersion || '未知'}。请升级统一递交器。`);
      }
      const runGroupId = String(session.runGroupId || '').trim();
      const { payloads } = buildImportPayloads(groups, settings, runGroupId);
      const lines = payloads.map(payload => {
        const total = payload.tasks.reduce((sum, task) => sum + Number(task.expectedCount || task.urls.length || 0), 0);
        if (payload.batchKind === V13_ExampleBrand_BATCH_KIND) return `ExampleBrand：${total}/21 条，英语18、西语3，7个账号各3条`;
        const projects = [...new Set(payload.tasks.map(task => `${task.project} ${task.expectedCount}条`))];
        return `正式项目：${projects.join('、')}（合计${total}条）`;
      });
      const total = payloads.reduce((sum, payload) => sum + payload.tasks.reduce((n, task) => n + Number(task.expectedCount || 0), 0), 0);
      if (!confirm(`V13.3 导入前对账\n\n运行组：${runGroupId}\n${lines.join('\n')}\n\n合计 ${total} 条。\n链接将传入本机递交器任务框，仍需你检查后手动提交。\n\n确认传入吗？`)) return;
      const importId = newImportId('import');
      const envelope = {
        protocolVersion: V13_BRIDGE_PROTOCOL,
        importId,
        sessionId: String(session.sessionId || ''),
        runGroupId,
        productionDate: settings.date,
        voicePlanIntent: settings.voicePlan,
        audioBatchIntent: settings.audioBatch,
        payloads,
      };
      const result = await bridgeRequest('/api/imports', envelope);
      const pendingEntry = {
        importId: result.importId || importId,
        runGroupId,
        expectedKinds: payloads.map(payload => String(payload.batchKind || '')).filter(Boolean),
        items: groups.flatMap(group => {
          const payload = payloads.find(item => String(item.batchKind || '') === String(group.mapping.batchKind || V13_FORMAL_BATCH_KIND));
          return group.items.map(item => ({
            url: item.url,
            batchKind: String(group.mapping.batchKind || V13_FORMAL_BATCH_KIND),
            batchId: payload ? String(payload.batchId || '') : '',
          }));
        }),
        createdAt: Date.now(),
      };
      pendingImports[pendingEntry.importId] = pendingEntry;
      savePendingImports();
      const loaded = await waitForImportLoaded(pendingEntry.importId, pendingEntry.expectedKinds, 15000);
      if (loaded.loaded) {
        markPendingImportSent(pendingEntry);
        removePendingImport(pendingEntry.importId);
      } else if (loaded.failed) {
        removePendingImport(pendingEntry.importId);
        throw new Error(loaded.error || '任务框拒绝加载导入记录');
      } else {
        alert(`导入记录已保存，但任务框加载较慢：${pendingEntry.importId}\n请保持统一递交器和对应窗口打开；刷新本页面后会自动查询，不要重复传入。`);
        showStatus(`导入已保存，等待任务框确认：${pendingEntry.importId}`, 7000);
        return;
      }
      renderPanel();
      const batchText = payloads.map(payload => `${payload.batchKind || 'formal'}=${payload.batchId}`).join('\n');
      alert(`链接已传入V13.3统一递交器：${total} 条。\n运行组：${runGroupId}\n${batchText}\n导入记录：${result.importId || importId}\n\n对应任务框已打开，请检查合同后手动点击提交。不会自动下载或启动工作台。`);
      showStatus(`V13.3 已传入 ${total} 条待检查任务`, 5000);
    } catch (error) {
      alert(`V13.3传入失败：\n${error && error.message ? error.message : error}`);
      showStatus('V13.3传入失败', 4000);
    }
  }

  async function undoLatestV12Import() {
    alert('V13任务已进入接收器。撤回请在接收器或Dashboard中按任务批次执行，采集器不会直接删除已保存的任务。');
  }

  function downloadText(filename, text, mime = 'text/plain;charset=utf-8') {
    const blob = new Blob([text], { type: mime });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  function allFilteredItems() {
    const query = (searchInput ? searchInput.value : '').trim().toLowerCase();
    const filterCat = uiState.categoryFilter || 'all';
    return [...collected.entries()]
      .map(([url, value]) => ({ url, ts: value.ts, cat: value.cat || DEFAULT_CAT }))
      .filter(item => {
        if (uiState.viewMode === 'today' && tsToDateKey(item.ts) !== todayKey()) return false;
        if (filterCat !== 'all' && item.cat !== filterCat) return false;
        if (!query) return true;
        const haystack = `${item.url} ${videoIdFromUrl(item.url)} ${item.cat} ${tsToDateKey(item.ts)}`.toLowerCase();
        return haystack.includes(query);
      })
      .sort((a, b) => (b.ts || 0) - (a.ts || 0));
  }

  function itemsForCopy(scope) {
    const all = [...collected.entries()]
      .map(([url, value]) => ({ url, ts: value.ts, cat: value.cat || DEFAULT_CAT }))
      .sort((a, b) => (b.ts || 0) - (a.ts || 0));
    if (scope === 'visible') return allFilteredItems();
    if (scope === 'today') return all.filter(item => tsToDateKey(item.ts) === todayKey());
    if (scope === 'category') return all.filter(item => item.cat === currentCat);
    return all;
  }

  function itemsForBatchScope(scope) {
    return itemsForCopy(scope || 'visible');
  }

  function pruneSelectedUrls() {
    [...selectedUrls].forEach(url => {
      if (!collected.has(url) || deleted.has(url)) selectedUrls.delete(url);
    });
  }

  function updateBatchSelectionInfo() {
    if (batchSelectionInfo) batchSelectionInfo.textContent = `已勾选 ${selectedUrls.size} 条`;
    if (batchTargetSelect) batchTargetSelect.disabled = selectedUrls.size === 0;
  }

  function rebuildBatchTargetSelect() {
    if (!batchTargetSelect) return;
    const previous = batchTargetSelect.value;
    batchTargetSelect.innerHTML = '';
    const placeholder = document.createElement('option');
    placeholder.value = '';
    placeholder.textContent = '移动到分类…';
    batchTargetSelect.appendChild(placeholder);
    categories.forEach(cat => {
      const option = document.createElement('option');
      option.value = cat;
      option.textContent = cat;
      batchTargetSelect.appendChild(option);
    });
    batchTargetSelect.value = categories.includes(previous) ? previous : '';
    updateBatchSelectionInfo();
  }

  function selectBatchScope() {
    const scope = batchScopeSelect ? batchScopeSelect.value : 'visible';
    const items = itemsForBatchScope(scope);
    items.forEach(item => selectedUrls.add(item.url));
    updateBatchSelectionInfo();
    renderPanel();
    showStatus(`已勾选 ${items.length} 条${scope === 'visible' ? '当前结果' : ''}`);
  }

  function clearBatchSelection() {
    selectedUrls.clear();
    updateBatchSelectionInfo();
    renderPanel();
    showStatus('已清空勾选');
  }

  function moveSelectedBatch() {
    pruneSelectedUrls();
    const target = batchTargetSelect ? batchTargetSelect.value : '';
    if (!selectedUrls.size) {
      showStatus('请先勾选链接，或使用“全选范围”');
      return;
    }
    if (!target) {
      showStatus('请先选择目标分类');
      return;
    }
    const urls = [...selectedUrls];
    const movable = urls.filter(url => collected.get(url) && collected.get(url).cat !== target);
    if (!movable.length) {
      showStatus('勾选链接已经在目标分类中');
      return;
    }
    if (!confirm(`确定把 ${movable.length} 条链接移动到“${target}”吗？\n不会删除链接，也不会改变已导入V13标记。`)) return;
    movable.forEach(url => {
      const value = collected.get(url);
      if (value) value.cat = target;
    });
    selectedUrls.clear();
    saveCollected(true);
    rebuildCatSelect();
    rebuildFilterSelect();
    rebuildBatchTargetSelect();
    renderPanel();
    showStatus(`已批量移动 ${movable.length} 条到“${target}”`);
  }

  function copySelectedScope() {
    const scope = copyScopeSelect ? copyScopeSelect.value : 'visible';
    copyUrls(itemsForCopy(scope).map(item => item.url), '链接');
  }

  function copyAll() {
    copyUrls([...collected.keys()], '链接');
  }

  function exportTxt() {
    const scope = copyScopeSelect ? copyScopeSelect.value : 'visible';
    const stamp = new Date().toISOString().slice(0, 10);
    downloadText(`douyin-links-${scope}-${stamp}.txt`, itemsForCopy(scope).map(item => item.url).join('\n'));
    showStatus('TXT 已导出');
  }

  function buildBackup() {
    return {
      format: 'douyin-video-link-collector-backup',
      version: 2,
      exportedAt: new Date().toISOString(),
      collected: serializeCollected(),
      deleted: [...deleted],
      categories: [...categories],
      currentCat,
      paused,
      v12Sent,
      v12Mappings,
      v12Settings: safeParseObject(GM_getValue(KEY_V12_SETTINGS, '{}')),
    };
  }

  function exportJson() {
    const stamp = new Date().toISOString().slice(0, 10);
    downloadText(
      `douyin-link-backup-${stamp}.json`,
      JSON.stringify(buildBackup(), null, 2),
      'application/json;charset=utf-8'
    );
    showStatus('JSON 备份已导出');
  }

  function normalizeBackup(raw) {
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) throw new Error('备份必须是 JSON 对象');
    if (raw.format !== 'douyin-video-link-collector-backup') throw new Error('不是本采集器的备份文件');
    if (!Array.isArray(raw.collected) || !Array.isArray(raw.deleted) || !Array.isArray(raw.categories)) {
      throw new Error('备份缺少必要字段');
    }
    const importedItems = raw.collected.map(item => {
      if (typeof item === 'string') return { url: item, ts: 0, cat: DEFAULT_CAT };
      if (item && typeof item.url === 'string') {
        return { url: item.url, ts: Number(item.ts) || 0, cat: item.cat || DEFAULT_CAT };
      }
      return null;
    }).filter(item => item && VIDEO_URL_RE.test(item.url));
    return {
      collected: importedItems,
      deleted: raw.deleted.filter(url => typeof url === 'string'),
      categories: raw.categories.filter(cat => typeof cat === 'string' && cat.trim()),
      currentCat: typeof raw.currentCat === 'string' ? raw.currentCat : DEFAULT_CAT,
      paused: raw.paused === true || raw.paused === '1',
      v12Sent: raw.v12Sent && typeof raw.v12Sent === 'object' && !Array.isArray(raw.v12Sent) ? raw.v12Sent : {},
      v12Mappings: raw.v12Mappings && typeof raw.v12Mappings === 'object' && !Array.isArray(raw.v12Mappings) ? raw.v12Mappings : {},
      v12Settings: raw.v12Settings && typeof raw.v12Settings === 'object' && !Array.isArray(raw.v12Settings) ? raw.v12Settings : {},
    };
  }

  function applyBackup(data, mode) {
    const importedDeleted = [...new Set(data.deleted)];
    if (mode === 'replace') {
      if (!confirm(`将替换当前 ${collected.size} 条链接、${deleted.size} 条黑名单记录，恢复备份中的 ${data.collected.length} 条链接。继续吗？`)) return;
      if (!confirm('这是不可逆的替换操作，请再次确认。')) return;
      collected.clear();
      sessionNew.clear();
      deleted.clear();
      importedDeleted.forEach(url => deleted.add(url));
      data.collected.forEach(item => {
        if (!deleted.has(item.url)) collected.set(item.url, { ts: item.ts, cat: item.cat || DEFAULT_CAT });
      });
      categories = [...new Set([DEFAULT_CAT, ...data.categories])];
      currentCat = categories.includes(data.currentCat) ? data.currentCat : DEFAULT_CAT;
      paused = data.paused;
      v12Sent = { ...data.v12Sent };
      v12Mappings = { ...data.v12Mappings };
    } else {
      const beforeLinks = collected.size;
      const beforeDeleted = deleted.size;
      importedDeleted.forEach(url => deleted.add(url));
      applyDeletedArray(importedDeleted);
      mergeCollectedArray(data.collected);
      categories = [...new Set([DEFAULT_CAT, ...categories, ...data.categories])];
      v12Sent = { ...v12Sent, ...data.v12Sent };
      v12Mappings = { ...v12Mappings, ...data.v12Mappings };
      showStatus(`已合并 ${collected.size - beforeLinks} 条链接、${deleted.size - beforeDeleted} 条黑名单`);
    }
    saveCollected(true);
    saveDeleted();
    saveCategories();
    GM_setValue(KEY_CURRENT_CAT, currentCat);
    GM_setValue(KEY_PAUSED, paused ? '1' : '0');
    GM_setValue(KEY_V12_SENT, JSON.stringify(v12Sent));
    GM_setValue(KEY_V12_MAPPINGS, JSON.stringify(v12Mappings));
    if (data.v12Settings && Object.keys(data.v12Settings).length) GM_setValue(KEY_V12_SETTINGS, JSON.stringify(data.v12Settings));
    applyPauseStyle();
    rebuildCatSelect();
    renderDeletedManager();
    renderPanel();
  }

  function chooseBackupFile(mode) {
    const input = document.createElement('input');
    input.type = 'file';
    input.accept = '.json,application/json';
    input.onchange = () => {
      const file = input.files && input.files[0];
      if (!file) return;
      const reader = new FileReader();
      reader.onload = () => {
        try {
          const data = normalizeBackup(JSON.parse(String(reader.result || '')));
          const action = mode === 'replace' ? '替换恢复' : '安全合并';
          const linkConflicts = data.collected.filter(item => collected.has(item.url)).length;
          const deletedConflicts = data.deleted.filter(url => deleted.has(url)).length;
          const conflictText = mode === 'replace'
            ? '替换模式会覆盖当前数据。'
            : `其中 ${linkConflicts} 条链接、${deletedConflicts} 条黑名单已存在，合并不会删除本地数据。`;
          if (confirm(`检测到 ${data.collected.length} 条链接、${data.deleted.length} 条黑名单记录。\n${conflictText}\n将执行：${action}。继续吗？`)) {
            applyBackup(data, mode);
          }
        } catch (e) {
          showStatus(`导入失败：${e.message || '文件无效'}`, 3000);
        }
      };
      reader.onerror = () => showStatus('导入失败：无法读取文件', 3000);
      reader.readAsText(file, 'utf-8');
    };
    input.click();
  }

  function styleBtn(btn, color, bg) {
    Object.assign(btn.style, {
      background: bg,
      color,
      border: 'none',
      borderRadius: '5px',
      padding: '4px 8px',
      minHeight: '28px',
      cursor: 'pointer',
      fontSize: '12px',
      whiteSpace: 'nowrap',
      transition: 'filter .15s, transform .05s',
    });
    btn.onmouseenter = () => { btn.style.filter = 'brightness(.94)'; };
    btn.onmouseleave = () => { btn.style.filter = ''; };
    btn.onmousedown = () => { btn.style.transform = 'scale(.98)'; };
    btn.onmouseup = () => { btn.style.transform = ''; };
  }

  function makeButton(text, bg, title = '') {
    const button = document.createElement('button');
    button.textContent = text;
    if (title) button.title = title;
    styleBtn(button, '#fff', bg);
    return button;
  }

  function createPanel() {
    panel = document.createElement('div');
    panel.id = '__douyin_collector__';
    Object.assign(panel.style, {
      position: 'fixed',
      top: `${uiState.top}px`,
      width: `${uiState.width}px`,
      height: `${uiState.height}px`,
      minWidth: '340px',
      maxWidth: 'min(680px, calc(100vw - 16px))',
      minHeight: '360px',
      maxHeight: '90vh',
      boxSizing: 'border-box',
      background: '#fff',
      border: `2px solid ${paused ? COLOR_PAUSE : BRAND}`,
      borderRadius: '12px',
      boxShadow: '0 8px 28px rgba(0,0,0,.20)',
      zIndex: '99999',
      fontFamily: 'system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif',
      fontSize: '13px',
      display: 'flex',
      flexDirection: 'column',
      overflow: 'hidden',
      color: '#333',
    });
    if (Number.isFinite(uiState.left)) {
      panel.style.left = `${uiState.left}px`;
      panel.style.right = 'auto';
    } else {
      panel.style.right = `${uiState.right}px`;
    }

    headerEl = document.createElement('div');
    Object.assign(headerEl.style, {
      background: paused ? COLOR_PAUSE : BRAND,
      color: '#fff',
      padding: '8px 10px',
      display: 'flex',
      alignItems: 'center',
      justifyContent: 'space-between',
      gap: '8px',
      cursor: 'move',
      userSelect: 'none',
      flexShrink: '0',
    });

    const titleWrap = document.createElement('div');
    titleWrap.style.cssText = 'display:flex;align-items:center;gap:7px;min-width:0;';
    const title = document.createElement('strong');
    title.textContent = '抖音链接采集器';
    title.style.cssText = 'font-size:13px;white-space:nowrap;';
    titleWrap.appendChild(title);
    statusDot = document.createElement('span');
    statusDot.style.cssText = 'width:7px;height:7px;border-radius:50%;background:#fff;opacity:.9;flex:none;';
    titleWrap.appendChild(statusDot);
    statusEl = document.createElement('span');
    statusEl.style.cssText = 'font-size:11px;opacity:.9;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;';
    titleWrap.appendChild(statusEl);
    headerEl.appendChild(titleWrap);

    const headerActions = document.createElement('div');
    headerActions.style.cssText = 'display:flex;align-items:center;gap:4px;flex-shrink:0;';
    countEl = document.createElement('span');
    countEl.style.cssText = 'font-size:11px;white-space:nowrap;margin-right:2px;';
    headerActions.appendChild(countEl);
    btnPauseEl = makeButton('⏸', COLOR_PAUSE, '暂停采集');
    btnPauseEl.style.padding = '2px 7px';
    btnPauseEl.onclick = togglePause;
    headerActions.appendChild(btnPauseEl);
    const btnTools = makeButton('⚙', '#555', '打开工具抽屉');
    btnTools.style.padding = '2px 7px';
    btnTools.onclick = () => setToolsOpen(true);
    headerActions.appendChild(btnTools);
    btnMinEl = makeButton('−', '#555', '最小化');
    btnMinEl.style.padding = '2px 7px';
    btnMinEl.onclick = () => setMinimized(true);
    headerActions.appendChild(btnMinEl);
    headerEl.appendChild(headerActions);
    panel.appendChild(headerEl);

    const collectionBar = document.createElement('div');
    Object.assign(collectionBar.style, {
      display: 'flex',
      alignItems: 'center',
      gap: '6px',
      padding: '7px 10px 5px',
      background: '#fff',
      flexShrink: '0',
    });
    catSelectEl = document.createElement('select');
    Object.assign(catSelectEl.style, {
      flex: '1',
      minWidth: '0',
      fontSize: '12px',
      padding: '5px 6px',
      border: `1px solid ${BRAND}`,
      borderRadius: '6px',
      outline: 'none',
      cursor: 'pointer',
    });
    catSelectEl.onchange = () => {
      currentCat = catSelectEl.value;
      GM_setValue(KEY_CURRENT_CAT, currentCat);
      renderPanel();
    };
    collectionBar.appendChild(catSelectEl);
    const btnAdd = makeButton('+ 分类', COLOR_RESUME, '新建分类');
    btnAdd.onclick = () => {
      setToolsOpen(true);
      setTimeout(() => newCatInput && newCatInput.focus(), 0);
    };
    collectionBar.appendChild(btnAdd);
    panel.appendChild(collectionBar);

    const searchBar = document.createElement('div');
    Object.assign(searchBar.style, {
      display: 'flex',
      alignItems: 'center',
      gap: '6px',
      padding: '0 10px 6px',
      flexShrink: '0',
    });
    searchInput = document.createElement('input');
    searchInput.type = 'search';
    searchInput.placeholder = '搜索视频 ID、分类或日期…';
    Object.assign(searchInput.style, {
      flex: '1',
      minWidth: '0',
      padding: '6px 8px',
      border: '1px solid #ddd',
      borderRadius: '6px',
      outline: 'none',
      fontSize: '12px',
    });
    searchInput.oninput = () => renderPanel();
    searchBar.appendChild(searchInput);
    filterSelectEl = document.createElement('select');
    Object.assign(filterSelectEl.style, {
      width: '108px',
      padding: '6px 4px',
      border: '1px solid #ddd',
      borderRadius: '6px',
      fontSize: '11px',
      outline: 'none',
    });
    filterSelectEl.onchange = () => {
      uiState.categoryFilter = filterSelectEl.value;
      saveUiState({ categoryFilter: uiState.categoryFilter });
      renderPanel();
    };
    searchBar.appendChild(filterSelectEl);
    panel.appendChild(searchBar);

    const viewBar = document.createElement('div');
    Object.assign(viewBar.style, {
      display: 'flex',
      alignItems: 'center',
      gap: '5px',
      padding: '0 10px 7px',
      borderBottom: '1px solid #eee',
      flexShrink: '0',
    });
    viewTodayBtn = makeButton('今日', BRAND, '只看今天');
    viewAllBtn = makeButton('全部', '#999', '查看全部日期');
    viewTodayBtn.onclick = () => {
      uiState.viewMode = 'today';
      saveUiState({ viewMode: 'today' });
      renderPanel();
    };
    viewAllBtn.onclick = () => {
      uiState.viewMode = 'all';
      saveUiState({ viewMode: 'all' });
      renderPanel();
    };
    viewBar.appendChild(viewTodayBtn);
    viewBar.appendChild(viewAllBtn);
    copyScopeSelect = document.createElement('select');
    Object.assign(copyScopeSelect.style, {
      flex: '1',
      minWidth: '0',
      padding: '5px 4px',
      border: '1px solid #ddd',
      borderRadius: '5px',
      fontSize: '11px',
    });
    [
      ['visible', '复制当前结果'],
      ['today', '复制今天'],
      ['category', '复制当前采集分类'],
      ['all', '复制全部'],
    ].forEach(([value, label]) => {
      const option = document.createElement('option');
      option.value = value;
      option.textContent = label;
      copyScopeSelect.appendChild(option);
    });
    viewBar.appendChild(copyScopeSelect);
    const btnCopy = makeButton('复制', BRAND_DARK, '按右侧范围复制');
    btnCopy.onclick = copySelectedScope;
    viewBar.appendChild(btnCopy);
    const btnSendAll = makeButton('全部传入统一递交器', '#1677ff', '把今天尚未发送的已分类链接传入当前电脑的V13.3统一递交器');
    btnSendAll.style.whiteSpace = 'nowrap';
    btnSendAll.onclick = () => sendCategoriesToV12(categories.filter(cat => cat !== DEFAULT_CAT && !isReplenishmentCategory(cat)));
    viewBar.appendChild(btnSendAll);
    panel.appendChild(viewBar);

    const batchBar = document.createElement('div');
    Object.assign(batchBar.style, {
      display: 'flex',
      alignItems: 'center',
      gap: '4px',
      padding: '0 10px 7px',
      borderBottom: '1px solid #eee',
      flexShrink: '0',
      flexWrap: 'wrap',
    });
    const batchLabel = document.createElement('span');
    batchLabel.textContent = '批量移动';
    batchLabel.style.cssText = 'font-size:11px;color:#666;white-space:nowrap;';
    batchBar.appendChild(batchLabel);
    batchScopeSelect = document.createElement('select');
    Object.assign(batchScopeSelect.style, {
      flex: '1 1 112px',
      minWidth: '108px',
      padding: '5px 4px',
      border: '1px solid #ddd',
      borderRadius: '5px',
      fontSize: '11px',
    });
    [
      ['visible', '当前结果'],
      ['today', '今天'],
      ['category', '当前采集分类'],
      ['all', '全部链接'],
    ].forEach(([value, label]) => {
      const option = document.createElement('option');
      option.value = value;
      option.textContent = `全选${label}`;
      batchScopeSelect.appendChild(option);
    });
    batchBar.appendChild(batchScopeSelect);
    const selectAllBtn = makeButton('全选', '#667085', '按左侧范围勾选链接');
    selectAllBtn.style.padding = '3px 6px';
    selectAllBtn.onclick = selectBatchScope;
    batchBar.appendChild(selectAllBtn);
    const clearSelectionBtn = makeButton('清空勾选', '#999', '清空当前勾选，不删除链接');
    clearSelectionBtn.style.padding = '3px 6px';
    clearSelectionBtn.onclick = clearBatchSelection;
    batchBar.appendChild(clearSelectionBtn);
    batchTargetSelect = document.createElement('select');
    Object.assign(batchTargetSelect.style, {
      flex: '1 1 108px',
      minWidth: '108px',
      padding: '5px 4px',
      border: '1px solid #ddd',
      borderRadius: '5px',
      fontSize: '11px',
    });
    batchBar.appendChild(batchTargetSelect);
    const moveBatchBtn = makeButton('移动', COLOR_RESUME, '把勾选链接移动到目标分类');
    moveBatchBtn.style.padding = '3px 8px';
    moveBatchBtn.onclick = moveSelectedBatch;
    batchBar.appendChild(moveBatchBtn);
    batchSelectionInfo = document.createElement('span');
    batchSelectionInfo.style.cssText = 'font-size:10px;color:#999;white-space:nowrap;';
    batchBar.appendChild(batchSelectionInfo);
    panel.appendChild(batchBar);
    rebuildBatchTargetSelect();

    listEl = document.createElement('div');
    Object.assign(listEl.style, {
      flex: '1 1 auto',
      minHeight: '0',
      overflowY: 'auto',
      overflowX: 'hidden',
      overscrollBehavior: 'contain',
      scrollbarGutter: 'stable',
      padding: '4px 0',
      background: '#fafafa',
    });
    listEl.addEventListener('wheel', () => {
      suppressAutoScrollUntil = Date.now() + 1600;
    }, { passive: true });
    listEl.addEventListener('pointerdown', () => {
      suppressAutoScrollUntil = Date.now() + 1600;
    }, { passive: true });
    listEl.addEventListener('scroll', () => {
      clearTimeout(restoreScrollTimer);
      suppressAutoScrollUntil = Date.now() + 800;
    }, { passive: true });
    panel.appendChild(listEl);

    const resizeHandle = document.createElement('div');
    resizeHandle.title = '拖动调整窗口大小';
    Object.assign(resizeHandle.style, {
      position: 'absolute',
      left: '0',
      bottom: '0',
      width: '18px',
      height: '18px',
      cursor: 'nesw-resize',
      zIndex: '4',
    });
    resizeHandle.onmousedown = startResize;
    panel.appendChild(resizeHandle);

    createToolsDrawer();
    document.body.appendChild(panel);
    createBubble();
    makeDraggable(panel, headerEl);
    applyPauseStyle();
    rebuildCatSelect();
    rebuildFilterSelect();
    clampPanelToViewport();
    setMinimized(Boolean(uiState.minimized), true);
    window.addEventListener('resize', clampPanelToViewport);
    document.addEventListener('click', closeRowMenus, true);
  }

  function createBubble() {
    bubble = document.createElement('button');
    bubble.id = '__douyin_collector_bubble__';
    bubble.textContent = '🎬';
    bubble.title = '打开抖音链接采集器';
    styleBtn(bubble, '#fff', BRAND);
    Object.assign(bubble.style, {
      position: 'fixed',
      right: '14px',
      bottom: '18px',
      zIndex: '100000',
      borderRadius: '20px',
      display: 'none',
      alignItems: 'center',
      gap: '5px',
      boxShadow: '0 4px 14px rgba(0,0,0,.22)',
    });
    bubble.onclick = () => setMinimized(false);
    document.body.appendChild(bubble);
  }

  function setMinimized(value, initial = false) {
    if (!panel || !bubble) return;
    uiState.minimized = Boolean(value);
    panel.style.display = uiState.minimized ? 'none' : 'flex';
    bubble.style.display = uiState.minimized ? 'flex' : 'none';
    if (btnMinEl) {
      btnMinEl.textContent = uiState.minimized ? '+' : '−';
      btnMinEl.title = uiState.minimized ? '恢复窗口' : '最小化';
    }
    if (!initial) saveUiState({ minimized: uiState.minimized }, true);
    updateBubble();
  }

  function updateBubble() {
    if (!bubble) return;
    bubble.textContent = `🎬 ${collected.size}`;
    bubble.style.background = paused ? COLOR_PAUSE : BRAND;
  }

  function setToolsOpen(open) {
    if (!toolsDrawer) return;
    toolsDrawer.style.display = open ? 'flex' : 'none';
    if (open && newCatInput) newCatInput.value = '';
  }

  function sectionTitle(text) {
    const title = document.createElement('div');
    title.textContent = text;
    title.style.cssText = 'font-size:11px;font-weight:bold;color:#888;margin:10px 0 5px;';
    return title;
  }

  function toolButton(text, bg = '#666') {
    const button = makeButton(text, bg);
    button.style.width = '100%';
    button.style.textAlign = 'left';
    button.style.marginBottom = '5px';
    return button;
  }

  function createToolsDrawer() {
    toolsDrawer = document.createElement('div');
    Object.assign(toolsDrawer.style, {
      position: 'absolute',
      inset: '0',
      zIndex: '5',
      display: 'none',
      flexDirection: 'column',
      background: '#fff',
      boxShadow: '-8px 0 22px rgba(0,0,0,.14)',
      overflowY: 'auto',
      padding: '10px',
    });
    const head = document.createElement('div');
    head.style.cssText = 'display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid #eee;padding-bottom:8px;';
    const title = document.createElement('strong');
    title.textContent = '工具与数据安全';
    title.style.fontSize = '14px';
    head.appendChild(title);
    const close = makeButton('×', '#777', '关闭工具抽屉');
    close.style.padding = '2px 8px';
    close.onclick = () => setToolsOpen(false);
    head.appendChild(close);
    toolsDrawer.appendChild(head);

    toolsDrawer.appendChild(sectionTitle('V13.3统一递交器导入桥'));
    const healthBtn = toolButton('检测统一递交器', '#1677ff');
    healthBtn.onclick = async () => {
      try {
        const result = await bridgeRequest('/api/health', null, 'GET');
        alert(`V13.3统一递交器导入桥运行正常。\n运行组：${result.runGroupId || '未知'}\n会话：${result.sessionId || '未知'}\n待导入：${result.pendingImports ?? '未知'}`);
      } catch (error) {
        alert(`检测失败：\n${error && error.message ? error.message : error}`);
      }
    };
    toolsDrawer.appendChild(healthBtn);
    const sendAllToolBtn = toolButton('传入今天未发链接到统一递交器', '#1677ff');
    sendAllToolBtn.onclick = () => sendCategoriesToV12(categories.filter(cat => cat !== DEFAULT_CAT && !isReplenishmentCategory(cat)));
    toolsDrawer.appendChild(sendAllToolBtn);
    const undoV12Btn = toolButton('查看V13任务撤回说明', '#d97706');
    undoV12Btn.onclick = undoLatestV12Import;
    toolsDrawer.appendChild(undoV12Btn);
    const resetTodaySentBtn = toolButton('重置今天的V13已导入标记', '#d97706');
    resetTodaySentBtn.onclick = () => resetV12SentForDate(todayKey(), true);
    toolsDrawer.appendChild(resetTodaySentBtn);
    const resetMappingsBtn = toolButton('清除项目/材料映射（不删链接）', '#777');
    resetMappingsBtn.onclick = () => {
      if (!confirm('只清除已保存的项目/材料映射，不会删除链接或分类。继续吗？')) return;
      v12Mappings = {};
      GM_setValue(KEY_V12_MAPPINGS, '{}');
      showStatus('项目/材料映射已清除');
    };
    toolsDrawer.appendChild(resetMappingsBtn);

    toolsDrawer.appendChild(sectionTitle('备份与导出'));
    const exportJsonBtn = toolButton('导出完整 JSON 备份', BRAND);
    exportJsonBtn.onclick = exportJson;
    toolsDrawer.appendChild(exportJsonBtn);
    const importMergeBtn = toolButton('导入 JSON（安全合并）', '#6c5ce7');
    importMergeBtn.onclick = () => chooseBackupFile('merge');
    toolsDrawer.appendChild(importMergeBtn);
    const importReplaceBtn = toolButton('导入 JSON（完全恢复）', '#b23a48');
    importReplaceBtn.onclick = () => chooseBackupFile('replace');
    toolsDrawer.appendChild(importReplaceBtn);
    const exportTxtBtn = toolButton('按当前复制范围导出 TXT', '#555');
    exportTxtBtn.onclick = exportTxt;
    toolsDrawer.appendChild(exportTxtBtn);

    toolsDrawer.appendChild(sectionTitle('分类管理'));
    const catRow = document.createElement('div');
    catRow.style.cssText = 'display:flex;gap:5px;margin-bottom:5px;';
    newCatInput = document.createElement('input');
    newCatInput.placeholder = '新分类名';
    Object.assign(newCatInput.style, {
      flex: '1',
      minWidth: '0',
      border: '1px solid #ddd',
      borderRadius: '5px',
      padding: '6px',
      fontSize: '12px',
    });
    newCatInput.onkeydown = e => { if (e.key === 'Enter') addCategoryFromInput(); };
    catRow.appendChild(newCatInput);
    const addCatBtn = makeButton('添加', COLOR_RESUME);
    addCatBtn.onclick = addCategoryFromInput;
    catRow.appendChild(addCatBtn);
    toolsDrawer.appendChild(catRow);
    const deleteCatHint = document.createElement('div');
    deleteCatHint.textContent = '删除分类或移动链接请使用分类标题右侧菜单。';
    deleteCatHint.style.cssText = 'font-size:11px;color:#999;line-height:1.5;';
    toolsDrawer.appendChild(deleteCatHint);

    toolsDrawer.appendChild(sectionTitle('黑名单'));
    blackInput = document.createElement('input');
    blackInput.placeholder = '粘贴视频链接或 ID，多个可换行';
    Object.assign(blackInput.style, {
      width: '100%',
      boxSizing: 'border-box',
      border: '1px solid #f2b5b5',
      borderRadius: '5px',
      padding: '6px',
      fontSize: '12px',
      marginBottom: '5px',
    });
    blackInput.onkeydown = e => { if (e.key === 'Enter' && !e.shiftKey) blacklistFromInput(); };
    toolsDrawer.appendChild(blackInput);
    const blackBtn = toolButton('删除并加入黑名单', '#e74c3c');
    blackBtn.onclick = blacklistFromInput;
    toolsDrawer.appendChild(blackBtn);

    toolsDrawer.appendChild(sectionTitle('已删除记录'));
    deletedManagerEl = document.createElement('div');
    toolsDrawer.appendChild(deletedManagerEl);
    renderDeletedManager();

    toolsDrawer.appendChild(sectionTitle('危险操作'));
    const clearBtn = toolButton('清空全部链接（不可逆）', '#c0392b');
    clearBtn.onclick = clearAll;
    toolsDrawer.appendChild(clearBtn);
    toolStatusEl = document.createElement('div');
    toolStatusEl.style.cssText = 'font-size:11px;color:#777;min-height:18px;margin-top:6px;line-height:1.45;';
    toolsDrawer.appendChild(toolStatusEl);
    panel.appendChild(toolsDrawer);
  }

  function addCategoryFromInput() {
    const name = (newCatInput && newCatInput.value || '').trim();
    if (!name || categories.includes(name)) {
      if (newCatInput) newCatInput.value = '';
      return;
    }
    categories.push(name);
    currentCat = name;
    saveCategories();
    GM_setValue(KEY_CURRENT_CAT, currentCat);
    if (newCatInput) newCatInput.value = '';
    rebuildCatSelect();
    rebuildFilterSelect();
    renderPanel();
  }

  function blacklistFromInput() {
    const raw = (blackInput && blackInput.value || '').trim();
    if (!raw) return;
    const urls = [];
    raw.split(/[\n\s,]+/).forEach(part => {
      if (!part) return;
      const match = part.match(VIDEO_URL_RE);
      if (match) {
        urls.push(`https://www.douyin.com/video/${match[1]}`);
        return;
      }
      try {
        const modalId = new URL(part).searchParams.get('modal_id');
        if (modalId && /^\d+$/.test(modalId)) {
          urls.push(`https://www.douyin.com/video/${modalId}`);
          return;
        }
      } catch (e) {}
      if (/^\d{10,}$/.test(part)) urls.push(`https://www.douyin.com/video/${part}`);
    });
    const unique = [...new Set(urls)];
    if (!unique.length) {
      showStatus('没有识别到视频链接或 ID');
      return;
    }
    unique.forEach(url => {
      collected.delete(url);
      sessionNew.delete(url);
      deleted.add(url);
    });
    saveDeleted();
    saveCollected(true);
    if (blackInput) blackInput.value = '';
    renderDeletedManager();
    showStatus(`已加入黑名单并删除 ${unique.length} 条`);
    renderPanel();
  }

  function renderDeletedManager() {
    if (!deletedManagerEl) return;
    deletedManagerEl.innerHTML = '';
    const urls = [...deleted].sort();
    const summary = document.createElement('div');
    summary.textContent = urls.length ? `黑名单中有 ${urls.length} 条，恢复后会放入当前分类并以当前时间重新记录。` : '暂无已删除记录。';
    summary.style.cssText = 'font-size:11px;color:#999;line-height:1.45;margin-bottom:5px;';
    deletedManagerEl.appendChild(summary);
    urls.slice(0, 100).forEach(url => {
      const row = document.createElement('div');
      row.style.cssText = 'display:flex;align-items:center;gap:5px;padding:4px 0;border-bottom:1px solid #f2f2f2;';
      const label = document.createElement('span');
      label.textContent = videoIdFromUrl(url);
      label.title = url;
      label.style.cssText = 'flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:11px;color:#666;';
      row.appendChild(label);
      const restore = makeButton('恢复', COLOR_RESUME);
      restore.style.padding = '2px 6px';
      restore.onclick = () => {
        deleted.delete(url);
        collected.set(url, { ts: Date.now(), cat: currentCat });
        saveDeleted();
        saveCollected(true);
        renderDeletedManager();
        renderPanel();
      };
      row.appendChild(restore);
      deletedManagerEl.appendChild(row);
    });
    if (urls.length > 100) {
      const more = document.createElement('div');
      more.textContent = `还有 ${urls.length - 100} 条未展开，请先通过导出备份管理。`;
      more.style.cssText = 'font-size:10px;color:#aaa;padding-top:5px;';
      deletedManagerEl.appendChild(more);
    }
  }

  function rebuildCatSelect() {
    if (!catSelectEl) return;
    catSelectEl.innerHTML = '';
    categories.forEach(cat => {
      const option = document.createElement('option');
      option.value = cat;
      option.textContent = `采集到：${cat}`;
      option.selected = cat === currentCat;
      catSelectEl.appendChild(option);
    });
  }

  function rebuildFilterSelect() {
    if (!filterSelectEl) return;
    const old = uiState.categoryFilter;
    filterSelectEl.innerHTML = '';
    const allOption = document.createElement('option');
    allOption.value = 'all';
    allOption.textContent = '全部分类';
    filterSelectEl.appendChild(allOption);
    categories.forEach(cat => {
      const option = document.createElement('option');
      option.value = cat;
      option.textContent = cat;
      filterSelectEl.appendChild(option);
    });
    uiState.categoryFilter = categories.includes(old) ? old : 'all';
    filterSelectEl.value = uiState.categoryFilter;
  }

  function applyPauseStyle() {
    if (!panel || !btnPauseEl || !headerEl) return;
    const active = !paused;
    headerEl.style.background = paused ? COLOR_PAUSE : BRAND;
    panel.style.borderColor = paused ? COLOR_PAUSE : BRAND;
    btnPauseEl.textContent = paused ? '▶' : '⏸';
    btnPauseEl.title = paused ? '继续采集' : '暂停采集';
    styleBtn(btnPauseEl, '#fff', paused ? COLOR_RESUME : COLOR_PAUSE);
    if (statusDot) statusDot.style.background = '#fff';
    if (statusEl) statusEl.textContent = paused ? '已暂停' : '采集中';
    if (bubble) updateBubble();
  }

  function togglePause() {
    paused = !paused;
    GM_setValue(KEY_PAUSED, paused ? '1' : '0');
    applyPauseStyle();
    renderPanel();
  }

  function updateViewControls() {
    if (!viewTodayBtn || !viewAllBtn) return;
    const today = uiState.viewMode === 'today';
    styleBtn(viewTodayBtn, '#fff', today ? BRAND : '#bbb');
    styleBtn(viewAllBtn, '#fff', today ? '#bbb' : BRAND);
    if (filterSelectEl) filterSelectEl.value = uiState.categoryFilter;
  }

  function createDateGroup(cat, dateKey, items, catBody) {
    const isToday = dateKey === todayKey();
    const dateGroupKey = `${cat}\u0000${dateKey}`;
    const collapsed = dateCollapseOverrides.has(dateGroupKey)
      ? dateCollapseOverrides.get(dateGroupKey)
      : !isToday;

    const dateHeader = document.createElement('div');
    Object.assign(dateHeader.style, {
      position: 'sticky',
      top: '34px',
      zIndex: '2',
      display: 'flex',
      alignItems: 'center',
      justifyContent: 'space-between',
      padding: '5px 14px 4px',
      background: isToday ? '#fff8f8' : '#fafafa',
      borderBottom: '1px solid #eee',
      cursor: 'pointer',
      userSelect: 'none',
    });
    const left = document.createElement('div');
    left.style.cssText = 'display:flex;align-items:center;gap:5px;';
    const arrow = document.createElement('span');
    arrow.textContent = collapsed ? '▸' : '▾';
    arrow.style.cssText = `font-size:10px;color:${isToday ? BRAND : '#999'};`;
    left.appendChild(arrow);
    const label = document.createElement('span');
    label.textContent = friendlyDateLabel(dateKey);
    label.style.cssText = `font-size:11px;color:${isToday ? BRAND : '#888'};font-weight:${isToday ? 'bold' : 'normal'};`;
    left.appendChild(label);
    dateHeader.appendChild(left);
    const right = document.createElement('div');
    right.style.cssText = 'display:flex;align-items:center;gap:5px;';
    const count = document.createElement('span');
    count.textContent = `${items.length} 条`;
    count.style.cssText = 'font-size:10px;color:#aaa;';
    right.appendChild(count);
    const copy = makeButton('复制', isToday ? BRAND : '#999');
    copy.style.padding = '2px 6px';
    copy.onclick = e => {
      e.stopPropagation();
      copyUrls(items.map(item => item.url), '链接');
    };
    right.appendChild(copy);
    dateHeader.appendChild(right);
    catBody.appendChild(dateHeader);

    const dateBody = document.createElement('div');
    dateBody.style.display = collapsed ? 'none' : 'block';
    catBody.appendChild(dateBody);

    const renderRows = () => {
      if (dateBody.dataset.rendered === '1') return;
      dateBody.dataset.rendered = '1';
      items.forEach(item => createRow(item, cat, dateBody));
    };
    if (!collapsed) renderRows();
    dateHeader.onclick = e => {
      const target = e.target instanceof Element ? e.target : null;
      if (target && target.closest('button')) return;
      const willCollapse = dateBody.style.display !== 'none';
      if (!willCollapse) renderRows();
      dateBody.style.display = willCollapse ? 'none' : 'block';
      arrow.textContent = willCollapse ? '▸' : '▾';
      dateCollapseOverrides.set(dateGroupKey, willCollapse);
    };
  }

  function closeRowMenus(event) {
    if (event && event.target && event.target.closest && event.target.closest('[data-dcc-menu-wrap]')) return;
    document.querySelectorAll('[data-dcc-menu]').forEach(menu => { menu.style.display = 'none'; });
  }

  function createRow(item, cat, parent) {
    const { url, ts } = item;
    const id = videoIdFromUrl(url);
    const isNew = sessionNew.has(url);
    const sentToV12 = isV12Sent(url);
    const isActive = id === currentVideoId;
    const row = document.createElement('div');
    row.dataset.vid = id;
    Object.assign(row.style, {
      position: 'relative',
      display: 'flex',
      alignItems: 'center',
      gap: '7px',
      padding: '6px 10px 6px 14px',
      borderBottom: '1px solid #f0f0f0',
      borderLeft: isActive ? `3px solid ${BRAND}` : '3px solid transparent',
      background: isActive ? '#fff0f3' : sentToV12 ? '#f1fbf5' : isNew ? '#fffbf0' : '#fff',
    });
    const check = document.createElement('input');
    check.type = 'checkbox';
    check.checked = selectedUrls.has(url);
    check.title = '勾选后可批量移动';
    check.style.cssText = 'width:15px;height:15px;flex:none;accent-color:#ff2f63;cursor:pointer;';
    check.onclick = event => event.stopPropagation();
    check.onchange = () => {
      if (check.checked) selectedUrls.add(url);
      else selectedUrls.delete(url);
      updateBatchSelectionInfo();
    };
    row.appendChild(check);
    const info = document.createElement('div');
    info.style.cssText = 'flex:1;min-width:0;overflow:hidden;';
    const idLine = document.createElement('div');
    idLine.textContent = `${isActive ? '▶' : sentToV12 ? '✅' : isNew ? '🆕' : '🎬'} ${id}`;
    idLine.title = url;
    idLine.style.cssText = `overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:12px;color:${isActive ? BRAND : isNew ? '#c0392b' : '#555'};font-weight:${isActive ? 'bold' : 'normal'};`;
    const metaLine = document.createElement('div');
    metaLine.textContent = `${tsToTimeStr(ts) || '时间未知'} · ${cat}${sentToV12 ? ' · 已导入V13' : ''}`;
    metaLine.style.cssText = 'font-size:10px;color:#aaa;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;margin-top:2px;';
    info.appendChild(idLine);
    info.appendChild(metaLine);
    row.appendChild(info);

    const actions = document.createElement('div');
    actions.style.cssText = 'display:flex;align-items:center;gap:4px;flex-shrink:0;';
    const copyBtn = makeButton('复制', BRAND);
    copyBtn.style.padding = '3px 6px';
    copyBtn.onclick = e => {
      e.stopPropagation();
      GM_setClipboard(url);
      showStatus('已复制 1 条链接');
    };
    actions.appendChild(copyBtn);
    const menuWrap = document.createElement('div');
    menuWrap.dataset.dccMenuWrap = '1';
    menuWrap.style.position = 'relative';
    const moreBtn = makeButton('⋯', '#777', '更多操作');
    moreBtn.style.padding = '3px 7px';
    menuWrap.appendChild(moreBtn);
    const menu = document.createElement('div');
    menu.dataset.dccMenu = '1';
    Object.assign(menu.style, {
      display: 'none',
      position: 'absolute',
      right: '0',
      top: '32px',
      zIndex: '10',
      width: '150px',
      padding: '6px',
      background: '#fff',
      border: '1px solid #ddd',
      borderRadius: '7px',
      boxShadow: '0 5px 18px rgba(0,0,0,.18)',
    });
    const openBtn = toolButton('打开视频', '#555');
    openBtn.onclick = () => {
      window.open(url, '_blank', 'noopener');
      menu.style.display = 'none';
    };
    menu.appendChild(openBtn);
    const moveSelect = document.createElement('select');
    moveSelect.style.cssText = 'width:100%;box-sizing:border-box;padding:5px;margin-bottom:5px;border:1px solid #ddd;border-radius:4px;font-size:11px;';
    const movePlaceholder = document.createElement('option');
    movePlaceholder.textContent = '移动到分类…';
    movePlaceholder.value = '';
    movePlaceholder.selected = true;
    moveSelect.appendChild(movePlaceholder);
    categories.filter(other => other !== cat).forEach(other => {
      const option = document.createElement('option');
      option.value = other;
      option.textContent = other;
      moveSelect.appendChild(option);
    });
    moveSelect.onchange = () => {
      if (!moveSelect.value) return;
      const value = collected.get(url);
      if (value) value.cat = moveSelect.value;
      saveCollected(true);
      menu.style.display = 'none';
      renderPanel();
    };
    menu.appendChild(moveSelect);
    const deleteBtn = toolButton('删除并加入黑名单', '#e74c3c');
    deleteBtn.onclick = () => {
      if (!confirm('删除此链接并加入黑名单？')) return;
      collected.delete(url);
      sessionNew.delete(url);
      deleted.add(url);
      saveDeleted();
      saveCollected(true);
      renderDeletedManager();
      menu.style.display = 'none';
      renderPanel();
    };
    menu.appendChild(deleteBtn);
    menuWrap.appendChild(menu);
    moreBtn.onclick = e => {
      e.stopPropagation();
      const open = menu.style.display !== 'none';
      closeRowMenus();
      menu.style.display = open ? 'none' : 'block';
    };
    actions.appendChild(menuWrap);
    row.appendChild(actions);
    parent.appendChild(row);
  }

  function renderPanelNow(options = {}) {
    if (!panel) createPanel();
    const oldScrollTop = listEl ? listEl.scrollTop : 0;
    const shouldAutoFocus = Boolean(options.autoFocus) && Date.now() > suppressAutoScrollUntil;
    const total = collected.size;
    const todayCount = [...collected.values()].filter(value => tsToDateKey(value.ts) === todayKey()).length;
    const newCount = sessionNew.size;
    countEl.textContent = newCount > 0 ? `共 ${total} · 本次 +${newCount}` : `共 ${total} · 今日 ${todayCount}`;
    rebuildFilterSelect();
    pruneSelectedUrls();
    rebuildBatchTargetSelect();
    updateViewControls();
    listEl.innerHTML = '';
    const items = allFilteredItems();
    if (!items.length) {
      const empty = document.createElement('div');
      empty.textContent = total === 0
        ? (paused ? '已暂停，点击 ▶ 继续采集' : '还没有采集到视频，继续刷吧～')
        : '当前筛选没有结果';
      empty.style.cssText = 'color:#999;text-align:center;padding:34px 16px;font-size:12px;';
      listEl.appendChild(empty);
      listEl.scrollTop = 0;
      updateBubble();
      return;
    }

    const groups = new Map();
    items.forEach(item => {
      if (!groups.has(item.cat)) groups.set(item.cat, new Map());
      const dates = groups.get(item.cat);
      const dateKey = tsToDateKey(item.ts);
      if (!dates.has(dateKey)) dates.set(dateKey, []);
      dates.get(dateKey).push(item);
    });
    const orderedCats = [
      ...categories.filter(cat => groups.has(cat) && cat !== DEFAULT_CAT),
      ...(groups.has(DEFAULT_CAT) ? [DEFAULT_CAT] : []),
      ...[...groups.keys()].filter(cat => !categories.includes(cat)),
    ];

    orderedCats.forEach(cat => {
      const dateMap = groups.get(cat);
      const catTotal = [...dateMap.values()].reduce((sum, values) => sum + values.length, 0);
      const catCollapsed = collapsedCats.has(cat);
      const catHeader = document.createElement('div');
      Object.assign(catHeader.style, {
        position: 'sticky',
        top: '0',
        zIndex: '3',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        gap: '6px',
        padding: '7px 10px 6px',
        background: cat === currentCat ? '#fff0f3' : '#f2f2f2',
        borderTop: `2px solid ${cat === currentCat ? BRAND : '#ddd'}`,
        borderBottom: '1px solid #eee',
        cursor: 'pointer',
        userSelect: 'none',
      });
      const catLeft = document.createElement('div');
      catLeft.style.cssText = 'display:flex;align-items:center;gap:6px;min-width:0;';
      const catArrow = document.createElement('span');
      catArrow.textContent = catCollapsed ? '▸' : '▾';
      catArrow.style.cssText = `font-size:11px;color:${cat === currentCat ? BRAND : '#999'};`;
      catLeft.appendChild(catArrow);
      const catName = document.createElement('strong');
      catName.textContent = cat;
      catName.style.cssText = `font-size:12px;color:${cat === currentCat ? BRAND : '#555'};overflow:hidden;text-overflow:ellipsis;white-space:nowrap;`;
      catLeft.appendChild(catName);
      const catCount = document.createElement('span');
      const catItems = [...dateMap.values()].flat();
      const unsentToday = catItems.filter(item => tsToDateKey(item.ts) === todayKey() && !isV12Sent(item.url)).length;
      catCount.textContent = `${catTotal} 条${unsentToday ? ` · 今日未发 ${unsentToday}` : ''}`;
      catCount.style.cssText = 'font-size:10px;color:#999;';
      catLeft.appendChild(catCount);
      catHeader.appendChild(catLeft);
      const catRight = document.createElement('div');
      catRight.style.cssText = 'display:flex;align-items:center;gap:4px;';
      const copyCat = makeButton('复制', cat === currentCat ? BRAND : '#888');
      copyCat.style.padding = '2px 6px';
      copyCat.onclick = e => {
        e.stopPropagation();
        copyUrls([...dateMap.values()].flat().map(item => item.url), '链接');
      };
      catRight.appendChild(copyCat);
      if (cat !== DEFAULT_CAT && !isReplenishmentCategory(cat)) {
        const sendCat = makeButton('传入统一递交器', '#1677ff', '把此分类今天尚未发送的链接传入V13.3统一递交器');
        sendCat.style.padding = '2px 6px';
        sendCat.onclick = e => {
          e.stopPropagation();
          sendCategoriesToV12([cat]);
        };
        catRight.appendChild(sendCat);
      }
      if (cat !== DEFAULT_CAT) {
        const delCat = makeButton('⋯', '#888', '分类操作');
        delCat.style.padding = '2px 7px';
        delCat.onclick = e => {
          e.stopPropagation();
          if (!confirm(`删除分类“${cat}”？`)) return;
          const deleteLinks = confirm(`同时删除此分类的 ${catTotal} 条链接吗？\n确定：链接和分类一起删除\n取消：链接移到未分类`);
          if (deleteLinks) {
            [...collected.entries()].forEach(([url, value]) => {
              if (value.cat === cat) collected.delete(url);
            });
          } else {
            collected.forEach(value => {
              if (value.cat === cat) value.cat = DEFAULT_CAT;
            });
          }
          categories = categories.filter(value => value !== cat);
          if (currentCat === cat) {
            currentCat = DEFAULT_CAT;
            GM_setValue(KEY_CURRENT_CAT, currentCat);
          }
          saveCategories();
          saveCollected(true);
          rebuildCatSelect();
          rebuildFilterSelect();
          renderPanel();
        };
        catRight.appendChild(delCat);
      }
      catHeader.appendChild(catRight);
      listEl.appendChild(catHeader);
      const catBody = document.createElement('div');
      catBody.style.display = catCollapsed ? 'none' : 'block';
      listEl.appendChild(catBody);
      catHeader.onclick = e => {
        const target = e.target instanceof Element ? e.target : null;
        if (target && target.closest('button')) return;
        const nextCollapsed = catBody.style.display !== 'none';
        collapsedCats[ nextCollapsed ? 'add' : 'delete' ](cat);
        catBody.style.display = nextCollapsed ? 'none' : 'block';
        catArrow.textContent = nextCollapsed ? '▸' : '▾';
      };
      dateMap.forEach((dateItems, dateKey) => createDateGroup(cat, dateKey, dateItems, catBody));
    });

    restoreScrollTimer = requestAnimationFrame(() => {
      listEl.scrollTop = Math.min(oldScrollTop, Math.max(0, listEl.scrollHeight - listEl.clientHeight));
      if (shouldAutoFocus && currentVideoId) {
        const activeRow = listEl.querySelector(`[data-vid="${currentVideoId}"]`);
        if (activeRow) activeRow.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
      }
    });
    updateBubble();
  }

  function renderPanel(options = {}) {
    queuedAutoFocus = queuedAutoFocus || Boolean(options.autoFocus);
    if (renderFrame) return;
    const schedule = window.requestAnimationFrame || (callback => setTimeout(callback, 0));
    renderFrame = schedule(() => {
      renderFrame = 0;
      const autoFocus = queuedAutoFocus;
      queuedAutoFocus = false;
      renderPanelNow({ autoFocus });
    });
  }

  function clearAll() {
    if (!confirm(`确认清空全部 ${collected.size} 条链接？\n（历史记录也会一并删除）`)) return;
    collected.clear();
    sessionNew.clear();
    GM_setValue(KEY_COLLECTED, '[]');
    showStatus('已清空全部链接');
    renderPanel();
  }

  function clampPanelToViewport() {
    if (!panel || uiState.minimized) return;
    const rect = panel.getBoundingClientRect();
    const maxLeft = Math.max(8, window.innerWidth - rect.width - 8);
    const maxTop = Math.max(8, window.innerHeight - rect.height - 8);
    const left = clamp(rect.left, 8, maxLeft);
    const top = clamp(rect.top, 8, maxTop);
    panel.style.left = `${left}px`;
    panel.style.top = `${top}px`;
    panel.style.right = 'auto';
    saveUiState({ left, top, right: null });
  }

  function makeDraggable(el, handle) {
    let startX = 0;
    let startY = 0;
    let startLeft = 0;
    let startTop = 0;
    let dragging = false;
    handle.addEventListener('mousedown', e => {
      const target = e.target instanceof Element ? e.target : null;
      if (e.button !== 0 || (target && target.closest('button,select,input'))) return;
      e.preventDefault();
      const rect = el.getBoundingClientRect();
      startX = e.clientX;
      startY = e.clientY;
      startLeft = rect.left;
      startTop = rect.top;
      dragging = true;
      document.body.style.userSelect = 'none';
      document.addEventListener('mousemove', drag);
      document.addEventListener('mouseup', stop);
    });
    function drag(e) {
      if (!dragging) return;
      const maxLeft = Math.max(8, window.innerWidth - el.offsetWidth - 8);
      const maxTop = Math.max(8, window.innerHeight - el.offsetHeight - 8);
      el.style.left = `${clamp(startLeft + e.clientX - startX, 8, maxLeft)}px`;
      el.style.top = `${clamp(startTop + e.clientY - startY, 8, maxTop)}px`;
      el.style.right = 'auto';
    }
    function stop() {
      if (!dragging) return;
      dragging = false;
      document.body.style.userSelect = '';
      document.removeEventListener('mousemove', drag);
      document.removeEventListener('mouseup', stop);
      saveUiState({
        left: parseFloat(el.style.left),
        top: parseFloat(el.style.top),
        right: null,
      }, true);
    }
  }

  function startResize(e) {
    e.preventDefault();
    e.stopPropagation();
    const rect = panel.getBoundingClientRect();
    const startX = e.clientX;
    const startY = e.clientY;
    const startWidth = rect.width;
    const startHeight = rect.height;
    const startLeft = rect.left;
    const startTop = rect.top;
    document.body.style.userSelect = 'none';
    const resize = event => {
      const width = clamp(startWidth - (event.clientX - startX), 340, Math.min(680, window.innerWidth - 16));
      const height = clamp(startHeight + (event.clientY - startY), 360, Math.min(900, window.innerHeight - 16));
      const left = clamp(startLeft + (startWidth - width), 8, Math.max(8, window.innerWidth - width - 8));
      panel.style.width = `${width}px`;
      panel.style.height = `${height}px`;
      panel.style.left = `${left}px`;
      panel.style.top = `${clamp(startTop, 8, Math.max(8, window.innerHeight - height - 8))}px`;
      panel.style.right = 'auto';
    };
    const stop = () => {
      document.body.style.userSelect = '';
      document.removeEventListener('mousemove', resize);
      document.removeEventListener('mouseup', stop);
      saveUiState({
        width: panel.getBoundingClientRect().width,
        height: panel.getBoundingClientRect().height,
        left: panel.getBoundingClientRect().left,
        top: panel.getBoundingClientRect().top,
        right: null,
      }, true);
    };
    document.addEventListener('mousemove', resize);
    document.addEventListener('mouseup', stop);
  }

  function syncFromStorage() {
    let changed = false;
    const nextV12Sent = {
      ...safeParseObject(GM_getValue(KEY_LEGACY_V12_SENT, '{}')),
      ...safeParseObject(GM_getValue(KEY_V12_SENT, '{}')),
    };
    const nextV12Mappings = {
      ...safeParseObject(GM_getValue(KEY_LEGACY_V12_MAPPINGS, '{}')),
      ...safeParseObject(GM_getValue(KEY_V12_MAPPINGS, '{}')),
    };
    if (JSON.stringify(nextV12Sent) !== JSON.stringify(v12Sent)) {
      v12Sent = nextV12Sent;
      changed = true;
    }
    if (JSON.stringify(nextV12Mappings) !== JSON.stringify(v12Mappings)) {
      v12Mappings = nextV12Mappings;
      changed = true;
    }
    const storedCollected = readCollectedArray();
    if (storedCollected.length === 0) {
      if (collected.size > 0 || sessionNew.size > 0) {
        collected.clear();
        sessionNew.clear();
        changed = true;
      }
    } else if (mergeCollectedArray(storedCollected)) {
      changed = true;
    }
    if (applyDeletedArray(readDeletedArray())) changed = true;
    const nextCatsRaw = safeParseArray(GM_getValue(KEY_CATEGORIES, '[]'));
    if (nextCatsRaw.length > 0) {
      const nextCats = [...new Set([DEFAULT_CAT, ...nextCatsRaw.filter(cat => typeof cat === 'string' && cat.trim())])];
      if (JSON.stringify(nextCats) !== JSON.stringify(categories)) {
        categories = nextCats;
        if (!categories.includes(currentCat)) currentCat = DEFAULT_CAT;
        changed = true;
      }
    }
    const savedCat = GM_getValue(KEY_CURRENT_CAT, DEFAULT_CAT);
    if (typeof savedCat === 'string' && categories.includes(savedCat) && savedCat !== currentCat) {
      currentCat = savedCat;
      changed = true;
    }
    const nextPaused = GM_getValue(KEY_PAUSED, '1') === '1';
    if (nextPaused !== paused) {
      paused = nextPaused;
      applyPauseStyle();
      changed = true;
    }
    if (changed) {
      rebuildCatSelect();
      rebuildFilterSelect();
      renderPanel();
    }
  }

  function setupCrossTabSync() {
    if (typeof GM_addValueChangeListener === 'function') {
      GM_addValueChangeListener(KEY_COLLECTED, (_name, _old, newValue, remote) => {
        if (!remote) return;
        const remoteItems = readCollectedArray(newValue);
        if (remoteItems.length === 0) {
          if (collected.size > 0 || sessionNew.size > 0) {
            collected.clear();
            sessionNew.clear();
            renderPanel();
          }
          return;
        }
        const merged = mergeCollectedArray(remoteItems);
        const remoteSet = new Set(remoteItems.map(item => item.url));
        const needRepair = [...collected.keys()].some(url => !deleted.has(url) && !remoteSet.has(url));
        if (needRepair) saveCollected();
        if (merged || needRepair) renderPanel();
      });
      GM_addValueChangeListener(KEY_DELETED, (_name, _old, newValue, remote) => {
        if (remote && applyDeletedArray(readDeletedArray(newValue))) renderPanel();
      });
      GM_addValueChangeListener(KEY_CATEGORIES, (_name, _old, _new, remote) => {
        if (remote) syncFromStorage();
      });
      GM_addValueChangeListener(KEY_CURRENT_CAT, (_name, _old, _new, remote) => {
        if (remote) syncFromStorage();
      });
      GM_addValueChangeListener(KEY_PAUSED, (_name, _old, _new, remote) => {
        if (remote) syncFromStorage();
      });
    }
    window.addEventListener('focus', syncFromStorage);
    setInterval(syncFromStorage, 2000);
  }

  function checkCurrentUrl() {
    updateCurrentVideoId();
    const added = collectUrl(location.href) || collectFromModalId(location.href);
    renderPanel({ autoFocus: added });
  }

  function watchRouteChange() {
    const originalPush = history.pushState.bind(history);
    history.pushState = function (...args) {
      originalPush(...args);
      setTimeout(checkCurrentUrl, 300);
    };
    const originalReplace = history.replaceState.bind(history);
    history.replaceState = function (...args) {
      originalReplace(...args);
      setTimeout(checkCurrentUrl, 300);
    };
    window.addEventListener('popstate', () => setTimeout(checkCurrentUrl, 300));
    let lastHref = location.href;
    setInterval(() => {
      if (location.href !== lastHref) {
        lastHref = location.href;
        checkCurrentUrl();
      }
    }, 1000);
  }

  if (GM_getValue(KEY_V12_RESET_20260818, '0') !== '1') {
    resetV12SentForDate('2026-08-18', false);
    GM_setValue(KEY_V12_RESET_20260818, '1');
  }
  createPanel();
  setupCrossTabSync();
  renderPanel();
  checkCurrentUrl();
  watchRouteChange();
  setTimeout(resumePendingImports, 2500);
})();
