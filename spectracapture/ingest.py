import hashlib,json
from pathlib import Path
from .store import connect
def capture(path,kind,file):
    raw=Path(file).read_bytes()
    digest=hashlib.sha256(raw).hexdigest()
    payload=json.loads(raw)
    if not isinstance(payload,dict) or not isinstance(payload.get('version'),str) or not payload['version']:
        raise ValueError('version_required')
    db=connect(path)
    try:
        with db:
            old=db.execute('SELECT id FROM export_file WHERE kind=? AND hash=?',(kind,digest)).fetchone()
            if old:return old['id'],payload
            source_id=db.execute('INSERT INTO export_file(kind,version,hash,body) VALUES (?,?,?,?)',
                                 (kind,payload['version'],digest,raw)).lastrowid
            for i,item in enumerate(payload.get('rows',[]),1):
                identity=str(item.get('id',i))
                db.execute('INSERT INTO export_record(source_id,line_no,identity,body,status) VALUES (?,?,?,?,?)',
                           (source_id,i,identity,json.dumps(item,sort_keys=True,ensure_ascii=False),'captured'))
        return source_id,payload
    finally:db.close()

