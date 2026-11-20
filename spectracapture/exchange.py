"""Offline evidence exchange between independently operated ledgers.

A package carries only committed material from one replica: raw source bytes
(base64), the replica's commit chain, the evidence nodes, links and decisions
emitted by those commits, and any causally required predecessor nodes as
*context*.  Nothing is addressed by a local integer id.

Receiving is validate-then-apply:

1. the whole package is structurally checked and every digest, reference and
   commit link is recomputed;
2. only then is it applied in one transaction, so a rejected or crashed
   package leaves no half-visible commit.

A child package whose parent commit is not yet present is parked as *deferred*
and retried after the next package.  Arrival order, duplicates and crash/retry
therefore converge to the same evidence graph.
"""
import base64
import hashlib
import json
from pathlib import Path
from .store import (TABLES, canonical, connect, digest_text, commit_id, evidence_id,
                    decision_id, state, replica_id, genesis, body_digest)

PACKAGE_VERSION = 'exchange-v1'
RELATIONS = ('precedes', 'references', 'retracts', 'supersedes')
MAX_PENDING_ROUNDS = 64


class ExchangeError(ValueError):
    pass


class SimulatedCrash(RuntimeError):
    """Raised by tests to model process death during application."""


# --------------------------------------------------------------------------
# Package construction
# --------------------------------------------------------------------------

def _source_chunk(row):
    return {'hash': row['hash'], 'kind': row['kind'], 'version': row['version'],
            'bytes': base64.b64encode(row['body']).decode('ascii')}


def _node_payload(db, row, with_origins=False):
    item = {'evidence_id': row['evidence_id'], 'family': row['family'],
            'entity_key': row['entity_key'], 'source_hash': row['source_hash'],
            'content_hash': row['content_hash'], 'body': json.loads(row['body']),
            'version': row['version'], 'status': row['status'], 'reason': row['reason'],
            'links': [{'relation': x['relation'], 'target': x['parent']}
                      for x in db.execute('SELECT relation,parent FROM evidence_link WHERE child=? '
                                          'ORDER BY relation,parent', (row['evidence_id'],))]}
    if with_origins:
        item['origins'] = [{'replica': x['replica_id'], 'seq': x['seq'], 'ordinal': x['ordinal'],
                            'commit': x['commit_id']}
                           for x in db.execute('SELECT * FROM evidence_origin WHERE evidence_id=? '
                                               'ORDER BY replica_id,seq,ordinal', (row['evidence_id'],))]
    return item


def _closure(db, seed_ids, included):
    """Collect context nodes needed to resolve links of the given evidence."""
    context = {}
    stack = [target for eid in seed_ids for target in _targets(db, eid)]
    while stack:
        target = stack.pop()
        if target in context or target in included:
            continue
        row = db.execute('SELECT * FROM evidence WHERE evidence_id=?', (target,)).fetchone()
        if not row:
            continue
        context[target] = _node_payload(db, row, with_origins=True)
        stack.extend(_targets(db, target))
    return context


def _targets(db, eid):
    return [x['parent'] for x in db.execute('SELECT parent FROM evidence_link WHERE child=?', (eid,))]


def build_package(path, since_seq=0):
    db = connect(path)
    try:
        rid = replica_id(db)
        current = state(db)
        start_seq = int(since_seq)
        if start_seq == 0:
            start_head = genesis(rid)
        else:
            start = db.execute('SELECT * FROM commit_chain WHERE replica_id=? AND seq=?',
                               (rid, start_seq)).fetchone()
            if not start:
                raise ExchangeError('unknown_cursor')
            start_head = start['commit_id']
        commits = db.execute('SELECT * FROM commit_chain WHERE replica_id=? AND seq>? ORDER BY seq',
                             (rid, start_seq)).fetchall()
        package = {'version': PACKAGE_VERSION, 'origin': rid,
                   'start': {'seq': start_seq, 'head': start_head},
                   'head': {'seq': current['seq'], 'head': current['head']},
                   'commits': [], 'context': [], 'context_sources': []}
        included = set()
        for commit in commits:
            source = db.execute('SELECT * FROM export_file WHERE commit_id=?', (commit['commit_id'],)).fetchone()
            if not source:
                raise ExchangeError('commit_source_missing')
            nodes = db.execute('SELECT e.* FROM evidence e JOIN evidence_origin o ON o.evidence_id=e.evidence_id '
                               'WHERE o.replica_id=? AND o.seq=? ORDER BY o.ordinal', (rid, commit['seq'])).fetchall()
            included.update(row['evidence_id'] for row in nodes)
            decisions = [dict(row) for row in db.execute(
                'SELECT decision_id,subsystem,subject,status,reason,source_hash FROM decision_evidence '
                'WHERE replica_id=? AND seq=? ORDER BY decision_id', (rid, commit['seq']))]
            package['commits'].append({'replica': rid, 'seq': commit['seq'], 'prev': commit['prev'],
                                       'commit_id': commit['commit_id'], 'source': _source_chunk(source),
                                       'evidence': None, 'decisions': decisions})
        context = _closure(db, included, included)
        package['context'] = [context[key] for key in sorted(context)]
        committed = {row['hash'] for row in db.execute('SELECT hash FROM export_file')}
        needed = {item['source_hash'] for item in context.values()}
        missing = sorted(needed - {commit['source']['hash'] for commit in package['commits']})
        for source_hash in missing:
            row = db.execute('SELECT * FROM export_file WHERE hash=?', (source_hash,)).fetchone()
            if row:
                package['context_sources'].append(_source_chunk(row))
        for position, commit in enumerate(commits):
            nodes = db.execute('SELECT e.* FROM evidence e JOIN evidence_origin o ON o.evidence_id=e.evidence_id '
                               'WHERE o.replica_id=? AND o.seq=? ORDER BY o.ordinal',
                               (rid, commit['seq'])).fetchall()
            package['commits'][position]['evidence'] = [_node_payload(db, row) for row in nodes]
        package['package_id'] = package_id(package)
        return package
    finally:
        db.close()


def package_id(package):
    body = {k: v for k, v in package.items() if k != 'package_id'}
    return digest_text(canonical(body))


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

def _reject(reason):
    raise ExchangeError(reason)


def _parse_json(raw, what):
    try:
        return json.loads(raw.decode('utf-8'))
    except Exception:
        _reject(f'{what}_unreadable')


def _check_node(item, source_hash=None, packaged=True):
    if not isinstance(item, dict):
        _reject('evidence_malformed')
    family, key = item.get('family'), item.get('entity_key')
    if family not in TABLES or key is None:
        _reject('evidence_family')
    body = item.get('body')
    if not isinstance(body, dict):
        _reject('evidence_body')
    if item.get('content_hash') != body_digest(body):
        _reject('evidence_digest')
    if source_hash is not None and item.get('source_hash') != source_hash:
        _reject('evidence_source')
    expected = evidence_id(family, key, body, item.get('source_hash'), item.get('version'),
                           item.get('status'), item.get('reason'))
    if item.get('evidence_id') != expected:
        _reject('evidence_identity')
    links = item.get('links')
    if not isinstance(links, list):
        _reject('evidence_links')
    for link in links:
        if not isinstance(link, dict) or link.get('relation') not in RELATIONS:
            _reject('evidence_links')
        if not isinstance(link.get('target'), str) or not link['target']:
            _reject('evidence_links')
    return expected


def validate_package(package, local_sources=None, local_evidence=None):
    """Fully verify a package.  Raises ExchangeError with a stable reason."""
    if not isinstance(package, dict):
        _reject('package_not_object')
    if package.get('version') != PACKAGE_VERSION:
        _reject('package_version')
    if package.get('package_id') != package_id(package):
        _reject('package_digest')
    origin = package.get('origin')
    if not isinstance(origin, str) or not origin:
        _reject('origin_missing')
    start, head = package.get('start') or {}, package.get('head') or {}
    if not isinstance(start.get('seq'), int) or not isinstance(head.get('seq'), int):
        _reject('package_cursor')
    if start.get('seq') == 0 and start.get('head') != genesis(origin):
        # A chain always starts at the origin's genesis; anything else is an
        # attempt to splice into an existing chain.
        _reject('commit_genesis')
    commits = package.get('commits')
    context = package.get('context', [])
    context_sources = package.get('context_sources', [])
    if not isinstance(commits, list) or not isinstance(context, list) \
            or not isinstance(context_sources, list):
        _reject('commits_missing')
    known = set(local_evidence or ())
    context_ids = [_check_node(item) for item in context]
    if len(set(context_ids)) != len(context_ids):
        _reject('evidence_duplicate')
    known.update(context_ids)
    verified_context_sources = {}
    for chunk in context_sources:
        if not isinstance(chunk, dict):
            _reject('context_source_malformed')
        try:
            raw = base64.b64decode(chunk.get('bytes', ''), validate=True)
        except Exception:
            _reject('context_source_encoding')
        source_hash = hashlib.sha256(raw).hexdigest()
        if chunk.get('hash') != source_hash:
            _reject('context_source_digest')
        if not isinstance(chunk.get('kind'), str) or not chunk.get('kind'):
            _reject('context_source_kind')
        payload = _parse_json(raw, 'context_source')
        if not isinstance(payload, dict) or payload.get('version') != chunk.get('version'):
            _reject('context_source_version')
        verified_context_sources[source_hash] = raw
    if not commits:
        if head.get('seq') != start.get('seq') or head.get('head') != start.get('head'):
            _reject('package_cursor')
        if context_ids or context_sources:
            _reject('package_cursor')
        return {'evidence': known, 'context': context_ids, 'context_sources': {}}
    expected_prev, expected_seq = start.get('head'), start.get('seq')
    seen = set()
    for commit in commits:
        if not isinstance(commit, dict):
            _reject('commit_malformed')
        if commit.get('replica') != origin:
            _reject('commit_replica')
        seq = commit.get('seq')
        if not isinstance(seq, int) or seq != expected_seq + 1:
            _reject('commit_sequence')
        if commit.get('prev') != expected_prev:
            _reject('commit_chain')
        source = commit.get('source')
        if not isinstance(source, dict):
            _reject('source_missing')
        try:
            raw = base64.b64decode(source.get('bytes', ''), validate=True)
        except Exception:
            _reject('source_encoding')
        source_hash = hashlib.sha256(raw).hexdigest()
        if source.get('hash') != source_hash:
            _reject('source_digest')
        if not isinstance(source.get('kind'), str) or not source.get('kind'):
            _reject('source_kind')
        payload = _parse_json(raw, 'source')
        if not isinstance(payload, dict) or not isinstance(payload.get('version'), str) or not payload['version']:
            _reject('source_version')
        if source.get('version') != payload['version']:
            _reject('source_version')
        if commit.get('commit_id') != commit_id(origin, seq, expected_prev, source_hash):
            _reject('commit_digest')
        for item in commit.get('evidence') or []:
            eid = _check_node(item, source_hash)
            if eid in seen:
                _reject('evidence_duplicate')
            seen.add(eid)
        known.update(seen)
        for decision in commit.get('decisions') or []:
            if not isinstance(decision, dict):
                _reject('decision_malformed')
            if decision.get('source_hash') != source_hash:
                _reject('decision_source')
            if decision.get('decision_id') != decision_id(decision.get('subsystem'), decision.get('subject'),
                                                          decision.get('status'), decision.get('reason'),
                                                          source_hash):
                _reject('decision_identity')
        expected_prev, expected_seq = commit.get('commit_id'), seq
    if expected_prev != head.get('head') or expected_seq != head.get('seq'):
        _reject('package_head')
    for item in commits:
        for node in item.get('evidence') or []:
            for link in node['links']:
                if link['target'] not in known:
                    _reject('evidence_dangling')
    for node in context:
        for link in node['links']:
            if link['target'] not in known:
                _reject('evidence_dangling')
        if node['source_hash'] not in verified_context_sources and \
                node['source_hash'] not in {c['source']['hash'] for c in commits} and \
                node['source_hash'] not in (local_sources or ()):
            _reject('context_source_missing')
    return {'evidence': known, 'context': context_ids, 'context_sources': verified_context_sources}


# --------------------------------------------------------------------------
# Application
# --------------------------------------------------------------------------

def _local_context(db):
    sources = {row['hash'] for row in db.execute('SELECT hash FROM export_file')}
    evidence = {row['evidence_id'] for row in db.execute('SELECT evidence_id FROM evidence')}
    return sources, evidence


def _commit_known(db, commit):
    return db.execute('SELECT 1 FROM commit_chain WHERE commit_id=?', (commit['commit_id'],)).fetchone() is not None


def _sync_point(db, origin):
    row = db.execute('SELECT seq,head FROM remote_state WHERE replica_id=?', (origin,)).fetchone()
    return (row['seq'], row['head']) if row else (0, genesis(origin))


def _defer_reason(db, package):
    if not package.get('commits'):
        return None
    origin, start = package['origin'], package['start']
    if start['seq'] == 0:
        return None
    row = db.execute('SELECT commit_id FROM commit_chain WHERE replica_id=? AND seq=?',
                     (origin, start['seq'])).fetchone()
    if row:
        return None if row['commit_id'] == start['head'] else 'commit_fork'
    return 'parent_commit_missing'


def _store_node(db, item, source_hash, origin, seq, ordinal, commit):
    db.execute('INSERT OR IGNORE INTO evidence(evidence_id,family,entity_key,source_hash,content_hash,body,'
               'version,status,reason) VALUES (?,?,?,?,?,?,?,?,?)',
               (item['evidence_id'], item['family'], item['entity_key'], source_hash,
                item['content_hash'], canonical(item['body']), item['version'], item['status'],
                item['reason']))
    db.execute('INSERT OR IGNORE INTO evidence_origin(evidence_id,replica_id,seq,ordinal,commit_id) '
               'VALUES (?,?,?,?,?)', (item['evidence_id'], origin, seq, ordinal, commit))


def _context_sources(package):
    return {chunk['hash']: base64.b64decode(chunk['bytes']) for chunk in package.get('context_sources') or []}


def _context_meta(package, source_hash):
    return next(chunk for chunk in package['context_sources'] if chunk['hash'] == source_hash)


def _apply(db, package, crash_after=None):
    origin = package['origin']
    applied = 0
    for source_hash, raw in (package.get('context_sources') and
                             _context_sources(package) or {}).items():
        if not db.execute('SELECT 1 FROM export_file WHERE hash=?', (source_hash,)).fetchone():
            meta = _context_meta(package, source_hash)
            local_id = db.execute('INSERT INTO export_file(kind,version,hash,body) VALUES (?,?,?,?)',
                                  (meta['kind'], meta['version'], source_hash, raw)).lastrowid
            payload = json.loads(raw.decode('utf-8'))
            for i, row in enumerate(payload.get('rows', []), 1):
                db.execute('INSERT INTO export_record(source_id,line_no,identity,body,status) '
                           'VALUES (?,?,?,?,?)',
                           (local_id, i, str(row.get('id', i)),
                            json.dumps(row, sort_keys=True, ensure_ascii=False), 'captured'))
    for item in package.get('context') or []:
        for origin_row in item.get('origins') or []:
            _store_node(db, item, item['source_hash'], origin_row['replica'], origin_row['seq'],
                        origin_row['ordinal'], 'context')
            local = db.execute('SELECT id FROM export_file WHERE hash=?', (item['source_hash'],)).fetchone()
            if local:
                db.execute('INSERT OR IGNORE INTO arrival(source_hash,commit_id,replica_id,seq,local_source_id) '
                           'VALUES (?,?,?,?,?)',
                           (item['source_hash'], origin_row.get('commit') or f"context:{item['evidence_id']}",
                            origin_row['replica'], origin_row['seq'], local['id']))
        for link in item.get('links') or []:
            db.execute('INSERT OR IGNORE INTO evidence_link(child,parent,relation) VALUES (?,?,?)',
                       (item['evidence_id'], link['target'], link['relation']))
    for commit in package['commits']:
        if _commit_known(db, commit):
            continue
        source = commit['source']
        raw = base64.b64decode(source['bytes'])
        source_hash = source['hash']
        local = db.execute('SELECT id FROM export_file WHERE hash=?', (source_hash,)).fetchone()
        if local:
            source_id = local['id']
        else:
            source_id = db.execute('INSERT INTO export_file(kind,version,hash,body) VALUES (?,?,?,?)',
                                   (source['kind'], source['version'], source_hash, raw)).lastrowid
            payload = json.loads(raw.decode('utf-8'))
            for i, row in enumerate(payload.get('rows', []), 1):
                text = json.dumps(row, sort_keys=True, ensure_ascii=False)
                db.execute('INSERT INTO export_record(source_id,line_no,identity,body,status) VALUES (?,?,?,?,?)',
                           (source_id, i, str(row.get('id', i)), text, 'captured'))
        db.execute('INSERT OR IGNORE INTO commit_chain(replica_id,seq,prev,commit_id,source_hash,kind,version) '
                   'VALUES (?,?,?,?,?,?,?)',
                   (origin, commit['seq'], commit['prev'], commit['commit_id'], source_hash,
                    source['kind'], source['version']))
        db.execute('INSERT OR IGNORE INTO arrival(source_hash,commit_id,replica_id,seq,local_source_id) '
                   'VALUES (?,?,?,?,?)', (source_hash, commit['commit_id'], origin, commit['seq'], source_id))
        for ordinal, item in enumerate(commit['evidence'], 1):
            _store_node(db, item, source_hash, origin, commit['seq'], ordinal, commit['commit_id'])
            applied += 1
            if crash_after is not None and applied >= crash_after:
                raise SimulatedCrash('crash_after_evidence')
        for item in commit['evidence']:
            for link in item['links']:
                db.execute('INSERT OR IGNORE INTO evidence_link(child,parent,relation) VALUES (?,?,?)',
                           (item['evidence_id'], link['target'], link['relation']))
        for decision in commit['decisions']:
            db.execute('INSERT OR IGNORE INTO decision_evidence(decision_id,replica_id,seq,commit_id,subsystem,'
                       'subject,status,reason,source_hash) VALUES (?,?,?,?,?,?,?,?,?)',
                       (decision['decision_id'], origin, commit['seq'], commit['commit_id'],
                        decision['subsystem'], str(decision['subject']), decision['status'],
                        decision['reason'], source_hash))
        current = db.execute('SELECT seq FROM remote_state WHERE replica_id=?', (origin,)).fetchone()
        if not current or commit['seq'] > current['seq']:
            db.execute('INSERT INTO remote_state(replica_id,seq,head) VALUES (?,?,?) '
                       'ON CONFLICT(replica_id) DO UPDATE SET seq=excluded.seq,head=excluded.head,'
                       'updated=CURRENT_TIMESTAMP', (origin, commit['seq'], commit['commit_id']))
    return applied


def _log(db, package_id_, origin, status, reason, cursor):
    db.execute('INSERT INTO merge_log(package_id,origin,status,reason,cursor) VALUES (?,?,?,?,?)',
               (package_id_, origin, status, reason, cursor))


def _try_pending(db):
    resumed = 0
    for _ in range(MAX_PENDING_ROUNDS):
        progressed = False
        for row in db.execute('SELECT * FROM pending_package ORDER BY rowid').fetchall():
            package = json.loads(row['payload'])
            if _defer_reason(db, package):
                continue
            sources, evidence = _local_context(db)
            verified = validate_package(package, sources, evidence)
            _apply(db, package)
            db.execute('DELETE FROM pending_package WHERE package_id=?', (row['package_id'],))
            _log(db, row['package_id'], package['origin'], 'resumed', 'parent_commit_available',
                 str(_sync_point(db, package['origin'])[0]))
            resumed += 1
            progressed = True
        if not progressed:
            break
    return resumed


def receive(path, package, crash_after=None):
    db = connect(path)
    try:
        package = load_package(package)
        pid = package.get('package_id') if isinstance(package, dict) else None
        origin = package.get('origin') if isinstance(package, dict) else None
        if origin == replica_id(db):
            # A replica cannot import a package claiming to be itself.
            with db:
                _log(db, pid, origin, 'rejected', 'origin_is_local', str(_sync_point(db, origin)[0]))
            return {'package_id': pid, 'origin': origin, 'status': 'rejected',
                    'reason': 'origin_is_local', 'cursor': _sync_point(db, origin)[0]}
        if pid:
            previous = db.execute('SELECT * FROM exchange_receipt WHERE package_id=?', (pid,)).fetchone()
            if previous and previous['status'] == 'applied':
                with db:
                    _log(db, pid, origin, 'duplicate', 'already_applied', previous['reason'])
                return {'package_id': pid, 'origin': origin, 'status': 'duplicate',
                        'reason': 'already_applied', 'cursor': int(previous['reason'])}
            if previous and previous['status'] == 'rejected':
                # A deterministic rejection is not retried with the same bytes.
                with db:
                    _log(db, pid, origin, 'duplicate', 'previously_rejected', previous['reason'])
                return {'package_id': pid, 'origin': origin, 'status': 'rejected',
                        'reason': previous['reason'], 'cursor': _sync_point(db, origin)[0]}
        try:
            sources, evidence = _local_context(db)
            verified = validate_package(package, sources, evidence)
        except ExchangeError as exc:
            reason = str(exc)
            cursor = _sync_point(db, origin or '')[0] if isinstance(origin, str) else 0
            with db:
                if pid:
                    db.execute('INSERT INTO exchange_receipt(package_id,origin,status,reason,applied) '
                               'VALUES (?,?,?,?,0) ON CONFLICT(package_id) DO UPDATE SET '
                               'status=excluded.status,reason=excluded.reason,applied=0',
                               (pid, origin or '', 'rejected', reason))
                _log(db, pid, origin, 'rejected', reason, str(cursor))
            return {'package_id': pid, 'origin': origin, 'status': 'rejected', 'reason': reason,
                    'cursor': cursor}
        defer = _defer_reason(db, package)
        if defer is None and not package.get('commits') and _sync_point(db, origin)[0] >= package['head']['seq']:
            with db:
                _log(db, pid, origin, 'duplicate', 'cursor_already_at_or_beyond_head',
                     str(_sync_point(db, origin)[0]))
            return {'package_id': pid, 'origin': origin, 'status': 'duplicate',
                    'reason': 'cursor_already_at_or_beyond_head',
                    'cursor': _sync_point(db, origin)[0]}
        if defer == 'commit_fork':
            with db:
                _log(db, pid, origin, 'rejected', defer, str(_sync_point(db, origin)[0]))
            return {'package_id': pid, 'origin': origin, 'status': 'rejected', 'reason': defer,
                    'cursor': _sync_point(db, origin)[0]}
        if defer:
            with db:
                db.execute('INSERT OR IGNORE INTO pending_package(package_id,origin,payload,missing) '
                           'VALUES (?,?,?,?)', (pid, origin, canonical(package), package['start']['head']))
                _log(db, pid, origin, 'deferred', defer, str(_sync_point(db, origin)[0]))
            return {'package_id': pid, 'origin': origin, 'status': 'deferred', 'reason': defer,
                    'cursor': _sync_point(db, origin)[0]}
        before = _sync_point(db, origin)
        try:
            with db:
                applied = _apply(db, package, crash_after=crash_after)
                _try_pending(db)
                after = _sync_point(db, origin)
                db.execute('INSERT INTO exchange_receipt(package_id,origin,status,reason,applied) '
                           'VALUES (?,?,?,?,?) ON CONFLICT(package_id) DO UPDATE SET '
                           'status=excluded.status,reason=excluded.reason,applied=excluded.applied',
                           (pid, origin, 'applied', str(after[0]), 1))
                _log(db, pid, origin, 'applied', 'merged', str(after[0]))
        except SimulatedCrash:
            # The transaction rolled back: no commit, evidence link or receipt
            # from this package is visible.
            db.rollback()
            with db:
                _log(db, pid, origin, 'crashed', 'simulated_crash', str(before[0]))
            raise
        after = _sync_point(db, origin)
        return {'package_id': pid, 'origin': origin, 'status': 'applied', 'reason': 'merged',
                'cursor': after[0], 'head': after[1], 'evidence': applied}
    finally:
        db.close()


def load_package(package):
    """Accept a dict, a filesystem path, or the JSON text of a package."""
    if isinstance(package, dict):
        return package
    if isinstance(package, Path):
        return json.loads(package.read_text(encoding='utf-8'))
    if isinstance(package, str):
        if package.lstrip().startswith('{'):
            return json.loads(package)
        return json.loads(Path(package).read_text(encoding='utf-8'))
    raise ExchangeError('package_malformed')


def receive_many(path, packages, crash_after=None):
    return [receive(path, package, crash_after=crash_after) for package in packages]


# --------------------------------------------------------------------------
# Canonical merged evidence graph
# --------------------------------------------------------------------------

def canonical_graph(path):
    db = connect(path)
    try:
        sources = []
        for row in db.execute('SELECT hash,kind,version,body FROM export_file ORDER BY hash'):
            # Only chained commits are arrival paths.  Context-carried bytes
            # have no origin commit of their own and must not alter identity.
            arrivals = sorted(({'replica': a['replica_id'], 'seq': a['seq'], 'commit': a['commit_id']}
                               for a in db.execute(
                                   'SELECT * FROM arrival a WHERE a.source_hash=? AND EXISTS '
                                   '(SELECT 1 FROM commit_chain c WHERE c.commit_id=a.commit_id)',
                                   (row['hash'],))),
                              key=lambda x: (x['replica'], x['seq'], x['commit']))
            sources.append({'hash': row['hash'], 'kind': row['kind'], 'version': row['version'],
                            'verified': hashlib.sha256(row['body']).hexdigest() == row['hash'],
                            'arrivals': arrivals})
        commits = sorted(({'replica': c['replica_id'], 'seq': c['seq'], 'prev': c['prev'],
                           'commit_id': c['commit_id'], 'source_hash': c['source_hash']}
                          for c in db.execute('SELECT * FROM commit_chain')),
                         key=lambda x: (x['replica'], x['seq']))
        nodes = []
        for row in db.execute('SELECT * FROM evidence'):
            origins = sorted(({'replica': o['replica_id'], 'seq': o['seq'], 'ordinal': o['ordinal']}
                              for o in db.execute('SELECT * FROM evidence_origin WHERE evidence_id=?',
                                                  (row['evidence_id'],))),
                             key=lambda x: (x['replica'], x['seq'], x['ordinal']))
            links = sorted(((x['relation'], x['parent']) for x in db.execute(
                'SELECT relation,parent FROM evidence_link WHERE child=?', (row['evidence_id'],))),
                key=lambda x: (x[0], x[1]))
            nodes.append({'evidence_id': row['evidence_id'], 'family': row['family'],
                          'entity_key': row['entity_key'], 'source_hash': row['source_hash'],
                          'content_hash': row['content_hash'], 'body': json.loads(row['body']),
                          'version': row['version'], 'status': row['status'], 'reason': row['reason'],
                          'origins': origins, 'links': [{'relation': r, 'target': t} for r, t in links]})
        nodes.sort(key=lambda x: x['evidence_id'])
        decisions = sorted(({'decision_id': d['decision_id'], 'replica': d['replica_id'],
                             'seq': d['seq'], 'subsystem': d['subsystem'], 'subject': d['subject'],
                             'status': d['status'], 'reason': d['reason'],
                             'source_hash': d['source_hash']}
                            for d in db.execute('SELECT * FROM decision_evidence')),
                           key=lambda x: (x['decision_id'], x['replica']))
        return {'sources': sources, 'commits': commits, 'evidence': nodes, 'decisions': decisions}
    finally:
        db.close()


def sync_status(path):
    """Sync state for a ledger path or an already-open connection."""
    if hasattr(path, 'execute'):
        return _sync_status(path)
    db = connect(path)
    try:
        return _sync_status(db)
    finally:
        db.close()


def load_package(package):
    """Accept a dict, a filesystem path, or the JSON text of a package."""
    if isinstance(package, dict):
        return package
    if isinstance(package, Path):
        return json.loads(package.read_text(encoding='utf-8'))
    if isinstance(package, str):
        if package.lstrip().startswith('{'):
            return json.loads(package)
        return json.loads(Path(package).read_text(encoding='utf-8'))
    raise ExchangeError('package_malformed')


def receive_many(path, packages, crash_after=None):
    return [receive(path, package, crash_after=crash_after) for package in packages]


# --------------------------------------------------------------------------
# Canonical merged evidence graph
# --------------------------------------------------------------------------

def canonical_graph(path):
    db = connect(path)
    try:
        sources = []
        for row in db.execute('SELECT hash,kind,version,body FROM export_file ORDER BY hash'):
            # Only chained commits are arrival paths.  Context-carried bytes
            # have no origin commit of their own and must not alter identity.
            arrivals = sorted(({'replica': a['replica_id'], 'seq': a['seq'], 'commit': a['commit_id']}
                               for a in db.execute(
                                   'SELECT * FROM arrival a WHERE a.source_hash=? AND EXISTS '
                                   '(SELECT 1 FROM commit_chain c WHERE c.commit_id=a.commit_id)',
                                   (row['hash'],))),
                              key=lambda x: (x['replica'], x['seq'], x['commit']))
            sources.append({'hash': row['hash'], 'kind': row['kind'], 'version': row['version'],
                            'verified': hashlib.sha256(row['body']).hexdigest() == row['hash'],
                            'arrivals': arrivals})
        commits = sorted(({'replica': c['replica_id'], 'seq': c['seq'], 'prev': c['prev'],
                           'commit_id': c['commit_id'], 'source_hash': c['source_hash']}
                          for c in db.execute('SELECT * FROM commit_chain')),
                         key=lambda x: (x['replica'], x['seq']))
        nodes = []
        for row in db.execute('SELECT * FROM evidence'):
            origins = sorted(({'replica': o['replica_id'], 'seq': o['seq'], 'ordinal': o['ordinal']}
                              for o in db.execute('SELECT * FROM evidence_origin WHERE evidence_id=?',
                                                  (row['evidence_id'],))),
                             key=lambda x: (x['replica'], x['seq'], x['ordinal']))
            links = sorted(((x['relation'], x['parent']) for x in db.execute(
                'SELECT relation,parent FROM evidence_link WHERE child=?', (row['evidence_id'],))),
                key=lambda x: (x[0], x[1]))
            nodes.append({'evidence_id': row['evidence_id'], 'family': row['family'],
                          'entity_key': row['entity_key'], 'source_hash': row['source_hash'],
                          'content_hash': row['content_hash'], 'body': json.loads(row['body']),
                          'version': row['version'], 'status': row['status'], 'reason': row['reason'],
                          'origins': origins, 'links': [{'relation': r, 'target': t} for r, t in links]})
        nodes.sort(key=lambda x: x['evidence_id'])
        decisions = sorted(({'decision_id': d['decision_id'], 'replica': d['replica_id'],
                             'seq': d['seq'], 'subsystem': d['subsystem'], 'subject': d['subject'],
                             'status': d['status'], 'reason': d['reason'],
                             'source_hash': d['source_hash']}
                            for d in db.execute('SELECT * FROM decision_evidence')),
                           key=lambda x: (x['decision_id'], x['replica']))
        return {'sources': sources, 'commits': commits, 'evidence': nodes, 'decisions': decisions}
    finally:
        db.close()


def sync_status(path):
    """Sync state for a ledger path or an already-open connection."""
    if hasattr(path, 'execute'):
        return _sync_status(path)
    db = connect(path)
    try:
        return _sync_status(db)
    finally:
        db.close()


def _sync_status(db):
    local = state(db)
    chain = db.execute('SELECT * FROM commit_chain WHERE replica_id=? ORDER BY seq',
                       (local['replica_id'],)).fetchall()
    prev, ok = genesis(local['replica_id']), True
    for expected, row in enumerate(chain, 1):
        if row['seq'] != expected or row['prev'] != prev or \
                row['commit_id'] != commit_id(local['replica_id'], row['seq'], prev, row['source_hash']):
            ok = False
            break
        prev = row['commit_id']
    ok = ok and prev == local['head']
    remotes, pending = [], []
    for row in db.execute('SELECT * FROM remote_state ORDER BY replica_id'):
        head = db.execute('SELECT commit_id FROM commit_chain WHERE replica_id=? AND seq=?',
                          (row['replica_id'], row['seq'])).fetchone()
        remotes.append({'replica': row['replica_id'], 'seq': row['seq'], 'head': row['head'],
                        'verified': bool(head) and head['commit_id'] == row['head']})
    for row in db.execute('SELECT * FROM pending_package ORDER BY rowid'):
        pending.append({'package': row['package_id'], 'origin': row['origin'],
                        'missing': row['missing']})
    return {'replica_id': local['replica_id'], 'seq': local['seq'], 'head': local['head'],
            'chain_verified': ok, 'remotes': remotes, 'pending': pending}
