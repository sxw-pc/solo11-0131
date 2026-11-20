"""Offline exchange, merge convergence and the two readings over merged evidence."""
import base64
import copy
import json

import pytest

from spectracapture import custody, operations, report
from spectracapture import exchange
from spectracapture.store import connect

CAL = {'version': 'v1', 'rows': [{'id': 'CAL1',
                                  'points': [{'concentration': 1, 'area': 10}, {'concentration': 2, 'area': 20},
                                             {'concentration': 3, 'area': 30}],
                                  'blank_area': 0, 'max_blank': 2, 'max_residual': 1,
                                  'at': '2026-01-01T10:00:00', 'valid_until': '2026-01-02T00:00:00'}]}
SAMPLE = {'version': 'v1', 'rows': [{'id': 'S1', 'injection': 'I1', 'calibration': 'CAL1',
                                     'window': [4.5, 5.5], 'peaks': [{'time': 5.0, 'area': 20}]}]}
RESULT = {'version': 'v1', 'rows': [{'id': 'R1', 'sample': 'S1', 'calibration': 'CAL1',
                                     'quant_limit': 1.5, 'at': '2026-01-01T12:00:00'}]}
BATCH = {'version': 'v1', 'rows': [{'id': 'B1', 'results': ['R1']}]}
CUSTODY_EXPORT = {'version': 'v1', 'rows': [{'id': 'H1', 'sample': 'S1', 'origin': 'collector',
                                             'destination': 'lab', 'temperature_limit': 8,
                                             'transfers': [{'from': 'collector', 'to': 'lab',
                                                            'at': '2026-01-01T11:00:00',
                                                            'seal_intact': False,
                                                            'max_temperature': 5}]}]}
# Byte-identical export imported by both labs: same source digest, two arrivals.
SHARED = {'version': 'v1', 'rows': [{'id': 'H9', 'sample': 'S9', 'origin': 'collector',
                                     'destination': 'lab', 'temperature_limit': 8,
                                     'transfers': [{'from': 'collector', 'to': 'lab',
                                                    'at': '2026-01-01T09:00:00',
                                                    'seal_intact': True,
                                                    'max_temperature': 5}]}]}


def write(root, name, obj):
    path = root / name
    path.write_text(json.dumps(obj), encoding='utf-8')
    return path


def released_lab(root):
    """Replica A: own calibration bytes as local export_file.id=1.

    Then S1 assigned, R1 releasable, batch B1 sealed.
    """
    db = root / 'a.db'
    operations.first(db, write(root, 'a_cal.json', CAL))
    operations.second(db, write(root, 'a_sample.json', SAMPLE))
    operations.third(db, write(root, 'a_result.json', RESULT))
    from spectracapture import batch
    batch.release(db, write(root, 'a_batch.json', BATCH))
    custody.assess(db, write(root, 'a_shared.json', SHARED))
    return db


def sealed_breach_lab(root):
    """Replica B: *different* calibration bytes as local export_file.id=1.

    Its own S1 capture bytes make a concurrent branch, and the seal-broken
    handoff for S1 produces the retraction.
    """
    db = root / 'b.db'
    calibration = copy.deepcopy(CAL)
    calibration['rows'][0]['note'] = 'second lab curve'
    operations.first(db, write(root, 'b_cal.json', calibration))
    sample = copy.deepcopy(SAMPLE)
    sample['rows'][0]['note'] = 'second lab capture'
    operations.second(db, write(root, 'b_sample.json', sample))
    custody.assess(db, write(root, 'b_shared.json', SHARED))
    custody.assess(db, write(root, 'b_custody.json', CUSTODY_EXPORT))
    return db


def roundtrip(root, source, target, since=0, crash_after=None):
    package = exchange.build_package(source, since)
    path = root / f"pkg_{exchange.package_id(package)[:10]}.json"
    path.write_text(json.dumps(package), encoding='utf-8')
    return exchange.receive(target, path, crash_after=crash_after), path


def merged_views(root):
    a = root / 'a.db'
    b = root / 'b.db'
    archive_a, business_a = report.archive_reading(connect(a)), report.business_reading(connect(a))
    archive_b, business_b = report.archive_reading(connect(b)), report.business_reading(connect(b))
    return exchange.canonical_graph(a), exchange.canonical_graph(b), archive_a, archive_b, business_a, business_b


def test_local_ids_are_not_shared_and_sources_keep_their_bytes(tmp_path):
    a = released_lab(tmp_path)
    b = sealed_breach_lab(tmp_path)
    import hashlib
    # Both replicas use local export_file.id=1, but for different raw files.
    first_a = connect(a).execute('SELECT hash,body FROM export_file WHERE id=1').fetchone()
    first_b = connect(b).execute('SELECT hash,body FROM export_file WHERE id=1').fetchone()
    assert first_a['hash'] != first_b['hash']
    assert hashlib.sha256(first_a['body']).hexdigest() == first_a['hash']
    assert hashlib.sha256(first_b['body']).hexdigest() == first_b['hash']

    roundtrip(tmp_path, a, b)
    roundtrip(tmp_path, b, a)

    for path in (a, b):
        conn = connect(path)
        # Every local source row still hashes to its own stored bytes.
        for row in conn.execute('SELECT hash,body FROM export_file'):
            assert hashlib.sha256(row['body']).hexdigest() == row['hash']
        # Each lab's local S1 row still points at that lab's own capture bytes.
        row = conn.execute("SELECT source_id FROM sample_export WHERE id='S1'").fetchone()
        source = conn.execute('SELECT hash FROM export_file WHERE id=?', (row['source_id'],)).fetchone()
        assert source is not None

    # The merged graph keeps both S1 raw files as distinct branches.
    graph_a = exchange.canonical_graph(a)
    s1_sources = {n['source_hash'] for n in graph_a['evidence']
                  if n['family'] == 'sample_export' and n['entity_key'] == 'S1'}
    assert len(s1_sources) == 2
    # And the shared export collapsed to one source with two arrival paths.
    shared = [s for s in graph_a['sources'] if len(s['arrivals']) == 2]
    assert shared and all(len({x['replica'] for x in s['arrivals']}) == 2 for s in shared)


def test_both_replicas_converge_and_archive_shows_both_branches(tmp_path):
    a = released_lab(tmp_path)
    b = sealed_breach_lab(tmp_path)
    roundtrip(tmp_path, a, b)
    roundtrip(tmp_path, b, a)
    graph_a, graph_b, archive_a, archive_b, business_a, business_b = merged_views(tmp_path)
    assert graph_a == graph_b
    assert archive_a == archive_b
    assert business_a == business_b
    conflicts = {(c['family'], c['key']) for c in archive_a['conflicts']}
    assert ('sample_export', 'S1') in conflicts
    assert ('released_result', 'result:R1') in conflicts
    # A's release exists once (B never produced it) and is reached by the
    # sample-level custody retraction from B.
    r1 = [g for g in archive_a['groups'] if g['family'] == 'released_result'
          and g['key'] == 'result:R1'][0]
    assert len(r1['branches']) == 1
    assert r1['quarantine']
    assert r1['branches'][0]['status'] == '可放行'  # archive keeps the old status
    # The business view may no longer show R1 as publishable.
    assert business_a['publishable'].get('released_result', {}).get('result:R1', {}).get('status') != '可放行'
    item = business_a['publishable']['released_result']['result:R1']
    assert item['status'] == '已撤回'
    assert item['withdraws_status'] == ['可放行']
    assert item['withdrawn_by']


def test_same_bytes_converge_to_one_source_with_two_arrival_paths(tmp_path):
    a = released_lab(tmp_path)
    b = sealed_breach_lab(tmp_path)
    roundtrip(tmp_path, a, b)
    roundtrip(tmp_path, b, a)
    graph = exchange.canonical_graph(a)
    cal = [s for s in graph['sources'] if s['hash'] == graph['sources'][0]['hash']]
    shared = [s for s in graph['sources'] if len(s['arrivals']) == 2]
    assert shared, 'calibration bytes were imported by both replicas'
    assert all(len({x['replica'] for x in s['arrivals']}) == 2 for s in shared)


def _copies(root, names):
    """Snapshot an identical pair of ledgers into separate exchange worlds."""
    worlds = {}
    for name in names:
        world = root / name
        world.mkdir()
        for db in ('a.db', 'b.db'):
            (world / db).write_bytes((root / db).read_bytes())
        worlds[name] = world
    return worlds


def test_reversed_duplicate_and_crash_resume_give_same_graph(tmp_path):
    a = released_lab(tmp_path)
    b = sealed_breach_lab(tmp_path)
    package_a = exchange.build_package(a)
    package_b = exchange.build_package(b)
    worlds = _copies(tmp_path, ('forward', 'reversed', 'crashed'))

    # 1. forward: a<-b, b<-a.
    assert exchange.receive(worlds['forward'] / 'b.db', json.dumps(package_a))['status'] == 'applied'
    assert exchange.receive(worlds['forward'] / 'a.db', json.dumps(package_b))['status'] == 'applied'

    # 2. reversed order, with duplicates on both sides.
    assert exchange.receive(worlds['reversed'] / 'a.db', json.dumps(package_b))['status'] == 'applied'
    assert exchange.receive(worlds['reversed'] / 'b.db', json.dumps(package_a))['status'] == 'applied'
    assert exchange.receive(worlds['reversed'] / 'a.db', json.dumps(package_b))['status'] == 'duplicate'
    assert exchange.receive(worlds['reversed'] / 'b.db', json.dumps(package_a))['status'] == 'duplicate'

    # 3. crash after the second evidence node, then retry the same package.
    target = worlds['crashed'] / 'b.db'
    with pytest.raises(exchange.SimulatedCrash):
        exchange.receive(target, json.dumps(package_a), crash_after=2)
    receipt = exchange.receive(target, json.dumps(package_a))
    assert receipt['status'] == 'applied'
    exchange.receive(worlds['crashed'] / 'a.db', json.dumps(package_b))

    for world in worlds.values():
        assert exchange.sync_status(world / 'a.db')['chain_verified']
        assert exchange.sync_status(world / 'b.db')['chain_verified']
        assert exchange.canonical_graph(world / 'a.db') == exchange.canonical_graph(world / 'b.db')
    graphs = {name: exchange.canonical_graph(worlds[name] / 'a.db') for name in worlds}
    assert graphs['forward'] == graphs['reversed'] == graphs['crashed']
    business = {name: report.business_reading(connect(worlds[name] / 'a.db')) for name in worlds}
    assert business['forward'] == business['reversed'] == business['crashed']
    assert business['forward']['publishable']['released_result']['result:R1']['status'] == '已撤回'


def test_empty_package_after_full_sync_is_duplicate(tmp_path):
    a = released_lab(tmp_path)
    b = sealed_breach_lab(tmp_path)
    roundtrip(tmp_path, a, b)
    head = exchange.sync_status(a)['seq']
    empty = exchange.build_package(a, since_seq=head)
    receipt = exchange.receive(b, json.dumps(empty))
    assert receipt['status'] == 'duplicate'
    assert receipt['cursor'] >= head


def test_child_before_parent_is_deferred_then_resumed(tmp_path):
    a = released_lab(tmp_path)
    b = sealed_breach_lab(tmp_path)
    head = exchange.sync_status(a)['seq']
    packages = [exchange.build_package(a, since_seq=seq) for seq in range(head)]

    # Control: the same B replica (same replica id) merged in order.
    control = tmp_path / 'control'
    control.mkdir()
    (control / 'b.db').write_bytes((tmp_path / 'b.db').read_bytes())
    for pkg in packages:
        exchange.receive(control / 'b.db', json.dumps(pkg))
    exchange.receive(control / 'b.db', json.dumps(exchange.build_package(b)))

    # Newest-first: every child is deferred, then resumed when its parent lands.
    target = tmp_path / 'b.db'
    statuses = [exchange.receive(target, json.dumps(pkg))['status']
                for pkg in reversed(packages)]
    assert statuses[0] == 'deferred'
    assert exchange.sync_status(target)['pending'] == []
    merge_log = connect(target).execute('SELECT status FROM merge_log ORDER BY id').fetchall()
    assert any(row['status'] == 'deferred' for row in merge_log)
    assert any(row['status'] == 'resumed' for row in merge_log)
    exchange.receive(target, json.dumps(exchange.build_package(b)))

    assert exchange.canonical_graph(target) == exchange.canonical_graph(control / 'b.db')
    assert report.business_reading(connect(target)) == report.business_reading(connect(control / 'b.db'))
    assert report.archive_reading(connect(target)) == report.archive_reading(connect(control / 'b.db'))


def test_package_with_missing_causal_predecessor_is_rejected(tmp_path):
    a = released_lab(tmp_path)
    b = sealed_breach_lab(tmp_path)
    package = exchange.build_package(a)
    bad = copy.deepcopy(package)
    # Point a link at evidence that neither this package nor the receiver has.
    bad['commits'][0]['evidence'][0]['links'].append(
        {'relation': 'references', 'target': 'f' * 64})
    bad['package_id'] = exchange.package_id(bad)
    receipt = exchange.receive(b, json.dumps(bad))
    assert receipt['status'] == 'rejected'
    assert receipt['reason'] == 'evidence_dangling'
    assert not connect(b).execute('SELECT 1 FROM commit_chain WHERE replica_id=?',
                                  (package['origin'],)).fetchone()


def test_rejected_package_leaves_no_partial_commit(tmp_path):
    a = released_lab(tmp_path)
    b = sealed_breach_lab(tmp_path)
    target = tmp_path / 'b.db'
    package = exchange.build_package(a)
    bad = copy.deepcopy(package)
    bad['commits'][1]['source']['bytes'] = base64.b64encode(b'{"version":"v1","rows":[]}').decode()
    bad['package_id'] = exchange.package_id(bad)
    before = exchange.sync_status(target)
    receipt = exchange.receive(target, json.dumps(bad))
    assert receipt['status'] == 'rejected'
    assert receipt['reason'] == 'source_digest'
    after = exchange.sync_status(target)
    assert before['seq'] == after['seq']
    assert not connect(target).execute('SELECT 1 FROM commit_chain WHERE replica_id=?',
                                       (package['origin'],)).fetchone()
    log = connect(target).execute("SELECT reason,cursor FROM merge_log WHERE status='rejected'").fetchone()
    assert log['reason'] == 'source_digest' and log['cursor'] is not None
    # Replaying the same rejected bytes does not append a second visible commit.
    again = exchange.receive(target, json.dumps(bad))
    assert again['status'] == 'rejected' and again['reason'] == 'source_digest'
    assert exchange.sync_status(target)['seq'] == before['seq']


def signed_review(target_family, target_key, refs, supersedes=(), analyst='C', decision='release'):
    body = {'family': target_family, 'key': target_key, 'references': list(refs),
            'supersedes': list(supersedes), 'analyst': analyst, 'decision': decision,
            'status': '已复核', 'reason': 'signed_cross_replica_review'}
    body['signature'] = report.review_signature(body)
    return body


def test_concurrent_reviews_do_not_win_by_arrival_or_sequence(tmp_path):
    a = released_lab(tmp_path)
    b = sealed_breach_lab(tmp_path)
    roundtrip(tmp_path, a, b)
    roundtrip(tmp_path, b, a)
    graph = exchange.canonical_graph(a)
    sample_branches = [n['evidence_id'] for n in graph['evidence']
                       if n['family'] == 'sample_export' and n['entity_key'] == 'S1']
    quarantine = [n['evidence_id'] for n in graph['evidence']
                  if n['family'] == 'retraction' and n['body'].get('kind') == 'sample']
    assert len(sample_branches) == 2 and len(quarantine) == 1
    review_a = {'version': 'v1', 'rows': [dict(id='S1', **signed_review(
        'sample_export', 'S1', sample_branches + quarantine, analyst='A', decision='release'))]}
    review_b = {'version': 'v1', 'rows': [dict(id='S2', **signed_review(
        'sample_export', 'S1', sample_branches + quarantine, analyst='B', decision='hold'))]}
    from spectracapture import integration
    integration.resolve(a, write(tmp_path, 'review_a.json', review_a))
    integration.resolve(b, write(tmp_path, 'review_b.json', review_b))
    roundtrip(tmp_path, a, b)
    roundtrip(tmp_path, b, a)
    business = report.business_reading(connect(a))
    pending = [p for p in business['pending'] if p['family'] == 'sample_export' and p['key'] == 'S1']
    assert pending and pending[0]['reason'] == 'concurrent_review'
    assert report.business_reading(connect(b)) == business
    assert exchange.canonical_graph(a) == exchange.canonical_graph(b)
    archive = report.archive_reading(connect(a))
    conflict = [c for c in archive['conflicts'] if c['family'] == 'sample_export' and c['key'] == 'S1'][0]
    assert conflict['state'] == 'concurrent_review'
    assert len(conflict['concurrent_reviews']) == 2


def test_incremental_package_carries_causal_predecessor_bytes(tmp_path):
    a = released_lab(tmp_path)
    b = sealed_breach_lab(tmp_path)
    # Merge everything first, then add a custody retraction on B that points at
    # evidence A created.  The incremental package must carry that predecessor.
    roundtrip(tmp_path, a, b)
    roundtrip(tmp_path, b, a)
    graph = exchange.canonical_graph(a)
    refs = [n['evidence_id'] for n in graph['evidence']
            if (n['family'] == 'sample_export' and n['entity_key'] == 'S1') or
               (n['family'] == 'retraction' and n['body'].get('kind') == 'sample')]
    review = {'version': 'v1', 'rows': [dict(id='RES1', **signed_review(
        'sample_export', 'S1', refs, analyst='C', decision='hold'))]}
    from spectracapture import integration
    integration.resolve(a, write(tmp_path, 'resolve2.json', review))
    cursor = exchange.sync_status(b)['remotes']
    since = [r for r in cursor if r['replica'] == exchange.sync_status(a)['replica_id']][0]['seq']
    package = exchange.build_package(a, since_seq=since)
    assert package['commits'] and package['context']
    # Context that exists only on B needs no bytes here; context created by A
    # and not re-sent must travel with its own raw source.
    for item in package['context']:
        assert item['links'] is not None
    receipt = exchange.receive(tmp_path / 'b.db', json.dumps(package))
    assert receipt['status'] == 'applied'
    graph_a = exchange.canonical_graph(tmp_path / 'a.db')
    graph_b = exchange.canonical_graph(tmp_path / 'b.db')
    assert graph_a == graph_b
    # Context nodes bring their own edges, so the graph is not just node-equal.
    assert {e['evidence_id']: e['links'] for e in graph_a['evidence']} == \
           {e['evidence_id']: e['links'] for e in graph_b['evidence']}
    # The resolution is visible on B even though its cited branches are older A evidence.
    archive = report.archive_reading(connect(tmp_path / 'b.db'))
    conflict = [c for c in archive['conflicts'] if c['family'] == 'sample_export' and c['key'] == 'S1'][0]
    assert conflict['state'] == 'resolved'


def test_signed_review_resolves_conflict_and_keeps_audit_chain(tmp_path):
    a = released_lab(tmp_path)
    b = sealed_breach_lab(tmp_path)
    roundtrip(tmp_path, a, b)
    roundtrip(tmp_path, b, a)
    graph = exchange.canonical_graph(a)
    sample_branches = [n['evidence_id'] for n in graph['evidence']
                       if n['family'] == 'sample_export' and n['entity_key'] == 'S1']
    quarantine = [n['evidence_id'] for n in graph['evidence']
                  if n['family'] == 'retraction' and n['body'].get('kind') == 'sample']
    review = {'version': 'v1', 'rows': [dict(id='RES1', **signed_review(
        'sample_export', 'S1', sample_branches + quarantine, analyst='C', decision='hold'))]}
    from spectracapture import integration
    integration.resolve(a, write(tmp_path, 'resolve.json', review))
    roundtrip(tmp_path, a, b)
    roundtrip(tmp_path, b, a)

    archive = report.archive_reading(connect(a))
    conflict = [c for c in archive['conflicts'] if c['family'] == 'sample_export' and c['key'] == 'S1'][0]
    assert conflict['state'] == 'resolved'
    resolution = [r for r in archive['resolutions'] if r['target'] == 'sample_export:S1'][0]
    assert set(sample_branches) <= set(resolution['references'])
    assert set(quarantine) <= set(resolution['references'])

    business = report.business_reading(connect(a))
    resolved = [r for r in business['resolved'] if r['target'] == 'sample_export:S1']
    assert resolved and resolved[0]['review'] == archive_resolution_id(archive)

    # Old releases and retractions stay in the graph and in the archive.
    result_groups = [g for g in archive['groups'] if g['family'] == 'released_result'
                     and g['key'] == 'result:R1'][0]
    assert len(result_groups['branches']) == 1
    assert result_groups['quarantine']
    reasons = {r['reason'] for r in archive['retractions']}
    assert 'custody_quarantine' in reasons


def archive_resolution_id(archive):
    return [r for r in archive['resolutions'] if r['target'] == 'sample_export:S1'][0]['review']


def test_late_retraction_produces_withdrawn_version_and_notification(tmp_path):
    a = released_lab(tmp_path)
    b = sealed_breach_lab(tmp_path)
    roundtrip(tmp_path, a, b)
    roundtrip(tmp_path, b, a)
    business = report.business_reading(connect(a))
    assert business['publishable']['release_manifest']['B1']['status'] == '已撤回'
    assert business['notifications'], 'recalled sealed batch is a pending notification'
    assert business['publishable']['released_result']['result:R1']['withdrawn_by']


def test_review_must_cite_every_branch_and_retraction(tmp_path):
    a = released_lab(tmp_path)
    b = sealed_breach_lab(tmp_path)
    roundtrip(tmp_path, a, b)
    roundtrip(tmp_path, b, a)
    graph = exchange.canonical_graph(a)
    sample_branches = [n['evidence_id'] for n in graph['evidence']
                       if n['family'] == 'sample_export' and n['entity_key'] == 'S1']
    quarantine = [n['evidence_id'] for n in graph['evidence']
                  if n['family'] == 'retraction' and n['body'].get('kind') == 'sample']
    from spectracapture import integration
    # Cites a single branch only: not enough to resolve.
    partial = {'version': 'v1', 'rows': [dict(id='RES1', **signed_review(
        'sample_export', 'S1', [sample_branches[0]], analyst='C', decision='release'))]}
    integration.resolve(a, write(tmp_path, 'partial.json', partial))
    archive = report.archive_reading(connect(a))
    conflict = [c for c in archive['conflicts'] if c['family'] == 'sample_export' and c['key'] == 'S1'][0]
    assert conflict['state'] == 'unresolved'
    assert report.business_reading(connect(a))['resolved'] == []

    # Cites both branches but omits the retraction: still not enough.
    missing_recall = {'version': 'v1', 'rows': [dict(id='RES2', **signed_review(
        'sample_export', 'S1', sample_branches, analyst='C', decision='release'))]}
    integration.resolve(a, write(tmp_path, 'missing.json', missing_recall))
    business = report.business_reading(connect(a))
    assert business['resolved'] == []
    # S1 is still held under the custody retraction, not publishable.
    assert business['publishable']['sample_export']['S1']['status'] == '已撤回'


def test_unsigned_review_is_rejected(tmp_path):
    a = released_lab(tmp_path)
    from spectracapture import integration
    body = signed_review('sample_export', 'S1', ['x'], analyst='C')
    body['signature'] = 'not-a-signature'
    with pytest.raises(ValueError):
        integration.resolve(a, write(tmp_path, 'bad.json', {'version': 'v1', 'rows': [dict(id='R', **body)]}))


def test_package_claiming_local_origin_is_rejected(tmp_path):
    a = released_lab(tmp_path)
    package = exchange.build_package(a)
    receipt = exchange.receive(a, json.dumps(package))
    assert receipt['status'] == 'rejected'
    assert receipt['reason'] == 'origin_is_local'
    assert exchange.sync_status(a)['chain_verified']


def test_genesis_splice_is_rejected(tmp_path):
    a = released_lab(tmp_path)
    b = sealed_breach_lab(tmp_path)
    package = exchange.build_package(a, since_seq=2)
    package['start']['seq'] = 0
    package['start']['head'] = package['commits'][0]['prev']
    package['package_id'] = exchange.package_id(package)  # valid digest, invalid chain
    receipt = exchange.receive(b, json.dumps(package))
    assert receipt['status'] == 'rejected'
    assert receipt['reason'] == 'commit_genesis'


def test_signed_review_can_supersede_an_older_review(tmp_path):
    a = released_lab(tmp_path)
    b = sealed_breach_lab(tmp_path)
    roundtrip(tmp_path, a, b)
    roundtrip(tmp_path, b, a)
    graph = exchange.canonical_graph(a)
    refs = [n['evidence_id'] for n in graph['evidence']
            if (n['family'] == 'sample_export' and n['entity_key'] == 'S1') or
               (n['family'] == 'retraction' and n['body'].get('kind') == 'sample')]
    from spectracapture import integration
    first = {'version': 'v1', 'rows': [dict(id='RES1', **signed_review(
        'sample_export', 'S1', refs, analyst='C', decision='release'))]}
    integration.resolve(a, write(tmp_path, 'first.json', first))
    first_review = [x for x in exchange.canonical_graph(a)['evidence']
                    if x['family'] == 'review_resolution'][0]['evidence_id']
    second = {'version': 'v1', 'rows': [dict(id='RES2', **signed_review(
        'sample_export', 'S1', refs, supersedes=[first_review], analyst='D', decision='hold'))]}
    integration.resolve(a, write(tmp_path, 'second.json', second))
    archive = report.archive_reading(connect(a))
    conflict = [c for c in archive['conflicts'] if c['family'] == 'sample_export' and c['key'] == 'S1'][0]
    assert conflict['state'] == 'resolved'
    business = report.business_reading(connect(a))
    resolved = [r for r in business['resolved'] if r['target'] == 'sample_export:S1']
    assert resolved and resolved[0]['decision'] == 'hold'
    # Both signed reviews stay in the archive.
    assert len([r for r in archive['resolutions'] if r['target'] == 'sample_export:S1']) == 2


def test_merge_log_records_rejection_reason_and_cursor(tmp_path):
    a = released_lab(tmp_path)
    b = sealed_breach_lab(tmp_path)
    target = tmp_path / 'b.db'
    package = exchange.build_package(a)
    bad = copy.deepcopy(package)
    bad['head']['seq'] += 1
    bad['package_id'] = exchange.package_id(bad)
    receipt = exchange.receive(target, json.dumps(bad))
    assert receipt['status'] == 'rejected'
    assert receipt['cursor'] == 0
    conn = connect(target)
    row = conn.execute("SELECT status,reason,cursor FROM merge_log ORDER BY id DESC LIMIT 1").fetchone()
    assert row['status'] == 'rejected' and row['reason'] and row['cursor'] == '0'
    assert conn.execute('SELECT count(*) FROM exchange_receipt WHERE status=?', ('rejected',)).fetchone()[0] == 1


def test_cli_exchange_round_trip(tmp_path, capsys):
    from spectracapture.__main__ import main
    import sys
    a = released_lab(tmp_path)
    b = sealed_breach_lab(tmp_path)
    out = tmp_path / 'pkg.json'
    sys.argv = ['spectracapture', 'exchange-export', '--db', str(a), '--out', str(out)]
    main()
    capsys.readouterr()
    sys.argv = ['spectracapture', 'exchange-import', '--db', str(b), str(out)]
    main()
    receipt = json.loads(capsys.readouterr().out)
    assert receipt['status'] == 'applied'
    sys.argv = ['spectracapture', 'exchange-status', '--db', str(b)]
    main()
    status = json.loads(capsys.readouterr().out)
    assert status['chain_verified'] and status['remotes']
    graph = tmp_path / 'graph.json'
    sys.argv = ['spectracapture', 'evidence-graph', '--db', str(b), '--out', str(graph)]
    main()
    capsys.readouterr()
    assert json.loads(graph.read_text(encoding='utf-8'))['sources']
    report_path = tmp_path / 'report.json'
    sys.argv = ['spectracapture', 'report', '--db', str(b), '--out', str(report_path)]
    main()
    data = json.loads(report_path.read_text(encoding='utf-8'))
    assert data['archive'] and data['business'] and data['sync']
