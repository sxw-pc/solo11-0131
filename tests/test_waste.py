import json
from spectracapture import waste
from spectracapture.store import connect


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
