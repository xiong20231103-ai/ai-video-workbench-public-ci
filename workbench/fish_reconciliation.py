"""Read-only reconciliation of a Fish launch whose controller receipt was lost."""
import ctypes
import os
import hashlib
import subprocess
from pathlib import Path
from datetime import datetime


def process_absent(pid):
    """Access failure is unknown, never proof that a producer has exited."""
    if os.name != 'nt' or not isinstance(pid, int) or pid <= 0:
        return False
    k = ctypes.WinDLL('kernel32', use_last_error=True)
    k.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    k.OpenProcess.restype = ctypes.c_void_p
    k.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = k.OpenProcess(0x1000 | 0x100000, False, pid)
    if handle:
        # Windows may retain a terminated process object while another process
        # holds a handle. Opening it is not proof that it is still running.
        k.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        k.GetExitCodeProcess.restype = ctypes.c_int
        k.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        k.WaitForSingleObject.restype = ctypes.c_ulong
        try:
            code = ctypes.c_ulong()
            return (bool(k.GetExitCodeProcess(handle, ctypes.byref(code)))
                    and code.value != 259 and k.WaitForSingleObject(handle, 0) == 0)
        finally:
            k.CloseHandle(handle)
    return ctypes.get_last_error() == 87


def verify_audio_results(handoff):
    from examplebrand_production import ExamplebrandMediaTools
    tools = ExamplebrandMediaTools()
    verified = []
    for item in handoff['completedItems']:
        path = Path(item.get('audioPath') or '')
        if not path.is_file() or path.stat().st_size < 1024:
            raise ValueError('audio file missing or empty')
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if not item.get('audioSha256') or digest != item['audioSha256']:
            raise ValueError('audio hash mismatch')
        info = tools.probe(path)
        if not info.has_audio or info.duration <= 0:
            raise ValueError('invalid audio stream')
        result = subprocess.run([tools.ffmpeg, '-v', 'error', '-xerror', '-i', str(path), '-map', '0:a:0', '-f', 'null', '-'],
                                capture_output=True, timeout=30, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if result.returncode:
            raise ValueError('audio decode failed')
        verified.append({'taskId': item['taskId'], 'path': str(path), 'sha256': digest, 'duration': info.duration})
    return verified


def observe_failed_fish(command, mailbox, runtime, handoff, absent=process_absent, verify=verify_audio_results):
    """Only close a failed command with exact launch, batch and owner evidence.

    No successful output is inferred, no generation is dispatched and no
    existing audio is changed. Unmatched evidence stays blocked.
    """
    unknown = {'status': 'unknown', 'reason': 'Fish execution evidence incomplete or conflicting'}
    if command.get('step') != 'step3':
        return unknown
    command_id = str(command.get('commandId') or '')
    mailbox_matches = bool(command_id) and str(mailbox.get('commandId') or '') == command_id
    task = command.get('taskId') or command.get('canonicalTaskId')
    if not task or handoff.get('canonicalTaskId') != task:
        return unknown
    launch_facts_match = True
    for key, other in [('runGroupId', 'runGroupId'), ('batchId', 'sourceBatchId')]:
        if not command.get(key) or handoff.get(other) != command[key]:
            launch_facts_match = False
            return unknown
        if mailbox_matches and mailbox.get(key) != command[key]:
            mailbox_matches = False
    # The mailbox is transport, not producer evidence.  A later safe-continue
    # request may already have replaced it by the time an old Fish command is
    # reconciled.  Permit that race only for a command already blocked in
    # reconciliation_required and only when the immutable runtime/handoff
    # facts below prove the exact launch and terminal result.
    stale_mailbox_reconcile = (
        not mailbox_matches
        and str(command.get('state') or '').lower() == 'reconciliation_required'
        and launch_facts_match
        and runtime.get('state') == 'terminal'
        and handoff.get('status') in ('failed', 'completed', 'completed_with_errors')
        and runtime.get('terminalStatus') == handoff.get('status')
    )
    if not mailbox_matches and not stale_mailbox_reconcile:
        return unknown
    pid = runtime.get('pid')
    if not isinstance(pid, int) or pid <= 0 or (mailbox_matches and mailbox.get('bridgePid') != pid):
        return unknown
    if not runtime.get('batchId') or runtime['batchId'] != handoff.get('batchId'):
        return unknown
    if runtime.get('canonicalTaskIds') != [task]:
        return unknown
    tasks = runtime.get('taskIds')
    other_tasks = handoff.get('taskIds')
    if (not isinstance(tasks, list) or not tasks or not all(isinstance(t, str) and t for t in tasks)
            or not isinstance(other_tasks, list) or not all(isinstance(t, str) and t for t in other_tasks)
            or len(tasks) != len(set(tasks)) or len(other_tasks) != len(set(other_tasks))
            or set(tasks) != set(other_tasks)):
        return unknown
    terminal = handoff.get('status')
    if runtime.get('state') != 'terminal' or terminal not in ('failed', 'completed', 'completed_with_errors') or runtime.get('terminalStatus') != terminal:
        return unknown
    try:
        claimed = datetime.fromisoformat(command['claimedAt'])
        started = datetime.fromisoformat(runtime['startedAt'].replace('Z', '+00:00'))
        finished = datetime.fromisoformat(handoff['completedAt'].replace('Z', '+00:00'))
        if not (claimed <= started <= finished):
            return unknown
    except (KeyError, TypeError, ValueError, AttributeError):
        return unknown
    if not absent(pid):
        return unknown
    if terminal == 'completed_with_errors':
        # A partial batch is a terminal failure of the *recovery command*, not
        # permission to regenerate its successful siblings or advance Step 4.
        items = handoff.get('completedItems')
        failures = handoff.get('failedItems')
        if (not isinstance(items, list) or not isinstance(failures, list)
                or not all(isinstance(i, dict) for i in items + failures)):
            return unknown
        good = [i.get('taskId') for i in items]
        bad = [i.get('taskId') for i in failures]
        if (not bad or handoff.get('total') != len(tasks)
                or handoff.get('completed') != len(good) or handoff.get('failed') != len(bad)
                or not all(isinstance(t, str) and t for t in good + bad)
                or len(set(good + bad)) != len(tasks) or set(good + bad) != set(tasks)):
            return unknown
        return {'status': 'failed', 'reason': 'Matched Fish batch ended with partial results',
                'fishBatchId': runtime['batchId'], 'taskId': task, 'bridgePid': pid,
                'completedTaskIds': good, 'failedTaskIds': bad,
                'processObservation': 'exited', 'evidence': 'exact_partial_fish_terminal',
                'nextAction': 'retry_missing_voice_only'}
    if terminal == 'completed':
        items = handoff.get('completedItems')
        if (not isinstance(items, list) or len(items) != len(tasks)
                or handoff.get('total') != len(tasks) or handoff.get('completed') != len(tasks)
                or handoff.get('failed', 0) != 0
                or any(not isinstance(i, dict) for i in items)
                or sorted(str(i.get('taskId') or '') for i in items) != sorted(tasks)):
            return unknown
        for item in items:
            if (item.get('canonicalTaskId') != task or not item.get('copyTextHash')
                    or not item.get('copyId') or not item.get('language')
                    or item.get('languageContractVerified') is not True or item.get('voiceVerified') is not True
                    or not item.get('expectedVoice') or item['expectedVoice'] != item.get('actualActiveVoice')):
                return unknown
        try:
            artifacts = verify(handoff)
            if len(artifacts) != len(tasks):
                return unknown
        except Exception as exc:
            return {**unknown, 'reason': 'Fish audio verification failed: ' + str(exc)}
        return {'status': 'completed', 'fishBatchId': runtime['batchId'], 'taskId': task,
                'processObservation': 'exited', 'verifiedAudioArtifacts': artifacts,
                'evidence': 'exact_launch_and_hashed_decoded_audio', 'nextAction': 'resume_step4'}
    return {'status': 'failed', 'reason': 'Matched Fish batch ended with a durable failure',
            'fishBatchId': runtime['batchId'], 'taskId': task, 'bridgePid': pid,
            'producerErrorCode': handoff.get('errorCode') or runtime.get('errorCode'),
            'processObservation': 'exited', 'evidence': 'fish_runtime_and_handoff_and_command'}
