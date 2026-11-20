"""Two independent readings over the same merged evidence.

Archive reading
    every source, every concurrent branch, who cites whom, every retraction and
    every unresolved conflict.  Nothing is hidden or overwritten.

Business reading
    only evidence whose causal history is closed and that is not retracted or
    contradicted by a live custody retraction.  Concurrent branches and
    concurrent signed reviews stay ``待核``; commit sequence numbers never break
    such a tie.  A late retraction yields a withdrawn version of the published
    view plus a notification item.
"""
import hashlib
import json
from pathlib import Path
from .store import connect, canonical


def _nodes(db):
    origins = {}
    for row in db.execute('SELECT * FROM evidence_origin'):
        origins.setdefault(row['evidence_id'], []).append(dict(row))
    links = {}
    for row in db.execute('SELECT * FROM evidence_link'):
        links.setdefault(row['relation'], {}).setdefault(row['child'], []).append(row['parent'])
    nodes = {}
    for row in db.execute('SELECT * FROM evidence'):
        eid = row['evidence_id']
        nodes[eid] = {
            'evidence_id': eid, 'family': row['family'], 'entity_key': row['entity_key'],
            'source_hash': row['source_hash'], 'content_hash': row['content_hash'],
            'body': json.loads(row['body']), 'version': row['version'], 'status': row['status'],
            'reason': row['reason'],
            'origins': sorted(origins.get(eid, []),
                              key=lambda x: (x['replica_id'], x['seq'], x['ordinal'])),
            'parents': sorted(links.get('precedes', {}).get(eid, [])),
            'references': sorted(links.get('references', {}).get(eid, [])),
            'retracts': sorted(links.get('retracts', {}).get(eid, [])),
            'supersedes': sorted(links.get('supersedes', {}).get(eid, [])),
            'retracted_by': [],
        }
    for child, targets in links.get('retracts', {}).items():
        for target in targets:
            if target in nodes:
                nodes[target]['retracted_by'].append(child)
    for node in nodes.values():
        node['retracted_by'].sort()
    return nodes


def _ancestors(nodes, node, limit=10000):
    seen = set()
    stack = list(node['parents'])
    while stack and len(seen) < limit:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        parent = nodes.get(current)
        if parent:
            stack.extend(parent['parents'])
    return seen


def _groups(nodes):
    groups = {}
    for node in nodes.values():
        groups.setdefault((node['family'], node['entity_key']), []).append(node)
    for group in groups.values():
        group.sort(key=lambda n: n['evidence_id'])
    return groups


def _sinks(nodes, group):
    """Live branches that no other live branch descends from."""
    live = [n for n in group if not n['retracted_by'] and n['family'] == group[0]['family']]
    history = {n['evidence_id']: _ancestors(nodes, n) for n in live}
    return [n for n in live if not any(n['evidence_id'] in history[o['evidence_id']]
                                       for o in live if o is not n)]


def _retractions(nodes, group):
    ids = set()
    for node in group:
        ids.update(node['retracted_by'])
    return sorted(ids)


def _live_quarantine(nodes):
    """Sample-level custody retractions that are still live, keyed by sample."""
    q = {}
    for node in nodes.values():
        if node['family'] == 'retraction' and node['body'].get('kind') == 'sample' \
                and not node['retracted_by']:
            q.setdefault(str(node['body'].get('sample')), []).append(node)
    for value in q.values():
        value.sort(key=lambda n: n['evidence_id'])
    return q


def _samples_of(group, nodes, quarantine):
    """Samples touched by a group, including via evidence it references."""
    samples = set()
    for node in group:
        body = node['body']
        if body.get('sample') is not None:
            samples.add(str(body['sample']))
        if node['family'] in ('release_item', 'release_manifest'):
            ids = [body.get('result')] if node['family'] == 'release_item' else body.get('results') or []
            for rid in ids:
                if rid is None:
                    continue
                for candidate in nodes.values():
                    if candidate['family'] == 'released_result' and \
                            candidate['entity_key'] == f'result:{rid}' and \
                            candidate['body'].get('sample') is not None:
                        samples.add(str(candidate['body']['sample']))
        for ref in node['references']:
            target = nodes.get(ref)
            if target and target['family'] == 'sample_export':
                samples.add(str(target['entity_key']))
    return {s for s in samples if s in quarantine}


def _quarantine_hits(group, nodes, quarantine):
    hits = []
    for sample in sorted(_samples_of(group, nodes, quarantine)):
        hits.extend(quarantine[sample])
    return hits


def _withdrawn_branches(nodes, group, family):
    """Branches of this group that some retraction withdrew (new version source)."""
    out = [n for n in group if n['family'] == family and n['retracted_by']]
    if not out:
        out = [n for n in _sinks(nodes, group)]
    return sorted(out, key=lambda n: n['evidence_id'])


def _causally_closed(db, nodes, node):
    """Every parent exists and every origin commit is present and chained."""
    if any(parent not in nodes for parent in node['parents']):
        return False
    for origin in node['origins']:
        row = db.execute('SELECT commit_id FROM commit_chain WHERE replica_id=? AND seq=?',
                         (origin['replica_id'], origin['seq'])).fetchone()
        if not row:
            # Context nodes travel with the package but not with their chain.
            if origin['commit_id'] == 'context':
                continue
            return False
        if origin['commit_id'] != 'context' and row['commit_id'] != origin['commit_id']:
            return False
    return True


def _valid_resolution(nodes, resolution, group, extra=()):
    body = resolution['body']
    refs = set(body.get('references') or []) | set(resolution['references'])
    if not body.get('analyst'):
        return False
    if body.get('signature') != review_signature(body):
        return False
    sinks = _sinks(nodes, group)
    required = set(n['evidence_id'] for n in sinks) | set(_retractions(nodes, group)) | set(extra)
    if not required <= refs:
        return False
    return all(ref in nodes for ref in refs)


def _resolution_state(nodes):
    by_target = {}
    for node in nodes.values():
        if node['family'] != 'review_resolution':
            continue
        body = node['body']
        target = f"{body.get('family')}:{body.get('key')}"
        by_target.setdefault(target, []).append(node)
    state = {}
    for target, reviews in by_target.items():
        live = sorted((r for r in reviews if not r['retracted_by']), key=lambda r: r['evidence_id'])
        superseded = set()
        for review in live:
            superseded.update(review['supersedes'])
            superseded.update(str(x) for x in (review['body'].get('supersedes') or []))
        survivors = [r for r in live if r['evidence_id'] not in superseded]
        state[target] = {'live': live, 'survivors': survivors}
    return state


def _target(family, key):
    return f'{family}:{key}'


def _branch_summary(node):
    return {'evidence_id': node['evidence_id'], 'status': node['status'], 'reason': node['reason'],
            'source_hash': node['source_hash'], 'version': node['version'],
            'origins': node['origins'],
            'retracted_by': node['retracted_by'],
            'references': node['references'], 'retracts': node['retracts']}


def archive_reading(db):
    nodes = _nodes(db)
    groups = _groups(nodes)
    reviews = _resolution_state(nodes)
    quarantine = _live_quarantine(nodes)
    archive = {'sources': [], 'commits': [], 'evidence': [], 'groups': [],
               'conflicts': [], 'retractions': [], 'resolutions': [], 'quarantine': []}
    for row in db.execute('SELECT * FROM export_file ORDER BY hash'):
        arrivals = [dict(x) for x in db.execute(
            'SELECT replica_id,seq,commit_id FROM arrival a WHERE source_hash=? AND EXISTS '
            '(SELECT 1 FROM commit_chain c WHERE c.commit_id=a.commit_id) ORDER BY replica_id,seq',
            (row['hash'],))]
        archive['sources'].append({'hash': row['hash'], 'kind': row['kind'], 'version': row['version'],
                                   'verified': hashlib.sha256(row['body']).hexdigest() == row['hash'],
                                   'arrivals': arrivals})
    for row in db.execute('SELECT * FROM commit_chain ORDER BY replica_id,seq'):
        commit = dict(row)
        commit.pop('created', None)  # local wall clock is not evidence identity
        archive['commits'].append(commit)
    for node in sorted(nodes.values(), key=lambda n: n['evidence_id']):
        archive['evidence'].append(node)
    for sample, hits in sorted(quarantine.items()):
        for hit in hits:
            archive['quarantine'].append({'sample': sample, 'evidence_id': hit['evidence_id'],
                                          'reason': hit['reason'], 'status': hit['status']})
    for (family, key), group in sorted(groups.items()):
        sinks = _sinks(nodes, group)
        retractions = _retractions(nodes, group)
        hits = [n['evidence_id'] for n in _quarantine_hits(group, nodes, quarantine)]
        state = reviews.get(_target(family, key), {'live': [], 'survivors': []})
        targets = state['live']
        valid = [r for r in state['survivors'] if _valid_resolution(nodes, r, group, hits)]
        resolved_by = None
        if len(valid) == 1:
            resolved_by = valid[0]['evidence_id']
        elif len(valid) > 1:
            resolved_by = 'unresolved'
        conflict = len(sinks) > 1 or bool(retractions) or bool(hits)
        if conflict:
            archive['conflicts'].append({
                'family': family, 'key': key,
                'branches': [n['evidence_id'] for n in (sinks or group)],
                'retractions': retractions, 'quarantine': hits,
                'concurrent_reviews': [r['evidence_id'] for r in targets],
                'resolved_by': resolved_by,
                'state': 'resolved' if resolved_by and resolved_by != 'unresolved'
                         else ('concurrent_review' if len(valid) > 1 else 'unresolved'),
            })
        archive['groups'].append({'family': family, 'key': key,
                                  'branches': [_branch_summary(n) for n in group],
                                  'sinks': [n['evidence_id'] for n in sinks],
                                  'retractions': retractions, 'quarantine': hits,
                                  'resolved_by': resolved_by})
    for node in sorted(nodes.values(), key=lambda n: n['evidence_id']):
        for target in node['retracts']:
            archive['retractions'].append({'by': node['evidence_id'], 'target': target,
                                           'reason': node['reason'], 'status': node['status']})
        if node['family'] == 'review_resolution':
            archive['resolutions'].append({'review': node['evidence_id'],
                                           'target': f"{node['body'].get('family')}:{node['body'].get('key')}",
                                           'references': node['references'],
                                           'supersedes': node['supersedes'],
                                           'analyst': node['body'].get('analyst'),
                                           'status': node['status'], 'reason': node['reason'],
                                           'signature': node['body'].get('signature')})
    return archive


def business_reading(db):
    nodes = _nodes(db)
    groups = _groups(nodes)
    reviews = _resolution_state(nodes)
    quarantine = _live_quarantine(nodes)
    view = {}
    pending = []
    notifications = []
    resolved = []
    for (family, key), group in sorted(groups.items()):
        sinks = _sinks(nodes, group)
        retractions = _retractions(nodes, group)
        if family in ('retraction', 'review_resolution'):
            continue
        hits = _quarantine_hits(group, nodes, quarantine)
        hit_ids = sorted(n['evidence_id'] for n in hits)
        state = reviews.get(_target(family, key), {'live': [], 'survivors': []})
        live_reviews = state['live']
        # Only *surviving* reviews compete.  A review explicitly superseded by
        # a later one is audit history, not a tie.
        competing = [r for r in state['survivors'] if _valid_resolution(nodes, r, group, hit_ids)]
        if len(competing) > 1:
            pending.append({'family': family, 'key': key, 'reason': 'concurrent_review',
                            'reviews': [r['evidence_id'] for r in competing]})
            continue
        if live_reviews and not state['survivors']:
            # Every review has been superseded: the chain is ambiguous.
            pending.append({'family': family, 'key': key, 'reason': 'concurrent_review',
                            'reviews': [r['evidence_id'] for r in live_reviews]})
            continue
        if len(competing) == 1 and _causally_closed(db, nodes, competing[0]):
            resolution = competing[0]
            resolved.append({'target': _target(family, key), 'review': resolution['evidence_id'],
                             'references': resolution['references'],
                             'supersedes': resolution['supersedes'],
                             'status': resolution['status'], 'reason': resolution['reason'],
                             'decision': resolution['body'].get('decision')})
            view.setdefault(family, {})[key] = {
                'evidence_id': resolution['evidence_id'], 'status': resolution['status'],
                'reason': resolution['reason'], 'resolved_by': resolution['evidence_id'],
                'cites': resolution['references'], 'body': resolution['body'],
            }
            continue
        if hits:
            newest = hits[0]
            # A sample-level withdrawal retracts the sample branch it cites;
            # every release bound to that sample (matched through referenced
            # evidence) becomes a new withdrawn version of this group.
            direct = [nodes[t] for h in hits for t in h['retracts'] if t in nodes]
            withdrawn = _withdrawn_branches(nodes, group, family)
            view.setdefault(family, {})[key] = {
                'evidence_id': newest['evidence_id'], 'status': '已撤回',
                'reason': newest['reason'] or 'custody_quarantine',
                'withdraws': [x['evidence_id'] for x in withdrawn],
                'withdraws_status': [x['status'] for x in withdrawn],
                'withdrawn_by': hit_ids,
                'quarantine_targets': sorted({x['evidence_id'] for x in direct}),
            }
            if family in ('released_result', 'release_manifest', 'release_item'):
                target_ids = sorted({t for h in hits for t in h['retracts'] if t in nodes})
                notifications.append({'evidence_id': newest['evidence_id'], 'family': family,
                                      'key': key, 'status': '待通知',
                                      'reason': 'custody_quarantine',
                                      'sample': newest['body'].get('sample'),
                                      'withdraws': target_ids or [x['evidence_id'] for x in withdrawn],
                                      'withdrawn_by': hit_ids})
            continue
        if retractions:
            first = nodes.get(retractions[0])
            withdrawn = _withdrawn_branches(nodes, group, family)
            view.setdefault(family, {})[key] = {
                'evidence_id': retractions[0], 'status': '已撤回',
                'reason': first['reason'] if first else 'retracted',
                'withdraws': [x['evidence_id'] for x in withdrawn],
                'withdraws_status': [x['status'] for x in withdrawn],
                'withdrawn_by': retractions,
            }
            for recall in retractions:
                node = nodes[recall]
                notifications.append({'evidence_id': recall,
                                      'family': family, 'key': key,
                                      'target': node['retracts'][0] if node['retracts'] else None,
                                      'reason': node['reason'], 'status': '待通知'})
            continue
        if not sinks:
            pending.append({'family': family, 'key': key, 'reason': 'all_branches_retracted'})
            continue
        if len(sinks) > 1:
            pending.append({'family': family, 'key': key, 'reason': 'concurrent_evidence',
                            'branches': [n['evidence_id'] for n in sinks]})
            continue
        latest = sinks[0]
        missing = [p for p in latest['parents'] if p not in nodes]
        if missing:
            pending.append({'family': family, 'key': key, 'reason': 'causal_history_open',
                            'missing': missing})
            continue
        if not _causally_closed(db, nodes, latest):
            pending.append({'family': family, 'key': key, 'reason': 'commit_chain_open'})
            continue
        view.setdefault(family, {})[key] = {
            'evidence_id': latest['evidence_id'], 'status': latest['status'],
            'reason': latest['reason'], 'body': latest['body'],
        }
    notifications.sort(key=lambda x: (x.get('family', ''), x.get('key', ''), x['evidence_id']))
    pending.sort(key=lambda x: (x['family'], x['key'], x['reason']))
    decisions = [dict(row) for row in db.execute(
        'SELECT * FROM decision_evidence ORDER BY decision_id,replica_id')]
    return {'publishable': view, 'pending': pending, 'notifications': notifications,
            'resolved': sorted(resolved, key=lambda x: x['review']), 'decisions': decisions}


def review_signature(body):
    fields = ['family', 'key', 'references', 'supersedes', 'analyst', 'decision', 'status', 'reason']
    payload = {}
    for name in fields:
        if name in body:
            value = body.get(name)
            payload[name] = list(value) if name in ('references', 'supersedes') and value is not None else value
    return hashlib.sha256(canonical(payload).encode('utf-8')).hexdigest()


def merged_ledger_summary(db):
    """Per-replica sync state plus local projection of the evidence graph."""
    from .exchange import sync_status
    sync = sync_status(db)
    counts = {}
    for row in db.execute('SELECT family,COUNT(*) AS n FROM evidence GROUP BY family ORDER BY family'):
        counts[row['family']] = row['n']
    pending = [{'package': r['package_id'], 'origin': r['origin'], 'missing': r['missing']}
               for r in db.execute('SELECT * FROM pending_package ORDER BY rowid')]
    receipts = [dict(r) for r in db.execute('SELECT * FROM exchange_receipt ORDER BY package_id')]
    return {'local': {k: sync[k] for k in ('replica_id', 'seq', 'head', 'chain_verified')},
            'remotes': sync['remotes'], 'pending': pending or sync['pending'],
            'evidence_counts': counts, 'receipts': receipts}


def build(path, out):
    """Write both readings.  ``sources``/``issues`` stay at top level for
    existing consumers; the authoritative results are ``archive``/``business``."""
    db = connect(path)
    try:
        archive = archive_reading(db)
        business = business_reading(db)
        data = {'sources': archive['sources'],
                'issues': [dict(x) for x in db.execute('SELECT * FROM issue ORDER BY id')],
                'decisions': [dict(x) for x in db.execute('SELECT * FROM decision_log ORDER BY id')],
                'archive': archive, 'business': business,
                'sync': merged_ledger_summary(db)}
    finally:
        db.close()
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    return out
