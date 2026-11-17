"""Reconcile desktop sample handoff exports and revoke compromised results."""
import json
from datetime import datetime
from .ingest import capture
from .store import connect, save, decide


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
                if not sample_record:
                    status, reason = '待核', 'sample_missing'
                elif not holder or not transfers:
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
                    if status == '合格' and holder != str(row['destination']):
                        status, reason = '待核', 'destination_missing'
                save(db, 'custody_case', key, source, payload['version'], row, status, reason)
                if status == '隔离':
                    released = db.execute("SELECT * FROM released_result WHERE json_extract(body,'$.sample')=? AND status='可放行'",
                                          (sample,)).fetchall()
                    for result in released:
                        db.execute("UPDATE released_result SET status='撤回',reason='custody_quarantine' WHERE id=?", (result['id'],))
                        result_id = result['id'].removeprefix('result:')
                        manifests = db.execute("SELECT DISTINCT m.* FROM release_manifest m JOIN release_item i ON i.id LIKE m.id || ':%' "
                                               "WHERE json_extract(i.body,'$.result')=? AND m.status='已封账'", (result_id,)).fetchall()
                        for manifest in manifests:
                            db.execute("UPDATE release_manifest SET status='撤回',reason='custody_quarantine' WHERE id=?",
                                       (manifest['id'],))
                            save(db, 'release_recall', f'{key}:{manifest["id"]}', source, payload['version'],
                                 {'sample': sample, 'result': result_id, 'manifest': manifest['id']},
                                 '待通知', 'sealed_batch_recalled')
                results.append(decide(db, '样本交接', key, status, reason, source))
        return results
    finally:
        db.close()
