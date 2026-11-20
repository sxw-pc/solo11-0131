import json
from pathlib import Path
from .store import connect,TABLES
from . import archive
def build(path,out):
    db=connect(path)
    try:
        sources=[]
        with db:
            for item in db.execute('SELECT * FROM export_file ORDER BY id'):
                x=dict(item);x.pop('body')
                verdict=archive.verify(db,item)
                x['verified']=verdict['ok']
                if item['storage']=='cold' and not verdict['ok']:
                    archive.request_repair(db,item,verdict['detail'])
                x['recoverable']=archive.recoverable_status(db,item,verdict)
                state=db.execute('SELECT * FROM disposal_state WHERE source_id=? ORDER BY seq DESC LIMIT 1',
                                 (item['id'],)).fetchone()
                x['disposal']=dict(state) if state else None
                sources.append(x)
            data={'sources':sources,'entities':{},'issues':[dict(x) for x in db.execute('SELECT * FROM issue')],
                  'decisions':[dict(x) for x in db.execute('SELECT * FROM decision_log')],
                  'archive':archive.snapshot(db)}
            for table in TABLES:
                data['entities'][table]=[dict(x) for x in db.execute(f'SELECT * FROM {table} ORDER BY id')]
        out=Path(out);out.parent.mkdir(parents=True,exist_ok=True)
        out.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
        return out
    finally:db.close()
