import json
from spectracapture import operations,report,qc,integration,batch,custody
from spectracapture.store import connect

def write(root,name,obj):
    p=root/name;p.write_text(json.dumps(obj),encoding='utf-8');return p
def cal(id='CAL1',blank=0,areas=None):
    areas=areas or [10,20,30]
    return dict(id=id,points=[{'concentration':i+1,'area':a} for i,a in enumerate(areas)],
                blank_area=blank,max_blank=2,max_residual=1,
                at='2026-01-01T10:00:00',valid_until='2026-01-02T00:00:00')
def sample(id='S1',injection='I1',peaks=None):
    return dict(id=id,injection=injection,calibration='CAL1',window=[4.5,5.5],
                peaks=peaks if peaks is not None else [{'time':5.0,'area':20}])

def test_calibration_sample_release_full_chain(tmp_path):
    result=operations.demo(tmp_path)
    assert [x[0]['status'] for x in result]==['有效','已归属','可放行','已封账']
    data=json.loads(report.build(tmp_path/'ledger.db',tmp_path/'report.json').read_text(encoding='utf-8'))
    assert len(data['sources'])==6 and all(x['verified'] for x in data['sources'])

def test_calibration_blank_residual_and_concentration_order(tmp_path):
    db=tmp_path/'db';rows=[cal('C1'),cal('C2',blank=5),cal('C3',areas=[10,30,30]),
         dict(cal('C4'),points=[{'concentration':2,'area':20},{'concentration':1,'area':10},{'concentration':3,'area':30}])]
    result=operations.first(db,write(tmp_path,'cal.json',{'version':'v1','rows':rows}))
    assert [x['reason'] for x in result]==['curve_fit','blank_contamination','residual_exceeded','concentration_order']

def test_peak_window_ambiguity_and_injection_identity(tmp_path):
    db=tmp_path/'db';rows=[sample(),sample('S2',injection='I1'),
        sample('S3',injection='I3',peaks=[{'time':8,'area':20}]),
        sample('S4',injection='I4',peaks=[{'time':5,'area':20},{'time':5.1,'area':22}])]
    result=operations.second(db,write(tmp_path,'sample.json',{'version':'v1','rows':rows}))
    assert [x['reason'] for x in result]==['unique_peak','injection_conflict','peak_outside_window','ambiguous_peaks']

def test_range_limit_and_expiry_change_release(tmp_path):
    db=tmp_path/'db';operations.first(db,write(tmp_path,'cal.json',{'version':'v1','rows':[cal()]}))
    operations.second(db,write(tmp_path,'sample.json',{'version':'v1','rows':[
        sample('S1',peaks=[{'time':5,'area':40}]),sample('S2',injection='I2',peaks=[{'time':5,'area':10}]),
        sample('S3',injection='I3',peaks=[{'time':5,'area':20}])]}))
    rows=[{'id':'R1','sample':'S1','calibration':'CAL1','quant_limit':1,'at':'2026-01-01T12:00:00'},
          {'id':'R2','sample':'S2','calibration':'CAL1','quant_limit':2,'at':'2026-01-01T12:00:00'},
          {'id':'R3','sample':'S3','calibration':'CAL1','quant_limit':1,'at':'2026-01-03T12:00:00'}]
    result=operations.third(db,write(tmp_path,'result.json',{'version':'v1','rows':rows}))
    assert [x['reason'] for x in result]==['outside_calibration_range','below_quant_limit','calibration_expired']

def test_invalid_calibration_blocks_valid_sample_peak(tmp_path):
    db=tmp_path/'db';operations.first(db,write(tmp_path,'cal.json',{'version':'v1','rows':[cal(blank=5)]}))
    operations.second(db,write(tmp_path,'sample.json',{'version':'v1','rows':[sample()]}))
    result=operations.third(db,write(tmp_path,'result.json',{'version':'v1','rows':[
        {'id':'R1','sample':'S1','calibration':'CAL1','quant_limit':1,'at':'2026-01-01T12:00:00'}]}))
    assert result[0]['reason']=='calibration_invalid'
    assert connect(db).execute("SELECT status FROM released_result WHERE id='result:R1'").fetchone()[0]=='拒放'

def test_qc_failure_blocks_otherwise_traceable_concentration(tmp_path):
    db=tmp_path/'db';operations.first(db,write(tmp_path,'cal.json',{'version':'v1','rows':[cal()]}))
    operations.second(db,write(tmp_path,'sample.json',{'version':'v1','rows':[sample()]}))
    controls={'version':'v1','rows':[{'id':'QC1','calibration':'CAL1','blank_area':0,'max_blank':2,
        'controls':[{'expected':2,'area':30,'max_relative_error':0.1}]}]}
    assert qc.assess(db,write(tmp_path,'qc.json',controls))[0]['reason']=='control_recovery_failed'
    result=operations.third(db,write(tmp_path,'result.json',{'version':'v1','rows':[
        {'id':'R1','sample':'S1','calibration':'CAL1','quant_limit':1,'at':'2026-01-01T12:00:00'}]}))
    assert result[0]['reason']=='qc_batch_failed'

def test_dual_analyst_peak_review_resolves_ambiguity_for_release(tmp_path):
    db=tmp_path/'db';operations.first(db,write(tmp_path,'cal.json',{'version':'v1','rows':[cal()]}))
    operations.second(db,write(tmp_path,'sample.json',{'version':'v1','rows':[
        sample(peaks=[{'time':5.0,'area':20},{'time':5.1,'area':22}])]}))
    weak={'version':'v1','rows':[{'id':'V0','sample':'S1','chosen_time':5.0,'analyst_a':'A','analyst_b':'A'}]}
    assert integration.review(db,write(tmp_path,'weak.json',weak))[0]['reason']=='dual_review_missing'
    approved={'version':'v1','rows':[{'id':'V1','sample':'S1','chosen_time':5.0,'analyst_a':'A','analyst_b':'B'}]}
    file=write(tmp_path,'approved.json',approved)
    assert integration.review(db,file)==integration.review(db,file)
    result=operations.third(db,write(tmp_path,'result.json',{'version':'v1','rows':[
        {'id':'R1','sample':'S1','calibration':'CAL1','quant_limit':1,'at':'2026-01-01T12:00:00'}]}))
    assert result[0]['status']=='可放行'
    assert connect(db).execute('SELECT count(*) FROM integration_revision').fetchone()[0]==1

def test_result_batch_distinguishes_partial_from_full_release(tmp_path):
    db=tmp_path/'db';operations.first(db,write(tmp_path,'cal.json',{'version':'v1','rows':[cal()]}))
    operations.second(db,write(tmp_path,'sample.json',{'version':'v1','rows':[
        sample('S1',peaks=[{'time':5,'area':20}]),
        sample('S2',injection='I2',peaks=[{'time':5,'area':40}])]}))
    operations.third(db,write(tmp_path,'result.json',{'version':'v1','rows':[
        {'id':'R1','sample':'S1','calibration':'CAL1','quant_limit':1,'at':'2026-01-01T12:00:00'},
        {'id':'R2','sample':'S2','calibration':'CAL1','quant_limit':1,'at':'2026-01-01T12:00:00'}]}))
    result=batch.release(db,write(tmp_path,'batch.json',{'version':'v1','rows':[
        {'id':'B1','results':['R1','R2']},{'id':'B2','results':['R1']}]}))
    assert [x['status'] for x in result]==['部分待核','已封账']
    assert connect(db).execute('SELECT count(*) FROM release_item').fetchone()[0]==3

def test_peak_review_cannot_select_outside_retention_window(tmp_path):
    db=tmp_path/'db';operations.second(db,write(tmp_path,'sample.json',{'version':'v1','rows':[
        sample(peaks=[{'time':5.0,'area':20},{'time':5.1,'area':22},{'time':8.0,'area':25}])]}))
    result=integration.review(db,write(tmp_path,'review.json',{'version':'v1','rows':[
        {'id':'V1','sample':'S1','chosen_time':8.0,'analyst_a':'A','analyst_b':'B'}]}))
    assert result[0]['reason']=='chosen_peak_not_unique'
    assert connect(db).execute("SELECT status FROM sample_export WHERE id='S1'").fetchone()[0]=='待核'

def custody_row(id='H1', limit=8, seal=True):
    return {'id':id,'sample':'S1','origin':'collector','destination':'lab','temperature_limit':limit,
            'transfers':[{'from':'collector','to':'courier','at':'2026-01-01T09:00:00',
                          'seal_intact':seal,'max_temperature':5},
                         {'from':'courier','to':'lab','at':'2026-01-01T10:00:00',
                          'seal_intact':True,'max_temperature':5}]}

def test_custody_continuity_qualifies_result_and_conflict_blocks_it(tmp_path):
    db=tmp_path/'db';operations.first(db,write(tmp_path,'cal.json',{'version':'v1','rows':[cal()]}))
    operations.second(db,write(tmp_path,'sample.json',{'version':'v1','rows':[sample()]}))
    good=write(tmp_path,'good.json',{'version':'v1','rows':[custody_row()]})
    assert custody.assess(db,good)==custody.assess(db,good)
    request={'version':'v1','rows':[{'id':'R1','sample':'S1','calibration':'CAL1',
                                 'quant_limit':1,'at':'2026-01-01T12:00:00'}]}
    assert operations.third(db,write(tmp_path,'r1.json',request))[0]['status']=='可放行'
    broken=custody_row('H2');broken['transfers'][1]['from']='unknown'
    assert custody.assess(db,write(tmp_path,'bad.json',{'version':'v1','rows':[broken]}))[0]['reason']=='broken_custody_chain'
    request['rows'][0]['id']='R2'
    assert operations.third(db,write(tmp_path,'r2.json',request))[0]['reason']=='custody_unqualified'

def test_late_temperature_excursion_recalls_sealed_batch(tmp_path):
    db=tmp_path/'db';operations.first(db,write(tmp_path,'cal.json',{'version':'v1','rows':[cal()]}))
    operations.second(db,write(tmp_path,'sample.json',{'version':'v1','rows':[sample()]}))
    operations.third(db,write(tmp_path,'result.json',{'version':'v1','rows':[
        {'id':'R1','sample':'S1','calibration':'CAL1','quant_limit':1,'at':'2026-01-01T12:00:00'}]}))
    frozen=write(tmp_path,'batch.json',{'version':'v1','rows':[{'id':'B1','results':['R1']}]})
    assert batch.release(db,frozen)[0]['status']=='已封账'
    excursion=custody_row(limit=4)
    assert custody.assess(db,write(tmp_path,'handoff.json',{'version':'v1','rows':[excursion]}))[0]['reason']=='temperature_excursion'
    conn=connect(db)
    assert conn.execute("SELECT status FROM released_result WHERE id='result:R1'").fetchone()[0]=='撤回'
    assert conn.execute("SELECT status FROM release_manifest WHERE id='B1'").fetchone()[0]=='撤回'
    assert conn.execute('SELECT count(*) FROM release_recall').fetchone()[0]==1
    assert batch.release(db,frozen)[0]['status']=='撤回'
    assert batch.release(db,write(tmp_path,'next.json',{'version':'v1','rows':[
        {'id':'B2','results':['R1']}]}))[0]['status']=='部分待核'
