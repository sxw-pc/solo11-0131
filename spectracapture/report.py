import hashlib,json
from pathlib import Path
from .store import connect,TABLES
from . import trust
def build(path,out):
    db=connect(path)
    try:
        sources=[]
        for item in db.execute('SELECT * FROM export_file ORDER BY id'):
            x=dict(item);x.pop('body')
            # Byte-integrity check, unchanged: only proves post-ingest immutability.
            x['verified']=hashlib.sha256(item['body']).hexdigest()==item['hash']
            verdict=trust.source_verdict(db,item['id'])
            if verdict is not None:
                x['source_archive']=verdict['archive']
                x['source_business']=verdict['business']
                if 'instrument_id' in verdict:
                    x['instrument_id']=verdict['instrument_id']
                    x['sequence']=verdict['sequence']
            sources.append(x)
        data={'sources':sources,'entities':{},'issues':[dict(x) for x in db.execute('SELECT * FROM issue')],
              'decisions':[dict(x) for x in db.execute('SELECT * FROM decision_log')],
              'trust_roots':[dict(x) for x in db.execute('SELECT instrument_id,key_id,imported_at FROM trust_root')],
              'signing_keys':[dict(x) for x in db.execute('SELECT instrument_id,key_id,from_sequence,status FROM signing_key')],
              'key_rotations':[dict(x) for x in db.execute('SELECT instrument_id,old_key_id,new_key_id,from_sequence,status,reason FROM key_rotation')],
              'device_forks':[dict(x) for x in db.execute('SELECT * FROM device_fork')],
              'revocation_bulletins':[dict(x) for x in db.execute('SELECT bulletin_id,instrument_id,issuer,key_id,first_sequence,last_sequence,created FROM revocation_bulletin')],
              'source_verdict_history':[dict(x) for x in db.execute('SELECT * FROM source_verdict_history ORDER BY id')]}
        for table in TABLES:
            data['entities'][table]=[dict(x) for x in db.execute(f'SELECT * FROM {table} ORDER BY id')]
        out=Path(out);out.parent.mkdir(parents=True,exist_ok=True)
        out.write_text(json.dumps(data,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
        return out
    finally:db.close()
