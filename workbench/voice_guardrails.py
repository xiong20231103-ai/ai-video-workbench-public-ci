from __future__ import annotations

import csv
import hashlib
from pathlib import Path


def _normalized(value):
    return " ".join(str(value or "").split()).casefold()


def load_voice_rules(file_path):
    rules = {}
    path = Path(file_path)
    if not path.is_file():
        return rules
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            language = str(row.get("language") or "").strip()
            voice = str(row.get("voice") or "").strip()
            if language and voice:
                rules[_normalized(language)] = voice
    return rules


def sha256_file(file_path):
    digest = hashlib.sha256()
    with Path(file_path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_voice_handoff(items, *, task_id, rules_file):
    """Validate task identity, language/voice evidence and audio artifacts.

    This gate is fully deterministic and never calls a model.  It deliberately
    does not claim that a file hash is an acoustic speaker embedding; acoustic
    verification remains pending until user-confirmed local baselines exist.
    """

    rules = load_voice_rules(rules_file)
    failures = []
    warnings = []
    verified = []
    hashes = {}
    for index, item in enumerate(items or []):
        if not isinstance(item, dict):
            failures.append({"code": "invalid_voice_item", "index": index})
            continue
        item_task_id = str(item.get("canonicalTaskId") or item.get("taskId") or "").strip()
        if task_id and item_task_id != task_id:
            failures.append({
                "code": "voice_task_identity_mismatch",
                "index": index,
                "expectedTaskId": task_id,
                "actualTaskId": item_task_id,
            })
            continue
        language = str(item.get("language") or "").strip()
        expected = str(item.get("expectedVoice") or "").strip()
        actual = str(item.get("actualActiveVoice") or "").strip()
        mapped = rules.get(_normalized(language), "")
        evidence = item.get("voiceVerification") if isinstance(item.get("voiceVerification"), dict) else {}
        matching = evidence.get("matchingSources") if isinstance(evidence.get("matchingSources"), list) else []
        conflicts = evidence.get("conflictingSources") if isinstance(evidence.get("conflictingSources"), list) else []
        if not mapped:
            failures.append({"code": "language_voice_mapping_missing", "language": language})
        elif _normalized(expected) != _normalized(mapped):
            failures.append({
                "code": "expected_voice_mapping_mismatch",
                "language": language,
                "expectedVoice": mapped,
                "handoffVoice": expected,
            })
        if item.get("voiceVerified") is not True:
            failures.append({"code": "fish_voice_not_verified", "language": language})
        if not actual or _normalized(actual) != _normalized(expected):
            failures.append({
                "code": "active_voice_mismatch",
                "language": language,
                "expectedVoice": expected,
                "actualActiveVoice": actual,
            })
        if len(matching) < 2 or conflicts:
            failures.append({
                "code": "voice_evidence_invalid",
                "language": language,
                "matchingEvidence": len(matching),
                "conflictCount": len(conflicts),
            })
        audio_path = Path(str(item.get("audioPath") or ""))
        if not audio_path.is_file() or audio_path.stat().st_size <= 0:
            failures.append({"code": "voice_audio_missing", "language": language})
            continue
        actual_hash = sha256_file(audio_path)
        declared_hash = _normalized(item.get("audioSha256"))
        if not declared_hash:
            failures.append({"code": "voice_audio_hash_missing", "language": language})
        elif actual_hash.casefold() != declared_hash:
            failures.append({"code": "voice_audio_hash_mismatch", "language": language})
        previous = hashes.get(actual_hash)
        identity = f"{item_task_id}|{language}|{expected}"
        if previous and previous != identity:
            failures.append({
                "code": "voice_audio_reused_across_languages",
                "language": language,
                "otherAssignment": previous,
            })
        hashes[actual_hash] = identity
        verified.append({
            "language": language,
            "expectedVoice": expected,
            "actualActiveVoice": actual,
            "audioSha256": actual_hash,
        })

    warnings.append({
        "code": "acoustic_voiceprint_baseline_pending",
        "message": "A user-confirmed local speaker baseline is not configured; no acoustic identity claim was made.",
    })
    return {
        "ok": not failures,
        "modelCalled": False,
        "taskId": task_id,
        "verified": verified,
        "failures": failures,
        "warnings": warnings,
    }
