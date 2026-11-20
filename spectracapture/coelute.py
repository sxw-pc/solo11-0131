"""Mixed-peak quantitation for up to two co-eluting substances.

A desktop mixed-peak export keeps one integrated peak with per-ion-channel
areas. Each channel observation is modelled as the channel blank plus the
response contributions of the co-eluting substances. Only concentration
combinations that are non-negative, inside each substance's calibration
range and within every approved channel's error bound are accepted; every
verdict is derived from the bounds of the whole feasible set, never from
splitting the total area, copying the peak onto two results, picking a
least-residual solution, or treating a reviewed peak time as separation.

Channel eligibility is evidence-driven: routine contracts admit only the
registered primary channels, spiked-confirmation contracts may add a third
channel once it has an independent standard, passing recovery QC and a
dual-analyst sign-off bound to the raw export digest and channel standard
versions. Late QC failures, standard corrections and sign-off revocations
re-adjudicate every affected request and withdraw sealed manifests.
"""
import json
from fractions import Fraction
from .ingest import capture
from .store import connect, save, decide


def _num(value):
    return Fraction(str(value))


def _out(value):
    return value.numerator if value.denominator == 1 else float(value)


def _validate_mixed(row):
    substances = row.get('substances')
    if not isinstance(substances, list) or not 1 <= len(substances) <= 2:
        return '失效', 'substance_count'
    if not row.get('group') or not row.get('peak') or not row.get('sample') or not row.get('injection'):
        return '失效', 'identity_fields'
    ranges, limits = row.get('ranges') or {}, row.get('quant_limits') or {}
    for substance in substances:
        span = ranges.get(substance)
        if not isinstance(span, list) or len(span) != 2 or float(span[0]) < 0 or float(span[0]) > float(span[1]):
            return '失效', 'calibration_range'
        if substance not in limits or float(limits[substance]) < 0:
            return '失效', 'quant_limit'
    channels = row.get('channels')
    if not isinstance(channels, list) or not channels:
        return '失效', 'channel_layout'
    ids = [str(channel.get('id')) for channel in channels]
    if len(set(ids)) != len(ids):
        return '失效', 'channel_layout'
    for channel in channels:
        if 'area' not in channel or 'blank' not in channel or float(channel.get('max_error', -1)) < 0:
            return '失效', 'channel_layout'
        responses = channel.get('responses') or {}
        for substance in substances:
            entry = responses.get(substance)
            if not isinstance(entry, dict) or float(entry.get('response', -1)) < 0 or not entry.get('standard_version'):
                return '失效', 'channel_response'
    return '待裁', 'structure_ok'


def ingest(path, file):
    source, payload = capture(path, 'desktop_mixed_peak_export', file)
    db = connect(path)
    results = []
    try:
        with db:
            for row in payload.get('rows', []):
                key = str(row['id'])
                status, reason = _validate_mixed(row)
                save(db, 'mixed_peak_export', key, source, payload['version'], row, status, reason)
                if status == '待裁':
                    for channel in row['channels']:
                        save(db, 'mixed_channel', f"{key}:{channel['id']}", source, payload['version'],
                             dict(channel, mixed_peak=key), status, reason)
                results.append(decide(db, '混合峰导出', key, status, reason, source))
        return results
    finally:
        db.close()


def contract(path, file):
    source, payload = capture(path, 'desktop_assay_contract', file)
    db = connect(path)
    results = []
    try:
        with db:
            for row in payload.get('rows', []):
                key = str(row['id'])
                primary = row.get('primary_channels')
                if row.get('kind') not in ('常规', '加标确证'):
                    status, reason = '失效', 'contract_kind'
                elif not isinstance(primary, list) or not primary or len(set(map(str, primary))) != len(primary):
                    status, reason = '失效', 'primary_channels'
                else:
                    status, reason = '有效', 'contract_registered'
                save(db, 'assay_contract', key, source, payload['version'], row, status, reason)
                results.append(decide(db, '实验合同', key, status, reason, source))
        return results
    finally:
        db.close()


def _effective_channel(db, mixed_peak, channel):
    responses = {str(substance): dict(entry) for substance, entry in channel['responses'].items()}
    applied = []
    rows = db.execute("SELECT body FROM standard_correction WHERE json_extract(body,'$.mixed_peak')=? "
                      "AND json_extract(body,'$.channel')=? AND status='已应用' ORDER BY rowid",
                      (mixed_peak, str(channel['id']))).fetchall()
    for row in rows:
        correction = json.loads(row['body'])
        responses[str(correction['substance'])] = {'response': correction['response'],
                                                   'standard_version': correction['standard_version']}
        applied.append(str(correction['id']))
    return dict(channel, responses=responses), applied


def _latest_channel_qc(db, channel_id):
    row = db.execute("SELECT status FROM channel_qc WHERE json_extract(body,'$.channel')=? "
                     "ORDER BY rowid DESC LIMIT 1", (channel_id,)).fetchone()
    return row['status'] if row else None


def _valid_signoff(db, mixed_peak, channel_id, effective):
    row = db.execute("SELECT * FROM spike_signoff WHERE json_extract(body,'$.mixed_peak')=? "
                     "AND json_extract(body,'$.channel')=? ORDER BY rowid DESC LIMIT 1",
                     (mixed_peak, channel_id)).fetchone()
    if not row or row['status'] != '有效':
        return None
    versions = {str(k): str(v) for k, v in (json.loads(row['body']).get('standard_versions') or {}).items()}
    for substance, entry in effective['responses'].items():
        if versions.get(str(substance)) != str(entry['standard_version']):
            return None
    return row['id']


def _eligible_channels(db, peak_id, peak_body, contract_body):
    primary = {str(channel) for channel in contract_body['primary_channels']}
    spiked = contract_body['kind'] == '加标确证'
    eligible, corroborating = [], []
    evidence = {'channel_qc': {}, 'signoffs': {}, 'corrections': {}}
    for channel in peak_body['channels']:
        channel_id = str(channel['id'])
        effective, applied = _effective_channel(db, peak_id, channel)
        qc_status = _latest_channel_qc(db, channel_id)
        evidence['corrections'][channel_id] = applied
        evidence['channel_qc'][channel_id] = qc_status
        if channel_id in primary:
            if qc_status != '失控':
                eligible.append(effective)
        elif spiked:
            signoff = _valid_signoff(db, peak_id, channel_id, effective)
            evidence['signoffs'][channel_id] = signoff
            if qc_status == '合格' and signoff:
                eligible.append(effective)
            else:
                corroborating.append(channel_id)
        else:
            corroborating.append(channel_id)
    return eligible, corroborating, evidence


def _feasible_bounds(substances, ranges, channels):
    limits = {}
    for substance in substances:
        low, high = ranges[substance]
        limits[substance] = (max(_num(low), Fraction(0)), _num(high))
    if len(substances) == 1:
        substance = substances[0]
        low, high = limits[substance]
        for channel in channels:
            response = _num(channel['responses'][substance]['response'])
            target = _num(channel['area']) - _num(channel['blank'])
            error = _num(channel['max_error'])
            if response == 0:
                if abs(target) > error:
                    return None
                continue
            low, high = max(low, (target - error) / response), min(high, (target + error) / response)
        return None if low > high else {substance: (low, high)}
    first, second = substances
    inequalities, lines = [], []

    def add(ca, cb, rhs):
        inequalities.append((ca, cb, rhs))
        if ca != 0 or cb != 0:
            lines.append((ca, cb, rhs))

    for substance, (ca, cb) in ((first, (1, 0)), (second, (0, 1))):
        low, high = limits[substance]
        add(Fraction(ca), Fraction(cb), high)
        add(Fraction(-ca), Fraction(-cb), -low)
    for channel in channels:
        ra = _num(channel['responses'][first]['response'])
        rb = _num(channel['responses'][second]['response'])
        target = _num(channel['area']) - _num(channel['blank'])
        error = _num(channel['max_error'])
        add(ra, rb, target + error)
        add(-ra, -rb, error - target)
    for ca, cb, rhs in inequalities:
        if ca == 0 and cb == 0 and rhs < 0:
            return None
    points = []
    for i, (a1, b1, c1) in enumerate(lines):
        for a2, b2, c2 in lines[i + 1:]:
            det = a1 * b2 - a2 * b1
            if det == 0:
                continue
            x, y = (c1 * b2 - c2 * b1) / det, (a1 * c2 - a2 * c1) / det
            if all(ca * x + cb * y <= rhs for ca, cb, rhs in inequalities):
                points.append((x, y))
    if not points:
        return None
    xs, ys = [point[0] for point in points], [point[1] for point in points]
    return {first: (min(xs), max(xs)), second: (min(ys), max(ys))}


def _verdicts(peak_body, eligible, substances):
    bounds = _feasible_bounds(substances, peak_body['ranges'], eligible)
    outcomes = {}
    for substance in substances:
        if bounds is None:
            outcomes[substance] = ('拒放', 'no_feasible_combination', None, None)
            continue
        low, high = bounds[substance]
        limit = _num(peak_body['quant_limits'][substance])
        interval = [_out(low), _out(high)]
        concentration = _out(low) if low == high else interval
        if high < limit:
            outcomes[substance] = ('低于定量限', 'below_quant_limit', concentration, interval)
        elif low >= limit:
            reason = 'unique_solution' if low == high else 'bounded_solution'
            outcomes[substance] = ('可放行', reason, concentration, interval)
        else:
            outcomes[substance] = ('待核', 'quant_limit_crossing', None, interval)
    return outcomes


def _withdraw_manifests(db, source, version, result_id, request_key):
    manifests = db.execute("SELECT DISTINCT m.* FROM release_manifest m JOIN release_item i ON i.id LIKE m.id || ':%' "
                           "WHERE json_extract(i.body,'$.result')=? AND m.status='已封账'", (result_id,)).fetchall()
    for manifest in manifests:
        db.execute("UPDATE release_manifest SET status='撤回',reason='coelution_readjudicated' WHERE id=?",
                   (manifest['id'],))
        save(db, 'release_recall', f'{request_key}:{result_id}:{manifest["id"]}', source, version,
             {'request': request_key, 'result': result_id, 'manifest': manifest['id']}, '待通知', 'sealed_batch_recalled')


def _adjudicate(db, source, version, row):
    key = str(row['id'])
    requested = {str(s): str(r) for s, r in (row.get('results') or {}).items()}
    if not requested:
        save(db, 'mixed_quant', key, source, version, row, '拒绝', 'results_missing')
        return [decide(db, '混合峰定量', key, '拒绝', 'results_missing', source)]
    save(db, 'mixed_quant', key, source, version, row, '已裁定', 'quant_request')
    peak = db.execute('SELECT * FROM mixed_peak_export WHERE id=?', (str(row.get('mixed_peak')),)).fetchone()
    contract = db.execute('SELECT * FROM assay_contract WHERE id=?', (str(row.get('contract')),)).fetchone()
    peak_body = json.loads(peak['body']) if peak else None
    contract_body = json.loads(contract['body']) if contract else None
    eligible, corroborating = [], []
    evidence = {'channel_qc': {}, 'signoffs': {}, 'corrections': {}}
    custody_status = None
    if not peak or peak['status'] != '待裁':
        outcomes = {s: ('拒放', 'mixed_peak_invalid', None, None) for s in requested}
    elif not contract or contract['status'] != '有效':
        outcomes = {s: ('拒放', 'contract_invalid', None, None) for s in requested}
    elif any(s not in peak_body['substances'] for s in requested):
        outcomes = {s: ('拒放', 'substance_unknown', None, None) for s in requested}
    else:
        custody = db.execute("SELECT status FROM custody_case WHERE json_extract(body,'$.sample')=? "
                             "ORDER BY rowid DESC LIMIT 1", (str(peak_body['sample']),)).fetchone()
        custody_status = custody['status'] if custody else None
        if custody and custody['status'] != '合格':
            outcomes = {s: ('拒放', 'custody_unqualified', None, None) for s in requested}
        else:
            eligible, corroborating, evidence = _eligible_channels(db, str(row['mixed_peak']), peak_body, contract_body)
            outcomes = _verdicts(peak_body, eligible, list(requested))
    revision = {'request': key, 'mixed_peak': str(row.get('mixed_peak')),
                'group': peak_body['group'] if peak_body else None, 'contract': str(row.get('contract')),
                'channels_used': [str(c['id']) for c in eligible], 'corroborating_channels': corroborating,
                'evidence': evidence, 'custody': custody_status,
                'standard_versions': {str(c['id']): {s: v['standard_version'] for s, v in c['responses'].items()}
                                      for c in eligible},
                'verdicts': {s: {'status': o[0], 'reason': o[1], 'concentration': o[2], 'bounds': o[3]}
                             for s, o in outcomes.items()}}
    encoded = json.dumps(revision, sort_keys=True, ensure_ascii=False)
    latest = db.execute('SELECT * FROM mixed_adjudication WHERE id LIKE ? ORDER BY rowid DESC LIMIT 1',
                        (f'adj:{key}:%',)).fetchone()
    if latest and latest['body'] == encoded:
        return [decide(db, '混合峰定量', f'{key}:{s}', o[0], o[1], source) for s, o in outcomes.items()]
    sequence = db.execute('SELECT count(*) AS c FROM mixed_adjudication WHERE id LIKE ?',
                          (f'adj:{key}:%',)).fetchone()['c'] + 1
    save(db, 'mixed_adjudication', f'adj:{key}:{sequence}', source, version, revision, '已裁定', 'adjudication')
    decisions = []
    for substance, (status, reason, concentration, bounds) in outcomes.items():
        result_id = requested[substance]
        released = {'sample': peak_body['sample'] if peak_body else None, 'mixed_peak': str(row.get('mixed_peak')),
                    'group': revision['group'], 'contract': str(row.get('contract')), 'substance': substance,
                    'concentration': concentration, 'bounds': bounds, 'channels': revision['channels_used'],
                    'standard_versions': revision['standard_versions'], 'channel_qc': evidence['channel_qc'],
                    'signoffs': evidence['signoffs'], 'custody': custody_status,
                    'adjudication': f'adj:{key}:{sequence}'}
        rid = f'result:{result_id}'
        old = db.execute('SELECT * FROM released_result WHERE id=?', (rid,)).fetchone()
        if old is None:
            save(db, 'released_result', rid, source, version, released, status, reason)
        else:
            db.execute('UPDATE released_result SET body=?, status=?, reason=? WHERE id=?',
                       (json.dumps(released, sort_keys=True, ensure_ascii=False), status, reason, rid))
            if old['status'] == '可放行' and status != '可放行':
                _withdraw_manifests(db, source, version, result_id, key)
        decisions.append(decide(db, '混合峰定量', f'{key}:{substance}', status, reason, source))
    return decisions


def adjudicate(path, file):
    source, payload = capture(path, 'desktop_mixed_quant_request', file)
    db = connect(path)
    results = []
    try:
        with db:
            for row in payload.get('rows', []):
                results.extend(_adjudicate(db, source, payload['version'], row))
        return results
    finally:
        db.close()


def readjudicate(db, source, version, mixed_peak_ids):
    if not mixed_peak_ids:
        return
    for row in db.execute('SELECT * FROM mixed_quant').fetchall():
        body = json.loads(row['body'])
        if str(body.get('mixed_peak')) in mixed_peak_ids:
            _adjudicate(db, source, version, body)


def readjudicate_channels(db, source, version, channel_ids):
    touched = set()
    for row in db.execute('SELECT * FROM mixed_peak_export').fetchall():
        body = json.loads(row['body'])
        if any(str(channel.get('id')) in channel_ids for channel in body.get('channels', [])):
            touched.add(str(body['id']))
    readjudicate(db, source, version, touched)


def _validate_signoff(db, row):
    peak = db.execute('SELECT * FROM mixed_peak_export WHERE id=?', (str(row.get('mixed_peak')),)).fetchone()
    if not peak or peak['status'] != '待裁':
        return '拒绝', 'mixed_peak_missing'
    body = json.loads(peak['body'])
    channel = next((c for c in body['channels'] if str(c['id']) == str(row.get('channel'))), None)
    if channel is None:
        return '拒绝', 'channel_missing'
    export = db.execute('SELECT hash FROM export_file WHERE id=?', (peak['source_id'],)).fetchone()
    if not export or str(row.get('digest')) != export['hash']:
        return '拒绝', 'digest_mismatch'
    if not row.get('analyst_a') or row.get('analyst_a') == row.get('analyst_b'):
        return '待核', 'dual_signoff_missing'
    effective, _ = _effective_channel(db, str(row['mixed_peak']), channel)
    versions = row.get('standard_versions') or {}
    for substance in body['substances']:
        if str(versions.get(substance)) != str(effective['responses'][substance]['standard_version']):
            return '拒绝', 'standard_version_mismatch'
    return '有效', 'dual_analyst_signoff'


def signoff(path, file):
    source, payload = capture(path, 'desktop_spike_signoff', file)
    db = connect(path)
    results = []
    try:
        with db:
            touched = set()
            for row in payload.get('rows', []):
                key = str(row['id'])
                if row.get('revoke'):
                    old = db.execute('SELECT * FROM spike_signoff WHERE id=?', (key,)).fetchone()
                    if not old:
                        results.append(decide(db, '加标签认', key, '拒绝', 'signoff_missing', source))
                        continue
                    reason = str(row.get('reason', 'analyst_withdrawal'))
                    db.execute("UPDATE spike_signoff SET status='撤销',reason=? WHERE id=?", (reason, key))
                    touched.add(str(json.loads(old['body'])['mixed_peak']))
                    results.append(decide(db, '加标签认', key, '撤销', reason, source))
                    continue
                old = db.execute('SELECT * FROM spike_signoff WHERE id=?', (key,)).fetchone()
                if old:
                    if old['body'] != json.dumps(row, sort_keys=True, ensure_ascii=False):
                        raise ValueError('signoff_identity_conflict')
                    results.append(decide(db, '加标签认', key, old['status'], old['reason'], source))
                    continue
                status, reason = _validate_signoff(db, row)
                save(db, 'spike_signoff', key, source, payload['version'], row, status, reason)
                touched.add(str(row.get('mixed_peak')))
                results.append(decide(db, '加标签认', key, status, reason, source))
            readjudicate(db, source, payload['version'], touched)
        return results
    finally:
        db.close()


def correct(path, file):
    source, payload = capture(path, 'desktop_standard_correction', file)
    db = connect(path)
    results = []
    try:
        with db:
            touched = set()
            for row in payload.get('rows', []):
                key = str(row['id'])
                peak = db.execute('SELECT * FROM mixed_peak_export WHERE id=?', (str(row.get('mixed_peak')),)).fetchone()
                body = json.loads(peak['body']) if peak else None
                channel = next((c for c in body['channels'] if str(c['id']) == str(row.get('channel'))), None) if body else None
                if not peak or peak['status'] != '待裁':
                    status, reason = '拒绝', 'mixed_peak_missing'
                elif channel is None:
                    status, reason = '拒绝', 'channel_missing'
                elif str(row.get('substance')) not in body['substances']:
                    status, reason = '拒绝', 'substance_unknown'
                elif float(row.get('response', -1)) < 0 or not row.get('standard_version'):
                    status, reason = '拒绝', 'standard_invalid'
                else:
                    status, reason = '已应用', 'standard_corrected'
                save(db, 'standard_correction', key, source, payload['version'], row, status, reason)
                if status == '已应用':
                    touched.add(str(row['mixed_peak']))
                results.append(decide(db, '标准版次更正', key, status, reason, source))
            readjudicate(db, source, payload['version'], touched)
        return results
    finally:
        db.close()
