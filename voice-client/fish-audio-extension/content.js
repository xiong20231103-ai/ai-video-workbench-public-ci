(() => {
  const extensionApi = globalThis.browser ?? globalThis.chrome;
  const BRIDGE = 'http://127.0.0.1:5178';
  const POLL_MS = 2000;
  // Returned with every heartbeat so the desktop client can detect a stale
  // content script immediately after an extension upgrade.
  const SCRIPT_VERSION = '1.3.47';
  const catalogSelectors = globalThis.FishCatalogSelectors;
  // Fish is often opened as a dedicated, maximised window, but it may briefly
  // become a background tab while Fish redraws its voice catalogue.  A short
  // heartbeat is useful while foregrounded; the bridge keeps the longer
  // timeout separately so Chrome timer throttling cannot mislabel a live task
  // as a failed one.
  const HEARTBEAT_MS = 4_000;
  const activeVoiceApi = globalThis.FishActiveVoice;
  const BROWSER_SESSION_ID = loadBrowserSessionId();
  let busy = false;
  let stopped = false;
  let lastDiagnosticsAt = 0;
  let badge;
  let activeExpectedVoice = '';
  let currentTaskId = '';
  let currentPhase = 'idle';
  let lastVoiceEvidence = {};
  let heartbeatTimer = null;
  let heartbeatInFlight = false;
  // Activation belongs to the current DOM card, not to the page or batch.
  // Fish replaces this card while switching voices; a boolean here previously
  // made the next language skip the native activation after that replacement.
  let primaryVoiceActivation = { element: null, voice: '', clicked: false };
  let voiceNavigationDiagnostics = createVoiceNavigationDiagnostics();

  if (!location.pathname.includes('/app/text-to-speech') || !BROWSER_SESSION_ID) return;
  createBadge();
  startHeartbeatLoop();
  sendDiagnostics().catch(() => {});
  poll();

  async function poll() {
    while (!stopped) {
      try {
        const status = await bridge('/api/bridge/status');
        if (Date.now() - lastDiagnosticsAt > 15_000) {
          await sendDiagnostics();
          lastDiagnosticsAt = Date.now();
        }
        if (status.active && !busy) {
          if (status.checkOnly) {
            setBadge('连接测试成功', '#059669');
            await sendDiagnostics().catch(() => {});
            await delay(POLL_MS);
            continue;
          }
          setBadge(`已连接 · ${status.completed}/${status.total}`, '#059669');
          if (!status.done) await processNext();
          else setBadge(`已完成 ${status.completed}，失败 ${status.failed}`, status.failed ? '#dc2626' : '#059669');
        }
      } catch {
        if (!busy) setBadge('等待客户端开始任务', '#64748b');
      }
      await delay(POLL_MS);
    }
  }

  async function processNext() {
    busy = true;
    let task;
    try {
      const response = await bridge('/api/bridge/next', { method: 'POST', body: '{}' });
      if (response.done || !response.task) return;
      task = response.task;
      currentTaskId = String(task.id || '');
      setPhase('preparing');
      activeExpectedVoice = normalizedTextValue(task.voice);
      setBadge(`处理中：${task.id} · ${task.language}`, '#2563eb');

      await ensurePageReady();
      setPhase('locating_primary_voice_card');
      setBadge(`切换音色：${task.voice}`, '#d97706');
      const selectedEditor = await selectVoice(task.voice);
      if (task.voiceCheckOnly) {
        await bridge('/api/bridge/result', {
          method: 'POST',
          body: JSON.stringify({ taskId: task.id, ok: true, voiceChecked: task.voice })
        });
        setBadge(`音色检查成功：${task.voice}`, '#059669');
        return;
      }
      let editor = selectedEditor || await waitFor(findEditor, 60_000, '找不到 Fish Audio 正文编辑区');
      setPhase('preparing_editor');
      const baselineCharacterCount = await clearAllSpeakerEditors();
      editor = findEditor() || editor;
      if (!editor?.isConnected) throw new Error(`清空文本后找不到音色“${task.voice}”的正文编辑区`);
      setPhase('verifying_voice');
      assertActiveVoice(task.voice);
      setPhase('writing_text');
      await fillEditor(editor, task.text);
      await waitFor(
        () => editorMatches(editor, task.text),
        15_000,
        'Fish Audio 正文没有更新，已停止生成'
      );
      const expectedCharacterCount = baselineCharacterCount + new TextEncoder().encode(task.text).length;
      const characterCountTolerance = Math.max(
        2,
        (task.text.match(/\n/g) || []).length * 2
      );
      // Current Fish UI variants omit the character counter entirely.  Text
      // insertion above is independently verified from the live editor, so a
      // missing counter is not an error.  When the counter exists, retain the
      // stricter consistency check.
      if (readCharacterCount() !== null) {
        try {
          await waitFor(
            () => characterCountMatches(expectedCharacterCount, characterCountTolerance),
            15_000,
            'Fish Audio 字符计数没有同步'
          );
        } catch {
          const actualCount = readCharacterCount();
          const actualTextLength = String(editor.innerText || editor.textContent || editor.value || '').length;
          throw new Error(
            `Fish Audio 字符计数未同步：预期约 ${expectedCharacterCount}（允许 ±${characterCountTolerance}），` +
            `实际 ${actualCount ?? '未知'}，编辑框 ${actualTextLength}，已停止生成`
          );
        }
      }

      setPhase('verifying_voice');
      const voiceVerification = assertActiveVoice(task.voice);
      const generateButton = await waitFor(findGenerateButton, 60_000, '找不到可用的“生成语音”按钮');
      const beforeControls = findPlayerDownloadControls(editor, generateButton);
      const beforePlayerSignature = playerSignature(beforeControls);
      await startGeneration(editor, generateButton, beforeControls, beforePlayerSignature);
      setPhase('generating');
      setBadge(`正在生成：${task.id}`, '#7c3aed');

      const result = await waitForGeneration(editor, generateButton, beforeControls, beforePlayerSignature, 10 * 60_000);
      setPhase('downloading');
      setBadge(`正在下载：${task.id}`, '#0891b2');
      const download = await downloadResult(result, task.outputName);

      const reported = await bridge('/api/bridge/result', {
        method: 'POST',
        body: JSON.stringify({
          taskId: task.id,
          ok: true,
          download,
          expectedVoice: voiceVerification.evidence.expected,
          actualActiveVoice: voiceVerification.actualActiveVoice,
          voiceVerification: voiceVerification.evidence
        })
      });
      setPhase('completed');
      setBadge(`已完成：${task.id}`, '#059669');
      if (reported?.done) await closeOwnedFishTab('completed');
      else await interactionPause(1800, 2800);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      const errorCode = String(error?.code || classifyAutomationError(message));
      const failurePhase = currentPhase;
      if (error?.retryable === false || errorCode === 'COPY_ASSIGNMENT_CONFLICT' ||
          errorCode === 'COPY_LEGACY_IDENTITY_UNVERIFIED' || errorCode === 'COPY_SAVED_TEXT_HASH_MISMATCH' ||
          errorCode === 'COPY_TEXT_HASH_CONFLICT' || errorCode === 'COPY_IDENTITY_CONFLICT') {
        stopped = true;
        if (heartbeatTimer) {
          clearInterval(heartbeatTimer);
          heartbeatTimer = null;
        }
        setPhase('blocked');
        setBadge(`已停止：${message}`, '#dc2626');
        return;
      }
      setPhase('failed');
      setBadge(`失败：${message}`, '#dc2626');
      if (task) {
        const reported = await bridge('/api/bridge/result', {
          method: 'POST',
          body: JSON.stringify({
            taskId: task.id,
            ok: false,
            error: message,
            errorCode,
            phase: failurePhase,
            voiceEvidence: lastVoiceEvidence
          })
        }).catch(() => {});
        if (reported?.retry) {
          const retryLabel = reported.retryReason === 'download'
            ? '下载失败'
            : reported.retryReason === 'voice-selection'
              ? '音色切换未完成'
              : '页面操作未完成';
          const retryLimit = reported.maxVoiceDialogRetries ?? reported.maxDownloadRetries ?? 1;
          setBadge(`${retryLabel}，自动重试 ${reported.retryAttempt}/${retryLimit}`, '#d97706');
          if (errorCode === 'FISH_PAGE_NOT_STABLE') {
            // A page that never settled is retried on a fresh load; the
            // bridge hands the same task back to the reloaded script.
            await delay(1500);
            location.reload();
          }
        }
        if (reported?.done && !reported?.loginRequired) await closeOwnedFishTab('terminal-failure');
      }
      await delay(3000);
    } finally {
      activeExpectedVoice = '';
      if (currentPhase !== 'completed') setPhase('idle');
      currentTaskId = '';
      busy = false;
    }
  }

  async function ensurePageReady() {
    if (!location.pathname.includes('/app/text-to-speech')) {
      throw new Error('请停留在 Fish Audio 文本转语音页面');
    }
    await ensureCurrentFishWindowMaximized();
    await dismissTextToSpeechWelcome();
    const login = Array.from(document.querySelectorAll('button')).find((item) => normalizedText(item) === '登录 / 注册');
    if (login && visible(login)) throw new Error('Fish Audio 尚未登录');
    await waitFor(
      // Before a speaker is selected, Fish renders the correct Generate
      // control in a disabled state. It is still sufficient evidence that
      // the text-to-speech page is ready; selectVoice() enables it later.
      () => findEditor() && findAnyGenerateButton(),
      30_000,
      () => `Fish Audio 页面尚未加载完成：${pageReadinessDetail()}`
    );
    await waitForStablePrimaryVoiceWorkspace(60_000);
  }

  async function waitForStablePrimaryVoiceWorkspace(timeoutMs = 45_000) {
    let lastCard = null;
    let lastEditor = null;
    let lastSignature = '';
    let stableSince = 0;
    return waitFor(() => {
      const editor = findEditor();
      const card = findPrimaryVoiceCard();
      if (!editor?.isConnected || !isUsablePrimaryVoiceCard(card)) {
        lastCard = null;
        lastEditor = null;
        lastSignature = '';
        stableSince = 0;
        voiceNavigationDiagnostics.pageStable = false;
        return null;
      }
      const voice = voiceNameFromCardElement(card);
      const rect = card.getBoundingClientRect();
      if (!voice) {
        stableSince = 0;
        voiceNavigationDiagnostics.pageStable = false;
        return null;
      }
      const signature = [
        voice,
        Math.round(rect.left), Math.round(rect.top),
        Math.round(rect.width), Math.round(rect.height)
      ].join('|');
      if (card !== lastCard || editor !== lastEditor || signature !== lastSignature) {
        lastCard = card;
        lastEditor = editor;
        lastSignature = signature;
        stableSince = Date.now();
        voiceNavigationDiagnostics.pageStable = false;
        voiceNavigationDiagnostics.stableVoice = voice;
        voiceNavigationDiagnostics.stableRect = rectInfo(card);
        return null;
      }
      const stableMs = Date.now() - stableSince;
      voiceNavigationDiagnostics.stableForMs = stableMs;
      if (stableMs < 1_800) return null;
      voiceNavigationDiagnostics.pageStable = true;
      return card;
    }, timeoutMs, () => automationError(
      'FISH_PAGE_NOT_STABLE',
      `Fish Audio 页面仍在加载或重绘，尚未形成稳定的正文编辑区和主音色卡：${pageReadinessDetail()}`
    ));
  }

  function isUsablePrimaryVoiceCard(card) {
    if (!card?.isConnected || !visible(card)) return false;
    const rect = card.getBoundingClientRect();
    return rect.left >= innerWidth * 0.55
      && rect.right <= innerWidth + 2
      && rect.top >= 40
      && rect.bottom <= innerHeight + 2
      && rect.width >= 220
      && rect.height >= 60;
  }

  async function dismissTextToSpeechWelcome() {
    // Fish occasionally shows a first-run tutorial over the text-to-speech
    // editor.  Dismiss only the exact, known onboarding card; never click a
    // generic close button or an unrelated dialog.
    const welcomes = Array.from(document.querySelectorAll('[role="dialog"], [aria-modal="true"], body *'))
      .filter(visible)
      .filter((element) => {
        const text = normalizedText(element);
        return text.includes('欢迎使用文字转语音')
          && text.includes('我们准备了一个示例')
          && text.includes('跳过');
      });
    const match = welcomes
      .map((welcome) => ({
        welcome,
        skip: Array.from(welcome.querySelectorAll('button, [role="button"], [tabindex="0"]'))
          .filter(visible)
          .find((element) => normalizedText(element) === '跳过'),
      }))
      .find((entry) => entry.skip);
    if (!match) {
      if (welcomes.length === 0) return;
      throw new Error('Fish Audio 首次引导遮挡了文字转语音页面，但未找到“跳过”按钮');
    }
    const { welcome, skip } = match;
    skip.click();
    await waitFor(
      () => !welcome.isConnected || !visible(welcome),
      6_000,
      'Fish Audio 首次引导未能关闭，已停止以避免误操作'
    );
  }

  async function selectVoice(voice) {
    const desired = normalizedTextValue(voice);
    if (!desired || desired.toLowerCase() === 'default') return findEditor();
    voiceNavigationDiagnostics = createVoiceNavigationDiagnostics(desired);

    // A voice listed in the right panel is merely available, not active.
    // Fish can retain several rows there (for example Russian plus Ben), while
    // the editor still belongs to the first speaker.  Only the primary
    // speaker at the top-left and the primary settings card prove activation.
    try {
      assertActiveVoice(desired);
      return await waitFor(findEditor, 10_000, '找不到当前主音色的正文编辑区');
    } catch {
      // Continue to the replacement chooser.  The final assertion below is
      // authoritative and prevents a stale/available row from being accepted.
    }

    const { card, catalog } = await openVoiceCatalogFromPrimary();
    const option = await revealSavedVoiceOption(desired, card, catalog);
    await interactionPause(600, 1000);
    option.click();

    setPhase('verifying_voice');
    try {
      await waitForStableActiveVoice(desired, 20_000);
    } catch (error) {
      if (error?.code === 'VOICE_EVIDENCE_CONFLICT' || error?.code === 'VOICE_VERIFICATION_FAILED') throw error;
      throw automationError(
        'VOICE_TARGET_ACTIVATION_FAILED',
        `音色“${desired}”点击后未能确认已激活`,
        { cause: error instanceof Error ? error.message : String(error) }
      );
    }
    return await waitFor(findEditor, 10_000, '切换音色后找不到正文编辑区');
  }

  async function openVoiceCatalogFromPrimary() {
    // The only supported entry is the crossed-arrows switch control in the
    // right-hand primary voice card.  “Add another voice” enables multi-speaker
    // mode and is deliberately excluded from every candidate set below.
    const card = await waitForStablePrimaryVoiceWorkspace(45_000).catch((error) => {
      if (error?.code === 'FISH_PAGE_NOT_STABLE') throw error;
      throw automationError(
        'PRIMARY_VOICE_CARD_NOT_FOUND',
        '找不到右侧当前主音色卡片；已停止以避免点击“添加另一个音色”'
      );
    });
    if (!card) {
      throw automationError(
        'PRIMARY_VOICE_CARD_NOT_FOUND',
        '找不到右侧当前主音色卡片；已停止以避免点击“添加另一个音色”'
      );
    }
    voiceNavigationDiagnostics.primaryVoiceCard = {
      text: normalizedText(card).slice(0, 240),
      voice: voiceNameFromCardElement(card),
      rect: rectInfo(card)
    };
    for (let attempt = 0; attempt < 3; attempt += 1) {
      const currentCard = await waitForStablePrimaryVoiceWorkspace(30_000);
      if (!currentCard) throw automationError('PRIMARY_VOICE_CARD_NOT_FOUND', '右侧当前主音色卡在页面重绘后消失');
      const currentVoice = voiceNameFromCardElement(currentCard);
      if (
        primaryVoiceActivation.element !== currentCard
        || !primaryVoiceActivation.element?.isConnected
        || primaryVoiceActivation.voice !== currentVoice
      ) {
        primaryVoiceActivation = { element: currentCard, voice: currentVoice, clicked: false };
      }
      setPhase('waiting_for_switch_button');
      currentCard.scrollIntoView({ block: 'center', behavior: 'auto' });
      await interactionPause(350, 650);
      hoverPrimaryVoiceCard(currentCard);
      await nativeHoverPrimaryVoiceCard(currentCard);
      voiceNavigationDiagnostics.hoverCompleted = true;
      let switchControl = await waitFor(
        () => findVoiceSwitchControl(currentCard),
        1_800,
        ''
      ).catch(() => null);

      if (!switchControl && !primaryVoiceActivation.clicked) {
        const voiceNameElement = findPrimaryVoiceNameElement(currentCard);
        if (!voiceNameElement) {
          voiceNavigationDiagnostics.failureStage = 'primary_voice_name_not_found';
          throw automationError(
            'PRIMARY_VOICE_NAME_NOT_FOUND',
            '已找到右侧主音色卡，但无法唯一定位当前音色名称；已停止以避免点击整张卡片'
          );
        }
        voiceNavigationDiagnostics.voiceNameElementDetected = true;
        voiceNavigationDiagnostics.voiceNameElement = {
          text: normalizedText(voiceNameElement),
          className: String(voiceNameElement.className || '').slice(0, 240),
          rect: rectInfo(voiceNameElement)
        };
        setPhase('activating_primary_voice_name');
        const activation = await activatePrimaryVoiceName(currentCard, voiceNameElement);
        if (!activation?.ok) {
          throw automationError('PRIMARY_VOICE_ACTIVATION_FAILED', '无法用原生指针激活当前主音色卡');
        }
        primaryVoiceActivation.clicked = true;
        voiceNavigationDiagnostics.voiceNameClicked = Boolean(activation?.ok);
        voiceNavigationDiagnostics.primaryVoiceNameActivation = activation || null;
        await interactionPause(650, 950);
        switchControl = await waitFor(
          () => findVoiceSwitchControl(findPrimaryVoiceCard()),
          4_000,
          ''
        ).catch(() => null);
      }
      if (!switchControl) {
        await interactionPause(900, 1400);
        continue;
      }

      voiceNavigationDiagnostics.switchButtonDetected = true;
      voiceNavigationDiagnostics.switchButton = {
        label: controlLabel(switchControl).slice(0, 160),
        rect: rectInfo(switchControl)
      };
      setPhase('opening_voice_catalog');
      switchControl.click();
      const catalog = await waitFor(
        findVoiceCatalog,
        10_000,
        ''
      ).catch(() => null);
      if (catalog) {
        voiceNavigationDiagnostics.catalogDetected = true;
        await interactionPause(700, 1100);
        return { card: findPrimaryVoiceCard() || currentCard, catalog };
      }
      await interactionPause(900, 1400);
    }
    voiceNavigationDiagnostics.failureStage = voiceNavigationDiagnostics.switchButtonDetected
      ? 'catalog_not_opened'
      : 'switch_button_not_found';
    throw automationError(
      voiceNavigationDiagnostics.switchButtonDetected
        ? 'VOICE_CATALOG_NOT_OPENED'
        : 'VOICE_SWITCH_CONTROL_NOT_FOUND',
      voiceNavigationDiagnostics.switchButtonDetected
        ? '已点击右侧音色卡切换图标，但没有打开完整音色库'
        : '右侧音色卡悬停后没有出现交叉箭头切换图标'
    );
  }

  async function activatePrimaryVoiceName(card, voiceNameElement) {
    hoverPrimaryVoiceName(card, voiceNameElement);
    await interactionPause(300, 550);
    const rect = voiceNameElement.getBoundingClientRect();
    const x = Math.round(rect.left + (rect.width / 2));
    const y = Math.round(rect.top + (rect.height / 2));
    try {
      const result = await extensionApi.runtime.sendMessage({
        type: 'hover-and-click-primary-voice-name',
        x,
        y,
        text: normalizedText(voiceNameElement).slice(0, 120)
      });
      if (result?.ok) {
        voiceNavigationDiagnostics.browserPointerActivation = {
          method: result.method || 'unknown', x, y, tabId: result.tabId ?? null
        };
        return result;
      }
      voiceNavigationDiagnostics.browserPointerActivation = {
        method: 'unavailable', x, y, error: String(result?.error || 'unknown error')
      };
    } catch (error) {
      voiceNavigationDiagnostics.browserPointerActivation = {
        method: 'unavailable', x, y, error: String(error)
      };
    }

    // Firefox and older Chromium builds cannot expose debugger input to an
    // extension. Keep the old DOM fallback for compatibility; it is recorded
    // as such and does not pretend to have created a native hover state.
    voiceNameElement.click();
    return { ok: false, method: 'dom-click-fallback', x, y };
  }

  async function nativeHoverPrimaryVoiceCard(card) {
    if (!card) return { ok: false, error: 'missing card' };
    if (!isUsablePrimaryVoiceCard(card)) {
      const result = { ok: false, error: 'primary voice card is detached, hidden, or outside the settings panel' };
      voiceNavigationDiagnostics.nativeHover = result;
      return result;
    }
    const rect = card.getBoundingClientRect();
    const x = Math.round(rect.left + Math.min(Math.max(rect.width * 0.78, 24), Math.max(24, rect.width - 12)));
    const y = Math.round(rect.top + Math.min(Math.max(rect.height * 0.18, 18), Math.max(18, rect.height - 12)));
    try {
      const result = await extensionApi.runtime.sendMessage({ type: 'move-pointer-to-primary-voice-card', x, y });
      voiceNavigationDiagnostics.nativeHover = { ...result, x, y };
      return result || { ok: false, error: 'no response' };
    } catch (error) {
      voiceNavigationDiagnostics.nativeHover = { ok: false, x, y, error: String(error) };
      return { ok: false, error: String(error) };
    }
  }

  function findPrimaryVoiceCard() {
    // The current Fish page puts “添加另一个音色” inside the same outer card
    // as the primary voice.  Therefore the name span is the authoritative
    // anchor; rejecting an ancestor merely because it also contains that
    // footer would incorrectly reject the real card.
    const nameElement = findRightPrimaryVoiceNameElement();
    if (nameElement) {
      const anchored = [];
      for (
        let node = nameElement.parentElement, depth = 0;
        node && node !== document.body && depth < 8;
        node = node.parentElement, depth += 1
      ) {
        const rect = node.getBoundingClientRect();
        const text = normalizedText(node);
        if (
          visible(node)
          && rect.left >= innerWidth * 0.55
          && rect.top >= 40
          && rect.top < Math.min(innerHeight * 0.7, 720)
          && rect.width >= 220
          && rect.width <= Math.min(760, innerWidth * 0.48)
          && rect.height >= 70
          && rect.height <= 360
          && (
            Boolean(node.querySelector('img'))
            || Boolean(node.querySelector('button, [role="button"]'))
            || /(?:添加另一个音色|添加另一个声音|add another voice)/i.test(text)
          )
        ) {
          anchored.push(node);
        }
      }
      if (anchored.length > 0) {
        anchored.sort((a, b) => area(a) - area(b));
        return anchored[0];
      }
    }

    const candidates = Array.from(document.querySelectorAll('section, article, li, div'))
      .filter(visible)
      .filter((element) => {
        const rect = element.getBoundingClientRect();
        const text = normalizedText(element);
        return rect.left >= innerWidth * 0.55
          && rect.top >= 40
          && rect.top < Math.min(innerHeight * 0.62, 620)
          && rect.width >= 220
          && rect.width <= Math.min(720, innerWidth * 0.46)
          && rect.height >= 70
          && rect.height <= 280
          && text.length > 0
          && text.length < 600
          && Boolean(element.querySelector('img'));
      })
      .map((element) => ({ element, score: primaryVoiceCardScore(element) }))
      .sort((a, b) => b.score - a.score);
    return candidates.length > 0 && candidates[0].score >= 50 ? candidates[0].element : null;
  }

  function primaryVoiceCardScore(element) {
    const rect = element.getBoundingClientRect();
    const text = normalizedText(element);
    let score = 0;
    if (rect.left >= innerWidth * 0.72) score += 25;
    if (rect.top < 360) score += 20;
    if (rect.height >= 100 && rect.height <= 240) score += 20;
    if (element.querySelector('img')) score += 20;
    if (voiceNameFromCardElement(element)) score += 25;
    if (Array.from(element.querySelectorAll('button, [role="button"]')).length >= 2) score += 15;
    score -= Math.max(0, rect.height - 180) / 10;
    return score;
  }

  function hoverPrimaryVoiceCard(card) {
    card.focus?.({ preventScroll: true });
    for (const type of ['pointerover', 'pointerenter']) {
      try {
        card.dispatchEvent(new PointerEvent(type, {
          bubbles: type === 'pointerover',
          pointerType: 'mouse',
          isPrimary: true
        }));
      } catch {
        // Older Chromium builds may not expose PointerEvent in isolated worlds.
      }
    }
    for (const type of ['mouseover', 'mouseenter']) {
      card.dispatchEvent(new MouseEvent(type, {
        bubbles: type === 'mouseover',
        view: window
      }));
    }
  }

  function findPrimaryVoiceNameElement(card) {
    if (!card) return null;
    const anchored = findRightPrimaryVoiceNameElement();
    if (anchored && card.contains(anchored)) return anchored;
    const expected = voiceNameFromCardElement(card);
    if (!expected) return null;
    const cardRect = card.getBoundingClientRect();
    const matches = Array.from(card.querySelectorAll('span, strong, b, p, div'))
      .filter(visible)
      .map((element) => {
        const text = normalizedText(element);
        const rect = element.getBoundingClientRect();
        const className = String(element.className || '');
        let score = 0;
        if (text === expected) score += 100;
        else if (activeVoiceApi?.sameVoice?.(text, expected)) score += 80;
        if (element.tagName === 'SPAN') score += 20;
        if (/\btruncate\b/.test(className)) score += 20;
        if (/\bfont-medium\b/.test(className)) score += 15;
        if (/\bleading-5\b/.test(className)) score += 10;
        if (/text-\[14px\]/.test(className)) score += 10;
        if (element.children.length === 0) score += 10;
        if (rect.top < cardRect.top + Math.min(80, cardRect.height * 0.5)) score += 10;
        return { element, text, rect, score };
      })
      .filter(({ text, rect, score }) =>
        score >= 100
        && rect.left >= cardRect.left
        && rect.right <= cardRect.right + 1
        && rect.top >= cardRect.top
        && rect.bottom <= cardRect.bottom + 1
        && !/(?:添加另一个音色|添加另一个声音|add another voice|删除|移除)/i.test(text)
      )
      .sort((a, b) =>
        b.score - a.score
        || area(a.element) - area(b.element)
      );
    if (matches.length === 0) return null;
    if (matches.length > 1 && matches[0].score === matches[1].score) {
      const firstRect = matches[0].rect;
      const secondRect = matches[1].rect;
      if (
        Math.abs(firstRect.left - secondRect.left) > 2
        || Math.abs(firstRect.top - secondRect.top) > 2
      ) return null;
    }
    return matches[0].element;
  }

  function findRightPrimaryVoiceNameElement() {
    const headerSelector = findVoiceSelector();
    const headerVoice = activeVoiceApi?.headerVoiceFromSelector
      ? activeVoiceApi.headerVoiceFromSelector(normalizedText(headerSelector))
      : normalizedText(headerSelector);
    const candidates = Array.from(document.querySelectorAll(
      'span.min-w-0.flex-1.truncate, span.truncate.font-medium, span[class*="truncate"][class*="font-medium"]'
    ))
      .filter(visible)
      .map((element) => {
        const text = normalizedText(element);
        const rect = element.getBoundingClientRect();
        const className = String(element.className || '');
        let score = 0;
        if (headerVoice && activeVoiceApi?.sameVoice?.(text, headerVoice)) score += 100;
        if (/\bmin-w-0\b/.test(className)) score += 20;
        if (/\bflex-1\b/.test(className)) score += 20;
        if (/\btruncate\b/.test(className)) score += 20;
        if (/\bfont-medium\b/.test(className)) score += 15;
        if (/\bleading-5\b/.test(className)) score += 10;
        if (/text-\[14px\]/.test(className)) score += 10;
        if (element.children.length === 0) score += 10;
        return { element, text, rect, score };
      })
      .filter(({ text, rect, score }) =>
        text.length > 0
        && text.length <= 80
        && score >= (headerVoice ? 100 : 65)
        && rect.left >= innerWidth * 0.55
        && rect.top >= 40
        && rect.top < Math.min(innerHeight * 0.7, 720)
        && rect.width >= 20
        && rect.height >= 14
        && rect.height <= 80
        && !/^(?:语音|音色|声音|设置|历史)$/i.test(text)
        && !/(?:添加另一个音色|添加另一个声音|add another voice|删除|移除)/i.test(text)
      )
      .sort((a, b) => b.score - a.score || a.rect.top - b.rect.top || a.rect.left - b.rect.left);
    if (candidates.length === 0) return null;
    if (candidates.length > 1 && candidates[0].score === candidates[1].score) {
      const first = candidates[0];
      const second = candidates[1];
      if (!activeVoiceApi?.sameVoice?.(first.text, second.text)) return null;
    }
    return candidates[0].element;
  }

  function hoverPrimaryVoiceName(card, voiceNameElement) {
    hoverPrimaryVoiceCard(card);
    voiceNameElement.scrollIntoView({ block: 'nearest', inline: 'nearest', behavior: 'auto' });
    voiceNameElement.focus?.({ preventScroll: true });
    for (const type of ['pointerover', 'pointerenter', 'pointermove']) {
      try {
        voiceNameElement.dispatchEvent(new PointerEvent(type, {
          bubbles: type !== 'pointerenter',
          pointerType: 'mouse',
          isPrimary: true
        }));
      } catch {
        // Older Chromium builds may not expose PointerEvent in isolated worlds.
      }
    }
    for (const type of ['mouseover', 'mouseenter', 'mousemove']) {
      voiceNameElement.dispatchEvent(new MouseEvent(type, {
        bubbles: type !== 'mouseenter',
        view: window
      }));
    }
  }

  function findVoiceSwitchControl(card) {
    if (!card) return null;
    const cardRect = card.getBoundingClientRect();
    const candidates = Array.from(document.querySelectorAll('button, [role="button"], [tabindex="0"]'))
      .filter((element) => {
        const rect = element.getBoundingClientRect();
        const label = controlLabel(element);
        const withinCard = card.contains(element);
        const nearCardPortal = rect.left >= cardRect.right - 100
          && rect.left <= cardRect.right + 90
          && rect.top >= cardRect.top - 30
          && rect.top <= cardRect.top + 120;
        return rect.width >= 22
          && rect.width <= 72
          && rect.height >= 22
          && rect.height <= 72
          && (withinCard || nearCardPortal)
          && !/(?:添加|add another|删除|移除|垃圾|trash|delete|remove)/i.test(label);
      })
      .sort((a, b) => a.getBoundingClientRect().left - b.getBoundingClientRect().left);
    const explicitlyNamed = candidates.find((element) =>
      /(?:切换|替换|更换|shuffle|switch|change|replace)/i.test(controlLabel(element))
      || Boolean(element.querySelector('svg[data-lucide*="shuffle"], svg[data-icon*="shuffle"], [aria-label*="switch" i]'))
    );
    if (explicitlyNamed) return explicitlyNamed;

    const safeControls = candidates.filter((element) => !/(?:删除|移除|垃圾|trash|delete|remove)/i.test(controlLabel(element)));
    // The fallback is allowed only for exactly one safe candidate.  Guessing
    // the leftmost of several controls was the source of accidental clicks.
    if (safeControls.length === 1) return safeControls[0];
    return null;
  }

  function voiceNameFromCardElement(card) {
    if (!card) return '';
    const anchored = findRightPrimaryVoiceNameElement();
    if (anchored && card.contains(anchored)) return normalizedText(anchored);
    const candidates = Array.from(card.querySelectorAll('h1, h2, h3, h4, strong, b, span, p, div'))
      .filter(visible)
      .map((element) => ({
        text: normalizedText(element),
        raw: String(element.innerText || element.textContent || ''),
        rect: element.getBoundingClientRect()
      }))
      .filter(({ text, rect }) =>
        text.length > 0
        && text.length <= 100
        && rect.top < card.getBoundingClientRect().top + 90
        && !/^(?:语音|音色|声音|男性|女性|男声|女声|中年|青年|老年|EN|PT|RU)$/i.test(text)
        && !/^\d+(?:\.\d+)?\s*[KkMm]?$/.test(text)
      )
      .sort((a, b) => a.rect.top - b.rect.top || a.rect.left - b.rect.left);
    for (const candidate of candidates) {
      const name = activeVoiceApi?.voiceNameFromCard
        ? activeVoiceApi.voiceNameFromCard(candidate.raw)
        : candidate.text;
      if (name && name.length <= 80) return name;
    }
    return activeVoiceApi?.voiceNameFromCard
      ? activeVoiceApi.voiceNameFromCard(String(card.innerText || card.textContent || ''))
      : '';
  }

  function activeVoiceEvidence(expectedVoice = '') {
    const primaryCard = findPrimaryVoiceCard();
    const rightCardVoice = voiceNameFromCardElement(primaryCard);
    // "文本转语音" is a page heading, not a speaker name.  Do not let it
    // conflict with an otherwise correct selected voice.
    const editorVoiceCandidate = findEditorVoice();
    const editorVoice = isGenericEditorVoiceLabel(editorVoiceCandidate) ? '' : editorVoiceCandidate;
    const availableVoices = Array.from(new Set(
      voiceSettingCandidates({ includeText: true })
        .filter(visible)
        .map((element) => normalizedText(element))
        .filter((text) => text && text.length < 100)
    ));
    const headerSelector = findVoiceSelector();
    const headerVoice = activeVoiceApi?.headerVoiceFromSelector
      ? activeVoiceApi.headerVoiceFromSelector(normalizedText(headerSelector))
      : '';
    return {
      expectedVoice: normalizedTextValue(expectedVoice),
      headerVoice,
      editorVoice,
      rightCardVoice,
      primarySettingVoice: rightCardVoice,
      availableVoices,
      editorCount: findEditors().length
    };
  }

  function findEditorVoice() {
    const editor = findEditor();
    if (!editor) return '';
    const editorRect = editor.getBoundingClientRect();
    const headerSelector = findVoiceSelector();
    const candidates = Array.from(document.querySelectorAll('h1, h2, h3, h4, strong, b, span, p, button, [role="button"]'))
      .filter(visible)
      .filter((element) => element !== headerSelector && !headerSelector?.contains(element))
      .map((element) => ({
        element,
        raw: String(element.innerText || element.textContent || ''),
        text: normalizedText(element),
        rect: element.getBoundingClientRect()
      }))
      .filter(({ text, rect }) =>
        text.length > 0
        && text.length <= 80
        && rect.bottom <= editorRect.top + 12
        && rect.bottom >= editorRect.top - 120
        && rect.left >= editorRect.left - 80
        && rect.left <= editorRect.right
        && !/^(?:标签|自动加标签|添加说话人|字符|语音|音色|声音)$/i.test(text)
      )
      .sort((a, b) =>
        Math.abs(a.rect.bottom - editorRect.top) - Math.abs(b.rect.bottom - editorRect.top)
        || a.rect.left - b.rect.left
      );
    for (const candidate of candidates) {
      const name = activeVoiceApi?.voiceNameFromCard
        ? activeVoiceApi.voiceNameFromCard(candidate.raw)
        : candidate.text;
      if (name && name.length <= 80) return name;
    }
    return '';
  }

  function isGenericEditorVoiceLabel(value) {
    const normalized = normalizedTextValue(value).toLocaleLowerCase();
    return new Set([
      '\u6587\u672c\u8f6c\u8bed\u97f3',
      '\\u6587\\u5b57\\u8f6c\\u8bed\\u97f3',
      '\\u8bed\\u97f3\\u5408\\u6210',
      'text to speech',
      'voice synthesis',
      '\u6dfb\u52a0\u8bf4\u8bdd\u4eba',
      'add speaker'
    ]).has(normalized);
  }

  function assertActiveVoice(expectedVoice) {
    if (!activeVoiceApi?.evaluate) {
      throw new Error('Fish 音色验证模块未加载；请重新加载扩展后再继续');
    }
    const evidence = activeVoiceEvidence(expectedVoice);
    const verdict = activeVoiceApi.evaluate(evidence);
    lastVoiceEvidence = verdict.evidence || evidence;
    if (!verdict.ok) {
      const errorCode = verdict.reason === 'voice_evidence_conflict'
        ? 'VOICE_EVIDENCE_CONFLICT'
        : verdict.reason === 'insufficient_voice_evidence'
          ? 'VOICE_EVIDENCE_INCOMPLETE'
          : 'VOICE_VERIFICATION_FAILED';
      throw automationError(
        errorCode,
        `Fish 音色不匹配：要求“${evidence.expectedVoice}”，当前主音色“${verdict.actualActiveVoice || '未识别'}”；` +
        `原因 ${verdict.reason}。已阻止填写、生成和下载`,
        { voiceEvidence: lastVoiceEvidence }
      );
    }
    return verdict;
  }

  async function waitForStableActiveVoice(expectedVoice, timeoutMs = 20_000) {
    let stableSince = 0;
    let stableSignature = '';
    let lastVerificationError = null;
    try {
      return await waitFor(() => {
        try {
          const verdict = assertActiveVoice(expectedVoice);
          const signature = [
            verdict.evidence.header,
            verdict.evidence.editor,
            verdict.evidence.rightCard
          ].join('\\u0000').toLocaleLowerCase();
          if (signature !== stableSignature) {
            stableSignature = signature;
            stableSince = Date.now();
            return null;
          }
          return Date.now() - stableSince >= 900 ? verdict : null;
        } catch (error) {
          lastVerificationError = error;
          stableSince = 0;
          stableSignature = '';
          return null;
        }
      }, timeoutMs, () => lastVerificationError || `Fish Audio 音色没有稳定切换到“${expectedVoice}”`);
    } catch (error) {
      if (lastVerificationError?.code) throw lastVerificationError;
      throw error;
    }
  }

  function voiceSettingCandidates({ includeText = false } = {}) {
    const selector = includeText
      ? 'button, [role="button"], [role="combobox"], div, span, p'
      : 'button, [role="button"], [role="combobox"]';
    return Array.from(document.querySelectorAll(selector))
      .filter((element) => isVoiceSettingsArea(element));
  }

  function isVoiceSettingsArea(element) {
    if (!element || !visible(element)) return false;
    const rect = element.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return false;
    const labels = `${element.getAttribute('aria-label') || ''} ${element.getAttribute('title') || ''} ${normalizedText(element)}`;
    if (/音色|声音|语音|voice|speaker/i.test(labels)) return true;
    for (let parent = element.parentElement; parent && parent !== document.body; parent = parent.parentElement) {
      const parentLabels = `${parent.getAttribute('aria-label') || ''} ${parent.getAttribute('data-slot') || ''} ${normalizedText(parent)}`;
      if (/音色|声音|语音|voice|speaker|settings|设置/i.test(parentLabels)) return true;
      const parentRect = parent.getBoundingClientRect();
      if (parent.matches('aside, [role="complementary"]') && parentRect.width >= 180) return true;
    }
    // Last-resort layout fallback for Fish variants that expose no accessible
    // settings labels.  This deliberately uses a broad right pane, not a
    // fixed 75%/320px coordinate box.
    return rect.left >= innerWidth * 0.55 && rect.width >= 24;
  }

  async function revealSavedVoiceOption(desired, selector, initialCatalog) {
    let catalog = initialCatalog || await waitFor(
      findVoiceCatalog,
      10_000,
      'Fish Audio 没有打开完整音色库'
    );
    setPhase('waiting_for_catalog_tabs');
    let savedResult;
    try {
      savedResult = await waitForStableCatalogControl(
        ['已收藏', 'Saved'],
        15_000,
        'VOICE_SAVED_TAB_TIMEOUT',
        '完整音色库标签尚未加载完成，找不到“已收藏”入口'
      );
    } catch (error) {
      await dismissVoiceCatalogIfOpen();
      throw error;
    }
    catalog = savedResult.catalog;
    const savedTab = savedResult.control;
    voiceNavigationDiagnostics.savedTabDetected = true;
    voiceNavigationDiagnostics.savedTabLabel = controlLabel(savedTab).slice(0, 120);
    voiceNavigationDiagnostics.catalogTabsReadyAt = new Date().toISOString();
    if (!isSelectedControl(savedTab)) {
      setPhase('selecting_saved_voice');
      savedTab.click();
      await interactionPause(700, 1200);
      try {
        await waitFor(() => {
          const currentCatalog = findVoiceCatalog() || catalog;
          const currentSavedTab = findExactTextControl(currentCatalog, ['已收藏', 'Saved']);
          return isSelectedControl(currentSavedTab)
            || catalogSelectors.isDirectSavedList(normalizedText(currentCatalog), Boolean(findVoiceOption(currentCatalog, desired, selector)))
            || Boolean(findExactTextControl(currentCatalog, ['音频1', 'Audio 1']));
        }, 5_000, '“已收藏”标签点击后没有确认进入');
      } catch (error) {
        throw automationError(
          'VOICE_SAVED_TAB_NOT_SELECTED',
          '点击“已收藏”后没有确认进入收藏内容',
          { cause: error instanceof Error ? error.message : String(error) }
        );
      }
    } else {
      voiceNavigationDiagnostics.savedTabAlreadySelected = true;
    }

    voiceNavigationDiagnostics.failureStage = 'collection_not_found';
    setPhase('selecting_voice_collection');
    let collectionResult;
    try {
      const currentCatalog = findVoiceCatalog() || catalog;
      const directSavedList = catalogSelectors.isDirectSavedList(
        normalizedText(currentCatalog), Boolean(findVoiceOption(currentCatalog, desired, selector))
      );
      const explicitCollection = findExactTextControl(currentCatalog, ['音频1', 'Audio 1']);
      if (directSavedList && !explicitCollection) {
        collectionResult = { catalog: currentCatalog, control: null };
        voiceNavigationDiagnostics.directSavedList = true;
      } else {
      collectionResult = await waitForStableCatalogControl(
        ['音频1', 'Audio 1'],
        15_000,
        'VOICE_COLLECTION_TIMEOUT',
        '已进入“已收藏”，但找不到音色收藏夹“音频1”；请确认10个音色（含JWY0521）仍保存在该收藏夹中'
      );
      }
    } catch (error) {
      await dismissVoiceCatalogIfOpen();
      throw error;
    }
    catalog = collectionResult.catalog;
    const collection = collectionResult.control;
    voiceNavigationDiagnostics.collectionDetected = Boolean(collection);
    voiceNavigationDiagnostics.failureStage = '';
    if (collection && !isSelectedControl(collection)) {
      collection.click();
      await interactionPause(800, 1300);
    } else {
      voiceNavigationDiagnostics.collectionAlreadySelected = true;
    }

    voiceNavigationDiagnostics.failureStage = 'target_voice_not_found';
    setPhase('selecting_target_voice');
    let option;
    try {
      option = await waitFor(
        () => {
          catalog = findVoiceCatalog() || catalog;
          return findVoiceOption(catalog, desired, selector);
        },
        20_000,
        `在“已收藏 → 音频1”中找不到音色“${desired}”`
      );
    } catch (error) {
      await dismissVoiceCatalogIfOpen();
      throw automationError(
        'VOICE_TARGET_CARD_TIMEOUT',
        `在“已收藏 → 音频1”中找不到音色“${desired}”`,
        { cause: error instanceof Error ? error.message : String(error) }
      );
    }
    voiceNavigationDiagnostics.targetVoiceDetected = true;
    voiceNavigationDiagnostics.targetVoiceCard = {
      voice: desired,
      rect: rectInfo(option.card),
      useControl: {
        label: controlLabel(option.useControl),
        rect: rectInfo(option.useControl)
      }
    };
    voiceNavigationDiagnostics.failureStage = '';
    return option.useControl;
  }

  async function dismissVoiceCatalogIfOpen() {
    const catalog = findVoiceCatalog();
    if (!catalog) return false;
    const close = Array.from(catalog.querySelectorAll(
      'button, [role="button"], [aria-label], [title]'
    ))
      .filter((element) => visible(element) && inViewport(element))
      .find((element) => {
        const label = `${normalizedControlLabel(element)} ${normalizedControlLabelValue(element.getAttribute('aria-label') || '')} ${normalizedControlLabelValue(element.getAttribute('title') || '')}`;
        return /^(?:关闭|close|cancel|取消|×|x)$/.test(label) || /(?:关闭|close|cancel|取消)/i.test(label);
      });
    if (!close) return false;
    try {
      close.click();
      await interactionPause(300, 600);
      voiceNavigationDiagnostics.catalogReopened = true;
      return true;
    } catch {
      return false;
    }
  }

  async function waitForStableCatalogControl(labels, timeoutMs, errorCode, errorMessage) {
    const startedAt = Date.now();
    let stableKey = '';
    let stableCount = 0;
    let lastCatalog = null;
    let lastCandidates = [];
    while (Date.now() - startedAt < timeoutMs) {
      const catalog = findVoiceCatalog();
      if (catalog) {
        lastCatalog = catalog;
        const control = findExactTextControl(catalog, labels);
        lastCandidates = catalogControlDiagnostics(catalog, labels);
        if (control && control.isConnected && visible(control)) {
          const rect = control.getBoundingClientRect();
          const key = [
            normalizedControlLabel(control),
            Math.round(rect.x), Math.round(rect.y),
            Math.round(rect.width), Math.round(rect.height),
            control.getAttribute('aria-selected') || '',
            control.getAttribute('data-state') || ''
          ].join('|');
          if (key === stableKey) stableCount += 1;
          else {
            stableKey = key;
            stableCount = 1;
          }
          if (stableCount >= 2) return { catalog, control };
        } else {
          stableKey = '';
          stableCount = 0;
        }
      } else {
        stableKey = '';
        stableCount = 0;
      }
      await delay(300);
    }
    voiceNavigationDiagnostics.failureStage = errorCode.toLowerCase();
    voiceNavigationDiagnostics.catalogControlDiagnostics = lastCandidates;
    throw automationError(errorCode, errorMessage, {
      labels,
      elapsedMs: Date.now() - startedAt,
      catalogDetected: Boolean(lastCatalog),
      candidates: lastCandidates
    });
  }

  function findVoiceCatalog() {
    const semanticDialog = Array.from(document.querySelectorAll('[role="dialog"], [aria-modal="true"]'))
      .filter(visible)
      .find(isFullVoiceCatalog);
    if (semanticDialog) return semanticDialog;

    const saved = Array.from(document.querySelectorAll(
      'button, [role="tab"], [role="button"], [tabindex="0"], [aria-label], [title], div, span, p'
    ))
      .filter(visible)
      .find((element) => ['已收藏', 'Saved'].some((label) => {
        const wanted = normalizedControlLabelValue(label);
        return normalizedControlLabel(element) === wanted
          || normalizedControlLabelValue(element.getAttribute('aria-label') || '') === wanted
          || normalizedControlLabelValue(element.getAttribute('title') || '') === wanted;
      }));
    if (!saved) return null;

    for (let node = saved.parentElement; node && node !== document.body; node = node.parentElement) {
      if (isFullVoiceCatalog(node)) return node;
    }
    return null;
  }

  function isFullVoiceCatalog(element) {
    if (!element || !visible(element)) return false;
    const rect = element.getBoundingClientRect();
    if (rect.width < 500 || rect.height < 300) return false;
    const text = normalizedText(element);
    const saved = findExactTextControl(element, ['已收藏', 'Saved']);
    const search = Array.from(element.querySelectorAll('input, [role="searchbox"]'))
      .filter(visible)
      .some((input) => /搜索|search/i.test(`${input.placeholder || ''} ${input.getAttribute('aria-label') || ''}`));
    const catalogHeading = /选择(?:语音|音色|声音)|音色库|声音库|voice\s*(?:library|catalog)|select\s+voice/i.test(text);
    return Boolean(saved || search || catalogHeading);
  }

  function findExactTextControl(root, labels) {
    if (catalogSelectors?.findExactTextControl) {
      return catalogSelectors.findExactTextControl(root, labels, {
        visible,
        inViewport,
        isClickableElement
      });
    }
    if (!root) return null;
    const wanted = labels.map(normalizedControlLabelValue).filter(Boolean);
    const matchesWanted = (value) => wanted.some((label) => value === label || value.includes(label));
    const candidates = [root, ...Array.from(root.querySelectorAll(
      'button, [role="tab"], [role="button"], [tabindex="0"], [aria-label], [title], div, span, p'
    ))]
      .filter((element) => element !== document.body && element.isConnected && visible(element))
      .filter((element) => inViewport(element) || element === root)
      .map((element) => {
        const text = normalizedControlLabel(element);
        const aria = normalizedControlLabelValue(element.getAttribute('aria-label') || '');
        const title = normalizedControlLabelValue(element.getAttribute('title') || '');
         const labelMatch = matchesWanted(text) || matchesWanted(aria) || matchesWanted(title);
        if (!labelMatch) return null;
        const control = element.matches('button, [role="tab"], [role="button"], [tabindex="0"]')
          ? element
          : element.closest('button, [role="tab"], [role="button"], [tabindex="0"]');
        const usable = control || isClickableElement(element) ? (control || element) : null;
        if (!usable || !visible(usable) || !inViewport(usable)) return null;
        const semanticRole = usable.getAttribute('role') || usable.tagName.toLowerCase();
        const score = (control ? 100 : 40)
          + (semanticRole === 'tab' ? 30 : 0)
          + (aria || title ? 10 : 0)
          + (text === normalizedControlLabelValue(labels[0]) ? 5 : 0);
        return { element: usable, score };
      })
      .filter(Boolean)
      .sort((a, b) => b.score - a.score);
    return candidates[0]?.element || null;
  }

  function normalizedControlLabel(element) {
    if (!element) return '';
    const text = element instanceof HTMLTextAreaElement || element instanceof HTMLInputElement
      ? element.value
      : element.innerText || element.textContent || '';
    return normalizedControlLabelValue(text);
  }

  function normalizedControlLabelValue(value) {
    return normalizedTextValue(value)
      .replace(/[.…]+$/g, '')
      .replace(/[\u3000\s]+/g, ' ')
      .trim()
      .toLocaleLowerCase();
  }

  function isClickableElement(element) {
    if (!element || !visible(element)) return false;
    if (typeof element.onclick === 'function' || element.hasAttribute('data-state')) return true;
    return getComputedStyle(element).cursor === 'pointer';
  }

  function isSelectedControl(element) {
    if (catalogSelectors?.isSelectedControl) return catalogSelectors.isSelectedControl(element);
    if (!element) return false;
    const attributes = [
      element.getAttribute('aria-selected'),
      element.getAttribute('aria-current'),
      element.getAttribute('data-state'),
      element.getAttribute('data-active')
    ].map((value) => String(value || '').toLocaleLowerCase());
    if (attributes.some((value) => ['true', 'page', 'active', 'selected', 'on'].includes(value))) return true;
    return /(?:active|selected|current|checked)/i.test(String(element.className || ''));
  }

  function catalogControlDiagnostics(root, labels) {
    const wanted = new Set(labels.map(normalizedControlLabelValue));
    return Array.from(root?.querySelectorAll?.(
      'button, [role="tab"], [role="button"], [tabindex="0"], [aria-label], [title], div, span, p'
    ) || [])
      .filter((element) => visible(element) && inViewport(element))
      .map((element) => ({
        text: normalizedControlLabel(element).slice(0, 120),
        ariaLabel: String(element.getAttribute('aria-label') || '').slice(0, 120),
        title: String(element.getAttribute('title') || '').slice(0, 120),
        role: element.getAttribute('role') || element.tagName.toLowerCase(),
        matched: wanted.has(normalizedControlLabel(element)),
        rect: rectInfo(element)
      }))
      .filter((item) => item.text || item.ariaLabel || item.title)
      .slice(0, 80);
  }

  function findVoiceSelector() {
    const candidates = Array.from(document.querySelectorAll('button, [role="button"], [role="combobox"]'))
      .filter(visible)
      .filter((element) => {
        const rect = element.getBoundingClientRect();
        const text = normalizedText(element);
        return rect.left < innerWidth * 0.55
          && rect.top < Math.max(280, innerHeight * 0.3)
          && rect.width >= 80
          && rect.width <= 520
          && rect.height <= 110
          && text.length > 0
          && text.length <= 80
          && Boolean(element.querySelector('img'));
      })
      .sort((a, b) => voiceSelectorScore(b) - voiceSelectorScore(a));
    return candidates.length > 0 && voiceSelectorScore(candidates[0]) > 0 ? candidates[0] : null;
  }

  function voiceSelectorScore(element) {
    const rect = element.getBoundingClientRect();
    let score = 0;
    if (rect.left < innerWidth * 0.4) score += 20;
    if (rect.top < 220) score += 20;
    if (rect.left < innerWidth * 0.75) score += 30;
    else score -= 80;
    if (rect.width >= 90 && rect.width <= 500) score += 10;
    if (element.querySelector('img')) score += 30;
    if (element.matches('[role="combobox"]')) score += 10;
    score -= rect.top / 100;
    return score;
  }

  function findVoiceOption(root, desired, selector) {
    if (!root || !root.querySelectorAll || !activeVoiceApi?.sameVoice) return null;
    // The label and the Use button must belong to the same compact card.  Do
    // not climb into a grid/list parent, which can contain another card's
    // first Use button (the cause of "chand" selecting Farid Dieck).
    const candidates = Array.from(root.querySelectorAll('h1, h2, h3, h4, strong, b, span, p, div, [role="option"], [role="menuitem"]'))
      .filter(visible)
      .filter((element) => element !== selector && !selector?.contains(element))
      .filter((element) => activeVoiceApi.sameVoice(normalizedText(element), desired))
      .sort((a, b) => a.getBoundingClientRect().width - b.getBoundingClientRect().width);
    const matches = new Map();
    for (const label of candidates) {
      const card = findTargetVoiceCard(label, root);
      if (!card) continue;
      const useControl = findUseControlInsideVoiceCard(card);
      if (useControl) matches.set(card, { card, useControl });
    }
    // Case-insensitive names can collide. Never choose the first card when
    // multiple cards claim the requested name; keep generation blocked.
    return matches.size === 1 ? matches.values().next().value : null;
  }

  function findTargetVoiceCard(label, catalog) {
    for (let node = label; node && node !== catalog && node !== document.body; node = node.parentElement) {
      if (!visible(node) || !node.contains(label)) continue;
      const rect = node.getBoundingClientRect();
      if (rect.width < 180 || rect.width > Math.min(620, innerWidth * 0.5) || rect.height < 80 || rect.height > 460) continue;
      const controls = findUseControls(node);
      if (controls.length !== 1) continue;
      const exactNames = Array.from(node.querySelectorAll('h1, h2, h3, h4, strong, b, span, p, div'))
        .filter(visible)
        .filter((element) => activeVoiceApi.sameVoice(normalizedText(element), normalizedText(label)));
      if (exactNames.length === 1) return node;
    }
    return null;
  }

  function findUseControls(card) {
    return Array.from(card.querySelectorAll('button, [role="button"]'))
      .filter(visible)
      .filter((element) => ['使用', 'Use', '选择', 'Select'].includes(normalizedText(element)));
  }

  function findUseControlInsideVoiceCard(card) {
    return findUseControls(card)[0] || null;
  }

  function voiceOptionScore(element) {
    const rect = element.getBoundingClientRect();
    let score = 0;
    if (element.matches('[role="option"], [role="menuitem"], [data-radix-collection-item]')) score += 40;
    if (rect.top < 500) score += 10;
    if (rect.left > 220 && rect.left < innerWidth * 0.75) score += 20;
    return score;
  }

  function findEditors() {
    return Array.from(document.querySelectorAll('[contenteditable="true"], textarea, [role="textbox"]'))
      .filter(visible)
      .filter((element) => editorCandidateReason(element) === 'accepted');
  }

  function findEditor() {
    return findEditors().sort((a, b) => editorCandidateScore(b) - editorCandidateScore(a))[0] || null;
  }

  function editorCandidateReason(element) {
    const rect = element.getBoundingClientRect();
    if (element.disabled || element.getAttribute('aria-disabled') === 'true') return 'disabled';
    if (element.readOnly || element.getAttribute('readonly') != null) return 'readonly';
    if (isKnownBlockingDialog(element)) return 'known-blocking-dialog';
    if (rect.width < 120 || rect.height < 20) return 'too-small';

    // Fish's current editor is a single-line contenteditable element that grows
    // only after text is inserted (typically about 20px high when empty). Do
    // not reject that real editor solely for its pre-input height. The compact
    // form is accepted only when it is a wide, explicit editable control and
    // the page has the uniquely identified Generate control. Fish currently
    // renders those two controls in adjacent React branches, so they do not
    // always share a direct DOM ancestor. Write-and-readback still guards the
    // actual generation action.
    if (rect.height < 36) {
      const generate = findAnyGenerateButton();
      const explicitEditable = element.matches('[contenteditable="true"], textarea');
      if (!(rect.width >= 480 && generate && (sharedWorkflowContainer(element, generate) || explicitEditable))) {
        return 'too-small';
      }
    }
    return 'accepted';
  }

  function isKnownBlockingDialog(element) {
    const dialog = element.closest('[role="dialog"], [aria-modal="true"]');
    if (!dialog || !visible(dialog)) return false;
    const text = normalizedText(dialog);
    const welcomeTitle = '\u6b22\u8fce\u4f7f\u7528\u6587\u5b57\u8f6c\u8bed\u97f3';
    const skip = '\u8df3\u8fc7';
    const voicePicker = '\u9009\u62e9\u97f3\u8272';
    return (text.includes(welcomeTitle) && text.includes(skip)) || text.includes(voicePicker);
  }

  function editorCandidateScore(editor) {
    let score = area(editor);
    const generate = findAnyGenerateButton();
    if (generate && sharedWorkflowContainer(editor, generate)) score += 1_000_000;
    if (editor.matches('[contenteditable="true"], textarea')) score += 10_000;
    return score;
  }

  function createVoiceNavigationDiagnostics(expectedVoice = '') {
    return {
      expectedVoice: normalizedTextValue(expectedVoice),
      primaryVoiceCard: null,
      pageStable: false,
      stableForMs: 0,
      stableVoice: '',
      stableRect: null,
      hoverCompleted: false,
      voiceNameElementDetected: false,
      voiceNameElement: null,
      voiceNameClicked: false,
      primaryVoiceCardActivated: Boolean(primaryVoiceActivation.clicked && primaryVoiceActivation.element?.isConnected),
      nativeHover: null,
      switchButtonDetected: false,
      switchButton: null,
      compactMenuDetected: false,
      moreVoicesClicked: false,
      catalogDetected: false,
      savedTabDetected: false,
      savedTabLabel: '',
      savedTabAlreadySelected: false,
      catalogTabsReadyAt: '',
      collectionDetected: false,
      collectionAlreadySelected: false,
      targetVoiceDetected: false,
      catalogControlDiagnostics: [],
      catalogReopened: false,
      failureStage: ''
    };
  }

  function sharedWorkflowContainer(editor, generate) {
    for (let node = editor.parentElement, depth = 0; node && node !== document.body && depth < 7; node = node.parentElement, depth += 1) {
      if (node.contains(generate)) return true;
    }
    return false;
  }

  function findSpeakerEditor(desired) {
    const wanted = normalizedTextValue(desired);
    if (!wanted) return null;

    const labels = Array.from(document.querySelectorAll('button, [role="button"], [role="combobox"], span, div, p'))
      .filter(visible)
      .filter((element) => normalizedText(element) === wanted)
      .filter((element) => {
        const rect = element.getBoundingClientRect();
        return rect.left >= 210 && rect.left < innerWidth * 0.82;
      })
      .sort((a, b) => area(a) - area(b));

    for (const label of labels) {
      const editor = editorNearLabel(label);
      if (editor) return editor;
    }
    return null;
  }

  function editorNearLabel(label) {
    let container = label;
    for (let depth = 0; container && container !== document.body && depth < 9; depth += 1) {
      const editors = Array.from(container.querySelectorAll('[contenteditable="true"], textarea, [role="textbox"]'))
        .filter((element) => findEditors().includes(element));
      if (editors.length === 1) return editors[0];
      if (editors.length > 1 && depth >= 2) {
        const afterLabel = editors
          .filter((editor) => label.compareDocumentPosition(editor) & Node.DOCUMENT_POSITION_FOLLOWING)
          .sort((a, b) => editorDistance(label, a) - editorDistance(label, b));
        if (afterLabel.length > 0) return afterLabel[0];
      }
      container = container.parentElement;
    }

    return findEditors()
      .filter((editor) => label.compareDocumentPosition(editor) & Node.DOCUMENT_POSITION_FOLLOWING)
      .sort((a, b) => editorDistance(label, a) - editorDistance(label, b))[0] || null;
  }

  function editorDistance(label, editor) {
    const labelRect = label.getBoundingClientRect();
    const editorRect = editor.getBoundingClientRect();
    const vertical = Math.max(0, editorRect.top - labelRect.bottom);
    const horizontal = Math.abs(editorRect.left - labelRect.left);
    return vertical * 10 + horizontal;
  }

  function activateSpeakerEditor(editor) {
    const rect = editor.getBoundingClientRect();
    if (rect.top < 80 || rect.bottom > innerHeight - 80) {
      editor.scrollIntoView({ block: 'center', behavior: 'auto' });
    }
    editor.click();
    editor.focus();
  }

  async function clearAllSpeakerEditors() {
    const startedAt = Date.now();
    let stableSince = 0;
    let lastCount = readCharacterCount();

    while (Date.now() - startedAt < 25_000) {
      const editors = findEditors();
      for (const editor of editors) {
        if (!editorMatches(editor, '')) clearEditor(editor);
      }

      await delay(250);
      const count = readCharacterCount();
      // Fish now redraws the editor and uses different character-limit labels
      // on some accounts.  Trust a stable, visible empty editor; an absent
      // counter must not turn a successful clear into a failed voice job.
      const currentEditors = findEditors();
      const allEmpty = currentEditors.length > 0 && currentEditors.every((editor) => editorMatches(editor, ''));
      if (allEmpty && (count === 0 || count == null)) {
        if (!stableSince) stableSince = Date.now();
        if (Date.now() - stableSince >= 2000) return 0;
      } else {
        stableSince = 0;
      }
      lastCount = count;
    }

    throw new Error(`Fish Audio 没有完成旧文本清空（当前计数 ${lastCount ?? '未知'}），已停止生成`);
  }

  function clearEditor(editor) {
    if (!editor || !normalizedText(editor)) return;
    editor.focus();
    if (editor instanceof HTMLTextAreaElement || editor instanceof HTMLInputElement) {
      const descriptor = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(editor), 'value');
      descriptor?.set?.call(editor, '');
      editor.dispatchEvent(new InputEvent('input', {
        bubbles: true,
        inputType: 'deleteContentBackward',
        data: null
      }));
      editor.dispatchEvent(new Event('change', { bubbles: true }));
      return;
    }

    selectEditorContents(editor);
    const deleted = document.execCommand('delete', false);
    if (!deleted || normalizedText(editor)) editor.replaceChildren();
    editor.dispatchEvent(new InputEvent('input', {
      bubbles: true,
      inputType: 'deleteContentBackward',
      data: null
    }));
    editor.dispatchEvent(new Event('change', { bubbles: true }));
  }

  async function fillEditor(editor, text) {
    editor.focus();
    if (editor instanceof HTMLTextAreaElement || editor instanceof HTMLInputElement) {
      const descriptor = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(editor), 'value');
      descriptor?.set?.call(editor, text);
      editor.dispatchEvent(new Event('input', { bubbles: true }));
      editor.dispatchEvent(new Event('change', { bubbles: true }));
      return;
    }

    selectEditorContents(editor);
    let inserted = true;
    for (const character of Array.from(text)) {
      if (!document.execCommand('insertText', false, character)) {
        inserted = false;
        break;
      }
      await interactionPause(25, 65);
    }
    if (!inserted || normalizedText(editor) !== normalizedTextValue(text)) {
      editor.replaceChildren(document.createTextNode(text));
      editor.dispatchEvent(new InputEvent('input', {
        bubbles: true,
        inputType: 'insertText',
        data: text
      }));
    }
    editor.dispatchEvent(new Event('change', { bubbles: true }));
  }

  function selectEditorContents(editor) {
    editor.focus();
    const selection = window.getSelection();
    if (!selection) return;
    const range = document.createRange();
    range.selectNodeContents(editor);
    selection.removeAllRanges();
    selection.addRange(range);
  }

  function findGenerateButton() {
    const button = findAnyGenerateButton();
    return button && controlIsEnabled(button) ? button : null;
  }

  function findAnyGenerateButton() {
    // Fish Audio has shipped both semantic <button> controls and clickable
    // role=button wrappers.  The localized label is not stable, so inspect
    // visible text and accessibility labels rather than relying on one exact
    // Chinese string.
    return Array.from(document.querySelectorAll('button, [role="button"], input[type="button"], input[type="submit"]'))
      .filter(visible)
      .filter((button) => isGenerateControl(button))
      .sort((a, b) => generateButtonScore(b) - generateButtonScore(a))[0] || null;
  }

  function pageReadinessDetail() {
    const rawEditors = Array.from(document.querySelectorAll('[contenteditable="true"], textarea, [role="textbox"]'))
      .filter(visible);
    const editorCandidates = rawEditors
      .map((editor) => `${rectInfo(editor).width}x${rectInfo(editor).height}:${editorCandidateReason(editor)}`)
      .join(', ') || 'none';
    const rawGenerate = Array.from(document.querySelectorAll('button, [role="button"], input[type="button"], input[type="submit"]'))
      .filter(visible)
      .filter(isGenerateControl);
    return [
      `原始编辑区 ${rawEditors.length}`,
      `可用编辑区 ${findEditors().length}`,
      `原始生成按钮 ${rawGenerate.length}`,
      `可用生成按钮 ${findAnyGenerateButton() ? 1 : 0}`,
      `编辑区候选 ${editorCandidates}`,
      `页面 ${location.pathname}`
    ].join('；');
  }

  function controlLabel(element) {
    return [
      normalizedText(element),
      element.getAttribute('aria-label') || '',
      element.getAttribute('title') || '',
      element.getAttribute('data-testid') || '',
      element.getAttribute('name') || '',
      element.value || ''
    ].join(' ').toLowerCase().replace(/\s+/g, ' ').trim();
  }

  function isGenerateControl(element) {
    const label = controlLabel(element);
    return label.includes('generate')
      || label.includes('synthesize')
      || label.includes('text-to-speech')
      || label.includes('tts')
      || label.includes('生成语音');
  }

  function controlIsEnabled(element) {
    return !element.disabled
      && element.getAttribute('aria-disabled') !== 'true'
      && !element.matches('[disabled], .disabled, [data-disabled="true"]');
  }

  function generateButtonScore(button) {
    const rect = button.getBoundingClientRect();
    let score = 0;
    if (controlIsEnabled(button)) score += 100;
    if (rect.left > innerWidth * 0.5) score += 40;
    if (rect.bottom > innerHeight - 220) score += 40;
    if (normalizedText(button).startsWith('生成语音')) score += 20;
    return score;
  }

  // 2026-10-09 10:29: the text was in the editor but Fish never started
  // generating after generateButton.click(); the task then waited 10 minutes
  // until the heartbeat timed out.  Confirm the start, fall back to a full
  // pointer click and the Ctrl+Enter shortcut, and otherwise fail fast with
  // the button evidence so the bridge can retry the task.
  async function startGeneration(editor, generateButton, beforeControls, beforePlayerSignature) {
    const started = () => generationStarted(editor, generateButton, beforeControls, beforePlayerSignature);
    generateButton.click();
    if (await becomesTrue(started, 15_000)) return 'click';
    const retryButton = findGenerateButton() || generateButton;
    dispatchPointerClick(retryButton);
    if (await becomesTrue(started, 7_500)) return 'pointer-click';
    dispatchCtrlEnter(editor);
    if (await becomesTrue(started, 7_500)) return 'ctrl-enter';
    throw automationError(
      'FISH_GENERATE_NOT_STARTED',
      `已点击“生成语音”但 Fish 没有开始生成：${generateStartEvidence()}`
    );
  }

  function generationStarted(editor, generateButton, beforeControls, beforePlayerSignature) {
    const current = findAnyGenerateButton();
    if (!current || !controlIsEnabled(current)) return true;
    if (/生成中|generating/i.test(controlLabel(current))) return true;
    if (beforeControls.length > 0 && beforeControls.every((element) => !element.isConnected || !visible(element))) {
      return true;
    }
    const signature = playerSignature(findPlayerDownloadControls(editor, current));
    return Boolean(signature) && signature !== beforePlayerSignature;
  }

  async function becomesTrue(predicate, timeoutMs) {
    const startedAt = Date.now();
    while (Date.now() - startedAt < timeoutMs) {
      if (predicate()) return true;
      await delay(300);
    }
    return Boolean(predicate());
  }

  function dispatchPointerClick(element) {
    const rect = element.getBoundingClientRect();
    const point = { clientX: rect.left + rect.width / 2, clientY: rect.top + rect.height / 2 };
    element.focus?.({ preventScroll: true });
    for (const type of ['pointerover', 'pointerenter', 'pointerdown', 'pointerup']) {
      try {
        element.dispatchEvent(new PointerEvent(type, {
          ...point, bubbles: type !== 'pointerenter', cancelable: true,
          pointerType: 'mouse', isPrimary: true, button: 0,
          buttons: type === 'pointerdown' ? 1 : 0
        }));
      } catch {
        // Older Chromium builds may not expose PointerEvent in isolated worlds.
      }
      if (type === 'pointerdown') {
        element.dispatchEvent(new MouseEvent('mousedown', { ...point, bubbles: true, cancelable: true, view: window, button: 0, buttons: 1 }));
      }
      if (type === 'pointerup') {
        element.dispatchEvent(new MouseEvent('mouseup', { ...point, bubbles: true, cancelable: true, view: window, button: 0 }));
      }
    }
    element.dispatchEvent(new MouseEvent('click', { ...point, bubbles: true, cancelable: true, view: window, button: 0 }));
  }

  function dispatchCtrlEnter(editor) {
    editor.focus?.({ preventScroll: true });
    for (const type of ['keydown', 'keyup']) {
      editor.dispatchEvent(new KeyboardEvent(type, {
        key: 'Enter', code: 'Enter', keyCode: 13, which: 13,
        ctrlKey: true, bubbles: true, cancelable: true
      }));
    }
  }

  function generateStartEvidence() {
    const candidates = Array.from(document.querySelectorAll('button, [role="button"], input[type="button"], input[type="submit"]'))
      .filter(isGenerateControl)
      .slice(0, 3)
      .map((element) => {
        const rect = rectInfo(element);
        return `[${controlLabel(element).slice(0, 40)} @${rect.x},${rect.y} ${rect.width}x${rect.height} ` +
          `${controlIsEnabled(element) ? '可用' : '不可用'} 可见=${visible(element)} ` +
          `${String(element.outerHTML || '').replace(/\s+/g, ' ').slice(0, 300)}]`;
      });
    return `页面可见性 ${document.visibilityState}；生成按钮候选 ${candidates.length}：${candidates.join(' ') || '无'}`;
  }

  async function waitForGeneration(editor, generateButton, beforeControls, beforePlayerSignature, timeoutMs) {
    const startedAt = Date.now();
    let sawGeneratingState = false;
    let stableSignature = '';
    let stableSince = 0;
    while (Date.now() - startedAt < timeoutMs) {
      const currentGenerateButton = findAnyGenerateButton();
      const controls = findPlayerDownloadControls(editor, currentGenerateButton || generateButton);
      const oldControlsGone = beforeControls.length === 0 || beforeControls.every((element) => !element.isConnected || !visible(element));
      const currentSignature = playerSignature(controls);
      const playerChanged = Boolean(currentSignature) && currentSignature !== beforePlayerSignature;
      if ((currentGenerateButton && !controlIsEnabled(currentGenerateButton)) || controls.length === 0 || oldControlsGone || playerChanged) {
        sawGeneratingState = true;
      }

      if (sawGeneratingState && currentGenerateButton && controlIsEnabled(currentGenerateButton) && controls.length > 0) {
        if (currentSignature !== stableSignature) {
          stableSignature = currentSignature;
          stableSince = Date.now();
        } else if (Date.now() - stableSince >= 1500) {
          return { type: 'control', element: controls[0] };
        }
      }
      await delay(300);
    }
    throw new Error('等待 Fish Audio 新音频界面重新出现超时');
  }

  async function openHistory() {
    const history = Array.from(document.querySelectorAll('[role="tab"], button')).find((element) => normalizedText(element) === '历史' && visible(element));
    if (history) {
      history.click();
      await delay(1200);
    }
  }

  function findDownloadControls() {
    return Array.from(document.querySelectorAll('button, a'))
      .filter(visible)
      .filter((element) => {
        const label = `${normalizedText(element)} ${element.getAttribute('aria-label') || ''} ${element.getAttribute('title') || ''}`.toLowerCase();
        return label.includes('下载') || label.includes('download') || element.hasAttribute('download');
      });
  }

  function findPlayerDownloadControls(editor, generateButton) {
    const exactButtons = Array.from(document.querySelectorAll('button[aria-label="下载"]'))
      .filter(inViewport)
      .sort((a, b) => {
        const left = a.getBoundingClientRect();
        const right = b.getBoundingClientRect();
        return left.y - right.y || left.x - right.x;
      });
    if (exactButtons.length > 0) return exactButtons;

    const durationPattern = /\b\d+:\d+\s*\/\s*\d+:\d+\b/;
    const durationElements = Array.from(document.querySelectorAll('span, div, p'))
      .filter(inViewport)
      .filter((element) => {
        const text = normalizedText(element);
        return text.length <= 40 && durationPattern.test(text);
      });

    const containers = [];
    for (const durationElement of durationElements) {
      const container = closestAudioPlayer(durationElement);
      if (container && !containers.includes(container)) containers.push(container);
    }

    const playerDownloads = containers
      .sort((a, b) => {
        const left = a.getBoundingClientRect();
        const right = b.getBoundingClientRect();
        return left.y - right.y || left.x - right.x;
      })
      .map((container) => {
        const buttons = Array.from(container.querySelectorAll('button')).filter(inViewport).sort((a, b) => a.getBoundingClientRect().x - b.getBoundingClientRect().x);
        return buttons[buttons.length - 1] || null;
      })
      .filter(Boolean);

    if (playerDownloads.length > 0) return playerDownloads;

    const root = lowestCommonAncestor(editor, generateButton) || document;
    return findDownloadControls(root).filter(inViewport);
  }

  function closestAudioPlayer(element) {
    for (let node = element; node && node !== document.body; node = node.parentElement) {
      const rect = node.getBoundingClientRect();
      const buttons = Array.from(node.querySelectorAll('button')).filter(inViewport);
      if (buttons.length >= 2 && rect.width >= 220 && rect.height >= 35 && rect.height <= 180) {
        return node;
      }
    }
    return null;
  }

  function playerSignature(downloadButtons) {
    return downloadButtons.map((button) => {
      const container = playerContainerFromDownload(button);
      return container ? container.innerHTML : button.outerHTML;
    }).join('\n---PLAYER---\n');
  }

  function playerContainerFromDownload(button) {
    for (let node = button.parentElement; node && node !== document.body; node = node.parentElement) {
      const rect = node.getBoundingClientRect();
      if (rect.width >= 220 && rect.height >= 35 && rect.height <= 180) return node;
    }
    return button.parentElement;
  }

  async function downloadResult(result, baseName) {
    if (result.type === 'url' && result.url && !result.url.startsWith('blob:')) {
      const response = await extensionApi.runtime.sendMessage({ type: 'download-url', url: result.url, baseName });
      if (!response?.ok) throw new Error(response?.error || '下载音频失败');
      return waitForDownload(response.id, Date.now() - 1000);
    }

    if (result.type === 'control' && result.element) {
      const prepared = await extensionApi.runtime.sendMessage({ type: 'prepare-download', baseName });
      result.element.click();
      return waitForDownload(null, prepared?.since || Date.now() - 1000);
    }

    throw new Error('没有找到可下载的音频');
  }

  async function waitForDownload(expectedId, since) {
    const startedAt = Date.now();
    while (Date.now() - startedAt < 120_000) {
      const latest = await extensionApi.runtime.sendMessage({ type: 'latest-download', since });
      if (latest?.id && (!expectedId || latest.id === expectedId)) {
        if (latest.state === 'complete') return latest.filename || String(latest.id);
        if (latest.state === 'interrupted') throw new Error(latest.error || '下载被中断');
      }
      await delay(1000);
    }
    throw new Error('等待浏览器下载完成超时');
  }

  async function sendDiagnostics() {
    await delay(1500);
    const editors = Array.from(document.querySelectorAll('[contenteditable="true"], textarea, [role="textbox"]')).filter(visible);
    const downloads = findDownloadControls();
    const windowInfo = await currentFishWindowState();
    const voice = activeVoiceEvidence(activeExpectedVoice);
    await bridge('/api/bridge/diagnostics', {
      method: 'POST',
      body: JSON.stringify({
        scriptVersion: SCRIPT_VERSION,
        browserSessionId: BROWSER_SESSION_ID,
        url: location.href,
        loggedIn: !isLoginButtonVisible(),
        editorCount: editors.length,
        generateButtonCount: Array.from(document.querySelectorAll('button, [role="button"], input[type="button"], input[type="submit"]')).filter(isGenerateControl).length,
        downloadCandidateCount: downloads.length,
        audioElementCount: document.querySelectorAll('audio').length,
        voiceSelectorText: normalizedText(findVoiceSelector()),
        expectedVoice: voice.expectedVoice,
        actualActiveVoice: voice.headerVoice,
        primarySettingVoice: voice.primarySettingVoice,
        headerVoice: voice.headerVoice,
        editorVoice: voice.editorVoice,
        rightCardVoice: voice.rightCardVoice,
        verificationCount: Array.isArray(lastVoiceEvidence.matchingSources)
          ? lastVoiceEvidence.matchingSources.length
          : 0,
        phase: currentPhase,
        busy,
        taskId: currentTaskId,
        availableVoices: voice.availableVoices,
        voiceNavigation: { ...voiceNavigationDiagnostics },
        windowState: windowInfo.state || '',
        innerWidth,
        innerHeight,
        editorCandidates: editors.slice(0, 8).map((element) => ({
          ...rectInfo(element),
          tag: element.tagName,
          role: element.getAttribute('role') || '',
          reason: editorCandidateReason(element),
          selected: findEditor() === element
        })),
        editors: editors.slice(0, 5).map((element) => ({
          tag: element.tagName,
          className: String(element.className || '').slice(0, 300),
          role: element.getAttribute('role'),
          text: normalizedText(element).slice(0, 200),
          rect: rectInfo(element)
        })),
        controls: Array.from(document.querySelectorAll('button, [role="button"], input[type="button"], input[type="submit"]'))
          .filter(inViewport)
          .slice(0, 80)
          .map((element) => ({
            tag: element.tagName,
            label: controlLabel(element).slice(0, 240),
            enabled: controlIsEnabled(element),
            rect: rectInfo(element),
            html: element.outerHTML.slice(0, 800)
          })),
        downloads: downloads.slice(0, 20).map((element) => ({
          tag: element.tagName,
          text: normalizedText(element).slice(0, 120),
          ariaLabel: element.getAttribute('aria-label'),
          title: element.getAttribute('title'),
          download: element.getAttribute('download'),
          href: element.getAttribute('href'),
          rect: rectInfo(element),
          html: element.outerHTML.slice(0, 600),
          parentHtml: element.parentElement?.outerHTML.slice(0, 1200) || ''
        }))
      })
    });
  }

  function isLoginButtonVisible() {
    return Array.from(document.querySelectorAll('button')).some(
      (item) => normalizedText(item) === '登录 / 注册' && visible(item)
    );
  }

  async function bridge(path, options = {}) {
    const url = new URL(`${BRIDGE}${path}`);
    url.searchParams.set('browserSessionId', BROWSER_SESSION_ID);
    const response = await fetch(url, {
      method: options.method || 'GET',
      headers: { 'Content-Type': 'application/json' },
      body: options.body,
      cache: 'no-store'
    });
    const raw = await response.text();
    let payload = null;
    try {
      payload = raw ? JSON.parse(raw) : null;
    } catch {
      payload = null;
    }
    if (!response.ok) {
      const error = new Error(payload?.message || payload?.error || `本地桥接返回 ${response.status}`);
      error.code = String(payload?.errorCode || `BRIDGE_HTTP_${response.status}`);
      error.retryable = payload?.retryable === true;
      error.phase = String(payload?.phase || 'bridge');
      error.details = payload?.details || payload || null;
      throw error;
    }
    return payload ?? {};
  }

  async function ensureCurrentFishWindowMaximized() {
    const response = await extensionApi.runtime.sendMessage({ type: 'maximize-current-fish-window' });
    if (!response?.ok || response.state !== 'maximized') {
      throw new Error(`Fish Audio window was not maximized: ${response?.error || response?.state || 'unknown'}`);
    }
  }

  async function currentFishWindowState() {
    try {
      return await extensionApi.runtime.sendMessage({ type: 'current-fish-window-state' }) || {};
    } catch {
      return {};
    }
  }

  async function closeOwnedFishTab(reason) {
    stopped = true;
    if (heartbeatTimer) {
      clearInterval(heartbeatTimer);
      heartbeatTimer = null;
    }
    const response = await extensionApi.runtime.sendMessage({ type: 'close-current-fish-tab', reason });
    if (!response?.ok) {
      stopped = false;
      throw new Error(`Fish Audio tab could not be closed: ${response?.error || 'unknown'}`);
    }
  }

  function loadBrowserSessionId() {
    const fragment = new URLSearchParams(location.hash.replace(/^#/, ''));
    const fromFragment = fragment.get('fish-batch');
    if (fromFragment) {
      sessionStorage.setItem('fish-audio-batch-session', fromFragment);
      return fromFragment;
    }
    return sessionStorage.getItem('fish-audio-batch-session') || '';
  }

  async function waitFor(getter, timeoutMs, errorMessage) {
    const startedAt = Date.now();
    while (Date.now() - startedAt < timeoutMs) {
      const value = getter();
      if (value) return value;
      await delay(300);
    }
    const detail = typeof errorMessage === 'function' ? errorMessage() : errorMessage;
    if (detail instanceof Error) throw detail;
    throw new Error(detail);
  }

  function automationError(code, message, details = {}) {
    const error = new Error(message);
    error.code = code;
    error.details = details;
    return error;
  }

  function classifyAutomationError(message) {
    const text = normalizedTextValue(message).toLowerCase();
    if (/login|登录|登入/.test(text)) return 'LOGIN_REQUIRED';
    if (/voice|speaker|音色|声音|说话人/.test(text)) return 'VOICE_SELECTION_FAILED';
    if (/editor|text|正文|编辑/.test(text)) return 'EDITOR_FAILED';
    if (/download|下载/.test(text)) return 'DOWNLOAD_FAILED';
    if (/generate|生成/.test(text)) return 'GENERATION_FAILED';
    return 'AUTOMATION_FAILED';
  }

  function setPhase(phase) {
    currentPhase = normalizedTextValue(phase) || 'idle';
    // Do not wait for the next periodic pulse when a phase changes.  This
    // makes the bridge state readable even if Fish immediately redraws the
    // catalogue after a voice is selected.
    queueMicrotask(() => sendHeartbeat().catch(() => {}));
  }

  function startHeartbeatLoop() {
    if (heartbeatTimer) return;
    sendHeartbeat().catch(() => {});
    heartbeatTimer = setInterval(() => {
      sendHeartbeat().catch(() => {});
    }, HEARTBEAT_MS);
    // A Fish route redraw can temporarily deprioritise normal timers.  These
    // lifecycle pulses provide an additional, harmless status refresh; they
    // never claim that a task succeeded.
    addEventListener('visibilitychange', () => sendHeartbeat().catch(() => {}));
    addEventListener('focus', () => sendHeartbeat().catch(() => {}));
    addEventListener('pageshow', () => sendHeartbeat().catch(() => {}));
  }

  async function sendHeartbeat() {
    // A stalled request must not prevent later pulses from being attempted.
    if (heartbeatInFlight) return;
    heartbeatInFlight = true;
    try {
      await bridge('/api/bridge/heartbeat', {
        method: 'POST',
        body: JSON.stringify({
          scriptVersion: SCRIPT_VERSION,
          browserSessionId: BROWSER_SESSION_ID,
          taskId: currentTaskId,
          busy,
          phase: currentPhase,
          expectedVoice: activeExpectedVoice,
          voiceEvidence: { ...lastVoiceEvidence },
          voiceNavigation: { ...voiceNavigationDiagnostics },
          updatedAt: new Date().toISOString()
        })
      });
    } finally {
      heartbeatInFlight = false;
    }
  }

  function visible(element) {
    const style = getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
  }

  function inViewport(element) {
    const rect = element.getBoundingClientRect();
    return visible(element) && rect.bottom > 0 && rect.right > 0 && rect.top < innerHeight && rect.left < innerWidth;
  }

  function lowestCommonAncestor(first, second) {
    const ancestors = new Set();
    for (let node = first; node; node = node.parentElement) ancestors.add(node);
    for (let node = second; node; node = node.parentElement) {
      if (ancestors.has(node)) return node;
    }
    return null;
  }

  function area(element) {
    const rect = element.getBoundingClientRect();
    return rect.width * rect.height;
  }

  function rectInfo(element) {
    const rect = element.getBoundingClientRect();
    return { x: Math.round(rect.x), y: Math.round(rect.y), width: Math.round(rect.width), height: Math.round(rect.height) };
  }

  function audioUrl(element) {
    return element.currentSrc || element.src || element.querySelector('source')?.src || '';
  }

  function normalizedText(element) {
    if (element instanceof HTMLTextAreaElement || element instanceof HTMLInputElement) {
      return normalizedTextValue(element.value);
    }
    return normalizedTextValue(element?.innerText || element?.textContent || '');
  }

  function normalizedTextValue(value) {
    return String(value).replace(/\s+/g, ' ').trim();
  }

  function editorMatches(editor, expected) {
    const actual = editor instanceof HTMLTextAreaElement || editor instanceof HTMLInputElement
      ? editor.value
      : editor.innerText || editor.textContent || '';
    return normalizedTextValue(actual) === normalizedTextValue(expected);
  }

  function characterCountMatches(expectedLength, tolerance = 0) {
    const actual = readCharacterCount();
    return actual !== null && actual > 0 && Math.abs(actual - expectedLength) <= tolerance;
  }

  function readCharacterCount() {
    const counts = Array.from(document.querySelectorAll('div, span, p'))
      .filter(inViewport)
      .map((element) => normalizedText(element).replace(/\s+/g, '').match(/^(\d+)\/15000字符$/))
      .filter(Boolean)
      .map((match) => Number(match[1]));
    return counts.length > 0 ? counts[0] : null;
  }

  function delay(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  function interactionPause(minMs = 700, maxMs = 1200) {
    const duration = Math.round(minMs + Math.random() * (maxMs - minMs));
    return delay(duration);
  }

  function createBadge() {
    badge = document.createElement('div');
    badge.id = 'fish-audio-batch-bridge-status';
    Object.assign(badge.style, {
      position: 'fixed',
      right: '18px',
      bottom: '18px',
      zIndex: '2147483647',
      padding: '9px 13px',
      borderRadius: '10px',
      color: '#fff',
      background: '#64748b',
      font: '13px/1.3 "Microsoft YaHei", sans-serif',
      boxShadow: '0 6px 20px rgba(0,0,0,.18)',
      maxWidth: '360px'
    });
    badge.textContent = 'Fish批量桥接已加载';
    document.documentElement.appendChild(badge);
  }

  function setBadge(text, color) {
    if (!badge?.isConnected) createBadge();
    badge.textContent = text;
    badge.style.background = color;
  }
})();
