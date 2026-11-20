"""Capture entry point with instrument source authenticity verification.

The raw bytes are still stored exactly as before and the SHA-256 digest only proves
post-ingest integrity. In a strict store (a pre-installed trust root exists) every
export additionally carries an instrument signature over:

* the canonical payload digest,
* the instrument identity,
* the monotonic per-instrument sequence number,
* the previous capture's payload digest.

Canonicalization is used for verification only. Re-presenting the same canonical
payload with different JSON whitespace or field order returns the original capture
instead of creating a second one.
"""
import hashlib
import json
from pathlib import Path

from .store import connect
from . import trust


def _store_records(db, source_id, payload):
    for i, item in enumerate(payload.get('rows', []), 1):
        identity = str(item.get('id', i))
        db.execute('INSERT INTO export_record(source_id,line_no,identity,body,status) VALUES (?,?,?,?,?)',
                   (source_id, i, identity,
                    json.dumps(item, sort_keys=True, ensure_ascii=False), 'captured'))


def capture(path, kind, file):
    raw = Path(file).read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    payload = json.loads(raw)
    if not isinstance(payload, dict) or not isinstance(payload.get('version'), str) \
            or not payload['version']:
        raise ValueError('version_required')
    db = connect(path)
    try:
        with db:
            old = db.execute('SELECT id FROM export_file WHERE kind=? AND hash=?',
                             (kind, digest)).fetchone()
            if old:
                return old['id'], payload

            strict = trust.trust_mode(db)
            proof = payload.get('source_proof') if strict else None
            business_payload = {k: v for k, v in payload.items() if k != 'source_proof'}
            canonical_digest = trust.payload_digest(business_payload)

            replay = None
            if strict and isinstance(proof, dict):
                replay = db.execute(
                    'SELECT f.id FROM export_file f JOIN source_proof p ON p.source_id=f.id '
                    'WHERE p.instrument_id=? AND p.sequence=? AND p.payload_digest=? LIMIT 1',
                    (str(proof.get('instrument_id')),
                     _safe_int(proof.get('sequence')), canonical_digest)).fetchone()
            if replay is not None:
                # Same canonical payload, different JSON spelling: one capture only.
                return replay['id'], payload

            source_id = db.execute(
                'INSERT INTO export_file(kind,version,hash,body) VALUES (?,?,?,?)',
                (kind, payload['version'], digest, raw)).lastrowid
            _store_records(db, source_id, business_payload)

            if not strict:
                return source_id, payload

            if not isinstance(proof, dict):
                db.execute('INSERT INTO source_proof(source_id,instrument_id,sequence,'
                           'key_id,prev_digest,payload_digest,signature,present) '
                           'VALUES (?,NULL,NULL,NULL,NULL,?,NULL,0)',
                           (source_id, canonical_digest))
                trust.recompute(db)
                return source_id, payload

            try:
                instrument_id = str(proof['instrument_id'])
                sequence = int(proof['sequence'])
                key_id = str(proof['key_id'])
                prev_digest = proof['prev_digest']
                signature = trust.b64d(str(proof['signature']))
            except (KeyError, TypeError, ValueError):
                instrument_id = sequence = key_id = None
                prev_digest = signature = None
            db.execute('INSERT INTO source_proof(source_id,instrument_id,sequence,key_id,'
                       'prev_digest,payload_digest,signature,present) '
                       'VALUES (?,?,?,?,?,?,?,1)',
                       (source_id, instrument_id, sequence, key_id, prev_digest,
                        canonical_digest, signature))
            trust.recompute(db)
            return source_id, payload
    finally:
        db.close()


def _safe_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def verify_source(path, file):
    """CLI-facing source adjudication for one export. Stores nothing; reports both
    registered contracts separately instead of a single verified boolean."""
    raw = Path(file).read_bytes()
    payload = json.loads(raw)
    if not isinstance(payload, dict) or not isinstance(payload.get('version'), str) \
            or not payload['version']:
        raise ValueError('version_required')
    proof = payload.get('source_proof')
    business_payload = {k: v for k, v in payload.items() if k != 'source_proof'}
    digest = trust.payload_digest(business_payload)
    db = connect(path)
    try:
        if not trust.trust_mode(db):
            return {'file': str(file), 'raw_sha256': hashlib.sha256(raw).hexdigest(),
                    'mode': 'legacy',
                    'archive': {'status': trust.ARCHIVE_REJECTED,
                                'reason': trust.REASON_LEGACY},
                    'business': {'status': trust.BUSINESS_REJECTED,
                                 'reason': trust.REASON_LEGACY}}
        if not isinstance(proof, dict):
            return {'file': str(file), 'raw_sha256': hashlib.sha256(raw).hexdigest(),
                    'mode': 'strict',
                    'archive': {'status': trust.ARCHIVE_REJECTED,
                                'reason': trust.REASON_PROOF_MISSING},
                    'business': {'status': trust.BUSINESS_REJECTED,
                                 'reason': trust.REASON_PROOF_MISSING}}
        info, reason = trust.static_check(db, proof, digest)
        if info is None:
            return {'file': str(file), 'raw_sha256': hashlib.sha256(raw).hexdigest(),
                    'mode': 'strict', 'instrument_id': proof.get('instrument_id'),
                    'sequence': _safe_int(proof.get('sequence')),
                    'archive': {'status': trust.ARCHIVE_REJECTED, 'reason': reason},
                    'business': {'status': trust.BUSINESS_REJECTED, 'reason': reason}}
        raw_hash = hashlib.sha256(raw).hexdigest()
        # Prefer the exact stored capture (same bytes), so forks at one sequence are
        # distinguished instead of collapsing onto an arbitrary copy.
        stored = db.execute('SELECT v.* FROM export_file f JOIN source_verdict v '
                            'ON v.source_id=f.id WHERE f.hash=?', (raw_hash,)).fetchone()
        if stored is None:
            stored = db.execute('SELECT v.* FROM source_proof p JOIN source_verdict v '
                                'ON v.source_id=p.source_id WHERE p.instrument_id=? AND p.sequence=? '
                                'AND p.payload_digest=?',
                                (info['instrument_id'], info['sequence'], digest)).fetchone()
        if stored is None:
            result = {'file': str(file), 'raw_sha256': hashlib.sha256(raw).hexdigest(),
                      'mode': 'strict', 'instrument_id': info['instrument_id'],
                      'sequence': info['sequence'],
                      'archive': {'status': trust.ARCHIVE_PENDING,
                                  'reason': trust.REASON_PENDING_PREDECESSOR},
                      'business': {'status': trust.BUSINESS_PENDING,
                                   'reason': trust.REASON_PENDING_PREDECESSOR}}
        else:
            result = {'file': str(file), 'raw_sha256': hashlib.sha256(raw).hexdigest(),
                      'mode': 'strict', 'instrument_id': info['instrument_id'],
                      'sequence': info['sequence'],
                      'archive': {'status': stored['archive_status'],
                                  'reason': stored['archive_reason']},
                      'business': {'status': stored['business_status'],
                                   'reason': stored['business_reason']}}
        return result
    finally:
        db.close()
