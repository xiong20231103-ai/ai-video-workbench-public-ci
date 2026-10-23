"""Task-bound derived source replacement; never rewrites submitted contracts."""
import copy
import hashlib
import json
import os
import uuid
from pathlib import Path


def _records_for_owner(record):
    """Normalize one record or the ordered set of merged child records."""
    if isinstance(record, (list, tuple)):
        return [item for item in record if isinstance(item, dict)]
    return [record] if isinstance(record, dict) else []


def derive(task, sources, record, parent):
    def require(ok, message):
        if not ok:
            raise ValueError('ExampleBrand_REPLENISHMENT_IDENTITY_CONFLICT: ' + message)
    owner = task.get('canonicalTaskId') or task.get('taskId')
    require(task.get('batchKind') == 'examplebrand-natural-flow' and owner and task.get('runGroupId') and task.get('batchId'), 'parent identity')
    plans = task.get('outputPlan') or []
    require(len(plans) == 21 and len({p.get('canonicalTaskId') for p in plans}) == 21, 'output identities')
    source_map = {s['url']: s for s in sources}
    require(len(source_map) == len(sources) == 21, 'unique source count')
    missing = [p for p in plans if p.get('sourceUrl') not in source_map]
    if not missing:
        return copy.deepcopy(task), []
    records = _records_for_owner(record)
    require(records, 'replenishment ownership')
    child = str(task.get('replenishmentClosedByTaskId') or '').strip()
    expected_identity = {
        'project': str(task.get('project') or '').strip(),
        'material': str(task.get('material') or '').strip(),
        'productionDate': str(task.get('productionDate') or task.get('date') or '').strip(),
    }
    child_ids = set()
    for current in records:
        current_id = str(current.get('taskId') or '').strip()
        require(current_id and current.get('canonicalTaskId') == owner
                and current.get('replenishForTaskId') == owner
                and current.get('status') == 'merged_into_parent', 'replenishment ownership')
        if child and current_id == child:
            child_ids.add(current_id)
        for key, expected in expected_identity.items():
            require(expected and expected == str(current.get(key) or current.get('date') or '').strip(), key)
        child_ids.add(current_id)
    require(child_ids, 'replenishment ownership')
    require(parent.get('taskId') == owner, 'parent checkpoint owner')
    items = list((parent.get('items') or {}).values())
    failed = {i.get('url') for i in items if i.get('status') == 'failed'}
    require(all(p.get('sourceUrl') in failed for p in missing), 'missing URLs lack failed checkpoint')
    original = {p.get('sourceUrl') for p in plans}
    # A parent can be replenished more than once.  The previous implementation
    # compared the missing count with only the latest child's URL list, which
    # rejected a valid sequence such as 1 completed replacement in child A plus
    # 1 completed replacement in child B.  Failed/unfinished child URLs are
    # deliberately excluded: only a completed, identity-bound source can fill
    # a parent gap.
    submitted = []
    for current in records:
        current_id = str(current.get('taskId') or '').strip()
        for url in current.get('urls') or []:
            value = str(url or '').strip()
            if value and value in source_map and value not in original:
                source = source_map[value]
                require(str(source.get('checkpointId') or '').strip() == current_id,
                        'replacement checkpoint')
                submitted.append(value)
    replacements = list(dict.fromkeys(submitted))
    require(len(replacements) == len(set(replacements)) == len(missing), 'replacement count')
    require(set(replacements) == set(source_map) - original, 'replacement URL set')
    result = copy.deepcopy(task)
    audit = []
    for plan, url in zip([p for p in result['outputPlan'] if p.get('sourceUrl') not in source_map], replacements):
        checkpoint_id = str(source_map[url].get('checkpointId') or '').strip()
        audit.append({'canonicalTaskId': plan['canonicalTaskId'], 'originalSourceUrl': plan['sourceUrl'],
                      'sourceUrl': url, 'checkpointId': checkpoint_id,
                      'sourceHash': hashlib.sha256(Path(source_map[url]['path']).read_bytes()).hexdigest()})
        plan['sourceUrl'] = url
    return result, audit


def prepare(task, sources, imported_dir, checkpoint_dir, artifact_root):
    source_urls = {s.get('url') for s in sources}
    if all(p.get('sourceUrl') in source_urls for p in task.get('outputPlan') or []):
        return task
    owner = task.get('canonicalTaskId') or task.get('taskId')
    child = task.get('replenishmentClosedByTaskId')
    # These keys are used only as single filenames, never arbitrary paths.
    for key in (owner, child):
        if not key or any(c in str(key) for c in '/\\:') or key in ('.', '..'):
            raise ValueError('ExampleBrand_REPLENISHMENT_IDENTITY_CONFLICT: invalid task key')
    read = lambda p: json.loads(Path(p).read_text(encoding='utf-8-sig'))
    # Read every merged child for this parent.  The active task stores only the
    # latest child id, but earlier successful replacements remain valid evidence
    # and must be included in the identity/count reconciliation.
    records = []
    for record_path in sorted(Path(imported_dir).glob('*.json'), key=lambda p: p.name.casefold()):
        try:
            current = read(record_path)
        except (OSError, ValueError, UnicodeError):
            continue
        if not isinstance(current, dict):
            continue
        if str(current.get('replenishForTaskId') or '').strip() != owner:
            continue
        if str(current.get('canonicalTaskId') or owner).strip() != owner:
            continue
        if str(current.get('status') or '').strip() != 'merged_into_parent':
            continue
        records.append(current)
    if not records and child:
        records = [read(Path(imported_dir)/f'{child}.json')]
    result, audit = derive(task, sources, records, read(Path(checkpoint_dir)/f'{owner}.json'))
    folder = Path(artifact_root)/owner
    output = folder/'source-replacements.json'
    payload = {'version': 1, 'policy': 'missing_output_order_to_submitted_replenishment_order',
               'canonicalTaskId': owner, 'runGroupId': task['runGroupId'], 'batchId': task['batchId'],
               'contractHash': hashlib.sha256(json.dumps(task['outputPlan'],sort_keys=True,ensure_ascii=False).encode()).hexdigest(),
               'replacements': audit}
    if output.exists():
        if read(output) != payload:
            raise ValueError('ExampleBrand_REPLENISHMENT_IDENTITY_CONFLICT: saved replacement differs')
    else:
        ledger = folder/'execution-manifest.json'
        if ledger.exists() and read(ledger).get('outputs'):
            raise ValueError('ExampleBrand_REPLENISHMENT_IDENTITY_CONFLICT: existing production requires review')
        folder.mkdir(parents=True, exist_ok=True)
        temp = output.with_name(output.name+'.'+uuid.uuid4().hex+'.tmp')
        with temp.open('x',encoding='utf-8') as f:
            json.dump(payload,f,ensure_ascii=False,indent=2); f.flush(); os.fsync(f.fileno())
        os.replace(temp,output)
    result['sourceReplacementAudit'] = payload
    return result
