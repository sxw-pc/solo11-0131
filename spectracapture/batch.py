"""Freeze a client report scope and distinguish partial from full release."""
import json
from .ingest import capture
from .store import connect,save,decide,link


def release(path,file):
    source,payload=capture(path,'desktop_result_batch',file)
    db=connect(path);results=[]
    try:
        with db:
            for row in payload.get('rows',[]):
                key=str(row['id']);ids=[str(x) for x in row['results']]
                previous=db.execute('SELECT * FROM release_manifest WHERE id=?',(key,)).fetchone()
                if previous and previous['status']=='撤回':
                    if previous['body']!=json.dumps(row,sort_keys=True,ensure_ascii=False):
                        raise ValueError('batch_identity_conflict')
                    results.append(decide(db,'整批放行',key,'撤回','custody_quarantine',source));continue
                values=[db.execute('SELECT * FROM released_result WHERE id=?',(f'result:{rid}',)).fetchone() for rid in ids]
                if not ids or len(set(ids))!=len(ids):status,reason='待核','invalid_result_scope'
                elif any(x is None for x in values):status,reason='待核','result_missing'
                elif all(x['status']=='可放行' for x in values):status,reason='已封账','all_results_releasable'
                else:status,reason='部分待核','unreleasable_result'
                manifest_eid=save(db,'release_manifest',key,source,payload['version'],row,status,reason)
                for rid,value in zip(ids,values):
                    item_eid=save(db,'release_item',f'{key}:{rid}',source,payload['version'],
                         {'result':rid,'result_status':value['status'] if value else '缺失'},status,reason)
                    link(db,item_eid,manifest_eid,'references')
                results.append(decide(db,'整批放行',key,status,reason,source))
        return results
    finally:db.close()
