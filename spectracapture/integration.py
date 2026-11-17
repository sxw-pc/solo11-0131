"""Dual analyst resolution of an ambiguous desktop peak integration."""
import json
from .ingest import capture
from .store import connect,save,decide


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
                        db.execute("UPDATE peak_assignment SET body=?,status='已归属',reason='dual_review' WHERE id=?",
                                   (json.dumps(updated,sort_keys=True,ensure_ascii=False),assignment['id']))
                        db.execute("UPDATE sample_export SET status='已归属',reason='dual_review' WHERE id=?",(sample['id'],))
                        save(db,'integration_revision',f'revision:{key}',source,payload['version'],
                             {'sample':row['sample'],'before':previous,'after':updated},status,reason)
                save(db,'peak_review',key,source,payload['version'],row,status,reason)
                results.append(decide(db,'峰积分复核',key,status,reason,source))
        return results
    finally:db.close()
