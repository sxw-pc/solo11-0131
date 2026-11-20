import json
import pytest
from spectracapture import archive,integration,operations,report
from spectracapture.ingest import capture
from spectracapture.store import connect

def write(root,name,obj):
    p=root/name;p.write_text(json.dumps(obj),encoding='utf-8');return p
def read_report(db,out):
    return json.loads(report.build(db,out).read_text(encoding='utf-8'))
def sample_payload():
    return {'version':'v1','rows':[{'id':'S1','injection':'I1','calibration':'CAL1','window':[4.5,5.5],
            'peaks':[{'time':5.0,'area':20},{'time':5.1,'area':22}]}]}

@pytest.mark.parametrize('fault',['temp','publish','switch'])
def test_migrate_crash_recovers_and_report_verifies(tmp_path,fault):
    db=tmp_path/'ledger.db';store=tmp_path/'cold'
    raw=json.dumps({'version':'v1','rows':[{'id':'X1','note':'alpha'}]}).encode()
    file=write(tmp_path,'export.json',json.loads(raw))
    source,_=capture(db,'desktop_raw_export',file)
    with pytest.raises(archive.FaultInjected):
        archive.migrate(db,store,fault_at=fault)
    data=read_report(db,tmp_path/'r1.json')
    assert data['sources'][0]['verified'] is True
    archive.migrate(db,store)
    data=read_report(db,tmp_path/'r2.json')
    item=data['sources'][0]
    assert item['verified'] is True and item['storage']=='cold' and item['recoverable']=='可恢复'
    row=connect(db).execute('SELECT * FROM export_file WHERE id=?',(source,)).fetchone()
    assert row['body'] is None
    assert (store/'objects'/row['hash']).read_bytes()==raw
    assert connect(db).execute('SELECT count(*) FROM export_record WHERE source_id=?',(source,)).fetchone()[0]==1
    assert [s['state'] for s in data['archive']['states']]==['captured','migrated_to_cold']

def test_shared_refs_open_review_and_dual_contracts(tmp_path):
    db=tmp_path/'ledger.db';store=tmp_path/'cold'
    source,_=capture(db,'desktop_sample_export',write(tmp_path,'sample.json',sample_payload()))
    operations.second(db,write(tmp_path,'sample.json',sample_payload()))
    archive.attach_ref(db,source,'ref-a','2026-01-01T00:00:00')
    archive.attach_ref(db,source,'ref-b','2027-01-01T00:00:00')
    archive.migrate(db,store)
    weak={'version':'v1','rows':[{'id':'V1','sample':'S1','chosen_time':5.0,'analyst_a':'A','analyst_b':'A'}]}
    assert integration.review(db,write(tmp_path,'weak.json',weak))[0]['reason']=='dual_review_missing'
    archive.request_disposal(db,'req-1',source)
    rulings=archive.cleanup(db,store,now='2026-06-01T00:00:00')
    assert rulings[0]['status']=='blocked'
    assert set(rulings[0]['blockers'])=={'retention_not_expired','review_open'}
    digest=connect(db).execute('SELECT hash FROM export_file WHERE id=?',(source,)).fetchone()[0]
    assert (store/'objects'/digest).exists()
    verdict=archive.assess(db,source_id=source,now='2026-06-01T00:00:00')[0]
    assert verdict['recoverable']=='可恢复' and verdict['disposal']=='不可销毁'
    verdict=archive.assess(db,source_id=source,now='2027-06-01T00:00:00')[0]
    assert verdict['recoverable']=='可恢复' and verdict['disposal']=='不可销毁'
    assert verdict['blockers']==['review_open']
    approved={'version':'v1','rows':[{'id':'V2','sample':'S1','chosen_time':5.0,'analyst_a':'A','analyst_b':'B'}]}
    assert integration.review(db,write(tmp_path,'approved.json',approved))[0]['status']=='已裁定'
    rulings=archive.cleanup(db,store,now='2028-01-01T00:00:00')
    assert rulings[0]['status']=='completed'
    assert not (store/'objects'/digest).exists()
    archive.request_disposal(db,'req-2',source)
    rulings=archive.cleanup(db,store,now='2028-01-02T00:00:00')
    assert rulings[0]['status']=='completed' and rulings[0]['reason']=='credential_reused'
    conn=connect(db)
    assert conn.execute('SELECT count(*) FROM destruction_credential').fetchone()[0]==1
    assert conn.execute("SELECT storage FROM export_file WHERE id=?",(source,)).fetchone()[0]=='destroyed'

def test_hold_interleaved_with_cleanup_and_late_hold_fails(tmp_path):
    db=tmp_path/'ledger.db';store=tmp_path/'cold'
    source,_=capture(db,'desktop_raw_export',write(tmp_path,'export.json',
                      {'version':'v1','rows':[{'id':'X1'}]}))
    archive.attach_ref(db,source,'ref-a','2020-01-01T00:00:00')
    archive.migrate(db,store)
    digest=connect(db).execute('SELECT hash FROM export_file WHERE id=?',(source,)).fetchone()[0]
    archive.register_key(db,digest,'key-1','erase')
    archive.request_disposal(db,'req-1',source)
    with pytest.raises(archive.FaultInjected):
        archive.cleanup(db,store,now='2026-06-01T00:00:00',fault_at='claim')
    hold=archive.submit_hold(db,'HOLD-1',source)
    assert hold['status']=='active'
    rulings=archive.cleanup(db,store,now='2026-06-01T00:00:00')
    assert rulings[0]['status']=='blocked' and rulings[0]['reason']=='preservation_hold_active'
    assert (store/'objects'/digest).exists()
    assert read_report(db,tmp_path/'r1.json')['sources'][0]['verified'] is True
    archive.release_hold(db,'HOLD-1')
    rulings=archive.cleanup(db,store,now='2026-06-02T00:00:00')
    assert rulings[0]['status']=='completed' and rulings[0]['keys']==['key-1']
    assert not (store/'objects'/digest).exists()
    assert archive.cleanup(db,store,now='2026-06-03T00:00:00')==[]
    conn=connect(db)
    assert conn.execute('SELECT count(*) FROM destruction_credential').fetchone()[0]==1
    assert conn.execute("SELECT status FROM object_key WHERE key_id='key-1'").fetchone()[0]=='disposed'
    late=archive.submit_hold(db,'HOLD-2',source)
    assert late['status']=='无法执行' and late['evidence']==f'credential:{digest}'
    verdict=archive.assess(db,source_id=source,now='2026-06-04T00:00:00')[0]
    assert verdict['recoverable']=='已销毁' and verdict['disposal']=='已完成'
    chain=archive.replay(db,source)
    assert [s['seq'] for s in chain]==list(range(1,len(chain)+1))
    assert all(chain[i]['prev_seq']==chain[i-1]['seq'] for i in range(1,len(chain)))
    assert chain[0]['state']=='captured'
    assert chain[-2]['state']=='destroy_completed'
    assert chain[-1]['state']=='hold_failed' and chain[-1]['evidence']==f'credential:{digest}'
    assert archive.replay(db,source,after_seq=chain[-2]['seq'])==[chain[-1]]

def test_tampered_object_blocks_destroy_and_report_opens_repair(tmp_path):
    db=tmp_path/'ledger.db';store=tmp_path/'cold'
    source,_=capture(db,'desktop_raw_export',write(tmp_path,'export.json',
                      {'version':'v1','rows':[{'id':'X1'}]}))
    archive.migrate(db,store)
    digest=connect(db).execute('SELECT hash FROM export_file WHERE id=?',(source,)).fetchone()[0]
    (store/'objects'/digest).write_bytes(b'tampered-bytes')
    data=read_report(db,tmp_path/'r1.json')
    item=data['sources'][0]
    assert item['verified'] is False and item['recoverable']=='不可恢复'
    assert data['archive']['repairs'][0]['status']=='open'
    assert data['archive']['repairs'][0]['detail']=='digest_mismatch'
    archive.request_disposal(db,'req-1',source)
    rulings=archive.cleanup(db,store,now='2026-06-01T00:00:00')
    assert rulings[0]['status']=='blocked' and rulings[0]['reason']=='repair_open'
    assert (store/'objects'/digest).read_bytes()==b'tampered-bytes'
    verdict=archive.assess(db,source_id=source,now='2026-06-01T00:00:00')[0]
    assert verdict['recoverable']=='不可恢复' and verdict['disposal']=='不可销毁'
    data=read_report(db,tmp_path/'r2.json')
    assert len(data['archive']['repairs'])==1

def test_object_deduped_across_sources_keeps_independent_refs(tmp_path):
    db=tmp_path/'ledger.db';store=tmp_path/'cold'
    payload={'version':'v1','rows':[{'id':'X1'}]}
    file=write(tmp_path,'export.json',payload)
    first,_=capture(db,'kind_a',file)
    second,_=capture(db,'kind_b',file)
    assert first!=second
    archive.migrate(db,store)
    conn=connect(db)
    assert conn.execute('SELECT count(*) FROM archive_object').fetchone()[0]==1
    digest=conn.execute('SELECT hash FROM export_file WHERE id=?',(first,)).fetchone()[0]
    archive.attach_ref(db,first,'ref-a','2020-01-01T00:00:00')
    archive.attach_ref(db,second,'ref-b','2099-01-01T00:00:00')
    archive.request_disposal(db,'req-a',first)
    rulings=archive.cleanup(db,store,now='2026-06-01T00:00:00')
    assert rulings[0]['status']=='blocked' and rulings[0]['reason']=='retention_not_expired'
    assert (store/'objects'/digest).exists()
    data=read_report(db,tmp_path/'r.json')
    assert all(x['verified'] for x in data['sources'])
    assert len(data['archive']['refs'])==2
    rulings=archive.cleanup(db,store,now='2100-01-01T00:00:00')
    assert rulings[0]['status']=='completed'
    assert not (store/'objects'/digest).exists()
    assert conn.execute('SELECT count(*) FROM destruction_credential').fetchone()[0]==1
