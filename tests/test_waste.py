import json
from pathlib import Path

from spectracapture import report, waste
from spectracapture.store import connect


def write(root, name, obj):
    p = root / name
    p.write_text(json.dumps(obj), encoding='utf-8')
    return p


RULES = {'version': 'v1', 'rows': [
    {'id': 'CAT-ACID', 'kind': 'category', 'category': 'acid', 'label': '酸性清洗液'},
    {'id': 'CAT-NEUT', 'kind': 'category', 'category': 'neutralizer', 'label': '中和剂'},
    {'id': 'CAT-OX', 'kind': 'category', 'category': 'oxidizer', 'label': '氧化性废液'},
    {'id': 'NEUT-1', 'kind': 'neutralizer', 'category': 'neutralizer', 'max_single_dose_l': 2.0},
    {'id': 'R-CONSUME', 'kind': 'consumes', 'agent': 'neutralizer', 'target': 'acid', 'ratio': 1.0},
    {'id': 'R-GAS', 'kind': 'gas_reaction', 'category': 'oxidizer', 'with': 'acid', 'threshold_l': 0.05},
    {'id': 'R-HEAT', 'kind': 'exotherm_limit', 'agent': 'neutralizer', 'target': 'acid', 'max_reacted_l': 1.5},
    {'id': 'R-MAT', 'kind': 'material_limit', 'material': 'steel', 'category': 'oxidizer'},
]}


def container(id='W1', volume=3, capacity=5, sealed=True, material='hdpe'):
    return {'version': 'v1', 'rows': [{'id': id, 'volume_l': volume, 'capacity_l': capacity,
                                       'sealed': sealed, 'material': material}]}


def pour(eid, category, amount, after, cid='W1', **extra):
    row = {'id': eid, 'kind': 'pour', 'container': cid, 'category': category,
           'source_bottle': f'B-{eid}', 'amount_l': amount, 'operator': 'op-chen', 'after': after}
    row.update(extra)
    return row


def neutralize(eid, amount, after, cid='W1', **extra):
    row = {'id': eid, 'kind': 'neutralize', 'container': cid, 'category': 'neutralizer',
           'amount_l': amount, 'operator': 'op-li', 'after': after}
    row.update(extra)
    return row


def seal(eid, after, cid='W1'):
    return {'id': eid, 'kind': 'seal', 'container': cid, 'operator': 'op-li', 'after': after}


def proof(pid, order, cid='W1', device='PUMP-3', signed_by='sup-wang'):
    return {'version': 'v1', 'rows': [
        {'id': pid, 'kind': 'sequence_proof', 'container': cid, 'device': device,
         'signed_by': signed_by,
         'entries': [{'event': e, 'seq': (i + 1) * 10} for i, e in enumerate(order)]}]}


def scenario_events(neut_amount=(1.0, 1.0), cid='W1'):
    # 文件内顺序与 at 时间都暗示氧化性废液在先，两者均不得作为实物顺序依据
    return {'version': 'v1', 'rows': [
        pour('E1', 'acid', [1.0, 1.0], [], cid, at='2026-11-20T09:00:00'),
        pour('E3', 'oxidizer', [1.0, 1.0], ['E1'], cid, at='2026-11-20T09:20:00'),
        neutralize('E2', list(neut_amount), ['E1'], cid, at='2026-11-20T09:30:00'),
        seal('E4', ['E2', 'E3'], cid),
    ]}


def only_container(results, key='W1'):
    return [r for r in results if r['subject'] == key][0]


def test_basic_waste_handoff(tmp_path):
    db = tmp_path / 'ledger.db'
    file = tmp_path / 'waste.json'
    file.write_text(json.dumps({'version': 'v1', 'rows': [
        {'id': 'W1', 'volume_l': 4, 'capacity_l': 5, 'sealed': True},
        {'id': 'W2', 'volume_l': 6, 'capacity_l': 5, 'sealed': True},
    ]}), encoding='utf-8')
    result = waste.assess(db, file)
    assert [x['status'] for x in result] == ['可交运', '待核']
    assert waste.assess(db, file) == result
    with connect(db) as con:
        assert con.execute('SELECT count(*) FROM waste_container').fetchone()[0] == 2


def test_unordered_pours_hold_transport_but_allow_remediation(tmp_path):
    db = tmp_path / 'db'
    waste.rules(db, write(tmp_path, 'rules.json', RULES))
    first = waste.assess(db, write(tmp_path, 'container.json', container()))
    assert first[0]['status'] == '可交运' and first[0]['reason'] == 'volume_and_seal_ok'
    # 倒入记录到达后，交运合同不得再只看封口与容量
    result = waste.events(db, write(tmp_path, 'events.json', scenario_events()))
    re_eval = only_container(result)
    assert re_eval['transport'] == '待核' and re_eval['transport_reason'] == 'unresolved_process'
    assert re_eval['remediation'] == '可整改'
    assert re_eval['disposition'] == 2
    with connect(db) as con:
        body = json.loads(con.execute('SELECT body FROM waste_disposition ORDER BY rowid DESC LIMIT 1')
                          .fetchone()['body'])
        # 两种可行顺序终态相同（酸均被消耗），但其中一种中途产气越限
        assert sorted(map(tuple, body['feasible_orders'])) == \
            sorted(map(tuple, [['E1', 'E2', 'E3', 'E4'], ['E1', 'E3', 'E2', 'E4']]))
        assert body['final_state']['acid'] == [0.0, 0.0]
        assert con.execute('SELECT count(*) FROM waste_incident').fetchone()[0] == 0
        order = con.execute("SELECT * FROM waste_transport_order WHERE id='W1:T1'").fetchone()
        assert order['status'] == '撤回' and order['reason'] == 'unresolved_process'


def test_neutralizer_first_proof_clears_transport(tmp_path):
    db = tmp_path / 'db'
    waste.rules(db, write(tmp_path, 'rules.json', RULES))
    waste.assess(db, write(tmp_path, 'container.json', container()))
    waste.events(db, write(tmp_path, 'events.json', scenario_events()))
    result = waste.events(db, write(tmp_path, 'proof.json', proof('P1', ['E1', 'E2', 'E3', 'E4'])))
    re_eval = only_container(result)
    assert re_eval['transport'] == '可交运' and re_eval['transport_reason'] == 'process_and_state_ok'
    assert re_eval['remediation'] == '可整改'
    assert re_eval['disposition'] == 3
    with connect(db) as con:
        orders = con.execute('SELECT * FROM waste_transport_order ORDER BY rowid').fetchall()
        assert [(o['id'], o['status']) for o in orders] == [('W1:T1', '撤回'), ('W1:T2', '有效')]
        assert con.execute('SELECT count(*) FROM waste_incident').fetchone()[0] == 0
        assert con.execute('SELECT count(*) FROM waste_disposition').fetchone()[0] == 3
    # 重放证明与容器导出：幂等，不生成新处置版
    assert waste.events(db, write(tmp_path, 'proof2.json', proof('P1', ['E1', 'E2', 'E3', 'E4']))) == result
    again = waste.assess(db, write(tmp_path, 'container2.json', container()))
    assert again[0]['disposition'] == 3 and again[0]['transport'] == '可交运'
    with connect(db) as con:
        assert con.execute('SELECT count(*) FROM waste_disposition').fetchone()[0] == 3


def test_oxidizer_first_proof_records_incident_and_revokes_transport(tmp_path):
    db = tmp_path / 'db'
    waste.rules(db, write(tmp_path, 'rules.json', RULES))
    first = waste.assess(db, write(tmp_path, 'container.json', container()))
    assert first[0]['status'] == '可交运'
    payload = scenario_events()
    payload['rows'] += proof('P2', ['E1', 'E3', 'E2', 'E4'])['rows']
    result = waste.events(db, write(tmp_path, 'events.json', payload))
    re_eval = only_container(result)
    assert re_eval['transport'] == '不可交运' and re_eval['transport_reason'] == 'incident_confirmed'
    assert re_eval['remediation'] == '可整改'
    assert re_eval['incidents'] == ['W1:R-GAS:E3']
    with connect(db) as con:
        incident = con.execute("SELECT * FROM waste_incident WHERE id='W1:R-GAS:E3'").fetchone()
        assert incident['status'] == '已立案'
        order = con.execute("SELECT * FROM waste_transport_order WHERE id='W1:T1'").fetchone()
        assert order['status'] == '撤回' and order['reason'] == 'incident_confirmed'
        dispositions = con.execute('SELECT * FROM waste_disposition ORDER BY rowid').fetchall()
        assert len(dispositions) == 2
        assert json.loads(dispositions[0]['body'])['transport'] == '可交运'
        assert json.loads(dispositions[1]['body'])['transport'] == '不可交运'


def test_incident_survives_later_neutralization(tmp_path):
    db = tmp_path / 'db'
    waste.rules(db, write(tmp_path, 'rules.json', RULES))
    waste.assess(db, write(tmp_path, 'container.json', container()))
    payload = scenario_events()
    payload['rows'] += proof('P2', ['E1', 'E3', 'E2', 'E4'])['rows']
    waste.events(db, write(tmp_path, 'events.json', payload))
    # 事后补充中和记录：容器仍可整改，但事故证据不可抹除，交运资格不恢复
    late = {'version': 'v2', 'rows': [
        {'id': 'E5', 'kind': 'neutralize', 'container': 'W1', 'category': 'neutralizer',
         'amount_l': [0.5, 0.5], 'operator': 'op-li', 'after': ['E3'], 'observation': '补充中和'}]}
    result = waste.events(db, write(tmp_path, 'late.json', late))
    re_eval = only_container(result)
    assert re_eval['transport'] == '不可交运'
    assert re_eval['remediation'] == '可整改'
    assert re_eval['incidents'] == ['W1:R-GAS:E3']
    with connect(db) as con:
        assert con.execute("SELECT status FROM waste_incident WHERE id='W1:R-GAS:E3'").fetchone()[0] == '已立案'
        assert con.execute("SELECT status FROM waste_transport_order WHERE id='W1:T1'").fetchone()[0] == '撤回'


def test_out_of_order_arrival_does_not_change_outcome(tmp_path):
    rows = scenario_events()['rows']
    forward = {'version': 'v1', 'rows': [rows[0], rows[2], rows[1], rows[3]]}   # E1,E2,E3,E4
    backward = {'version': 'v1', 'rows': [rows[3], rows[1], rows[2], rows[0]]}  # E4,E3,E2,E1
    outcomes = []
    for name, payload in (('forward', forward), ('backward', backward)):
        root = tmp_path / name
        root.mkdir()
        db = root / 'db'
        waste.rules(db, write(root, 'rules.json', RULES))
        waste.assess(db, write(root, 'container.json', container()))
        waste.events(db, write(root, 'events.json', payload))
        result = waste.events(db, write(root, 'proof.json', proof('P1', ['E1', 'E2', 'E3', 'E4'])))
        with connect(db) as con:
            dispositions = [json.loads(x['body']) for x in
                            con.execute('SELECT body FROM waste_disposition ORDER BY rowid')]
        outcomes.append((only_container(result), dispositions))
    assert outcomes[0] == outcomes[1]
    assert outcomes[0][0]['transport'] == '可交运'


def test_events_arriving_before_container_export(tmp_path):
    db = tmp_path / 'db'
    waste.rules(db, write(tmp_path, 'rules.json', RULES))
    result = waste.events(db, write(tmp_path, 'events.json', scenario_events()))
    assert all(r['subject'] != 'W1' for r in result)
    waste.events(db, write(tmp_path, 'proof.json', proof('P1', ['E1', 'E2', 'E3', 'E4'])))
    result = waste.assess(db, write(tmp_path, 'container.json', container()))
    assert result[0]['transport'] == '可交运' and result[0]['disposition'] == 1


def test_dose_range_crossing_reaction_threshold_holds_transport(tmp_path):
    db = tmp_path / 'db'
    waste.rules(db, write(tmp_path, 'rules.json', RULES))
    waste.assess(db, write(tmp_path, 'container.json', container()))
    waste.events(db, write(tmp_path, 'events.json', scenario_events(neut_amount=(0.8, 1.2))))
    result = waste.events(db, write(tmp_path, 'proof.json', proof('P1', ['E1', 'E2', 'E3', 'E4'])))
    re_eval = only_container(result)
    # 名义剂量 1.0 恰好够中和 1 升酸，但区间下限 0.8 可能留下超过阈值的活性酸
    assert re_eval['transport'] == '待核' and re_eval['transport_reason'] == 'unresolved_process'
    with connect(db) as con:
        body = json.loads(con.execute('SELECT body FROM waste_disposition ORDER BY rowid DESC LIMIT 1')
                          .fetchone()['body'])
        assert body['final_state']['acid'] == [0.0, 0.2]
        assert con.execute('SELECT count(*) FROM waste_incident').fetchone()[0] == 0


def test_dose_range_fully_above_threshold_clears_transport(tmp_path):
    db = tmp_path / 'db'
    waste.rules(db, write(tmp_path, 'rules.json', RULES))
    waste.assess(db, write(tmp_path, 'container.json', container()))
    waste.events(db, write(tmp_path, 'events.json', scenario_events(neut_amount=(1.0, 1.2))))
    result = waste.events(db, write(tmp_path, 'proof.json', proof('P1', ['E1', 'E2', 'E3', 'E4'])))
    re_eval = only_container(result)
    assert re_eval['transport'] == '可交运' and re_eval['transport_reason'] == 'process_and_state_ok'


def test_duplicate_scans_are_idempotent(tmp_path):
    db = tmp_path / 'db'
    waste.rules(db, write(tmp_path, 'rules.json', RULES))
    waste.assess(db, write(tmp_path, 'container.json', container()))
    file = write(tmp_path, 'events.json', scenario_events())
    first = waste.events(db, file)
    assert waste.events(db, file) == first
    rescan = scenario_events()
    rescan['note'] = 'duplicate scan exported again'
    third = waste.events(db, write(tmp_path, 'rescan.json', rescan))
    assert only_container(third) == only_container(first)
    with connect(db) as con:
        assert con.execute('SELECT count(*) FROM waste_event').fetchone()[0] == 4
        assert con.execute('SELECT count(*) FROM waste_disposition').fetchone()[0] == 2


def test_conflicting_signed_proofs_report_minimal_conflict(tmp_path):
    db = tmp_path / 'db'
    waste.rules(db, write(tmp_path, 'rules.json', RULES))
    waste.assess(db, write(tmp_path, 'container.json', container()))
    waste.events(db, write(tmp_path, 'events.json', scenario_events()))
    both = {'version': 'v1', 'rows': [
        proof('P1', ['E1', 'E2', 'E3', 'E4'], signed_by='sup-wang')['rows'][0],
        proof('P2', ['E1', 'E3', 'E2', 'E4'], signed_by='sup-zhao')['rows'][0]]}
    result = waste.events(db, write(tmp_path, 'proofs.json', both))
    re_eval = only_container(result)
    # 无可行过程：明确报证据冲突与最小冲突记录，不任选一种顺序
    assert re_eval['transport'] == '待核' and re_eval['transport_reason'] == 'evidence_conflict'
    assert re_eval['remediation'] == '待核'
    with connect(db) as con:
        issue = con.execute("SELECT * FROM issue WHERE code='waste_evidence_conflict'").fetchone()
        assert json.loads(issue['detail'])['records'] == ['P1', 'P2']
        body = json.loads(con.execute('SELECT body FROM waste_disposition ORDER BY rowid DESC LIMIT 1')
                          .fetchone()['body'])
        assert body['conflict'] == ['P1', 'P2'] and body['feasible_orders'] == []
        assert con.execute('SELECT count(*) FROM waste_incident').fetchone()[0] == 0


def test_contradictory_signed_order_fields_conflict(tmp_path):
    db = tmp_path / 'db'
    waste.rules(db, write(tmp_path, 'rules.json', RULES))
    waste.assess(db, write(tmp_path, 'container.json', container()))
    rows = scenario_events()['rows']
    rows[1]['after'] = ['E1', 'E2']  # E3 声称在 E2 之后
    rows[2]['after'] = ['E1', 'E3']  # E2 声称在 E3 之后
    result = waste.events(db, write(tmp_path, 'events.json', {'version': 'v1', 'rows': rows}))
    re_eval = only_container(result)
    assert re_eval['transport'] == '待核' and re_eval['transport_reason'] == 'evidence_conflict'
    with connect(db) as con:
        issue = con.execute("SELECT * FROM issue WHERE code='waste_evidence_conflict'").fetchone()
        assert json.loads(issue['detail'])['records'] == ['E2', 'E3']


def test_remediation_route_when_acid_remains(tmp_path):
    db = tmp_path / 'db'
    waste.rules(db, write(tmp_path, 'rules.json', RULES))
    waste.assess(db, write(tmp_path, 'container.json', container(volume=3.5)))
    events = {'version': 'v1', 'rows': [
        pour('E1', 'acid', [1.5, 1.5], []),
        pour('E3', 'oxidizer', [1.0, 1.0], ['E1']),
        neutralize('E2', [1.0, 1.0], ['E1']),
        seal('E4', ['E2', 'E3'])]}
    waste.events(db, write(tmp_path, 'events.json', events))
    result = waste.events(db, write(tmp_path, 'proof.json', proof('P1', ['E1', 'E2', 'E3', 'E4'])))
    re_eval = only_container(result)
    # 中和剂不足：残留 0.5 升活性酸使氧化性废液倒入时必然产气，两种顺序都越限
    assert re_eval['transport'] == '不可交运'
    assert re_eval['remediation'] == '可整改' and re_eval['remediation_reason'] == 'remediation_route_available'
    assert re_eval['operations'] == [
        {'op': 'neutralize', 'agent': 'neutralizer', 'target': 'acid', 'dose_l': 0.45}]


def test_incompatible_container_material_blocks_both_contracts(tmp_path):
    db = tmp_path / 'db'
    waste.rules(db, write(tmp_path, 'rules.json', RULES))
    waste.assess(db, write(tmp_path, 'container.json', container(material='steel')))
    waste.events(db, write(tmp_path, 'events.json', scenario_events()))
    result = waste.events(db, write(tmp_path, 'proof.json', proof('P1', ['E1', 'E2', 'E3', 'E4'])))
    re_eval = only_container(result)
    assert re_eval['transport'] == '不可交运'
    assert re_eval['remediation'] == '不可整改' and re_eval['remediation_reason'] == 'isolation_unsafe'
    with connect(db) as con:
        body = json.loads(con.execute("SELECT body FROM waste_incident WHERE id='W1:R-MAT:E3'").fetchone()['body'])
        assert body['hazard'] == 'material'


def test_exotherm_limit_violation_records_incident(tmp_path):
    db = tmp_path / 'db'
    waste.rules(db, write(tmp_path, 'rules.json', RULES))
    waste.assess(db, write(tmp_path, 'container.json', container(volume=4, capacity=6)))
    events = {'version': 'v1', 'rows': [
        pour('E1', 'acid', [2.0, 2.0], []),
        neutralize('E2', [2.0, 2.0], ['E1']),
        seal('E4', ['E2'])]}
    result = waste.events(db, write(tmp_path, 'events.json', events))
    re_eval = only_container(result)
    # 一步反应 2 升超过放热限值 1.5 升：唯一可行过程必然越限
    assert re_eval['transport'] == '不可交运' and re_eval['transport_reason'] == 'incident_confirmed'
    assert re_eval['incidents'] == ['W1:R-HEAT:E2']
    assert re_eval['remediation'] == '可整改'


def test_mid_process_overfill_records_incident(tmp_path):
    db = tmp_path / 'db'
    waste.rules(db, write(tmp_path, 'rules.json', RULES))
    waste.assess(db, write(tmp_path, 'container.json', container(volume=3, capacity=2.5)))
    result = waste.events(db, write(tmp_path, 'events.json', scenario_events()))
    re_eval = only_container(result)
    # 总量 3 升超过 2.5 升容量：每种顺序都必然越限，但越限步骤取决于未定的顺序
    assert re_eval['transport'] == '不可交运'
    assert re_eval['incidents'] == ['W1:process_violation']
    assert re_eval['remediation'] == '不可整改' and re_eval['remediation_reason'] == 'isolation_unsafe'


def test_events_without_rules_hold_for_review(tmp_path):
    db = tmp_path / 'db'
    waste.assess(db, write(tmp_path, 'container.json', container()))
    result = waste.events(db, write(tmp_path, 'events.json', scenario_events()))
    re_eval = only_container(result)
    assert re_eval['transport'] == '待核' and re_eval['transport_reason'] == 'rules_missing'
    assert re_eval['remediation'] == '待核'


def test_shipped_samples_run_the_minimal_scenario(tmp_path):
    samples = Path(__file__).resolve().parent.parent / 'samples' / 'waste'
    db = tmp_path / 'db'
    waste.rules(db, samples / 'rules.json')
    first = waste.assess(db, samples / 'container.json')
    assert first[0]['status'] == '可交运'
    held = only_container(waste.events(db, samples / 'events_unordered.json'))
    assert held['transport'] == '待核' and held['remediation'] == '可整改'
    cleared = only_container(waste.events(db, samples / 'proof_neutralizer_first.json'))
    assert cleared['transport'] == '可交运'
    # 另一分支：设备顺序证明氧化性废液在先 → 事故记录 + 不可交运 + 可整改
    db2 = tmp_path / 'db2'
    waste.rules(db2, samples / 'rules.json')
    waste.assess(db2, samples / 'container.json')
    waste.events(db2, samples / 'events_unordered.json')
    blocked = only_container(waste.events(db2, samples / 'proof_oxidizer_first.json'))
    assert blocked['transport'] == '不可交运' and blocked['remediation'] == '可整改'
    assert blocked['incidents'] == ['W1:R-GAS:E3']


def test_report_surfaces_waste_contracts(tmp_path):
    db = tmp_path / 'db'
    waste.rules(db, write(tmp_path, 'rules.json', RULES))
    waste.assess(db, write(tmp_path, 'container.json', container()))
    payload = scenario_events()
    payload['rows'] += proof('P2', ['E1', 'E3', 'E2', 'E4'])['rows']
    waste.events(db, write(tmp_path, 'events.json', payload))
    data = json.loads(report.build(db, tmp_path / 'report.json').read_text(encoding='utf-8'))
    summary = data['waste']['W1']
    assert summary['transport'] == '不可交运' and summary['remediation'] == '可整改'
    assert summary['incidents'] == ['W1:R-GAS:E3']
    assert [o['status'] for o in summary['orders']] == ['撤回']
    assert summary['dispositions'] == 2
    assert len(data['entities']['waste_event']) == 5
