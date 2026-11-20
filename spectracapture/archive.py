"""Cold-storage custody, preservation holds, and expiry disposal of original export files.

Original bytes live either in export_file.body ('db') or in a digest-addressed
cold-storage object ('cold'); after a completed destruction contract the source
is 'destroyed'. Migration publishes the object before the database switches its
reference, and every ruling is appended to a per-source disposal_state chain
whose seq is a replayable cursor assigned inside an explicit transaction
boundary (BEGIN IMMEDIATE), never inferred from wall-clock order.
"""
import hashlib,json,os
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from .store import connect,decide


class FaultInjected(RuntimeError):
    """Raised at a named fault-injection point to simulate a crash."""


@contextmanager
def _txn(db):
    """Explicit commit boundary: one serialized writer at a time."""
    db.execute('BEGIN IMMEDIATE')
    try:
        yield db
    except BaseException:
        db.rollback();raise
    else:
        db.commit()


def _open(path):
    db=connect(path)
    db.isolation_level=None
    return db


def _now(now):
    return now or datetime.now().isoformat(timespec='seconds')


def _append_state(db,source_id,state,reason,evidence=None):
    """Append to the per-source chain; seq is the replayable recovery cursor."""
    row=db.execute('SELECT COALESCE(MAX(seq),0) AS s FROM disposal_state WHERE source_id=?',(source_id,)).fetchone()
    seq=row['s']+1
    db.execute('INSERT INTO disposal_state(source_id,seq,prev_seq,state,reason,evidence) VALUES (?,?,?,?,?,?)',
               (source_id,seq,seq-1 if seq>1 else None,state,reason,evidence))
    return seq


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def verify(db,source):
    """Actually retrieve the original bytes from their controlled location and
    check the digest; the stored digest alone never proves recoverability."""
    storage=source['storage']
    if storage=='db':
        body=source['body']
        if body is None:return {'ok':False,'location':'db','detail':'missing_db_body'}
        ok=_sha256(bytes(body))==source['hash']
        return {'ok':ok,'location':'db','detail':'ok' if ok else 'digest_mismatch'}
    if storage=='cold':
        obj=db.execute('SELECT * FROM archive_object WHERE digest=?',(source['hash'],)).fetchone()
        if not obj:return {'ok':False,'location':'cold','detail':'object_not_registered'}
        if obj['state']!='published':return {'ok':False,'location':'cold','detail':f"object_{obj['state']}"}
        path=Path(obj['path'])
        if not path.is_file():return {'ok':False,'location':'cold','detail':'object_missing'}
        ok=_sha256(path.read_bytes())==source['hash']
        return {'ok':ok,'location':'cold','detail':'ok' if ok else 'digest_mismatch'}
    if storage=='destroyed':
        return {'ok':False,'location':None,'detail':'destroyed'}
    return {'ok':False,'location':None,'detail':'unknown_storage'}


def request_repair(db,source,detail):
    """Corruption of a cold object opens a repair request once and blocks disposal."""
    cur=db.execute("INSERT OR IGNORE INTO repair_request(source_id,digest,status,detail) VALUES (?,?, 'open', ?)",
                   (source['id'],source['hash'],detail))
    if cur.rowcount:
        _append_state(db,source['id'],'repair_requested',detail,evidence=source['hash'])
        decide(db,'档案保全',f"source:{source['id']}",'不可恢复',detail,source['id'])
    return cur.rowcount>0


def recoverable_status(db,source,verdict):
    """The archive contract signs 可恢复 only when bytes were actually retrieved
    from a controlled location and the digest matched."""
    if verdict['ok']:return '可恢复'
    if source['storage']=='destroyed':return '已销毁'
    if db.execute('SELECT 1 FROM destruction_credential WHERE digest=?',(source['hash'],)).fetchone():
        return '已销毁'
    return '不可恢复'


def snapshot(db):
    tables={'objects':'archive_object','refs':'retention_ref','holds':'preservation_order',
            'requests':'disposal_request','credentials':'destruction_credential',
            'repairs':'repair_request','keys':'object_key'}
    out={name:[dict(r) for r in db.execute(f'SELECT * FROM {table}')] for name,table in tables.items()}
    out['states']=[dict(r) for r in db.execute('SELECT * FROM disposal_state ORDER BY source_id,seq')]
    return out


def replay(path,source_id,after_seq=0):
    """Replay the disposal chain of one source from a recovery cursor."""
    db=connect(path)
    try:
        return [dict(r) for r in db.execute(
            'SELECT * FROM disposal_state WHERE source_id=? AND seq>? ORDER BY seq',(source_id,after_seq))]
    finally:db.close()


def migrate(path,store_dir,source_id=None,fault_at=None):
    """Move original bytes to cold storage: write a temp object, verify its
    digest, atomically publish the addressable object, then switch the database
    reference only after the published object reads back verified. A crash at
    any point leaves either the in-db bytes or the cold object usable, and a
    re-run converges idempotently."""
    db=_open(path)
    store=Path(store_dir)
    (store/'tmp').mkdir(parents=True,exist_ok=True)
    (store/'objects').mkdir(parents=True,exist_ok=True)
    results=[]
    try:
        rows=db.execute('SELECT * FROM export_file WHERE storage<>\'destroyed\''+
                        (' AND id=?' if source_id is not None else '')+' ORDER BY id',
                        (source_id,) if source_id is not None else ()).fetchall()
        for row in rows:
            digest=row['hash'];obj_path=store/'objects'/digest
            if row['storage']=='cold':
                verdict=verify(db,row)
                if not verdict['ok']:request_repair(db,row,verdict['detail'])
                results.append({'source':row['id'],'digest':digest,'storage':'cold','verified':verdict['ok']})
                continue
            body=bytes(row['body'])
            if _sha256(body)!=digest:raise ValueError('source_digest_mismatch')
            tmp=store/'tmp'/f'{digest}.{os.getpid()}.tmp'
            with open(tmp,'wb') as handle:
                handle.write(body);handle.flush();os.fsync(handle.fileno())
            if _sha256(tmp.read_bytes())!=digest:raise IOError('temp_verify_failed')
            if fault_at=='temp':raise FaultInjected('temp')
            os.replace(tmp,obj_path)
            if _sha256(obj_path.read_bytes())!=digest:raise IOError('publish_verify_failed')
            if fault_at=='publish':raise FaultInjected('publish')
            with _txn(db):
                if _sha256(obj_path.read_bytes())!=digest:raise IOError('object_unreadable_before_switch')
                db.execute("INSERT OR IGNORE INTO archive_object(digest,path,size,state) VALUES (?,?,?,'published')",
                           (digest,str(obj_path),len(body)))
                db.execute("UPDATE export_file SET storage='cold',body=NULL WHERE id=? AND storage='db'",(row['id'],))
                _append_state(db,row['id'],'migrated_to_cold','cold_storage',evidence=str(obj_path))
            if fault_at=='switch':raise FaultInjected('switch')
            results.append({'source':row['id'],'digest':digest,'storage':'cold','object':str(obj_path)})
        return results
    finally:db.close()


def attach_ref(path,source_id,ref,retain_until):
    """Attach an independent retention period to one source; refs are never merged."""
    db=_open(path)
    try:
        with _txn(db):
            if not db.execute('SELECT 1 FROM export_file WHERE id=?',(source_id,)).fetchone():
                raise ValueError('unknown_source')
            old=db.execute('SELECT * FROM retention_ref WHERE source_id=? AND ref=?',(source_id,ref)).fetchone()
            if old:
                if old['retain_until']!=retain_until:raise ValueError('retention_conflict')
                return dict(old)
            datetime.fromisoformat(retain_until)
            db.execute('INSERT INTO retention_ref(source_id,ref,retain_until) VALUES (?,?,?)',
                       (source_id,ref,retain_until))
            _append_state(db,source_id,'retention_attached','retention_ref',evidence=f'{ref}@{retain_until}')
        return {'source':source_id,'ref':ref,'retain_until':retain_until}
    finally:db.close()


def register_key(path,digest,key_id,policy='erase'):
    """Register a decryption key and its disposal policy for a cold object."""
    db=_open(path)
    try:
        with _txn(db):
            db.execute("INSERT OR IGNORE INTO object_key(key_id,digest,policy,status) VALUES (?,?,?,'available')",
                       (key_id,digest,policy))
        return {'key_id':key_id,'digest':digest,'policy':policy,'status':'available'}
    finally:db.close()


def _credential(db,digest):
    return db.execute('SELECT * FROM destruction_credential WHERE digest=?',(digest,)).fetchone()


def submit_hold(path,order_id,source_id,reason='preservation_order'):
    """Accept a preservation order. If destruction already completed, the order
    cannot be executed: it is recorded as 无法执行 with the prior credential as
    evidence, and the file is never marked recovered."""
    db=_open(path)
    try:
        with _txn(db):
            old=db.execute('SELECT * FROM preservation_order WHERE id=?',(order_id,)).fetchone()
            if old:return dict(old)
            source=db.execute('SELECT * FROM export_file WHERE id=?',(source_id,)).fetchone()
            if not source:raise ValueError('unknown_source')
            cred=_credential(db,source['hash'])
            if cred:
                status='无法执行';evidence=cred['id']
                _append_state(db,source_id,'hold_failed','destruction_completed',evidence=evidence)
            else:
                status='active';evidence=None
                _append_state(db,source_id,'hold_activated',reason,evidence=order_id)
            db.execute('INSERT INTO preservation_order(id,source_id,status,reason,evidence) VALUES (?,?,?,?,?)',
                       (order_id,source_id,status,reason,evidence))
            decide(db,'档案保全',f'hold:{order_id}',status,reason,source_id)
        return {'order':order_id,'source':source_id,'status':status,'evidence':evidence}
    finally:db.close()


def release_hold(path,order_id,reason='hold_released'):
    db=_open(path)
    try:
        with _txn(db):
            row=db.execute('SELECT * FROM preservation_order WHERE id=?',(order_id,)).fetchone()
            if not row:raise ValueError('unknown_hold')
            if row['status']=='released':return dict(row)
            if row['status']!='active':raise ValueError('hold_not_active')
            db.execute("UPDATE preservation_order SET status='released' WHERE id=?",(order_id,))
            _append_state(db,row['source_id'],'hold_released',reason,evidence=order_id)
        return {'order':order_id,'source':row['source_id'],'status':'released'}
    finally:db.close()


def request_disposal(path,request_id,source_id,reason='retention_expired'):
    db=_open(path)
    try:
        with _txn(db):
            old=db.execute('SELECT * FROM disposal_request WHERE id=?',(request_id,)).fetchone()
            if old:return dict(old)
            if not db.execute('SELECT 1 FROM export_file WHERE id=?',(source_id,)).fetchone():
                raise ValueError('unknown_source')
            db.execute("INSERT INTO disposal_request(id,source_id,status,reason) VALUES (?,?,'pending',?)",
                       (request_id,source_id,reason))
            _append_state(db,source_id,'destroy_requested',reason,evidence=request_id)
        return {'request':request_id,'source':source_id,'status':'pending'}
    finally:db.close()


def _sharing(db,digest):
    return db.execute('SELECT * FROM export_file WHERE hash=? ORDER BY id',(digest,)).fetchall()


def evaluate(db,digest,now):
    """Blockers against the destruction contract, evaluated across every source
    sharing the physical object: open repairs, active holds, unexpired
    independent retention refs, and unresolved reviews."""
    rows=_sharing(db,digest)
    ids=[r['id'] for r in rows]
    marks=','.join('?'*len(ids))
    blockers=[]
    for row in rows:
        if row['storage']=='cold':
            verdict=verify(db,row)
            if not verdict['ok']:request_repair(db,row,verdict['detail'])
    if db.execute(f"SELECT 1 FROM repair_request WHERE status='open' AND source_id IN ({marks}) LIMIT 1",ids).fetchone():
        blockers.append('repair_open')
    if db.execute(f"SELECT 1 FROM preservation_order WHERE status='active' AND source_id IN ({marks}) LIMIT 1",ids).fetchone():
        blockers.append('preservation_hold_active')
    moment=datetime.fromisoformat(now)
    refs=db.execute(f'SELECT retain_until FROM retention_ref WHERE source_id IN ({marks})',ids).fetchall()
    if any(datetime.fromisoformat(ref['retain_until'])>moment for ref in refs):
        blockers.append('retention_not_expired')
    linked=db.execute(f"""SELECT 1 FROM peak_review pr
                          JOIN sample_export se ON se.id=json_extract(pr.body,'$.sample')
                          WHERE se.status='待核' AND se.source_id IN ({marks}) LIMIT 1""",ids).fetchone()
    own=db.execute(f"SELECT 1 FROM peak_review WHERE status='待核' AND source_id IN ({marks}) LIMIT 1",ids).fetchone()
    if linked or own:blockers.append('review_open')
    return blockers


def _complete(db,req,source,cred,now,rulings):
    """Close a request against an existing credential; never issue a second one."""
    db.execute("UPDATE disposal_request SET status='completed',reason=? WHERE id=?",
               (f"credential:{cred['id']}",req['id']))
    latest=db.execute('SELECT state FROM disposal_state WHERE source_id=? ORDER BY seq DESC LIMIT 1',
                      (req['source_id'],)).fetchone()
    if not latest or latest['state']!='destroy_completed':
        _append_state(db,req['source_id'],'destroy_completed','credential_reused',evidence=cred['id'])
    rulings.append({'request':req['id'],'source':req['source_id'],'status':'completed',
                    'reason':'credential_reused','credential':cred['id']})


def cleanup(path,store_dir=None,now=None,fault_at=None):
    """Background disposal. Phase one claims a request after evaluating the
    destruction contract; phase two re-evaluates inside a fresh commit boundary
    before disposing keys, deleting the physical object, and signing the single
    per-digest credential. A hold committed between the phases stops the delete."""
    now=_now(now)
    db=_open(path)
    rulings=[]
    try:
        pending=db.execute("SELECT * FROM disposal_request WHERE status IN ('pending','blocked','purging') ORDER BY id").fetchall()
        for req in pending:
            source=db.execute('SELECT * FROM export_file WHERE id=?',(req['source_id'],)).fetchone()
            if not source:
                with _txn(db):
                    db.execute("UPDATE disposal_request SET status='failed',reason='source_missing' WHERE id=?",(req['id'],))
                rulings.append({'request':req['id'],'source':req['source_id'],'status':'failed','reason':'source_missing'})
                continue
            digest=source['hash']
            if req['status']!='purging':
                with _txn(db):
                    cred=_credential(db,digest)
                    if cred:
                        _complete(db,req,source,cred,now,rulings);continue
                    blockers=evaluate(db,digest,now)
                    if blockers:
                        db.execute("UPDATE disposal_request SET status='blocked',reason=? WHERE id=?",
                                   (blockers[0],req['id']))
                        _append_state(db,req['source_id'],'destroy_blocked',blockers[0],
                                      evidence=json.dumps(blockers,ensure_ascii=False))
                        rulings.append({'request':req['id'],'source':req['source_id'],'status':'blocked',
                                        'reason':blockers[0],'blockers':blockers})
                        continue
                    db.execute("UPDATE disposal_request SET status='purging' WHERE id=?",(req['id'],))
                    _append_state(db,req['source_id'],'destroy_claimed','contract_satisfied',evidence=req['id'])
                if fault_at=='claim':raise FaultInjected('claim')
            with _txn(db):
                cred=_credential(db,digest)
                if cred:
                    _complete(db,req,source,cred,now,rulings);continue
                blockers=evaluate(db,digest,now)
                if blockers:
                    db.execute("UPDATE disposal_request SET status='blocked',reason=? WHERE id=?",
                               (blockers[0],req['id']))
                    _append_state(db,req['source_id'],'destroy_blocked',blockers[0],
                                  evidence=json.dumps(blockers,ensure_ascii=False))
                    rulings.append({'request':req['id'],'source':req['source_id'],'status':'blocked',
                                    'reason':blockers[0],'blockers':blockers})
                    continue
                keys=db.execute("SELECT * FROM object_key WHERE digest=? AND status='available'",(digest,)).fetchall()
                for key in keys:
                    db.execute("UPDATE object_key SET status='disposed',disposed=? WHERE key_id=?",(now,key['key_id']))
                obj=db.execute('SELECT * FROM archive_object WHERE digest=?',(digest,)).fetchone()
                obj_path=None
                if obj and obj['state']=='published':
                    obj_path=obj['path']
                    target=Path(obj_path)
                    if target.exists():target.unlink()
                    db.execute("UPDATE archive_object SET state='destroyed',disposed=? WHERE digest=?",(now,digest))
                db.execute("UPDATE export_file SET body=NULL,storage='destroyed' WHERE hash=?",(digest,))
                cred_id=f'credential:{digest}'
                db.execute('INSERT OR IGNORE INTO destruction_credential(id,digest,source_id,object_path,keys) VALUES (?,?,?,?,?)',
                           (cred_id,digest,req['source_id'],obj_path,
                            json.dumps([{'key_id':k['key_id'],'policy':k['policy']} for k in keys],ensure_ascii=False)))
                cred=_credential(db,digest)
                for shared in _sharing(db,digest):
                    _append_state(db,shared['id'],'destroy_completed','contract_satisfied',evidence=cred['id'])
                db.execute("UPDATE disposal_request SET status='completed',reason=? WHERE id=?",
                           (f"credential:{cred['id']}",req['id']))
                decide(db,'销毁处置',f"source:{req['source_id']}",'完成',f"credential:{cred['id']}",req['source_id'])
                rulings.append({'request':req['id'],'source':req['source_id'],'status':'completed',
                                'reason':'contract_satisfied','credential':cred['id'],
                                'keys':[k['key_id'] for k in keys]})
            if fault_at=='delete':raise FaultInjected('delete')
        return rulings
    finally:db.close()


def assess(path,source_id=None,now=None):
    """Main result per original file: the archive contract (recoverability) and
    the destruction contract (disposability) as independent rulings."""
    now=_now(now)
    db=_open(path)
    out=[]
    try:
        with _txn(db):
            rows=db.execute('SELECT * FROM export_file'+
                            (' WHERE id=?' if source_id is not None else '')+' ORDER BY id',
                            (source_id,) if source_id is not None else ()).fetchall()
            for row in rows:
                verdict=verify(db,row)
                if row['storage']=='cold' and not verdict['ok']:request_repair(db,row,verdict['detail'])
                recoverable=recoverable_status(db,row,verdict)
                cred=_credential(db,row['hash'])
                if cred:
                    disposal='已完成';blockers=[];disposal_reason=f"credential:{cred['id']}"
                else:
                    blockers=evaluate(db,row['hash'],now)
                    disposal='不可销毁' if blockers else '可销毁'
                    disposal_reason=blockers[0] if blockers else 'all_refs_expired'
                decide(db,'档案保全',f"source:{row['id']}",recoverable,verdict['detail'],row['id'])
                decide(db,'销毁处置',f"source:{row['id']}",disposal,disposal_reason,row['id'])
                out.append({'source':row['id'],'hash':row['hash'],'storage':row['storage'],
                            'recoverable':recoverable,'disposal':disposal,'blockers':blockers,
                            'verified':verdict['ok']})
        return out
    finally:db.close()
