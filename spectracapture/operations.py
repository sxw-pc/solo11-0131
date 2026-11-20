"""Desktop chromatogram calibration, peak assignment, and release.

Source authenticity gates business use only. Peak areas, concentrations, curve math and
already-signed analyst judgments are never recomputed or overwritten; when the source's
business contract is not usable the rows keep their computed bodies but are persisted
with a hold status carrying the source verdict reason.
"""
import json
from datetime import datetime
from .ingest import capture
from .store import connect,save,decide
from . import trust


def _source_hold(db, source):
    """Return the hold (status, reason) imposed by the business-use contract, or None."""
    blocked, reason = trust.business_gate(db, source)
    if not blocked:
        return None
    return reason


def first(path,file):
    source,payload=capture(path,'desktop_calibration_export',file)
    db=connect(path);results=[]
    try:
        with db:
            hold=_source_hold(db,source)
            for row in payload.get('rows',[]):
                key=str(row['id']);points=row.get('points',[])
                concentrations=[float(p['concentration']) for p in points]
                areas=[float(p['area']) for p in points]
                if len(points)<3 or any(b<=a for a,b in zip(concentrations,concentrations[1:])):status,reason='失效','concentration_order'
                elif float(row['blank_area'])>float(row['max_blank']):status,reason='失效','blank_contamination'
                elif datetime.fromisoformat(row['at'])>datetime.fromisoformat(row['valid_until']):status,reason='失效','calibration_expired'
                else:
                    slope=(areas[-1]-areas[0])/(concentrations[-1]-concentrations[0]);intercept=areas[0]-slope*concentrations[0]
                    residual=max(abs(a-(slope*c+intercept)) for a,c in zip(areas,concentrations))
                    status,reason=('有效','curve_fit') if slope>0 and residual<=float(row['max_residual']) else ('失效','residual_exceeded')
                if hold:status,reason='待核',f'source_hold:{hold}'
                info=dict(row)
                if len(points)>=3 and concentrations[-1]>concentrations[0]:
                    info['slope']=(areas[-1]-areas[0])/(concentrations[-1]-concentrations[0]);info['intercept']=areas[0]-info['slope']*concentrations[0]
                save(db,'calibration_export',key,source,payload['version'],info,status,reason)
                for i,point in enumerate(points):
                    save(db,'calibration_point',f'{key}:{i}',source,payload['version'],point,status,reason)
                results.append(decide(db,'标定曲线',key,status,reason,source))
        return results
    finally:db.close()


def second(path,file):
    source,payload=capture(path,'desktop_sample_export',file)
    db=connect(path);results=[]
    try:
        with db:
            hold=_source_hold(db,source)
            for row in payload.get('rows',[]):
                key=str(row['id']);candidates=[p for p in row.get('peaks',[]) if float(row['window'][0])<=float(p['time'])<=float(row['window'][1])]
                conflicting=db.execute("SELECT 1 FROM sample_export WHERE json_extract(body,'$.injection')=? AND id<>?",(row['injection'],key)).fetchone()
                if conflicting:status,reason='待核','injection_conflict'
                elif not candidates:status,reason='待核','peak_outside_window'
                elif len(candidates)>1:status,reason='待核','ambiguous_peaks'
                else:status,reason='已归属','unique_peak'
                if hold:status,reason='待核',f'source_hold:{hold}'
                save(db,'sample_export',key,source,payload['version'],row,status,reason)
                save(db,'peak_assignment',f'assignment:{key}',source,payload['version'],
                     {'sample':key,'peak':candidates[0] if len(candidates)==1 else None,'calibration':row['calibration']},status,reason)
                results.append(decide(db,'样本峰归属',key,status,reason,source))
        return results
    finally:db.close()


def third(path,file):
    source,payload=capture(path,'desktop_result_request',file)
    db=connect(path);results=[]
    try:
        with db:
            hold=_source_hold(db,source)
            for row in payload.get('rows',[]):
                key=str(row['id']);cal=db.execute('SELECT * FROM calibration_export WHERE id=?',(str(row['calibration']),)).fetchone()
                sample=db.execute('SELECT * FROM sample_export WHERE id=?',(str(row['sample']),)).fetchone()
                assignment=db.execute('SELECT * FROM peak_assignment WHERE id=?',(f"assignment:{row['sample']}",)).fetchone()
                qc=[x['status'] for x in db.execute("SELECT status FROM qc_batch WHERE json_extract(body,'$.calibration')=?",(str(row['calibration']),))]
                custody=db.execute("SELECT status FROM custody_case WHERE json_extract(body,'$.sample')=? ORDER BY rowid DESC LIMIT 1",
                                   (str(row['sample']),)).fetchone()
                concentration=None
                if not cal or cal['status']!='有效':status,reason='拒放','calibration_invalid'
                elif qc and any(x!='合格' for x in qc):status,reason='拒放','qc_batch_failed'
                elif custody and custody['status']!='合格':status,reason='拒放','custody_unqualified'
                elif not sample or sample['status']!='已归属' or not assignment or assignment['status']!='已归属':status,reason='待核','peak_unresolved'
                elif datetime.fromisoformat(row['at'])>datetime.fromisoformat(json.loads(cal['body'])['valid_until']):status,reason='拒放','calibration_expired'
                else:
                    curve=json.loads(cal['body']);peak=json.loads(assignment['body'])['peak']
                    concentration=(float(peak['area'])-curve['intercept'])/curve['slope']
                    low=min(float(p['concentration']) for p in curve['points']);high=max(float(p['concentration']) for p in curve['points'])
                    if concentration<low or concentration>high:status,reason='待核','outside_calibration_range'
                    elif concentration<float(row['quant_limit']):status,reason='待核','below_quant_limit'
                    else:status,reason='可放行','traceable_concentration'
                if hold:status,reason='拒放',f'source_hold:{hold}'
                save(db,'result_batch',key,source,payload['version'],row,status,reason)
                save(db,'released_result',f'result:{key}',source,payload['version'],
                     {'sample':row['sample'],'calibration':row['calibration'],'concentration':concentration},status,reason)
                results.append(decide(db,'结果放行',key,status,reason,source))
        return results
    finally:db.close()


def demo(root):
    db=root/'ledger.db';files=[
        {'version':'v1','rows':[{'id':'CAL1','points':[{'concentration':1,'area':10},{'concentration':2,'area':20},{'concentration':3,'area':30}],
                                'blank_area':0,'max_blank':2,'max_residual':1,'at':'2026-01-01T10:00:00','valid_until':'2026-01-02T00:00:00'}]},
        {'version':'v1','rows':[{'id':'S1','injection':'I1','calibration':'CAL1','window':[4.5,5.5],
                                'peaks':[{'time':5.0,'area':20}]}]},
        {'version':'v1','rows':[{'id':'R1','sample':'S1','calibration':'CAL1','quant_limit':1.5,'at':'2026-01-01T12:00:00'}]}]
    out=[]
    for i,(fn,item) in enumerate(zip((first,second,third),files)):
        if i==2:
            from .qc import assess
            p=root/'qc.json';p.write_text(json.dumps({'version':'v1','rows':[{'id':'QC1','calibration':'CAL1',
                'blank_area':0,'max_blank':2,'controls':[{'expected':2,'area':20,'max_relative_error':0.1}]}]}),encoding='utf-8')
            assess(db,p)
            from .custody import assess as assess_custody
            p=root/'custody.json';p.write_text(json.dumps({'version':'v1','rows':[{'id':'H1','sample':'S1',
                'origin':'collector','destination':'lab','temperature_limit':8,
                'transfers':[{'from':'collector','to':'lab','at':'2026-01-01T11:00:00',
                              'seal_intact':True,'max_temperature':5}]}]}),encoding='utf-8')
            assess_custody(db,p)
        p=root/f'export{i}.json';p.write_text(json.dumps(item),encoding='utf-8');out.append(fn(db,p))
    from .batch import release
    p=root/'batch.json';p.write_text(json.dumps({'version':'v1','rows':[{'id':'B1','results':['R1']}]}),encoding='utf-8')
    out.append(release(db,p))
    return out
