"""Instrument source authenticity tests.

Covers out-of-order chain completion, device forks with successor freezing, key
rotation, late revocation bulletins driven by sequence ranges (never export wall-clock
time), cross-device isolation, canonical replay idempotency and explicit rejection
reasons. The two registered contracts are always asserted separately.
"""
import json

import pytest

from spectracapture import operations, report, trust
from spectracapture.ingest import capture, verify_source
from spectracapture.store import connect


# --------------------------------------------------------------------------- helpers

def write(root, name, obj, *, indent=None, extra_spaces=False):
    p = root / name
    text = json.dumps(obj, indent=indent)
    if extra_spaces:
        text = text.replace('{', '{ ').replace('}', ' }')
    p.write_text(text, encoding='utf-8')
    return p


def make_keys():
    root = trust.generate_private_key()
    return root, trust.public_bytes(root)


def install_root(db_path, instrument='instrument-A', root_pub=None):
    if root_pub is None:
        _, root_pub = make_keys()
    db = connect(db_path)
    try:
        with db:
            trust.import_trust_root(db, instrument, root_pub)
    finally:
        db.close()
    return root_pub


def export_payload(seq, data='chromatogram'):
    return {'version': 'v1', 'rows': [{'id': f'PK-{seq}', 'area': 100 + seq,
                                       'note': data}]}


def sign_chain(root_priv, instrument, pairs, *, start=1, start_prev=None):
    """pairs: list of (sequence, payload, private_key). Returns envelope list."""
    out = []
    prev = start_prev
    key_cache = {}
    for item in pairs:
        seq, payload, priv = item
        key_id = trust.ROOT_KEY_ID if priv is root_priv else key_cache[id(priv)]
        out.append(trust.sign_export(priv, instrument, seq, prev, payload, key_id))
        prev = trust.payload_digest(payload)
    return out


def verdict(db_path, source_id):
    db = connect(db_path)
    try:
        return trust.source_verdict(db, source_id)
    finally:
        db.close()


def capture_env(db_path, root, name, envelope, kind='desktop_sample_export'):
    return capture(db_path, kind, write(root, name, envelope))[0]


def cal_rows():
    return {'version': 'v1', 'rows': [{
        'id': 'CAL1',
        'points': [{'concentration': 1, 'area': 10},
                   {'concentration': 2, 'area': 20},
                   {'concentration': 3, 'area': 30}],
        'blank_area': 0, 'max_blank': 2, 'max_residual': 1,
        'at': '2026-01-01T10:00:00', 'valid_until': '2026-01-02T00:00:00'}]}


def sample_rows():
    return {'version': 'v1', 'rows': [{'id': 'S1', 'injection': 'I1',
                                       'calibration': 'CAL1', 'window': [4.5, 5.5],
                                       'peaks': [{'time': 5.0, 'area': 20}]}]}


def result_rows(rid='R1'):
    return {'version': 'v1', 'rows': [{'id': rid, 'sample': 'S1',
                                       'calibration': 'CAL1', 'quant_limit': 1.5,
                                       'at': '2026-01-01T12:00:00'}]}


# --------------------------------------------------------------------------- 1. out of order

def test_out_of_order_arrival_completes_chain(tmp_path):
    db = tmp_path / 'db'
    root_priv, root_pub = make_keys()
    install_root(db, 'instrument-A', root_pub)
    p1, p2, p3 = (export_payload(1), export_payload(2), export_payload(3))
    e1, e2, e3 = sign_chain(root_priv, 'instrument-A',
                            [(1, p1, root_priv), (2, p2, root_priv), (3, p3, root_priv)])

    s1 = capture_env(db, tmp_path, 'e1.json', e1)
    assert verdict(db, s1)['archive']['status'] == trust.ARCHIVE_VERIFIABLE
    # Sequence 3 arrives first: predecessor missing is a pending chain, not forgery.
    s3 = capture_env(db, tmp_path, 'e3.json', e3)
    v3 = verdict(db, s3)
    assert v3['archive'] == {'status': trust.ARCHIVE_PENDING,
                             'reason': trust.REASON_PENDING_PREDECESSOR}
    assert v3['business']['status'] == trust.BUSINESS_PENDING
    # Sequence 2 then 3 link up; 3 moves from pending to verifiable.
    s2 = capture_env(db, tmp_path, 'e2.json', e2)
    assert verdict(db, s2)['archive']['status'] == trust.ARCHIVE_VERIFIABLE
    assert verdict(db, s3)['archive']['status'] == trust.ARCHIVE_VERIFIABLE
    assert verdict(db, s3)['business']['status'] == trust.BUSINESS_USABLE


# --------------------------------------------------------------------------- 2. device fork

def test_duplicate_sequence_with_two_valid_payloads_is_a_fork(tmp_path):
    db = tmp_path / 'db'
    root_priv, root_pub = make_keys()
    install_root(db, 'instrument-A', root_pub)
    p1, p2a, p2b, p3 = (export_payload(1), export_payload(2, 'run-a'),
                        export_payload(2, 'run-b'), export_payload(3))
    e1 = trust.sign_export(root_priv, 'instrument-A', 1, None, p1)
    d1 = trust.payload_digest(p1)
    e2a = trust.sign_export(root_priv, 'instrument-A', 2, d1, p2a)
    e2b = trust.sign_export(root_priv, 'instrument-A', 2, d1, p2b)
    e3 = trust.sign_export(root_priv, 'instrument-A', 3,
                           trust.payload_digest(p2a), p3)

    s1 = capture_env(db, tmp_path, 'e1.json', e1)
    s2a = capture_env(db, tmp_path, 'e2a.json', e2a)
    s2b = capture_env(db, tmp_path, 'e2b.json', e2b)
    s3 = capture_env(db, tmp_path, 'e3.json', e3)

    # Both copies pass cryptographic verification and stay as archive evidence.
    for sid in (s2a, s2b):
        v = verdict(db, sid)
        assert v['archive']['status'] == trust.ARCHIVE_VERIFIABLE
        assert v['business'] == {'status': trust.BUSINESS_FROZEN,
                                 'reason': trust.REASON_DEVICE_FORK}
    # Fork is recorded, not resolved by submission clock.
    conn = connect(db)
    forks = conn.execute('SELECT * FROM device_fork').fetchall()
    assert len(forks) == 1 and forks[0]['sequence'] == 2
    # The successor's archive is still checkable; business use is frozen.
    v3 = verdict(db, s3)
    assert v3['archive']['status'] == trust.ARCHIVE_VERIFIABLE
    assert v3['business'] == {'status': trust.BUSINESS_FROZEN,
                              'reason': trust.REASON_SUCCESSOR_OF_FORK}
    # Both raw copies are retained.
    assert conn.execute('SELECT count(*) FROM export_file').fetchone()[0] == 4
    conn.close()


def test_fork_does_not_erase_peak_area_or_concentration_bodies(tmp_path):
    db = tmp_path / 'db'
    root_priv, root_pub = make_keys()
    install_root(db, 'instrument-A', root_pub)
    cal = cal_rows()
    cal_env = trust.sign_export(root_priv, 'instrument-A', 1, None, cal)
    # Two distinct sample payloads at the same sequence, same predecessor digest.
    sa, sb = sample_rows(), sample_rows()
    sb['rows'][0]['id'] = 'S2'
    sb['rows'][0]['injection'] = 'I2'
    d_cal = trust.payload_digest(cal)
    env_a = trust.sign_export(root_priv, 'instrument-A', 2, d_cal, sa)
    env_b = trust.sign_export(root_priv, 'instrument-A', 2, d_cal, sb)
    operations.first(db, write(tmp_path, 'cal.json', cal_env))
    operations.second(db, write(tmp_path, 'sa.json', env_a))
    operations.second(db, write(tmp_path, 'sb.json', env_b))
    conn = connect(db)
    rows = {r['body'] and json.loads(r['body'])['id']: r
            for r in conn.execute("SELECT body,status,reason FROM sample_export ORDER BY id")}
    # The measured peak areas survive untouched even though business use is frozen.
    assert json.loads(rows['S1']['body'])['peaks'][0]['area'] == 20
    assert json.loads(rows['S2']['body'])['peaks'][0]['area'] == 20
    # The pre-fork judgment is not retroactively rewritten; the forked copy is held.
    assert rows['S1']['status'] == '已归属'
    assert rows['S2']['status'] == '待核'
    assert rows['S2']['reason'] == f'source_hold:{trust.REASON_DEVICE_FORK}'
    conn.close()


# --------------------------------------------------------------------------- 3. rotation + late revocation

def test_key_rotation_and_late_sequence_range_revocation(tmp_path):
    db = tmp_path / 'db'
    root_priv, root_pub = make_keys()
    install_root(db, 'instrument-A', root_pub)
    new_priv = trust.generate_private_key()
    rotation = trust.build_rotation('instrument-A', root_priv, trust.ROOT_KEY_ID,
                                    new_priv, 'key-2', 3, root_priv)
    p1, p2, p3, p4 = (export_payload(1), export_payload(2),
                      export_payload(3), export_payload(4))
    e1 = trust.sign_export(root_priv, 'instrument-A', 1, None, p1)
    e2 = trust.sign_export(root_priv, 'instrument-A', 2,
                           trust.payload_digest(p1), p2)
    # The instrument rotates keys; seq 3 and 4 use the new key.
    e3 = trust.sign_export(new_priv, 'instrument-A', 3,
                           trust.payload_digest(p2), p3, key_id='key-2')
    e4 = trust.sign_export(new_priv, 'instrument-A', 4,
                           trust.payload_digest(p3), p4, key_id='key-2')
    s1 = capture_env(db, tmp_path, 'e1.json', e1)
    s2 = capture_env(db, tmp_path, 'e2.json', e2)
    # Before the rotation proof arrives, the new key cannot validate seq 3.
    s3 = capture_env(db, tmp_path, 'e3.json', e3)
    assert verdict(db, s3)['archive']['reason'] == trust.REASON_KEY_UNKNOWN
    s4 = capture_env(db, tmp_path, 'e4.json', e4)
    conn = connect(db)
    with conn:
        rot_result = trust.import_rotation(conn, rotation)
    conn.close()
    assert rot_result['status'] == 'active'
    assert verdict(db, s3)['archive']['status'] == trust.ARCHIVE_VERIFIABLE
    assert verdict(db, s4)['business']['status'] == trust.BUSINESS_USABLE

    # Late key-theft bulletin covering seq 2..3. No export wall-clock time involved.
    bulletin = trust.build_revocation('instrument-A', 2, 3, root_priv,
                                      bulletin_id='bul-2026-001',
                                      issuer='lab-security-office', key_id='key-2')
    conn = connect(db)
    with conn:
        rev = trust.import_revocation(conn, bulletin)
    conn.close()
    assert rev['reason'] == 'revocation_applied'
    assert verdict(db, s2)['business'] == {'status': trust.BUSINESS_FROZEN,
                                           'reason': trust.REASON_REVOKED}
    assert verdict(db, s3)['business'] == {'status': trust.BUSINESS_FROZEN,
                                           'reason': trust.REASON_REVOKED}
    # Outside the range stays usable; archive contract is unaffected by revocation.
    assert verdict(db, s1)['business']['status'] == trust.BUSINESS_USABLE
    assert verdict(db, s4)['business']['status'] == trust.BUSINESS_USABLE
    for sid in (s1, s2, s3, s4):
        assert verdict(db, sid)['archive']['status'] == trust.ARCHIVE_VERIFIABLE

    # History keeps the pre-bulletin verdicts; nothing is rewritten away.
    conn = connect(db)
    history = conn.execute('SELECT * FROM source_verdict_history WHERE source_id=? '
                           'ORDER BY id', (s2,)).fetchall()
    reasons = [r['reason'] for r in history if r['contract'] == 'business']
    assert trust.REASON_CHAIN_VALID in reasons and trust.REASON_REVOKED in reasons
    conn.close()


def test_revocation_ignores_export_claimed_wall_clock(tmp_path):
    db = tmp_path / 'db'
    root_priv, root_pub = make_keys()
    install_root(db, 'instrument-A', root_pub)
    # Both files claim the same wall-clock time; only sequence numbers decide impact.
    p1 = dict(export_payload(1), captured_at='2020-01-01T00:00:00')
    p2 = dict(export_payload(2), captured_at='2020-01-01T00:00:00')
    e1 = trust.sign_export(root_priv, 'instrument-A', 1, None, p1)
    e2 = trust.sign_export(root_priv, 'instrument-A', 2,
                           trust.payload_digest(p1), p2)
    s1 = capture_env(db, tmp_path, 'e1.json', e1)
    s2 = capture_env(db, tmp_path, 'e2.json', e2)
    bulletin = trust.build_revocation('instrument-A', 2, 2, root_priv,
                                      bulletin_id='bul-time-001')
    conn = connect(db)
    with conn:
        trust.import_revocation(conn, bulletin)
    conn.close()
    assert verdict(db, s1)['business']['status'] == trust.BUSINESS_USABLE
    assert verdict(db, s2)['business']['reason'] == trust.REASON_REVOKED


# --------------------------------------------------------------------------- 4. isolation / replay / explicit rejects

def test_other_instrument_same_sequence_cannot_bridge_chain(tmp_path):
    db = tmp_path / 'db'
    priv_a, pub_a = make_keys()
    priv_b, pub_b = make_keys()
    install_root(db, 'instrument-A', pub_a)
    install_root(db, 'instrument-B', pub_b)
    pa1, pa2 = export_payload(1), export_payload(2)
    pb1 = export_payload(1)
    ea2 = trust.sign_export(priv_a, 'instrument-A', 2,
                            trust.payload_digest(pa1), pa2)
    eb1 = trust.sign_export(priv_b, 'instrument-B', 1, None, pb1)
    sb = capture_env(db, tmp_path, 'b1.json', eb1)
    sa = capture_env(db, tmp_path, 'a2.json', ea2)
    # B's sequence 1 must never serve as A's predecessor.
    va = verdict(db, sa)
    assert va['archive'] == {'status': trust.ARCHIVE_PENDING,
                             'reason': trust.REASON_PENDING_PREDECESSOR}
    assert verdict(db, sb)['archive']['status'] == trust.ARCHIVE_VERIFIABLE


def test_canonical_replay_with_whitespace_and_field_order_is_one_capture(tmp_path):
    db = tmp_path / 'db'
    root_priv, root_pub = make_keys()
    install_root(db, 'instrument-A', root_pub)
    payload = export_payload(1)
    envelope = trust.sign_export(root_priv, 'instrument-A', 1, None, payload)
    f1 = write(tmp_path, 'a.json', envelope)
    f2 = write(tmp_path, 'b.json', envelope, indent=4, extra_spaces=True)
    # Reorder keys while keeping the exact same canonical payload and proof.
    reordered = {'rows': envelope['rows'], 'version': envelope['version'],
                 'source_proof': envelope['source_proof']}
    f3 = write(tmp_path, 'c.json', reordered)
    s1 = capture(db, 'desktop_sample_export', f1)[0]
    s2 = capture(db, 'desktop_sample_export', f2)[0]
    s3 = capture(db, 'desktop_sample_export', f3)[0]
    assert s1 == s2 == s3
    conn = connect(db)
    assert conn.execute('SELECT count(*) FROM export_file').fetchone()[0] == 1
    assert conn.execute('SELECT count(*) FROM export_record').fetchone()[0] == 1
    conn.close()
    # verify-source on the re-spelled file reaches the same stored adjudication.
    result = verify_source(db, f2)
    assert result['archive']['status'] == trust.ARCHIVE_VERIFIABLE
    assert result['business']['status'] == trust.BUSINESS_USABLE


def test_forged_predecessor_digest_is_rejected_with_reason(tmp_path):
    db = tmp_path / 'db'
    root_priv, root_pub = make_keys()
    install_root(db, 'instrument-A', root_pub)
    p1, p2 = export_payload(1), export_payload(2)
    e1 = trust.sign_export(root_priv, 'instrument-A', 1, None, p1)
    e2 = trust.sign_export(root_priv, 'instrument-A', 2,
                           '0' * 64, p2)  # bogus predecessor
    capture_env(db, tmp_path, 'e1.json', e1)
    s2 = capture_env(db, tmp_path, 'e2.json', e2)
    v = verdict(db, s2)
    # Signature itself is valid, but the chain claim does not verify.
    assert v['archive'] == {'status': trust.ARCHIVE_REJECTED,
                            'reason': trust.REASON_PREV_DIGEST_MISMATCH}
    assert v['business']['status'] == trust.BUSINESS_REJECTED


def test_invalid_rotation_is_recorded_and_new_key_stays_untrusted(tmp_path):
    db = tmp_path / 'db'
    root_priv, root_pub = make_keys()
    install_root(db, 'instrument-A', root_pub)
    attacker_priv = trust.generate_private_key()
    forged = trust.build_rotation('instrument-A', root_priv, trust.ROOT_KEY_ID,
                                  attacker_priv, 'evil-key', 2, attacker_priv)
    conn = connect(db)
    with conn:
        result = trust.import_rotation(conn, forged)
    assert result['status'] == 'rejected'
    assert result['reason'] == trust.REASON_SIGNATURE_INVALID
    assert conn.execute("SELECT count(*) FROM signing_key WHERE key_id='evil-key'").fetchone()[0] == 0
    bad_row = conn.execute('SELECT status,reason FROM key_rotation').fetchone()
    assert bad_row['status'] == 'rejected'
    conn.close()

    payload = export_payload(1)
    env = trust.sign_export(attacker_priv, 'instrument-A', 1, None, payload,
                            key_id='evil-key')
    sid = capture_env(db, tmp_path, 'forged.json', env)
    assert verdict(db, sid)['archive']['reason'] == trust.REASON_KEY_UNKNOWN

    # A rotation that skips sequence order (from_sequence not ahead of the key) also fails.
    other_priv = trust.generate_private_key()
    skip = trust.build_rotation('instrument-A', root_priv, trust.ROOT_KEY_ID,
                                other_priv, 'skip-key', 1, root_priv)
    conn = connect(db)
    with conn:
        skip_result = trust.import_rotation(conn, skip)
    conn.close()
    assert skip_result['status'] == 'rejected'
    assert skip_result['reason'] == trust.REASON_SEQUENCE_ORDER


def test_tampered_signature_and_unsigned_file_are_rejected(tmp_path):
    db = tmp_path / 'db'
    root_priv, root_pub = make_keys()
    install_root(db, 'instrument-A', root_pub)
    payload = export_payload(1)
    env = trust.sign_export(root_priv, 'instrument-A', 1, None, payload)
    env['source_proof']['signature'] = trust.b64e(b'\x00' * 64)
    sid = capture_env(db, tmp_path, 'tampered.json', env)
    assert verdict(db, sid)['archive'] == {'status': trust.ARCHIVE_REJECTED,
                                           'reason': trust.REASON_SIGNATURE_INVALID}
    # Raw bytes are still preserved.
    conn = connect(db)
    assert conn.execute('SELECT hash FROM export_file WHERE id=?', (sid,)).fetchone() is not None
    conn.close()

    unsigned = export_payload(9)
    sid2 = capture_env(db, tmp_path, 'unsigned.json', unsigned)
    v = verdict(db, sid2)
    assert v['archive'] == {'status': trust.ARCHIVE_REJECTED,
                            'reason': trust.REASON_PROOF_MISSING}
    assert v['business']['status'] == trust.BUSINESS_REJECTED


# --------------------------------------------------------------------------- 5. business gate on operations + report

def test_verified_chain_supports_release_but_fork_freezes_new_results(tmp_path):
    db = tmp_path / 'db'
    root_priv, root_pub = make_keys()
    install_root(db, 'instrument-A', root_pub)
    cal = cal_rows()
    sample = sample_rows()
    cal_env = trust.sign_export(root_priv, 'instrument-A', 1, None, cal)
    sample_env = trust.sign_export(root_priv, 'instrument-A', 2,
                                   trust.payload_digest(cal), sample)
    r1 = operations.first(db, write(tmp_path, 'cal.json', cal_env))
    assert r1[0]['status'] == '有效'
    r2 = operations.second(db, write(tmp_path, 'sample.json', sample_env))
    assert r2[0]['status'] == '已归属'
    r3 = operations.third(db, write(tmp_path, 'result.json',
                                    trust.sign_export(root_priv, 'instrument-A', 3,
                                                      trust.payload_digest(sample),
                                                      result_rows())))
    assert r3[0]['status'] == '可放行'
    # Concentration body is computed and retained regardless of later verdicts.
    conn = connect(db)
    body = json.loads(conn.execute("SELECT body FROM released_result WHERE id='result:R1'").fetchone()['body'])
    assert body['concentration'] == pytest.approx(2.0)
    conn.close()

    # Now introduce a fork at sequence 2 with a second valid payload; R1's new business
    # use freezes even though the earlier released row is preserved.
    sample2 = sample_rows()
    sample2['rows'][0]['id'] = 'S2'
    sample2['rows'][0]['injection'] = 'I9'
    fork_env = trust.sign_export(root_priv, 'instrument-A', 2,
                                 trust.payload_digest(cal), sample2)
    operations.second(db, write(tmp_path, 'fork.json', fork_env))
    next_request = result_rows('R2')
    next_request['rows'][0]['sample'] = 'S2'
    r4 = operations.third(db, write(tmp_path, 'result2.json',
                                    trust.sign_export(root_priv, 'instrument-A', 4,
                                                      trust.payload_digest(sample2),
                                                      next_request)))
    assert r4[0]['status'] == '拒放'
    assert r4[0]['reason'].startswith('source_hold:')
    # The previously released R1 is not retroactively rewritten.
    conn = connect(db)
    assert conn.execute("SELECT status FROM released_result WHERE id='result:R1'").fetchone()[0] == '可放行'
    conn.close()


def test_report_carries_two_contracts_not_one_boolean(tmp_path):
    db = tmp_path / 'db'
    root_priv, root_pub = make_keys()
    install_root(db, 'instrument-A', root_pub)
    p1 = export_payload(1)
    e1 = trust.sign_export(root_priv, 'instrument-A', 1, None, p1)
    capture_env(db, tmp_path, 'e1.json', e1)
    out = report.build(db, tmp_path / 'report.json')
    data = json.loads(out.read_text(encoding='utf-8'))
    source = data['sources'][0]
    assert source['verified'] is True  # byte integrity still reported
    assert source['source_archive'] == {'status': trust.ARCHIVE_VERIFIABLE,
                                        'reason': trust.REASON_CHAIN_VALID}
    assert source['source_business'] == {'status': trust.BUSINESS_USABLE,
                                         'reason': trust.REASON_CHAIN_VALID}
    assert data['trust_roots'][0]['instrument_id'] == 'instrument-A'


def test_sequence_gap_stays_pending_until_chain_is_filled(tmp_path):
    db = tmp_path / 'db'
    root_priv, root_pub = make_keys()
    install_root(db, 'instrument-A', root_pub)
    p1, p2, p4 = export_payload(1), export_payload(2), export_payload(4)
    e1 = trust.sign_export(root_priv, 'instrument-A', 1, None, p1)
    e2 = trust.sign_export(root_priv, 'instrument-A', 2,
                           trust.payload_digest(p1), p2)
    # Sequence 4 referencing a non-existent sequence 3: never guessed true.
    e4 = trust.sign_export(root_priv, 'instrument-A', 4,
                           'a' * 64, p4)
    capture_env(db, tmp_path, 'e1.json', e1)
    capture_env(db, tmp_path, 'e2.json', e2)
    s4 = capture_env(db, tmp_path, 'e4.json', e4)
    assert verdict(db, s4)['archive']['status'] == trust.ARCHIVE_PENDING
    p3 = export_payload(3)
    e3 = trust.sign_export(root_priv, 'instrument-A', 3,
                           trust.payload_digest(p2), p3)
    s3 = capture_env(db, tmp_path, 'e3.json', e3)
    # 4 still does not link: its claimed predecessor digest does not match seq 3.
    assert verdict(db, s3)['archive']['status'] == trust.ARCHIVE_VERIFIABLE
    assert verdict(db, s4)['archive']['reason'] == trust.REASON_PREV_DIGEST_MISMATCH


def test_foreign_trust_root_cannot_authorize_an_instrument(tmp_path):
    db = tmp_path / 'db'
    root_priv, root_pub = make_keys()
    install_root(db, 'instrument-A', root_pub)
    foreign_priv = trust.generate_private_key()
    # Unknown key id: rejected before any signature comparison.
    env = trust.sign_export(foreign_priv, 'instrument-A', 1, None,
                            export_payload(1), key_id='foreign-key')
    sid = capture_env(db, tmp_path, 'foreign.json', env)
    assert verdict(db, sid)['archive'] == {'status': trust.ARCHIVE_REJECTED,
                                           'reason': trust.REASON_KEY_UNKNOWN}
    # Claiming the registered root key id while holding another key fails verification.
    env2 = trust.sign_export(foreign_priv, 'instrument-A', 1, None,
                             export_payload(1, 'different-payload'),
                             key_id=trust.ROOT_KEY_ID)
    sid2 = capture_env(db, tmp_path, 'foreign2.json', env2)
    assert verdict(db, sid2)['archive'] == {'status': trust.ARCHIVE_REJECTED,
                                            'reason': trust.REASON_SIGNATURE_INVALID}


def test_rotation_and_revocation_imports_are_idempotent(tmp_path):
    db = tmp_path / 'db'
    root_priv, root_pub = make_keys()
    install_root(db, 'instrument-A', root_pub)
    new_priv = trust.generate_private_key()
    rotation = trust.build_rotation('instrument-A', root_priv, trust.ROOT_KEY_ID,
                                    new_priv, 'key-2', 3, root_priv)
    bulletin = trust.build_revocation('instrument-A', 3, 5, root_priv, 'bul-x')
    conn = connect(db)
    with conn:
        first = trust.import_rotation(conn, rotation)
        again = trust.import_rotation(conn, dict(rotation))
        rev1 = trust.import_revocation(conn, bulletin)
        rev2 = trust.import_revocation(conn, dict(bulletin))
        assert first['status'] == 'active' and again['status'] == 'active'
        assert rev1['status'] == 'recorded' and rev2['status'] == 'recorded'
        assert conn.execute('SELECT count(*) FROM key_rotation').fetchone()[0] == 1
        assert conn.execute('SELECT count(*) FROM signing_key').fetchone()[0] == 2
        assert conn.execute('SELECT count(*) FROM revocation_bulletin').fetchone()[0] == 1
    conn.close()


def test_late_revocation_keeps_issued_usage_and_only_blocks_new_use(tmp_path):
    db = tmp_path / 'db'
    root_priv, root_pub = make_keys()
    install_root(db, 'instrument-A', root_pub)
    cal = cal_rows()
    sample = sample_rows()
    request = result_rows()
    cal_env = trust.sign_export(root_priv, 'instrument-A', 1, None, cal)
    sample_env = trust.sign_export(root_priv, 'instrument-A', 2,
                                   trust.payload_digest(cal), sample)
    req_env = trust.sign_export(root_priv, 'instrument-A', 3,
                                trust.payload_digest(sample), request)
    operations.first(db, write(tmp_path, 'cal.json', cal_env))
    operations.second(db, write(tmp_path, 'sample.json', sample_env))
    released = operations.third(db, write(tmp_path, 'req.json', req_env))
    assert released[0]['status'] == '可放行'
    req_source = 3

    bulletin = trust.build_revocation('instrument-A', 2, 3, root_priv, 'bul-late')
    conn = connect(db)
    with conn:
        trust.import_revocation(conn, bulletin)
    # Already-issued usage record is not retroactively rewritten.
    row = conn.execute("SELECT status,reason FROM released_result WHERE id='result:R1'").fetchone()
    assert dict(row) == {'status': '可放行', 'reason': 'traceable_concentration'}
    # New business use from the revoked source is blocked.
    blocked, reason = trust.business_gate(conn, req_source)
    assert blocked and reason == trust.REASON_REVOKED
    history = [r['reason'] for r in conn.execute(
        "SELECT reason FROM source_verdict_history WHERE source_id=? AND contract='business' ORDER BY id",
        (req_source,))]
    assert history == [trust.REASON_CHAIN_VALID, trust.REASON_REVOKED]
    conn.close()


def test_key_cannot_sign_captures_before_its_rotation_sequence(tmp_path):
    db = tmp_path / 'db'
    root_priv, root_pub = make_keys()
    install_root(db, 'instrument-A', root_pub)
    new_priv = trust.generate_private_key()
    rotation = trust.build_rotation('instrument-A', root_priv, trust.ROOT_KEY_ID,
                                    new_priv, 'key-2', 5, root_priv)
    conn = connect(db)
    with conn:
        assert trust.import_rotation(conn, rotation)['status'] == 'active'
    conn.close()
    # The new key is only valid from sequence 5; using it at sequence 2 must fail.
    env = trust.sign_export(new_priv, 'instrument-A', 2,
                            'b' * 64, export_payload(2), key_id='key-2')
    sid = capture_env(db, tmp_path, 'early.json', env)
    assert verdict(db, sid)['archive'] == {'status': trust.ARCHIVE_REJECTED,
                                           'reason': trust.REASON_KEY_NOT_YET_VALID}


def test_malformed_proof_shape_is_rejected_without_crashing(tmp_path):
    db = tmp_path / 'db'
    _, root_pub = make_keys()
    install_root(db, 'instrument-A', root_pub)
    bad = {'version': 'v1', 'rows': [],
           'source_proof': {'version': 'x', 'instrument_id': 'instrument-A'}}
    sid = capture_env(db, tmp_path, 'bad.json', bad)
    assert verdict(db, sid)['archive'] == {'status': trust.ARCHIVE_REJECTED,
                                           'reason': trust.REASON_BAD_PROOF}
    result = verify_source(db, write(tmp_path, 'bad2.json', bad))
    assert result['archive']['reason'] == trust.REASON_BAD_PROOF


def test_legacy_store_without_trust_root_keeps_original_behavior(tmp_path):
    db = tmp_path / 'db'
    sid = capture_env(db, tmp_path, 'plain.json', export_payload(1))
    conn = connect(db)
    assert conn.execute('SELECT count(*) FROM source_proof').fetchone()[0] == 0
    assert conn.execute('SELECT count(*) FROM source_verdict').fetchone()[0] == 0
    blocked, reason = trust.business_gate(conn, sid)
    assert blocked is False and reason == trust.REASON_LEGACY
    conn.close()
    result = verify_source(db, tmp_path / 'plain.json')
    assert result['mode'] == 'legacy'
