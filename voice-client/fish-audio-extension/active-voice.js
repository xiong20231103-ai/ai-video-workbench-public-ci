(() => {
  function normalize(value) {
    return String(value ?? '').replace(/\s+/g, ' ').trim();
  }

  function comparable(value) {
    return normalize(value).toLocaleLowerCase();
  }

  function sameVoice(first, second) {
    return Boolean(comparable(first)) && comparable(first) === comparable(second);
  }

  function voiceNameFromCard(cardText) {
    const raw = String(cardText ?? '').replace(/\r/g, '').trim();
    if (!raw) return '';
    const firstLine = raw.split('\n').map(normalize).find(Boolean) || '';
    return normalize(
      firstLine
        .split(/\s*[·•]\s*/)[0]
        .replace(/\s+(?:男性|女性|男声|女声|male|female)$/i, '')
    );
  }

  function evaluate({
    expectedVoice,
    headerVoice,
    editorVoice,
    rightCardVoice,
    primarySettingVoice,
    editorCount,
    availableVoices = []
  }) {
    const expected = normalize(expectedVoice);
    const header = normalize(headerVoice);
    const editorCandidate = normalize(editorVoice);
    // The page heading can sit directly above the editor and is not a voice.
    const editor = isGenericEditorLabel(editorCandidate) ? '' : editorCandidate;
    const rightCard = normalize(rightCardVoice || primarySettingVoice);
    const primary = rightCard;
    const available = [...new Set(availableVoices.map(normalize).filter(Boolean))];
    const sources = [
      ['header', header],
      ['editor', editor],
      ['rightCard', rightCard]
    ].filter(([, value]) => Boolean(value));
    const matchingSources = sources.filter(([, value]) => sameVoice(value, expected)).map(([name]) => name);
    const conflictingSources = sources.filter(([, value]) => !sameVoice(value, expected)).map(([name]) => name);
    const evidence = {
      expected,
      header,
      editor,
      rightCard,
      primary,
      editorCount: Number(editorCount || 0),
      available,
      matchingSources,
      conflictingSources
    };

    if (!expected) return { ok: false, actualActiveVoice: '', reason: 'missing_expected_voice', evidence };
    if (Number(editorCount || 0) !== 1) return { ok: false, actualActiveVoice: header, reason: 'ambiguous_editor_count', evidence };
    if (conflictingSources.length > 0) {
      return {
        ok: false,
        actualActiveVoice: sources[0]?.[1] || '',
        reason: 'voice_evidence_conflict',
        evidence
      };
    }
    if (matchingSources.length < 2) {
      return {
        ok: false,
        actualActiveVoice: sources[0]?.[1] || '',
        reason: 'insufficient_voice_evidence',
        evidence
      };
    }
    return { ok: true, actualActiveVoice: expected, reason: 'verified_by_consensus', evidence };
  }

  function headerVoiceFromSelector(selectorText) {
    const value = normalize(selectorText);
    // A real Fish speaker selector contains only the speaker name.  Returning
    // an empty value is safer than accepting a page/container whose text also
    // happens to include the expected voice.
    if (!value || value.length > 80) return '';
    return value;
  }

  function isGenericEditorLabel(value) {
    return new Set([
      '文本转语音',
      'text to speech',
      '文字转语音',
      '语音合成',
      'voice synthesis',
      '添加说话人',
      'add speaker'
    ]).has(comparable(value));
  }

  globalThis.FishActiveVoice = {
    normalize,
    comparable,
    sameVoice,
    voiceNameFromCard,
    evaluate,
    headerVoiceFromSelector,
    isGenericEditorLabel
  };
})();
