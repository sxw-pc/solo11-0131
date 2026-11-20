"""Dual analyst resolution of an ambiguous desktop peak integration.

A review is evidence like any other: the previous assignment stays in the
graph, and the revision points back at it.  Cross-replica conflict resolution
is a separate, signed act handled by :func:`resolve`.
"""
import json
from .ingest import capture
from .store import connect, save, decide, revise, link, head_evidence, replica_id
from .report import review_signature


def review(path,file):
    source,payload=capture(path,'desktop_peak_review',file)
    db=connect(path);results=[]
    try:
        with db:
            for row in payload.get('rows',[]):
                key=str(row['id']);sample=db.execute('SELECT * FROM sample_export WHERE id=?',(str(row['sample']),)).fetchone()
                assignment=db.execute('SELECT * FROM peak_assignment WHERE id=?',(f"assignment:{row['sample']}",)).fetchone()
                old=db.execute('SELECT * FROM peak_review WHERE id=?',(key,)).fetchone()
                if old:
                    if old['body']!=json.dumps(row,sort_keys=True,ensure_ascii=False):raise ValueError('review_identity_conflict')
                    results.append(decide(db,'峰积分复核',key,old['status'],old['reason'],source));continue
                if not sample or not assignment or sample['reason']!='ambiguous_peaks':status,reason='拒绝','no_ambiguous_peak'
                elif not row.get('analyst_a') or row.get('analyst_a')==row.get('analyst_b'):status,reason='待核','dual_review_missing'
                else:
                    sample_body=json.loads(sample['body']);peaks=sample_body['peaks']
                    matches=[p for p in peaks if p['time']==row['chosen_time'] and
                             float(sample_body['window'][0])<=float(p['time'])<=float(sample_body['window'][1])]
                    if len(matches)!=1:status,reason='待核','chosen_peak_not_unique'
                    else:
                        status,reason='已裁定','dual_analyst_agreement'
                        previous=json.loads(assignment['body'])
                        updated=dict(previous,peak=matches[0])
                        prior=prior_assignment(db,assignment['id'])
                        revise(db,'peak_assignment',assignment['id'],source,payload['version'],updated,'已归属','dual_review')
                        revision=save(db,'integration_revision',f'revision:{key}',source,payload['version'],
                             {'sample':row['sample'],'before':previous,'after':updated},status,reason)
                        link(db,revision,prior,'references')
                        revise(db,'sample_export',sample['id'],source,payload['version'],
                               json.loads(sample['body']),'已归属','dual_review')
                save(db,'peak_review',key,source,payload['version'],row,status,reason)
                results.append(decide(db,'峰积分复核',key,status,reason,source))
        return results
    finally:db.close()


def prior_assignment(db,key):
    return head_evidence(db,'peak_assignment',key,replica_id(db))


def resolve(path,file):
    """Record a signed cross-replica review that cites every branch and recall.

    The resolution is only a *claim* here; the report's business reading checks
    that the signature is valid, that it cites every live branch and every
    retraction, and that no concurrent competing review survives.  If two
    replicas publish different reviews for the same target, both remain in the
    graph and the business view stays ``待核``.
    """
    source,payload=capture(path,'desktop_review_resolution',file)
    db=connect(path);results=[]
    try:
        with db:
            for row in payload.get('rows',[]):
                key=str(row['id'])
                body=dict(row)
                if body.get('signature')!=review_signature(body):
                    raise ValueError('review_signature_invalid')
                refs=[str(x) for x in body.get('references') or []]
                if not refs:
                    raise ValueError('review_references_required')
                old=db.execute('SELECT * FROM review_resolution WHERE id=?',(key,)).fetchone()
                if old:
                    if old['body']!=json.dumps(row,sort_keys=True,ensure_ascii=False):
                        raise ValueError('review_resolution_conflict')
                    results.append(decide(db,'离线复核',key,old['status'],old['reason'],source));continue
                status=body.get('status') or '已复核'
                reason=body.get('reason') or 'signed_review'
                eid=save(db,'review_resolution',key,source,payload['version'],body,status,reason)
                for ref in refs:
                    if not db.execute('SELECT 1 FROM evidence WHERE evidence_id=?',(ref,)).fetchone():
                        raise ValueError('review_reference_unknown')
                    link(db,eid,ref,'references')
                for prior in body.get('supersedes') or []:
                    if db.execute('SELECT 1 FROM evidence WHERE evidence_id=?',(str(prior),)).fetchone():
                        link(db,eid,str(prior),'supersedes')
                results.append(decide(db,'离线复核',key,status,reason,source))
        return results
    finally:db.close()
