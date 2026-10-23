"""Read-only stage selection; producer evidence wins over cached pipeline labels."""
import hashlib
from pathlib import Path


def adopt_completed_voice(task, command, runtime, handoff):
    """Adoption is not a new generation receipt: old completion is permitted."""
    from fish_reconciliation import process_absent, verify_audio_results
    owner=task.get('canonicalTaskId') or task.get('taskId')
    if command.get('step')!='step3' or command.get('taskId')!=owner:
        return None
    for field, handoff_field in [('runGroupId','runGroupId'),('batchId','sourceBatchId')]:
        if not task.get(field) or command.get(field)!=task[field] or handoff.get(handoff_field)!=task[field]:
            return None
    if (handoff.get('canonicalTaskId')!=owner or handoff.get('status')!='completed'
            or runtime.get('batchId')!=handoff.get('batchId') or runtime.get('state')!='terminal'
            or runtime.get('terminalStatus')!='completed' or not process_absent(runtime.get('pid'))):
        return None
    items=handoff.get('completedItems') or []
    expected=runtime.get('taskIds') or []
    if (not expected or len(items)!=len(expected) or len(set(expected))!=len(expected)
            or sorted(i.get('taskId','') for i in items)!=sorted(expected)
            or handoff.get('completed')!=len(expected) or handoff.get('failed',0)!=0):
        return None
    for i in items:
        if (i.get('canonicalTaskId')!=owner or i.get('project')!=task.get('project')
                or i.get('material')!=task.get('material') or not i.get('copyTextHash') or not i.get('copyId')
                or not i.get('voiceVerified') or not i.get('languageContractVerified')
                or not i.get('expectedVoice') or i['expectedVoice']!=i.get('actualActiveVoice')):
            return None
    artifacts=verify_audio_results(handoff)
    return {'status':'completed','receiptKind':'existing_artifacts_adopted',
            'resultProducedAt':handoff.get('completedAt'),'fishBatchId':handoff['batchId'],
            'taskId':owner,'verifiedAudioArtifacts':artifacts,'reason':'Verified existing audio adopted without generation'}


def file_proof(value):
    path = Path(value or '')
    if not path.is_file() or path.stat().st_size < 1024:
        raise ValueError('missing or empty media: ' + str(path))
    digest = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            digest.update(chunk)
    return {'path': str(path), 'size': path.stat().st_size, 'sha256': digest.hexdigest()}


def verify_final_row(row):
    """Verify a completed CapCut row from durable local QC evidence.

    This is intentionally read-only and conservative: a row is completed
    only when the recorded QC status is passed, the final file is present and
    non-empty, and any recorded QC report is still present.  It lets Safe
    Continue adopt an output that predates the current command without
    treating a mere ``status=completed`` string as proof.
    """
    row = row if isinstance(row, dict) else {}
    if str(row.get('qualityStatus') or '').strip().lower() not in {'passed', 'pass', 'ok'}:
        return False
    output = Path(str(row.get('finalOutputPath') or row.get('outputPath') or ''))
    if not output.is_file() or output.stat().st_size <= 0:
        return False
    report = str(row.get('qualityReportPath') or '').strip()
    if report and not Path(report).is_file():
        return False
    return True


def released_final_is_verified(row):
    """A failed maintenance attempt cannot invalidate a proved, released final.

    Require a task-bound durable QC report and its exact current file bytes.
    No inference from a cached completed label or draft-release label alone.
    """
    import json
    if (row.get('productionStatus') != 'completed'
            or row.get('draftReleaseStatus') != 'released'
            or row.get('draftCleanupStatus') == 'release_pending'
            or not verify_final_row(row)):
        return False
    try:
        report = json.loads(Path(row.get('qualityReportPath') or '').read_text(encoding='utf-8-sig'))
        output = Path(row.get('finalOutputPath') or '')
        if (not isinstance(report, dict) or report.get('status') != 'passed' or not row.get('taskId')
                or report.get('taskId') != row['taskId']
                or report.get('canonicalTaskId') != row.get('remotePipelineTaskId')
                or not row.get('remotePipelineTaskId')
                or report.get('fileName') != output.name
                or report.get('fileSize') != output.stat().st_size
                or not report.get('sha256')):
            return False
        return file_proof(str(output))['sha256'].lower() == str(report['sha256']).lower()
    except (OSError, ValueError, TypeError):
        return False


def resolve_resume_plan(task, capcut_rows, *, audio_evidence=None, body_evidence=None,
                        verify_file=file_proof, verify_final=None,
                        preferred_child_task_id=None):
    """No state writes, no fallback from contradictory higher-stage evidence."""
    owner = task.get('canonicalTaskId') or task.get('taskId')
    base = {'taskId': owner, 'runGroupId': task.get('runGroupId'), 'batchId': task.get('batchId')}
    def blocked(reason):
        return {**base, 'decision': 'blocked', 'reasonCode': 'RESUME_EVIDENCE_CONFLICT', 'reason': reason}
    if not all(base.values()):
        return blocked('parent identity incomplete')
    rows = [r for r in capcut_rows if r.get('remotePipelineTaskId') == owner]
    if rows:
        expected = int(task.get('expectedCount') or 0)
        if not expected or len(rows) != expected or len({r.get('taskId') for r in rows}) != expected:
            return blocked('handoff count or duplicate child identity')
        proofs = {}; remaining = []; verified_finals = []
        try:
            for row in rows:
                for field in ('project', 'material', 'productionDate'):
                    wanted = task.get(field) or (task.get('date') if field == 'productionDate' else '')
                    actual = row.get(field) or (row.get('date') if field == 'productionDate' else '')
                    if not wanted or wanted != actual:
                        return blocked('handoff mismatch: ' + field)
                # Old formal contracts may not carry these fields; when
                # supplied they must agree, and ExampleBrand requires both.
                for field in ('runGroupId', 'batchId'):
                    # Older Step-4/5 handoffs were written before ExampleBrand
                    # carried the run/batch pair into each child row.  An
                    # empty value is safe to backfill only when the row is
                    # already tied to this exact remotePipelineTaskId; a
                    # supplied value must still match exactly.
                    if row.get(field) and row[field] != task[field]:
                        return blocked('handoff mismatch: ' + field)
                if not row.get('taskId'):
                    return blocked('missing output task identity')
                if row.get('productionStatus') == 'completed' or row.get('status') == 'completed':
                    if verify_final is None or not verify_final(row):
                        return blocked('completed output lacks verified QC')
                    verified_finals.append(row['taskId'])
                    continue
                evidence = {}
                # The sample is selected in Step 5 after the body/audio
                # handoff is accepted.  Requiring it here incorrectly blocks
                # a clean recovery of the current ExampleBrand rows, which have no
                # sample committed yet.  If an older row already has one,
                # verify it as additional evidence.
                for field in ('videoPath', 'audioPath'):
                    value = row.get(field)
                    if not value:
                        return blocked('handoff missing ' + field)
                    if value not in proofs: proofs[value] = verify_file(value)
                    evidence[field] = proofs[value]
                sample_value = row.get('originalSamplePath') or row.get('preparedSamplePath')
                if sample_value:
                    if sample_value not in proofs: proofs[sample_value] = verify_file(sample_value)
                    evidence['originalSamplePath'] = proofs[sample_value]
                remaining.append({'row': row, 'media': evidence})
        except (OSError, ValueError) as exc:
            return blocked(str(exc))
        if not remaining:
            return {**base, 'decision': 'completed', 'nextAction': 'finish_maintenance', 'verifiedFinals': verified_finals}
        # A durable project-status record may identify the exact failed child
        # in the latest CapCut attempt.  Prefer that child only after the full
        # handoff and all completed-output QC evidence have been verified;
        # never silently fall back to an older failed row or a later task.
        preferred_id = str(preferred_child_task_id or '').strip()
        if preferred_id:
            preferred = [item for item in remaining
                         if str(item['row'].get('taskId') or '').strip() == preferred_id]
            if len(preferred) == 1:
                first = preferred[0]
            elif preferred_id in verified_finals:
                completed_row = next(r for r in rows if r['taskId'] == preferred_id)
                if not released_final_is_verified(completed_row):
                    return blocked('active failed child contradicts QC-verified completed output')
                first = remaining[0]
            else:
                return blocked('active failed child is missing from the verified handoff')
        else:
            # Preserve handoff order when no producer has identified the
            # failed child; do not select an unrelated cached child ID.
            first = remaining[0]
        return {**base, 'decision': 'ready', 'step': 'step5', 'nextAction': 'resume_capcut',
                'childTaskId': first['row']['taskId'], 'row': first['row'],
                'sourceVideoHash': first['media']['videoPath']['sha256'], 'evidence': remaining,
                'verifiedFinals': verified_finals}
    if body_evidence:
        return {**base, 'decision': 'ready', 'step': 'step4', 'nextAction': 'rebuild_handoff', 'evidence': body_evidence}
    if audio_evidence:
        return {**base, 'decision': 'ready', 'step': 'step4', 'nextAction': 'resume_stitch', 'evidence': audio_evidence}
    return {**base, 'decision': 'ready', 'step': 'step3', 'nextAction': 'reconcile_voice'}
