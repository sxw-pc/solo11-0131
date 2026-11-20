"""Reconcile desktop sample handoff exports and revoke compromised results.

A quarantine finding is emitted as its own evidence branch: a ``retraction``
node that points at the released result and at the sealed manifest it
withdraws.  Retraction is append-only -- the original release evidence stays in
the graph and keeps its old status -- so the merged business view can produce a
new withdrawn version and a pending notification instead of overwriting
history.
"""
import json
from datetime import datetime
from .ingest import capture
from .store import connect, save, decide, link, head_evidence, replica_id


def _retraction(db, source_id, version, kind, sample, target_eid, case_eid, old_status, manifest=None):
    key = f'recall:{kind}:{target_eid or sample}'
    body = {'kind': kind, 'sample': sample, 'target': target_eid,
            'previous_status': old_status, 'reason': 'custody_quarantine'}
    if manifest is not None:
        body['manifest'] = manifest
    status = '待通知' if kind == 'manifest' else '已撤回'
    eid = save(db, 'retraction', key, source_id, version, body, status, 'custody_quarantine')
    if kind == 'manifest':
        # Notification item for the sealed batch that must be told about the recall.
        save(db, 'release_recall', f'{key}:{sample}', source_id, version,
             {'sample': sample, 'manifest': body.get('manifest'), 'target': target_eid,
              'retraction': eid}, '待通知', 'sealed_batch_recalled')
    link(db, eid, case_eid, 'references')
    if target_eid:
        link(db, eid, target_eid, 'retracts')
    return eid


def assess(path, file):
    source, payload = capture(path, 'desktop_custody_export', file)
    db = connect(path)
    results = []
    try:
        with db:
            for row in payload.get('rows', []):
                key = str(row['id'])
                sample = str(row['sample'])
                transfers = row.get('transfers', [])
                previous = db.execute('SELECT * FROM custody_case WHERE id=?', (key,)).fetchone()
                if previous:
                    if previous['body'] != json.dumps(row, sort_keys=True, ensure_ascii=False):
                        raise ValueError('custody_identity_conflict')
                    results.append(decide(db, '样本交接', key, previous['status'], previous['reason'], source))
                    continue
                sample_record = db.execute('SELECT 1 FROM sample_export WHERE id=?', (sample,)).fetchone()
                status, reason = '合格', 'continuous_custody'
                holder = str(row.get('origin', ''))
                last_time = None
                # Whether the seal/temperature chain is broken is intrinsic to
                # the handoff evidence: it is decided without needing the
                # sample record, which on a second replica may not exist yet.
                if not holder or not transfers:
                    status, reason = '待核', 'handoff_missing'
                else:
                    for i, transfer in enumerate(transfers):
                        at = datetime.fromisoformat(transfer['at'])
                        if transfer['from'] != holder or (last_time and at <= last_time):
                            status, reason = '隔离', 'broken_custody_chain'
                        elif not transfer.get('seal_intact', False):
                            status, reason = '隔离', 'seal_broken'
                        elif float(transfer['max_temperature']) > float(row['temperature_limit']):
                            status, reason = '隔离', 'temperature_excursion'
                        save(db, 'custody_transfer', f'{key}:{i}', source, payload['version'], transfer,
                             status, reason)
                        holder, last_time = str(transfer['to']), at
                        if status == '隔离':
                            break
                    if status == '合格' and not sample_record:
                        status, reason = '待核', 'sample_missing'
                    elif status == '合格' and holder != str(row['destination']):
                        status, reason = '待核', 'destination_missing'
                case_eid = save(db, 'custody_case', key, source, payload['version'], row, status, reason)
                if status == '隔离':
                    local = replica_id(db)
                    # Sample-level withdrawal: it follows the sample wherever the
                    # result evidence lived, including on another replica.
                    sample_target = head_evidence(db, 'sample_export', sample, local) or \
                        head_evidence(db, 'sample_export', sample)
                    _retraction(db, source, payload['version'], 'sample', sample,
                                sample_target, case_eid, '已归属')
                    released = db.execute("SELECT * FROM released_result WHERE json_extract(body,'$.sample')=? "
                                          "AND status='可放行'", (sample,)).fetchall()
                    for result in released:
                        target = head_evidence(db, 'released_result', result['id'], local) or \
                            head_evidence(db, 'released_result', result['id'])
                        _retraction(db, source, payload['version'], 'result', sample, target, case_eid, result['status'])
                        result_id = result['id'].removeprefix('result:')
                        manifests = db.execute(
                            "SELECT DISTINCT m.* FROM release_manifest m JOIN release_item i ON i.id LIKE m.id || ':%' "
                            "WHERE json_extract(i.body,'$.result')=? AND m.status='已封账'", (result_id,)).fetchall()
                        for manifest in manifests:
                            manifest_target = head_evidence(db, 'release_manifest', manifest['id'], local) or \
                                head_evidence(db, 'release_manifest', manifest['id'])
                            _retraction(db, source, payload['version'], 'manifest', sample,
                                        manifest_target, case_eid, manifest['status'],
                                        manifest=manifest['id'])
                    # Keep the local projection consistent for subsequent
                    # analyst commands; the evidence graph is the authority.
                    db.execute("UPDATE released_result SET status='撤回',reason='custody_quarantine' "
                               "WHERE json_extract(body,'$.sample')=? AND status='可放行'", (sample,))
                    for result in released:
                        result_id = result['id'].removeprefix('result:')
                        db.execute(
                            "UPDATE release_manifest SET status='撤回',reason='custody_quarantine' "
                            "WHERE id IN (SELECT DISTINCT m.id FROM release_manifest m JOIN release_item i "
                            "ON i.id LIKE m.id || ':%' WHERE json_extract(i.body,'$.result')=?)",
                            (result_id,))
                results.append(decide(db, '样本交接', key, status, reason, source))
        return results
    finally:
        db.close()
