"""Offline cross-instrument method transfer certification.

The method owner pre-registers compliant transfer schemes before sampling: a
single affine relation over the whole declared interval, or two affine
segments meeting continuously at a pre-registered breakpoint.  Bridging
evidence comes from paired injections of the same certified standards on both
instruments; each point keeps vial, export digest, injection time, instrument,
method and area, and verification points never join the fit.  The main result
is whether a concentration interval on a given instrument pair and method is
certified for a validity window - never the release of a single sample.
"""
import json
from datetime import datetime
from .ingest import capture
from .store import connect,save,decide

EPS=1e-9


def register(path,file):
    """Ingest pre-registered transfer schemes; a registered plan cannot be altered."""
    source,payload=capture(path,'desktop_transfer_plan',file)
    db=connect(path);results=[]
    try:
        with db:
            for row in payload.get('rows',[]):
                key=str(row['id']);status,reason=_validate_plan(row)
                save(db,'transfer_plan',key,source,payload['version'],row,status,reason)
                results.append(decide(db,'方法转移',key,status,reason,source))
        return results
    finally:db.close()


def bridge(path,file):
    """Ingest paired injections of certified standards run on both instruments."""
    source,payload=capture(path,'desktop_transfer_bridge',file)
    db=connect(path);results=[];affected=set()
    try:
        with db:
            for row in payload.get('rows',[]):
                key=str(row['id']);status,reason=_point_status(row)
                save(db,'bridge_point',key,source,payload['version'],row,status,reason)
                results.append(decide(db,'方法转移',key,status,reason,source))
                identity=_identity(row)
                if identity:affected.add(identity)
            for old,new,method in sorted(affected):
                reevaluate(db,pair=(old,new),method=method,source=source)
        return results
    finally:db.close()


def evaluate(path,plan=None):
    """Recompute interval certificates for one registered plan or all of them."""
    db=connect(path)
    try:
        with db:return reevaluate(db,plan_id=plan)
    finally:db.close()


def check(path,old,new,method,low,high,at):
    """Main result: is the transfer approved for this interval, instrument pair and time."""
    db=connect(path)
    try:
        lo,hi=float(low),float(high)
        if not lo<hi:return {'status':'拒绝','reason':'invalid_interval'}
        when=datetime.fromisoformat(at)
        plans=[]
        for row in db.execute('SELECT * FROM transfer_plan ORDER BY id'):
            if row['status']!='已登记':continue
            body=json.loads(row['body'])
            if (str(body['old_instrument']),str(body['new_instrument']),str(body['method']))!=(str(old),str(new),str(method)):continue
            plans.append((row,body))
        if not plans:return {'status':'拒绝','reason':'plan_not_registered'}
        fallback={'status':'拒绝','reason':'outside_certified_range'}
        for row,body in plans:
            if lo<float(body['interval'][0])-EPS or hi>float(body['interval'][1])+EPS:continue
            certs=[]
            for cert in db.execute("SELECT * FROM transfer_certificate WHERE json_extract(body,'$.plan')=?",(row['id'],)):
                certs.append((cert,json.loads(cert['body'])))
            certs.sort(key=lambda item:item[1]['segment'][0])
            cursor=lo;used=[];fail=None
            while cursor<hi-EPS:
                overlapping=[(c,cb) for c,cb in certs if cb['segment'][0]<=cursor+EPS and cb['segment'][1]>cursor+EPS]
                if not overlapping:fail='certificate_not_evaluated';break
                approved=[(c,cb) for c,cb in overlapping if c['status']=='获准']
                if not approved:fail=overlapping[0][0]['reason'];break
                best=max(approved,key=lambda item:item[1]['segment'][1])
                used.append((best[0]['id'],best[1]));cursor=best[1]['segment'][1]
            if fail is None:
                for cert_id,cb in used:
                    if when<datetime.fromisoformat(cb['valid_from']):fail='certificate_not_yet_valid';break
                    if when>datetime.fromisoformat(cb['valid_until']):fail='certificate_expired';break
            if fail is None:
                return {'status':'获准','reason':'transfer_certified','plan':row['id'],
                        'certificates':[cert_id for cert_id,cb in used]}
            if fallback['reason']=='outside_certified_range':
                fallback={'status':'拒绝','reason':fail,'plan':row['id']}
        return fallback
    finally:db.close()


def reevaluate(db,instrument=None,pair=None,method=None,plan_id=None,source=None):
    """Recompute certificates for registered plans matching the changed evidence."""
    results=[]
    for row in db.execute('SELECT * FROM transfer_plan ORDER BY id'):
        if row['status']!='已登记':continue
        body=json.loads(row['body'])
        if plan_id and row['id']!=plan_id:continue
        if pair and (str(body['old_instrument']),str(body['new_instrument']))!=(str(pair[0]),str(pair[1])):continue
        if instrument and str(instrument) not in (str(body['old_instrument']),str(body['new_instrument'])):continue
        if method and str(body['method'])!=str(method):continue
        results.extend(_evaluate_plan(db,row,source))
    return results


def reevaluate_for_rows(db,rows,source):
    """Trigger re-evaluation for plans touched by imported calibration/QC rows."""
    seen=set()
    for row in rows:
        instrument=row.get('instrument')
        if not instrument:continue
        method=str(row['method']) if row.get('method') else None
        key=(str(instrument),method)
        if key in seen:continue
        seen.add(key)
        reevaluate(db,instrument=key[0],method=method,source=source)


def _validate_plan(row):
    try:
        low,high=float(row['interval'][0]),float(row['interval'][1])
        if not low<high:return '拒绝','invalid_interval'
        if float(row['residual_budget'])<=0:return '拒绝','invalid_budget'
        datetime.fromisoformat(row['registered_at'])
        if not all(str(row.get(f) or '') for f in ('old_instrument','new_instrument','method')):return '拒绝','identity_missing'
        if str(row['old_instrument'])==str(row['new_instrument']):return '拒绝','identity_missing'
        scheme=row.get('scheme')
        if scheme not in ('single','segmented'):return '拒绝','invalid_scheme'
        if scheme=='segmented' and not low<float(row['breakpoint'])<high:return '拒绝','breakpoint_outside_interval'
    except (KeyError,TypeError,ValueError,IndexError):return '拒绝','plan_incomplete'
    return '已登记','plan_registered'


def _point_status(row):
    try:
        if not str(row.get('vial') or '') or not str(row.get('method') or ''):return '失控','point_incomplete'
        float(row['concentration'])
        if row.get('role') not in ('fit','verify'):return '失控','unknown_role'
        for side in ('old','new'):
            part=row[side]
            if not str(part.get('instrument') or ''):return '失控','point_incomplete'
            if float(part['area'])<=0:return '失控','nonpositive_area'
            datetime.fromisoformat(part['at'])
            if not str(part.get('digest') or ''):return '失控','digest_missing'
    except (KeyError,TypeError,ValueError):return '失控','point_incomplete'
    return '在控','paired_injection'


def _identity(row):
    try:return (str(row['old']['instrument']),str(row['new']['instrument']),str(row['method']))
    except (KeyError,TypeError):return None


def _conc(body):
    try:return float(body['concentration'])
    except (KeyError,TypeError,ValueError):return None


def _segments(plan):
    low,high=float(plan['interval'][0]),float(plan['interval'][1])
    if plan['scheme']=='segmented':
        bp=float(plan['breakpoint'])
        return [(low,bp),(bp,high)]
    return [(low,high)]


def _fit(pairs):
    n=len(pairs);xm=sum(x for x,_ in pairs)/n;ym=sum(y for _,y in pairs)/n
    den=sum((x-xm)**2 for x,_ in pairs)
    if den<=0:return None
    slope=sum((x-xm)*(y-ym) for x,y in pairs)/den
    return slope,ym-slope*xm


def _fit_segment(plan,seg,matching):
    lo,hi=seg
    pts=[(r,b) for r,b in matching if _conc(b) is not None and lo-EPS<=_conc(b)<=hi+EPS]
    if any(r['status']!='在控' for r,b in pts):return 'bridge_point_out_of_control',None
    fit_pts=[(r,b) for r,b in pts if b.get('role')=='fit']
    ver_pts=[(r,b) for r,b in pts if b.get('role')=='verify']
    need=3 if plan['scheme']=='single' else 2
    if len(fit_pts)<need:return 'insufficient_bridge_points',None
    concs=[float(b['concentration']) for r,b in fit_pts]
    if min(concs)>lo+EPS or max(concs)<hi-EPS:return 'interval_endpoints_unsupported',None
    if not ver_pts:return 'insufficient_verification_points',None
    pairs=[(float(b['old']['area']),float(b['new']['area'])) for r,b in fit_pts]
    found=_fit(pairs)
    if found is None:return 'degenerate_fit',None
    slope,intercept=found
    if slope<=0:return 'non_monotonic_transfer',None
    def residual(pair):
        r,b=pair;return abs(float(b['new']['area'])-(slope*float(b['old']['area'])+intercept))
    seg_times=[datetime.fromisoformat(b[side]['at']) for r,b in fit_pts+ver_pts for side in ('old','new')]
    return None,{'slope':slope,'intercept':intercept,
                 'fit_points':[r['id'] for r,b in fit_pts],'verify_points':[r['id'] for r,b in ver_pts],
                 'max_fit_residual':max(residual(p) for p in fit_pts),
                 'max_verify_residual':max(residual(p) for p in ver_pts),
                 'sources':sorted({r['source_id'] for r,b in fit_pts+ver_pts}),
                 'valid_from':max(seg_times).isoformat()}


def _evaluate_plan(db,plan_row,source):
    plan=json.loads(plan_row['body']);pid=plan_row['id']
    if source is None:source=plan_row['source_id']
    low,high=float(plan['interval'][0]),float(plan['interval'][1])
    budget=float(plan['residual_budget']);segments=_segments(plan)
    matching=[]
    for row in db.execute('SELECT * FROM bridge_point'):
        body=json.loads(row['body'])
        if _identity(body)!=(str(plan['old_instrument']),str(plan['new_instrument']),str(plan['method'])):continue
        matching.append((row,body))
    times=[]
    for row,body in matching:
        for side in ('old','new'):
            try:times.append(datetime.fromisoformat(body[side]['at']))
            except (KeyError,TypeError,ValueError):pass
    gate=None;curve=None;cal=None;qcr=None
    if times and min(times)<datetime.fromisoformat(plan['registered_at']):gate='plan_registered_after_sampling'
    if gate is None:
        cal=db.execute("SELECT * FROM calibration_export WHERE json_extract(body,'$.instrument')=? AND json_extract(body,'$.method')=? "
                       "ORDER BY rowid DESC LIMIT 1",(str(plan['old_instrument']),str(plan['method']))).fetchone()
        if not cal or cal['status']!='有效':gate='calibration_invalid'
        else:
            curve=json.loads(cal['body'])
            if times and max(times)>datetime.fromisoformat(curve['valid_until']):gate='calibration_expired'
            else:
                clow=min(float(p['concentration']) for p in curve['points']);chigh=max(float(p['concentration']) for p in curve['points'])
                if low<clow-EPS or high>chigh+EPS:gate='interval_beyond_calibration'
    if gate is None:
        qcr=db.execute("SELECT * FROM qc_batch WHERE json_extract(body,'$.instrument')=? AND json_extract(body,'$.method')=? "
                       "ORDER BY rowid DESC LIMIT 1",(str(plan['new_instrument']),str(plan['method']))).fetchone()
        if not qcr:gate='qc_missing'
        elif qcr['status']!='合格':gate='qc_out_of_control'
    details={};failed={}
    if gate is None:
        for seg in segments:
            reason,detail=_fit_segment(plan,seg,matching)
            if reason:failed[seg]=reason
            else:details[seg]=detail
        if plan['scheme']=='segmented' and len(details)==2:
            bp=float(plan['breakpoint']);bx=float(curve['slope'])*bp+float(curve['intercept'])
            left,right=details[segments[0]],details[segments[1]]
            if abs(left['slope']*bx+left['intercept']-(right['slope']*bx+right['intercept']))>budget:
                for seg in segments:failed[seg]='breakpoint_discontinuity'
        for seg,detail in details.items():
            if seg in failed:continue
            if detail['max_fit_residual']>budget:failed[seg]='transfer_residual_exceeded'
            elif detail['max_verify_residual']>budget:failed[seg]='verification_residual_exceeded'
    results=[]
    for seg in segments:
        lo,hi=seg
        body={'plan':pid,'scheme':plan['scheme'],'old_instrument':str(plan['old_instrument']),
              'new_instrument':str(plan['new_instrument']),'method':str(plan['method']),
              'segment':[lo,hi],'breakpoint':float(plan['breakpoint']) if plan['scheme']=='segmented' else None,
              'residual_budget':budget,'transfer':None,'fit_points':[],'verify_points':[],
              'max_fit_residual':None,'max_verify_residual':None,
              'sources':{'plan':plan_row['source_id'],'calibration':cal['source_id'] if cal else None,
                         'qc':qcr['source_id'] if qcr else None,'bridge':[]},
              'valid_from':None,'valid_until':curve['valid_until'] if curve else None}
        detail=details.get(seg)
        if detail:
            body['transfer']={'slope':detail['slope'],'intercept':detail['intercept']}
            body['fit_points']=detail['fit_points'];body['verify_points']=detail['verify_points']
            body['max_fit_residual']=detail['max_fit_residual'];body['max_verify_residual']=detail['max_verify_residual']
            body['sources']['bridge']=detail['sources'];body['valid_from']=detail['valid_from']
        results.append(_certify(db,pid,plan_row['version'],seg,body,source,gate or failed.get(seg)))
    return results


def _certify(db,pid,version,seg,body,source,reason):
    cert_id=f'{pid}:{seg[0]:g}:{seg[1]:g}'
    old=db.execute('SELECT status FROM transfer_certificate WHERE id=?',(cert_id,)).fetchone()
    if reason is None:status,reason='获准','transfer_certified'
    elif old and old['status'] in ('获准','撤销'):status='撤销'
    else:status='拒绝'
    db.execute('INSERT INTO transfer_certificate(id,source_id,version,body,status,reason) VALUES (?,?,?,?,?,?) '
               'ON CONFLICT(id) DO UPDATE SET source_id=excluded.source_id,version=excluded.version,'
               'body=excluded.body,status=excluded.status,reason=excluded.reason',
               (cert_id,source,version,json.dumps(body,sort_keys=True,ensure_ascii=False),status,reason))
    return decide(db,'方法转移',cert_id,status,reason,source)
