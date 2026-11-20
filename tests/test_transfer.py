import json
import pytest
from spectracapture import operations,qc,transfer,report
from spectracapture.store import connect

OLD,NEW,METH='OLD-001','NEW-002','M1'
AT='2026-01-01T12:00:00'

def write(root,name,obj):
    p=root/name;p.write_text(json.dumps(obj),encoding='utf-8');return p

def plan_single():
    return dict(id='PLAN-SINGLE',old_instrument=OLD,new_instrument=NEW,method=METH,interval=[1,3],
                scheme='single',residual_budget=1,registered_at='2026-01-01T08:00:00')
def plan_segmented():
    return dict(id='PLAN-SEG',old_instrument=OLD,new_instrument=NEW,method=METH,interval=[1,3],
                scheme='segmented',breakpoint=2,residual_budget=1,registered_at='2026-01-01T08:00:00')
def plans():
    return {'version':'v1','rows':[plan_single(),plan_segmented()]}

def cals():
    def cal(cid,inst,areas):
        return dict(id=cid,instrument=inst,method=METH,
                    points=[{'concentration':i+1,'area':a} for i,a in enumerate(areas)],
                    blank_area=0,max_blank=2,max_residual=1,
                    at='2026-01-01T08:30:00',valid_until='2026-01-02T00:00:00')
    return {'version':'v1','rows':[cal('CAL-OLD',OLD,[10,20,30]),cal('CAL-NEW',NEW,[12,24,36])]}

def qcs():
    return {'version':'v1','rows':[dict(id='QC-NEW',calibration='CAL-NEW',instrument=NEW,method=METH,
        blank_area=0,max_blank=2,controls=[{'expected':2,'area':24,'max_relative_error':0.1}])]}

def point(pid,conc,old_area,new_area,role,minute):
    return dict(id=pid,vial=f'VIAL-{pid}',concentration=conc,method=METH,role=role,
        old={'instrument':OLD,'area':old_area,'at':f'2026-01-01T09:{minute:02d}:00','digest':f'sha256:old{pid}'},
        new={'instrument':NEW,'area':new_area,'at':f'2026-01-01T09:{minute+1:02d}:00','digest':f'sha256:new{pid}'})

def bridges():
    return {'version':'v1','rows':[point('BP1',1,10,12,'fit',0),point('BP2',2,20,26,'fit',10),
        point('BP3',3,30,36,'fit',20),point('BP4',1.5,15,19,'verify',30),point('BP5',2.5,25,31,'verify',40)]}

def cert_map(db):
    return {r['id']:(r['status'],r['reason']) for r in connect(db).execute('SELECT * FROM transfer_certificate')}

def setup(root):
    db=root/'db'
    transfer.register(db,write(root,'plans.json',plans()))
    operations.first(db,write(root,'cals.json',cals()))
    qc.assess(db,write(root,'qc.json',qcs()))
    transfer.bridge(db,write(root,'bridge.json',bridges()))
    return db

def test_single_segment_rejected_but_segmented_approves(tmp_path):
    db=setup(tmp_path)
    certs=cert_map(db)
    # endpoint fit would predict 24 at the middle, observed 26, budget 1: single scheme fails
    assert certs['PLAN-SINGLE:1:3']==('拒绝','transfer_residual_exceeded')
    # pre-registered breakpoint 2 with independent verification at 1.5 (19) and 2.5 (31)
    assert certs['PLAN-SEG:1:2']==('获准','transfer_certified')
    assert certs['PLAN-SEG:2:3']==('获准','transfer_certified')
    ok=transfer.check(db,OLD,NEW,METH,1,2,AT)
    assert ok['status']=='获准' and ok['plan']=='PLAN-SEG' and ok['certificates']==['PLAN-SEG:1:2']
    assert transfer.check(db,OLD,NEW,METH,2,3,AT)['status']=='获准'
    assert transfer.check(db,OLD,NEW,METH,1,3,AT)['status']=='获准'
    # no extrapolation beyond the certified interval
    assert transfer.check(db,OLD,NEW,METH,0.5,2,AT)['reason']=='outside_certified_range'
    assert transfer.check(db,OLD,NEW,METH,2,3.5,AT)['reason']=='outside_certified_range'
    # certificate binds serial numbers, method version, bounds, validity window and sources
    body=json.loads(connect(db).execute("SELECT body FROM transfer_certificate WHERE id='PLAN-SEG:1:2'").fetchone()[0])
    assert body['old_instrument']==OLD and body['new_instrument']==NEW and body['method']==METH
    assert body['segment']==[1,2] and body['valid_until']=='2026-01-02T00:00:00'
    assert body['transfer']=={'slope':1.4,'intercept':-2.0}
    assert body['fit_points']==['BP1','BP2'] and body['verify_points']==['BP4']
    assert body['sources']['bridge'] and body['sources']['calibration'] and body['sources']['qc']

def test_late_pair_revokes_only_dependent_interval(tmp_path):
    db=setup(tmp_path)
    # source re-inspection finds the 2.5 verification point really read 34, not 31
    late={'version':'v1','rows':[point('BP6',2.5,25,34,'verify',50)]}
    transfer.bridge(db,write(tmp_path,'late.json',late))
    certs=cert_map(db)
    assert certs['PLAN-SEG:2:3']==('撤销','verification_residual_exceeded')
    assert certs['PLAN-SEG:1:2']==('获准','transfer_certified')
    assert transfer.check(db,OLD,NEW,METH,2,3,AT)['status']=='拒绝'
    assert transfer.check(db,OLD,NEW,METH,1,2,AT)['status']=='获准'
    # the old certificate cannot vouch for the high segment any more
    again=transfer.evaluate(db,'PLAN-SEG')
    assert [x['status'] for x in again]==['获准','撤销']
    # raw calibration and bridging exports are never rewritten
    conn=connect(db)
    bp5=conn.execute("SELECT * FROM bridge_point WHERE id='BP5'").fetchone()
    assert bp5['status']=='在控' and json.loads(bp5['body'])['new']['area']==31
    assert conn.execute("SELECT status FROM calibration_export WHERE id='CAL-OLD'").fetchone()[0]=='有效'

def test_other_instrument_or_method_cannot_reuse_certificate(tmp_path):
    db=setup(tmp_path)
    assert transfer.check(db,OLD,'NEW-999',METH,1,2,AT)['reason']=='plan_not_registered'
    assert transfer.check(db,'OLD-999',NEW,METH,1,2,AT)['reason']=='plan_not_registered'
    assert transfer.check(db,OLD,NEW,'M2',1,2,AT)['reason']=='plan_not_registered'
    assert transfer.check(db,OLD,NEW,METH,1,2,'2026-01-03T00:00:00')['reason']=='certificate_expired'

def test_qc_blank_out_of_control_revokes_both_schemes(tmp_path):
    db=setup(tmp_path)
    bad={'version':'v1','rows':[dict(id='QC-NEW2',calibration='CAL-NEW',instrument=NEW,method=METH,
        blank_area=5,max_blank=2,controls=[{'expected':2,'area':24,'max_relative_error':0.1}])]}
    assert qc.assess(db,write(tmp_path,'qc2.json',bad))[0]['reason']=='qc_blank_contamination'
    certs=cert_map(db)
    assert certs['PLAN-SEG:1:2']==('撤销','qc_out_of_control')
    assert certs['PLAN-SEG:2:3']==('撤销','qc_out_of_control')
    assert certs['PLAN-SINGLE:1:3']==('拒绝','qc_out_of_control')
    assert transfer.check(db,OLD,NEW,METH,1,2,AT)['status']=='拒绝'
    assert transfer.check(db,OLD,NEW,METH,2,3,AT)['status']=='拒绝'

def test_plan_registration_is_immutable_and_must_precede_sampling(tmp_path):
    db=setup(tmp_path)
    # a registered plan cannot be altered after the fact
    changed=plans();changed['rows'][1]['breakpoint']=2.5
    with pytest.raises(ValueError):
        transfer.register(db,write(tmp_path,'changed.json',changed))
    # identical replay is idempotent
    assert transfer.register(db,write(tmp_path,'same.json',plans()))[0]['reason']=='plan_registered'
    # a scheme registered only after sampling started cannot certify anything
    late={'version':'v1','rows':[dict(plan_single(),id='PLAN-LATE',registered_at='2026-01-01T10:00:00')]}
    transfer.register(db,write(tmp_path,'lateplan.json',late))
    result=transfer.evaluate(db,'PLAN-LATE')
    assert result[0]['reason']=='plan_registered_after_sampling'
    assert transfer.check(db,OLD,NEW,METH,1,3,AT)['plan']=='PLAN-SEG'

def test_out_of_control_bridge_point_cannot_be_dropped(tmp_path):
    db=setup(tmp_path)
    broken=dict(id='BP-BAD',vial='VIAL-X',concentration=1.8,method=METH,role='fit',
        old={'instrument':OLD,'area':18,'at':'2026-01-01T09:55:00','digest':'sha256:oldx'},
        new={'instrument':NEW,'at':'2026-01-01T09:56:00','digest':'sha256:newx'})
    result=transfer.bridge(db,write(tmp_path,'bad.json',{'version':'v1','rows':[broken]}))
    assert result[0]['status']=='失控' and result[0]['reason']=='point_incomplete'
    certs=cert_map(db)
    assert certs['PLAN-SEG:1:2']==('撤销','bridge_point_out_of_control')
    assert certs['PLAN-SEG:2:3']==('获准','transfer_certified')
    # the out-of-control point stays in the ledger; re-evaluation still fails
    again=transfer.evaluate(db,'PLAN-SEG')
    assert [x['reason'] for x in again]==['bridge_point_out_of_control','transfer_certified']

def test_bridge_point_identity_conflict_blocks_rewrite(tmp_path):
    db=setup(tmp_path)
    clash=bridges();clash['rows'][4]['new']['area']=34
    with pytest.raises(ValueError):
        transfer.bridge(db,write(tmp_path,'clash.json',clash))
    assert json.loads(connect(db).execute("SELECT body FROM bridge_point WHERE id='BP5'").fetchone()[0])['new']['area']==31

def test_segmented_scheme_requires_continuity_at_breakpoint(tmp_path):
    db=tmp_path/'db'
    transfer.register(db,write(tmp_path,'plans.json',{'version':'v1','rows':[plan_segmented()]}))
    operations.first(db,write(tmp_path,'cals.json',cals()))
    qc.assess(db,write(tmp_path,'qc.json',qcs()))
    rows=[point('BP1',1,10,12,'fit',0),point('BP2',1.5,15,20,'fit',10),point('BP3',2,20,26,'fit',20),
          point('BP4',2.5,25,28.5,'fit',30),point('BP5',3,30,36,'fit',40),
          point('BP6',1.2,12,14.5,'verify',50),point('BP7',2.8,28,33,'verify',52)]
    transfer.bridge(db,write(tmp_path,'bridge.json',{'version':'v1','rows':rows}))
    certs=cert_map(db)
    assert certs['PLAN-SEG:1:2']==('拒绝','breakpoint_discontinuity')
    assert certs['PLAN-SEG:2:3']==('拒绝','breakpoint_discontinuity')

def test_transfer_function_must_be_monotonic(tmp_path):
    db=tmp_path/'db'
    transfer.register(db,write(tmp_path,'plans.json',{'version':'v1','rows':[plan_single()]}))
    operations.first(db,write(tmp_path,'cals.json',cals()))
    qc.assess(db,write(tmp_path,'qc.json',qcs()))
    rows=[point('BP1',1,10,30,'fit',0),point('BP2',2,20,20,'fit',10),
          point('BP3',3,30,10,'fit',20),point('BP4',1.5,15,25,'verify',30)]
    transfer.bridge(db,write(tmp_path,'bridge.json',{'version':'v1','rows':rows}))
    assert cert_map(db)['PLAN-SINGLE:1:3']==('拒绝','non_monotonic_transfer')

def test_single_scheme_needs_interior_bridge_and_verification_points(tmp_path):
    # only the two endpoints fitted: the middle cannot be ignored
    db=tmp_path/'db1'
    transfer.register(db,write(tmp_path,'p1.json',{'version':'v1','rows':[plan_single()]}))
    operations.first(db,write(tmp_path,'c1.json',cals()))
    qc.assess(db,write(tmp_path,'q1.json',qcs()))
    rows=[point('BP1',1,10,12,'fit',0),point('BP3',3,30,36,'fit',20),point('BP4',1.5,15,19,'verify',30)]
    transfer.bridge(db,write(tmp_path,'b1.json',{'version':'v1','rows':rows}))
    assert cert_map(db)['PLAN-SINGLE:1:3']==('拒绝','insufficient_bridge_points')
    # enough fit points but no independent verification point
    db=tmp_path/'db2'
    transfer.register(db,write(tmp_path,'p2.json',{'version':'v1','rows':[plan_single()]}))
    operations.first(db,write(tmp_path,'c2.json',cals()))
    qc.assess(db,write(tmp_path,'q2.json',qcs()))
    rows=[point('BP1',1,10,12,'fit',0),point('BP2',2,20,26,'fit',10),point('BP3',3,30,36,'fit',20)]
    transfer.bridge(db,write(tmp_path,'b2.json',{'version':'v1','rows':rows}))
    assert cert_map(db)['PLAN-SINGLE:1:3']==('拒绝','insufficient_verification_points')
    # fit points must support the declared bounds, not a narrower interior
    db=tmp_path/'db3'
    transfer.register(db,write(tmp_path,'p3.json',{'version':'v1','rows':[plan_single()]}))
    operations.first(db,write(tmp_path,'c3.json',cals()))
    qc.assess(db,write(tmp_path,'q3.json',qcs()))
    rows=[point('BP1',1,10,12,'fit',0),point('BP2',1.5,15,19,'fit',10),
          point('BP3',2,20,26,'fit',20),point('BP4',1.2,12,14.5,'verify',30)]
    transfer.bridge(db,write(tmp_path,'b3.json',{'version':'v1','rows':rows}))
    assert cert_map(db)['PLAN-SINGLE:1:3']==('拒绝','interval_endpoints_unsupported')

def test_calibration_and_qc_gates_lift_in_sequence(tmp_path):
    db=tmp_path/'db'
    transfer.register(db,write(tmp_path,'plans.json',{'version':'v1','rows':[plan_single()]}))
    transfer.bridge(db,write(tmp_path,'bridge.json',bridges()))
    assert cert_map(db)['PLAN-SINGLE:1:3']==('拒绝','calibration_invalid')
    operations.first(db,write(tmp_path,'cals.json',cals()))
    assert cert_map(db)['PLAN-SINGLE:1:3']==('拒绝','qc_missing')
    qc.assess(db,write(tmp_path,'qc.json',qcs()))
    assert cert_map(db)['PLAN-SINGLE:1:3']==('拒绝','transfer_residual_exceeded')

def test_old_curve_expiry_blocks_transfer(tmp_path):
    expired=cals();expired['rows'][0]['valid_until']='2026-01-01T09:30:00'
    db=tmp_path/'db'
    transfer.register(db,write(tmp_path,'plans.json',{'version':'v1','rows':[plan_single()]}))
    operations.first(db,write(tmp_path,'cals.json',expired))
    qc.assess(db,write(tmp_path,'qc.json',qcs()))
    transfer.bridge(db,write(tmp_path,'bridge.json',bridges()))
    assert cert_map(db)['PLAN-SINGLE:1:3']==('拒绝','calibration_expired')

def test_interval_beyond_old_curve_range(tmp_path):
    wide=dict(plan_single(),interval=[0.5,3])
    db=tmp_path/'db'
    transfer.register(db,write(tmp_path,'plans.json',{'version':'v1','rows':[wide]}))
    operations.first(db,write(tmp_path,'cals.json',cals()))
    qc.assess(db,write(tmp_path,'qc.json',qcs()))
    transfer.bridge(db,write(tmp_path,'bridge.json',bridges()))
    assert cert_map(db)['PLAN-SINGLE:0.5:3']==('拒绝','interval_beyond_calibration')

def test_plan_registration_validation(tmp_path):
    db=tmp_path/'db'
    rows=[dict(plan_single(),id='P1',scheme='weird'),
          dict(plan_single(),id='P2',scheme='segmented'),
          dict(plan_single(),id='P3',interval=[3,1]),
          dict(plan_single(),id='P4',residual_budget=0),
          dict(plan_single(),id='P5',scheme='segmented',breakpoint=5)]
    result=transfer.register(db,write(tmp_path,'p.json',{'version':'v1','rows':rows}))
    assert [x['reason'] for x in result]==['invalid_scheme','plan_incomplete','invalid_interval',
                                           'invalid_budget','breakpoint_outside_interval']
    assert connect(db).execute("SELECT count(*) FROM transfer_plan WHERE status='已登记'").fetchone()[0]==0

def test_replay_is_idempotent(tmp_path):
    db=setup(tmp_path)
    first_run=transfer.evaluate(db)
    assert first_run==transfer.evaluate(db)
    assert connect(db).execute('SELECT count(*) FROM transfer_certificate').fetchone()[0]==3
    again=transfer.bridge(db,write(tmp_path,'bridge-again.json',bridges()))
    assert [x['status'] for x in again]==['在控']*5
    assert connect(db).execute('SELECT count(*) FROM transfer_certificate').fetchone()[0]==3
    assert cert_map(db)['PLAN-SEG:1:2']==('获准','transfer_certified')

def test_report_lists_interval_certificates_and_reasons(tmp_path):
    db=setup(tmp_path)
    data=json.loads(report.build(db,tmp_path/'report.json').read_text(encoding='utf-8'))
    certs={x['id']:x for x in data['transfer']['certificates']}
    assert certs['PLAN-SEG:1:2']['status']=='获准'
    assert certs['PLAN-SEG:1:2']['body']['segment']==[1,2]
    assert certs['PLAN-SINGLE:1:3']['reason']=='transfer_residual_exceeded'
    assert {p['id'] for p in data['transfer']['plans']}=={'PLAN-SINGLE','PLAN-SEG'}
    assert any(d['subject']=='PLAN-SEG:1:2' and d['status']=='获准' for d in data['decisions'])
    assert any(d['subject']=='PLAN-SINGLE:1:3' and d['reason']=='transfer_residual_exceeded' for d in data['decisions'])
    assert data['entities']['transfer_certificate'] and data['entities']['bridge_point']

def test_cli_transfer_check(tmp_path,monkeypatch,capsys):
    from spectracapture.__main__ import main
    db=setup(tmp_path)
    monkeypatch.setattr('sys.argv',['prog','transfer-check','--db',str(db),'--old',OLD,'--new',NEW,
                                    '--method',METH,'--low','1','--high','2','--at',AT])
    main()
    out=json.loads(capsys.readouterr().out)
    assert out['status']=='获准' and out['plan']=='PLAN-SEG'
    monkeypatch.setattr('sys.argv',['prog','transfer-evaluate','--db',str(db),'--plan','PLAN-SINGLE'])
    main()
    out=json.loads(capsys.readouterr().out)
    assert out[0]['reason']=='transfer_residual_exceeded'
