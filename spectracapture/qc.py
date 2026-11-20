"""Independent control injections that qualify a calibration for reporting."""
import json
from .ingest import capture
from .store import connect,save,decide
from .coelute import readjudicate_channels


def assess(path,file):
    source,payload=capture(path,'desktop_qc_sequence',file)
    db=connect(path);results=[]
    try:
        with db:
            for row in payload.get('rows',[]):
                key=str(row['id']);cal=db.execute('SELECT * FROM calibration_export WHERE id=?',(str(row['calibration']),)).fetchone()
                controls=[]
                if not cal or cal['status']!='有效':status,reason='失控','calibration_invalid'
                elif not row.get('controls'):status,reason='待核','control_missing'
                elif float(row['blank_area'])>float(row['max_blank']):status,reason='失控','qc_blank_contamination'
                else:
                    curve=json.loads(cal['body']);status,reason='合格','controls_within_recovery'
                    for control in row['controls']:
                        measured=(float(control['area'])-curve['intercept'])/curve['slope']
                        error=abs(measured-float(control['expected']))/float(control['expected'])
                        controls.append(dict(control,measured=measured,relative_error=error))
                        if error>float(control['max_relative_error']):status,reason='失控','control_recovery_failed'
                save(db,'qc_batch',key,source,payload['version'],row,status,reason)
                for i,control in enumerate(controls):
                    save(db,'qc_control',f'{key}:{i}',source,payload['version'],control,
                         '合格' if control['relative_error']<=float(control['max_relative_error']) else '失控',reason)
                results.append(decide(db,'质控序列',key,status,reason,source))
        return results
    finally:db.close()


def assess_channel(path,file):
    source,payload=capture(path,'desktop_channel_qc',file)
    db=connect(path);results=[]
    try:
        with db:
            touched=set()
            for row in payload.get('rows',[]):
                key=str(row['id']);channel=row.get('channel');controls=[]
                if not channel:status,reason='待核','channel_missing'
                elif not row.get('controls'):status,reason='待核','control_missing'
                elif float(row['blank_area'])>float(row['max_blank']):status,reason='失控','qc_blank_contamination'
                else:
                    status,reason='合格','controls_within_recovery';blank=float(row['blank_area'])
                    for control in row['controls']:
                        expected=blank+float(control['response'])*float(control['concentration'])
                        measured=float(control['area'])
                        error=abs(measured-expected)/expected if expected else abs(measured-expected)
                        controls.append(dict(control,expected=expected,relative_error=error))
                        if error>float(control['max_relative_error']):status,reason='失控','control_recovery_failed'
                save(db,'channel_qc',key,source,payload['version'],dict(row,evaluated=controls),status,reason)
                results.append(decide(db,'通道质控',key,status,reason,source))
                if channel:touched.add(str(channel))
            readjudicate_channels(db,source,payload['version'],touched)
        return results
    finally:db.close()
