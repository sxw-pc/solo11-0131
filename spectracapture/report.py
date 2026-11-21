import hashlib,json
from pathlib import Path
from .store import connect,TABLES
def build(path,out):
    db=connect(path)
    try:
        sources=[]
        for item in db.execute('SELECT * FROM export_file ORDER BY id'):
            x=dict(item);x.pop('body')
            x['verified']=hashlib.sha256(item['body']).hexdigest()==item['hash']
            sources.append(x)
        data={'sources':sources,'entities':{},'issues':[dict(x) for x in db.execute('SELECT * FROM issue')],
              'decisions':[dict(x) for x in db.execute('SELECT * FROM decision_log')]}
        for table in TABLES:
            data['entities'][table]=[dict(x) for x in db.execute(f'SELECT * FROM {table} ORDER BY id')]
        data['waste']=_waste_summary(db)
        out=Path(out);out.parent.mkdir(parents=True,exist_ok=True)
        out.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
        return out
    finally:db.close()

def _waste_summary(db):
    summary={}
    for row in db.execute('SELECT id FROM waste_container ORDER BY id'):
        key=row['id']
        dispositions=db.execute("SELECT * FROM waste_disposition WHERE json_extract(body,'$.container')=? ORDER BY rowid",(key,)).fetchall()
        if not dispositions:continue
        latest=json.loads(dispositions[-1]['body'])
        orders=[{'id':x['id'],'status':x['status'],'reason':x['reason']}
                for x in db.execute("SELECT * FROM waste_transport_order WHERE json_extract(body,'$.container')=? ORDER BY rowid",(key,))]
        incidents=[x['id'] for x in db.execute("SELECT id FROM waste_incident WHERE json_extract(body,'$.container')=? ORDER BY rowid",(key,))]
        summary[key]={'transport':latest['transport'],'remediation':latest['remediation'],
                      'operations':latest['operations'],'dispositions':len(dispositions),
                      'orders':orders,'incidents':incidents}
    return summary
