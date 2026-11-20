import hashlib
import json
from spectracapture import batch, coelute, custody, operations, qc, report
from spectracapture.store import connect


def write(root, name, obj):
    p = root / name
    p.write_text(json.dumps(obj), encoding='utf-8')
    return p


def channel(cid, area, responses, blank=0, error=0):
    return {'id': cid, 'area': area, 'blank': blank, 'max_error': error,
            'responses': {s: {'response': r, 'standard_version': f'STD-{cid}'} for s, r in responses.items()}}


def mixed_row(mid='M1', injection='I9', sample='SM1', channels=None, quant_limits=None, ranges=None):
    return {'id': mid, 'group': f'G-{mid}', 'injection': injection, 'sample': sample, 'peak': f'P-{mid}',
            'substances': ['甲', '乙'],
            'ranges': ranges or {'甲': [0, 3], '乙': [0, 3]},
            'quant_limits': quant_limits or {'甲': 1.5, '乙': 1.0},
            'channels': channels}


def base_channels():
    return [channel('ch1', 3, {'甲': 1, '乙': 1}),
            channel('ch2', 3, {'甲': 1, '乙': 1}),
            channel('ch3', 4, {'甲': 2, '乙': 1})]


def setup_m1(tmp_path, db):
    mixed_file = write(tmp_path, 'mixed.json', {'version': 'v1', 'rows': [mixed_row(channels=base_channels())]})
    coelute.ingest(db, mixed_file)
    coelute.contract(db, write(tmp_path, 'contracts.json', {'version': 'v1', 'rows': [
        {'id': 'CT-R', 'kind': '常规', 'primary_channels': ['ch1', 'ch2']},
        {'id': 'CT-S', 'kind': '加标确证', 'primary_channels': ['ch1', 'ch2']}]}))
    return hashlib.sha256(mixed_file.read_bytes()).hexdigest()


def spiked_evidence(tmp_path, db, digest, version='STD-ch3'):
    qc.assess_channel(db, write(tmp_path, 'qc-ch3.json', {'version': 'v1', 'rows': [
        {'id': 'CQ1', 'channel': 'ch3', 'blank_area': 0, 'max_blank': 0.5,
         'controls': [{'substance': '甲', 'concentration': 1, 'response': 2, 'area': 2, 'max_relative_error': 0.1},
                      {'substance': '乙', 'concentration': 2, 'response': 1, 'area': 2, 'max_relative_error': 0.1}]}]}))
    return coelute.signoff(db, write(tmp_path, 'signoff.json', {'version': 'v1', 'rows': [
        {'id': 'SO1', 'mixed_peak': 'M1', 'channel': 'ch3', 'digest': digest,
         'standard_versions': {'甲': version, '乙': version}, 'analyst_a': 'AN1', 'analyst_b': 'AN2'}]}))


def test_routine_contract_ignores_unapproved_third_channel(tmp_path):
    db = tmp_path / 'db'
    setup_m1(tmp_path, db)
    result = coelute.adjudicate(db, write(tmp_path, 'quant.json', {'version': 'v1', 'rows': [
        {'id': 'RQ1', 'mixed_peak': 'M1', 'contract': 'CT-R', 'results': {'甲': 'RA', '乙': 'RB'}}]}))
    assert [(r['subject'], r['status'], r['reason']) for r in result] == [
        ('RQ1:甲', '待核', 'quant_limit_crossing'), ('RQ1:乙', '待核', 'quant_limit_crossing')]
    conn = connect(db)
    body = json.loads(conn.execute("SELECT body FROM released_result WHERE id='result:RA'").fetchone()[0])
    assert body['channels'] == ['ch1', 'ch2'] and body['concentration'] is None and body['bounds'] == [0, 3]
    revision = json.loads(conn.execute("SELECT body FROM mixed_adjudication WHERE id='adj:RQ1:1'").fetchone()[0])
    assert revision['channels_used'] == ['ch1', 'ch2'] and revision['corroborating_channels'] == ['ch3']
    assert batch.release(db, write(tmp_path, 'batch.json', {'version': 'v1', 'rows': [
        {'id': 'B1', 'results': ['RA', 'RB']}]}))[0]['status'] == '部分待核'


def test_same_export_under_both_contracts(tmp_path):
    db = tmp_path / 'db'
    digest = setup_m1(tmp_path, db)
    assert spiked_evidence(tmp_path, db, digest)[0]['status'] == '有效'
    result = coelute.adjudicate(db, write(tmp_path, 'quant.json', {'version': 'v1', 'rows': [
        {'id': 'RQ-R', 'mixed_peak': 'M1', 'contract': 'CT-R', 'results': {'甲': 'RA-R', '乙': 'RB-R'}},
        {'id': 'RQ-S', 'mixed_peak': 'M1', 'contract': 'CT-S', 'results': {'甲': 'RA-S', '乙': 'RB-S'}}]}))
    assert [(r['subject'], r['status']) for r in result] == [
        ('RQ-R:甲', '待核'), ('RQ-R:乙', '待核'), ('RQ-S:甲', '低于定量限'), ('RQ-S:乙', '可放行')]
    conn = connect(db)
    spiked = json.loads(conn.execute("SELECT body FROM released_result WHERE id='result:RB-S'").fetchone()[0])
    assert spiked['concentration'] == 2 and spiked['channels'] == ['ch1', 'ch2', 'ch3']
    assert spiked['contract'] == 'CT-S' and spiked['group'] == 'G-M1' and spiked['signoffs'] == {'ch3': 'SO1'}
    assert spiked['standard_versions']['ch3'] == {'甲': 'STD-ch3', '乙': 'STD-ch3'}
    low = json.loads(conn.execute("SELECT body FROM released_result WHERE id='result:RA-S'").fetchone()[0])
    assert low['concentration'] == 1 and low['bounds'] == [1, 1]


def test_late_channel_qc_failure_recalled_sealed_result(tmp_path):
    db = tmp_path / 'db'
    digest = setup_m1(tmp_path, db)
    coelute.ingest(db, write(tmp_path, 'mixed2.json', {'version': 'v1', 'rows': [
        mixed_row('M2', injection='I10', sample='SM2',
                  channels=[channel('ch1', 3, {'甲': 1, '乙': 1}), channel('ch2', 5, {'甲': 1, '乙': 2})],
                  quant_limits={'甲': 0.5, '乙': 0.5})]}))
    spiked_evidence(tmp_path, db, digest)
    coelute.adjudicate(db, write(tmp_path, 'quant.json', {'version': 'v1', 'rows': [
        {'id': 'RQ2', 'mixed_peak': 'M1', 'contract': 'CT-S', 'results': {'甲': 'RA2', '乙': 'RB2'}},
        {'id': 'RQ3', 'mixed_peak': 'M2', 'contract': 'CT-R', 'results': {'甲': 'RA3', '乙': 'RB3'}}]}))
    sealed = write(tmp_path, 'batch.json', {'version': 'v1', 'rows': [
        {'id': 'B1', 'results': ['RB2']}, {'id': 'B2', 'results': ['RA3', 'RB3']}]})
    assert [r['status'] for r in batch.release(db, sealed)] == ['已封账', '已封账']
    result = qc.assess_channel(db, write(tmp_path, 'qc-ch3-late.json', {'version': 'v1', 'rows': [
        {'id': 'CQ2', 'channel': 'ch3', 'blank_area': 0, 'max_blank': 0.5,
         'controls': [{'substance': '甲', 'concentration': 1, 'response': 2, 'area': 5, 'max_relative_error': 0.1}]}]}))
    assert result[0]['reason'] == 'control_recovery_failed'
    conn = connect(db)
    for rid in ('RA2', 'RB2'):
        row = conn.execute('SELECT status, reason, body FROM released_result WHERE id=?', (f'result:{rid}',)).fetchone()
        assert (row['status'], row['reason']) == ('待核', 'quant_limit_crossing')
        assert json.loads(row['body'])['bounds'] == [0, 3]
    assert conn.execute("SELECT status FROM release_manifest WHERE id='B1'").fetchone()[0] == '撤回'
    assert conn.execute("SELECT status FROM release_manifest WHERE id='B2'").fetchone()[0] == '已封账'
    assert conn.execute("SELECT status FROM released_result WHERE id='result:RB3'").fetchone()[0] == '可放行'
    assert conn.execute('SELECT count(*) FROM release_recall').fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM mixed_adjudication WHERE id LIKE 'adj:RQ2:%'").fetchone()[0] == 2
    ch3 = json.loads(conn.execute("SELECT body FROM mixed_channel WHERE id='M1:ch3'").fetchone()[0])
    assert ch3['area'] == 4 and ch3['responses']['甲']['response'] == 2
    data = json.loads(report.build(db, tmp_path / 'report.json').read_text(encoding='utf-8'))
    assert all(x['verified'] for x in data['sources'])
    assert data['entities']['mixed_adjudication'] and data['entities']['channel_qc'] and data['entities']['release_recall']
    assert [r['status'] for r in batch.release(db, sealed)] == ['撤回', '已封账']


def test_unique_primary_solution_releases_without_review(tmp_path):
    db = tmp_path / 'db'
    coelute.ingest(db, write(tmp_path, 'mixed.json', {'version': 'v1', 'rows': [
        mixed_row(channels=[channel('ch1', 3, {'甲': 1, '乙': 1}), channel('ch2', 5, {'甲': 1, '乙': 2})],
                  quant_limits={'甲': 0.5, '乙': 0.5})]}))
    coelute.contract(db, write(tmp_path, 'contracts.json', {'version': 'v1', 'rows': [
        {'id': 'CT-R', 'kind': '常规', 'primary_channels': ['ch1', 'ch2']}]}))
    result = coelute.adjudicate(db, write(tmp_path, 'quant.json', {'version': 'v1', 'rows': [
        {'id': 'RQ1', 'mixed_peak': 'M1', 'contract': 'CT-R', 'results': {'甲': 'RA', '乙': 'RB'}}]}))
    assert [(r['status'], r['reason']) for r in result] == [('可放行', 'unique_solution'), ('可放行', 'unique_solution')]
    conn = connect(db)
    assert json.loads(conn.execute("SELECT body FROM released_result WHERE id='result:RA'").fetchone()[0])['concentration'] == 1
    assert json.loads(conn.execute("SELECT body FROM released_result WHERE id='result:RB'").fetchone()[0])['concentration'] == 2


def test_signoff_binding_revocation_and_replay(tmp_path):
    db = tmp_path / 'db'
    digest = setup_m1(tmp_path, db)
    qc.assess_channel(db, write(tmp_path, 'qc-ch3.json', {'version': 'v1', 'rows': [
        {'id': 'CQ1', 'channel': 'ch3', 'blank_area': 0, 'max_blank': 0.5,
         'controls': [{'substance': '甲', 'concentration': 1, 'response': 2, 'area': 2, 'max_relative_error': 0.1}]}]}))
    quant = write(tmp_path, 'quant.json', {'version': 'v1', 'rows': [
        {'id': 'RQ2', 'mixed_peak': 'M1', 'contract': 'CT-S', 'results': {'甲': 'RA2', '乙': 'RB2'}}]})
    same_analyst = coelute.signoff(db, write(tmp_path, 'bad1.json', {'version': 'v1', 'rows': [
        {'id': 'SO0', 'mixed_peak': 'M1', 'channel': 'ch3', 'digest': digest,
         'standard_versions': {'甲': 'STD-ch3', '乙': 'STD-ch3'}, 'analyst_a': 'AN1', 'analyst_b': 'AN1'}]}))
    assert same_analyst[0]['reason'] == 'dual_signoff_missing'
    wrong_digest = coelute.signoff(db, write(tmp_path, 'bad2.json', {'version': 'v1', 'rows': [
        {'id': 'SO1', 'mixed_peak': 'M1', 'channel': 'ch3', 'digest': '0' * 64,
         'standard_versions': {'甲': 'STD-ch3', '乙': 'STD-ch3'}, 'analyst_a': 'AN1', 'analyst_b': 'AN2'}]}))
    assert wrong_digest[0]['reason'] == 'digest_mismatch'
    wrong_version = coelute.signoff(db, write(tmp_path, 'bad3.json', {'version': 'v1', 'rows': [
        {'id': 'SO2', 'mixed_peak': 'M1', 'channel': 'ch3', 'digest': digest,
         'standard_versions': {'甲': 'STD-old', '乙': 'STD-ch3'}, 'analyst_a': 'AN1', 'analyst_b': 'AN2'}]}))
    assert wrong_version[0]['reason'] == 'standard_version_mismatch'
    assert [r['status'] for r in coelute.adjudicate(db, quant)] == ['待核', '待核']
    good = write(tmp_path, 'good.json', {'version': 'v1', 'rows': [
        {'id': 'SO3', 'mixed_peak': 'M1', 'channel': 'ch3', 'digest': digest,
         'standard_versions': {'甲': 'STD-ch3', '乙': 'STD-ch3'}, 'analyst_a': 'AN1', 'analyst_b': 'AN2'}]})
    assert coelute.signoff(db, good)[0]['status'] == '有效'
    conn = connect(db)
    assert conn.execute("SELECT status FROM released_result WHERE id='result:RB2'").fetchone()[0] == '可放行'
    assert coelute.signoff(db, write(tmp_path, 'revoke.json', {'version': 'v1', 'rows': [
        {'id': 'SO3', 'revoke': True, 'reason': 'analyst_withdrawal'}]}))[0]['status'] == '撤销'
    assert conn.execute("SELECT status FROM released_result WHERE id='result:RB2'").fetchone()[0] == '待核'
    coelute.signoff(db, good)
    assert conn.execute("SELECT status FROM spike_signoff WHERE id='SO3'").fetchone()[0] == '撤销'
    assert conn.execute("SELECT status FROM released_result WHERE id='result:RB2'").fetchone()[0] == '待核'


def test_standard_correction_readjudicates_and_replay_is_stable(tmp_path):
    db = tmp_path / 'db'
    digest = setup_m1(tmp_path, db)
    spiked_evidence(tmp_path, db, digest)
    quant = write(tmp_path, 'quant.json', {'version': 'v1', 'rows': [
        {'id': 'RQ2', 'mixed_peak': 'M1', 'contract': 'CT-S', 'results': {'甲': 'RA2', '乙': 'RB2'}}]})
    coelute.adjudicate(db, quant)
    sealed = write(tmp_path, 'batch.json', {'version': 'v1', 'rows': [{'id': 'B1', 'results': ['RB2']}]})
    assert batch.release(db, sealed)[0]['status'] == '已封账'
    correction = write(tmp_path, 'correct.json', {'version': 'v1', 'rows': [
        {'id': 'SC1', 'mixed_peak': 'M1', 'channel': 'ch3', 'substance': '甲',
         'response': 3, 'standard_version': 'STD-ch3b'}]})
    assert coelute.correct(db, correction)[0]['status'] == '已应用'
    conn = connect(db)
    assert conn.execute("SELECT status FROM released_result WHERE id='result:RB2'").fetchone()[0] == '待核'
    assert conn.execute("SELECT status FROM release_manifest WHERE id='B1'").fetchone()[0] == '撤回'
    coelute.signoff(db, write(tmp_path, 'signoff2.json', {'version': 'v1', 'rows': [
        {'id': 'SO2', 'mixed_peak': 'M1', 'channel': 'ch3', 'digest': digest,
         'standard_versions': {'甲': 'STD-ch3b', '乙': 'STD-ch3'}, 'analyst_a': 'AN1', 'analyst_b': 'AN2'}]}))
    assert [r['status'] for r in coelute.adjudicate(db, quant)] == ['低于定量限', '可放行']
    b = json.loads(conn.execute("SELECT body FROM released_result WHERE id='result:RB2'").fetchone()[0])
    a = json.loads(conn.execute("SELECT body FROM released_result WHERE id='result:RA2'").fetchone()[0])
    assert b['concentration'] == 2.5 and a['concentration'] == 0.5
    assert conn.execute("SELECT status FROM release_manifest WHERE id='B1'").fetchone()[0] == '撤回'
    before = conn.execute('SELECT count(*) FROM mixed_adjudication').fetchone()[0]
    coelute.adjudicate(db, quant)
    coelute.correct(db, correction)
    assert conn.execute('SELECT count(*) FROM mixed_adjudication').fetchone()[0] == before
    assert conn.execute("SELECT status FROM released_result WHERE id='result:RB2'").fetchone()[0] == '可放行'


def test_no_feasible_combination_rejected(tmp_path):
    db = tmp_path / 'db'
    coelute.ingest(db, write(tmp_path, 'mixed.json', {'version': 'v1', 'rows': [
        mixed_row(channels=[channel('ch1', 3, {'甲': 1, '乙': 1}), channel('ch2', 4, {'甲': 1, '乙': 1})])]}))
    coelute.contract(db, write(tmp_path, 'contracts.json', {'version': 'v1', 'rows': [
        {'id': 'CT-R', 'kind': '常规', 'primary_channels': ['ch1', 'ch2']}]}))
    result = coelute.adjudicate(db, write(tmp_path, 'quant.json', {'version': 'v1', 'rows': [
        {'id': 'RQ1', 'mixed_peak': 'M1', 'contract': 'CT-R', 'results': {'甲': 'RA', '乙': 'RB'}}]}))
    assert [(r['status'], r['reason']) for r in result] == [('拒放', 'no_feasible_combination')] * 2


def test_bounded_solution_releases_when_above_limit(tmp_path):
    db = tmp_path / 'db'
    coelute.ingest(db, write(tmp_path, 'mixed.json', {'version': 'v1', 'rows': [
        mixed_row(channels=[channel('ch1', 5, {'甲': 1, '乙': 1})],
                  ranges={'甲': [2, 3], '乙': [0, 3]}, quant_limits={'甲': 0.5, '乙': 0.5})]}))
    coelute.contract(db, write(tmp_path, 'contracts.json', {'version': 'v1', 'rows': [
        {'id': 'CT-R', 'kind': '常规', 'primary_channels': ['ch1']}]}))
    result = coelute.adjudicate(db, write(tmp_path, 'quant.json', {'version': 'v1', 'rows': [
        {'id': 'RQ1', 'mixed_peak': 'M1', 'contract': 'CT-R', 'results': {'甲': 'RA', '乙': 'RB'}}]}))
    assert [(r['status'], r['reason']) for r in result] == [('可放行', 'bounded_solution')] * 2
    conn = connect(db)
    body = json.loads(conn.execute("SELECT body FROM released_result WHERE id='result:RA'").fetchone()[0])
    assert body['concentration'] == [2, 3] and body['bounds'] == [2, 3]


def test_invalid_mixed_export_and_contract_rejected(tmp_path):
    db = tmp_path / 'db'
    row = mixed_row(channels=base_channels())
    row['substances'] = ['甲', '乙', '丙']
    assert coelute.ingest(db, write(tmp_path, 'mixed.json', {'version': 'v1', 'rows': [row]}))[0]['reason'] == 'substance_count'
    assert coelute.contract(db, write(tmp_path, 'contracts.json', {'version': 'v1', 'rows': [
        {'id': 'CT-X', 'kind': '特快', 'primary_channels': ['ch1']}]}))[0]['reason'] == 'contract_kind'
    coelute.contract(db, write(tmp_path, 'contracts2.json', {'version': 'v1', 'rows': [
        {'id': 'CT-R', 'kind': '常规', 'primary_channels': ['ch1', 'ch2']}]}))
    coelute.ingest(db, write(tmp_path, 'mixed2.json', {'version': 'v1', 'rows': [
        mixed_row('M2', injection='I10', sample='SM2',
                  channels=[channel('ch1', 3, {'甲': 1, '乙': 1}), channel('ch2', 5, {'甲': 1, '乙': 2})])]}))
    result = coelute.adjudicate(db, write(tmp_path, 'quant.json', {'version': 'v1', 'rows': [
        {'id': 'RQ1', 'mixed_peak': 'M1', 'contract': 'CT-R', 'results': {'甲': 'RA'}},
        {'id': 'RQ2', 'mixed_peak': 'M2', 'contract': 'CT-X', 'results': {'甲': 'RA2'}}]}))
    assert [(r['status'], r['reason']) for r in result] == [
        ('拒放', 'mixed_peak_invalid'), ('拒放', 'contract_invalid')]


def test_custody_quarantine_recalls_released_mixed_result(tmp_path):
    db = tmp_path / 'db'
    coelute.ingest(db, write(tmp_path, 'mixed.json', {'version': 'v1', 'rows': [
        mixed_row(channels=[channel('ch1', 3, {'甲': 1, '乙': 1}), channel('ch2', 5, {'甲': 1, '乙': 2})],
                  quant_limits={'甲': 0.5, '乙': 0.5})]}))
    operations.second(db, write(tmp_path, 'sample.json', {'version': 'v1', 'rows': [
        {'id': 'SM1', 'injection': 'I9', 'calibration': 'CAL1', 'window': [4.5, 5.5],
         'peaks': [{'time': 5.0, 'area': 20}]}]}))
    coelute.contract(db, write(tmp_path, 'contracts.json', {'version': 'v1', 'rows': [
        {'id': 'CT-R', 'kind': '常规', 'primary_channels': ['ch1', 'ch2']}]}))
    coelute.adjudicate(db, write(tmp_path, 'quant.json', {'version': 'v1', 'rows': [
        {'id': 'RQ1', 'mixed_peak': 'M1', 'contract': 'CT-R', 'results': {'甲': 'RA', '乙': 'RB'}}]}))
    assert batch.release(db, write(tmp_path, 'batch.json', {'version': 'v1', 'rows': [
        {'id': 'B1', 'results': ['RA', 'RB']}]}))[0]['status'] == '已封账'
    broken = {'id': 'H1', 'sample': 'SM1', 'origin': 'collector', 'destination': 'lab', 'temperature_limit': 8,
              'transfers': [{'from': 'collector', 'to': 'lab', 'at': '2026-01-01T11:00:00',
                             'seal_intact': False, 'max_temperature': 5}]}
    assert custody.assess(db, write(tmp_path, 'handoff.json', {'version': 'v1', 'rows': [broken]}))[0]['reason'] == 'seal_broken'
    conn = connect(db)
    assert conn.execute("SELECT status FROM released_result WHERE id='result:RA'").fetchone()[0] == '撤回'
    assert conn.execute("SELECT status FROM release_manifest WHERE id='B1'").fetchone()[0] == '撤回'
    result = coelute.adjudicate(db, write(tmp_path, 'quant2.json', {'version': 'v1', 'rows': [
        {'id': 'RQ2', 'mixed_peak': 'M1', 'contract': 'CT-R', 'results': {'甲': 'RA2', '乙': 'RB2'}}]}))
    assert [(r['status'], r['reason']) for r in result] == [('拒放', 'custody_unqualified')] * 2
