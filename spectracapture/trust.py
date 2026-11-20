"""Instrument source authenticity: trust roots, key rotation, hash chains, forks, revocations.

Two registered contracts are kept as separate verdicts and never collapsed into one
boolean:

* archive contract  -- bytes, signature and predecessor must be checkable; forked or
  later-revoked evidence stays authentic dispute evidence;
* business contract -- additionally requires no device fork and no revocation range
  coverage before the source may support new results.

Cryptography is Ed25519 over a canonical JSON encoding. Canonicalization is used only
for signature verification; the original bytes are always stored unchanged.
"""
import base64
import hashlib
import json

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT_KEY_ID = 'root'
DOC_ROTATION = 'spectracapture.key_rotation/v1'
DOC_REVOCATION = 'spectracapture.revocation/v1'

# Archive contract verdicts.
ARCHIVE_VERIFIABLE = '可归档'
ARCHIVE_PENDING = '待补链'
ARCHIVE_REJECTED = '拒绝'
# Business contract verdicts.
BUSINESS_USABLE = '可业务使用'
BUSINESS_FROZEN = '冻结'
BUSINESS_PENDING = '待补链'
BUSINESS_REJECTED = '拒绝'

REASON_PROOF_MISSING = 'proof_missing'
REASON_BAD_PROOF = 'malformed_proof'
REASON_INSTRUMENT_UNKNOWN = 'instrument_unknown'
REASON_KEY_UNKNOWN = 'key_unknown'
REASON_SIGNATURE_INVALID = 'signature_invalid'
REASON_PAYLOAD_DIGEST_MISMATCH = 'payload_digest_mismatch'
REASON_KEY_NOT_YET_VALID = 'key_not_yet_valid'
REASON_ROTATION_INVALID = 'rotation_invalid'
REASON_GENESIS_MISMATCH = 'genesis_sequence_mismatch'
REASON_SEQUENCE_ORDER = 'sequence_gap'
REASON_PREV_DIGEST_MISMATCH = 'prev_digest_mismatch'
REASON_PENDING_PREDECESSOR = 'predecessor_missing'
REASON_DEVICE_FORK = 'device_fork'
REASON_SUCCESSOR_OF_FORK = 'successor_of_fork'
REASON_REVOKED = 'key_revoked'
REASON_CHAIN_VALID = 'chain_verified'
REASON_LEGACY = 'legacy_store_no_trust_root'
REASON_REPLAY = 'canonical_replay_same_source'


def canonical(obj):
    """Deterministic JSON encoding used for every signed message and payload digest."""
    return json.dumps(obj, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def sha256_hex(data):
    if isinstance(data, str):
        data = data.encode('utf-8')
    return hashlib.sha256(data).hexdigest()


def payload_digest(payload):
    return sha256_hex(canonical(payload))


def b64e(raw):
    return base64.b64encode(raw).decode('ascii')


def b64d(text):
    return base64.b64decode(text.encode('ascii'))


# --------------------------------------------------------------------------- key handling

def generate_private_key():
    return Ed25519PrivateKey.generate()


def public_bytes(private_key):
    return private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw)


def sign(private_key, document):
    return private_key.sign(canonical(document).encode('utf-8'))


def verify(public_key, signature, document):
    """Return True on a valid Ed25519 signature; never raises on bad input."""
    try:
        public_key.verify(signature, canonical(document).encode('utf-8'))
        return True
    except (InvalidSignature, TypeError, ValueError):
        return False


def sign_export(private_key, instrument_id, sequence, prev_digest, payload,
                key_id=ROOT_KEY_ID):
    """Build a signed export envelope. The proof covers the canonical payload digest,
    instrument identity, monotonic sequence number and predecessor digest.

    The digest is computed over the business payload without the proof block, so it
    stays identical when the same payload is re-spelled with different JSON whitespace
    or field order."""
    business = {k: v for k, v in payload.items() if k != 'source_proof'}
    digest = payload_digest(business)
    document = ['spectracapture.export/v1', digest, instrument_id, sequence,
                prev_digest, key_id]
    signature = sign(private_key, document)
    return dict(business, source_proof={
        'version': 'spectracapture.export/v1',
        'instrument_id': instrument_id,
        'sequence': sequence,
        'key_id': key_id,
        'prev_digest': prev_digest,
        'payload_digest': digest,
        'signature': b64e(signature),
    })


def build_rotation(instrument_id, old_private_key, old_key_id,
                   new_private_key, new_key_id, from_sequence, root_private_key):
    """A rotation statement must be signed by BOTH the old key and the trust root."""
    new_pub = public_bytes(new_private_key)
    statement = [DOC_ROTATION, instrument_id, old_key_id, new_key_id,
                 b64e(new_pub), from_sequence]
    return {
        'document_type': DOC_ROTATION,
        'instrument_id': instrument_id,
        'old_key_id': old_key_id,
        'new_key_id': new_key_id,
        'new_public_key': b64e(new_pub),
        'from_sequence': from_sequence,
        'old_signature': b64e(sign(old_private_key, statement)),
        'root_signature': b64e(sign(root_private_key, statement)),
    }


def build_revocation(instrument_id, first_sequence, last_sequence, root_private_key,
                     bulletin_id, issuer='lab-trust-anchor', key_id=None):
    """Key theft bulletin. It names sequence ranges and an issuing source; export files'
    own wall-clock time is never consulted when applying it."""
    pub = public_bytes(root_private_key)
    statement = [DOC_REVOCATION, bulletin_id, instrument_id, issuer, key_id,
                 first_sequence, last_sequence, b64e(pub)]
    return {
        'document_type': DOC_REVOCATION,
        'bulletin_id': bulletin_id,
        'instrument_id': instrument_id,
        'issuer': issuer,
        'key_id': key_id,
        'first_sequence': first_sequence,
        'last_sequence': last_sequence,
        'root_public_key': b64e(pub),
        'signature': b64e(sign(root_private_key, statement)),
    }


# --------------------------------------------------------------------------- trust store

def import_trust_root(db, instrument_id, public_key):
    """Pre-install the instrument trust root and register its genesis (root) key,
    which is valid from sequence 1."""
    row = db.execute('SELECT public_key FROM trust_root WHERE instrument_id=?',
                     (instrument_id,)).fetchone()
    if row is not None:
        if bytes(row['public_key']) != bytes(public_key):
            raise ValueError('trust_root_conflict')
        return
    db.execute('INSERT INTO trust_root(instrument_id,public_key,key_id) VALUES (?,?,?)',
               (instrument_id, bytes(public_key), ROOT_KEY_ID))
    db.execute('INSERT INTO signing_key(instrument_id,key_id,public_key,from_sequence,status) '
               'VALUES (?,?,?,?,?)',
               (instrument_id, ROOT_KEY_ID, bytes(public_key), 1, 'active'))


def trust_mode(db):
    """A store with no pre-installed trust root keeps the legacy capture behavior."""
    return db.execute('SELECT 1 FROM trust_root LIMIT 1').fetchone() is not None


def import_rotation(db, doc):
    """Validate a rotation against both the old key and the trust root.

    Invalid rotations are recorded as rejected and never add a signing key, so exports
    signed by the purported new key keep failing signature verification."""
    required = ('document_type', 'instrument_id', 'old_key_id', 'new_key_id',
                'new_public_key', 'from_sequence', 'old_signature', 'root_signature')
    try:
        if doc.get('document_type') != DOC_ROTATION:
            raise ValueError(REASON_BAD_PROOF)
        for field in required:
            if field not in doc:
                raise ValueError(REASON_BAD_PROOF)
        instrument_id = str(doc['instrument_id'])
        old_key_id = str(doc['old_key_id'])
        new_key_id = str(doc['new_key_id'])
        from_sequence = int(doc['from_sequence'])
        new_pub = b64d(str(doc['new_public_key']))
        old_sig = b64d(str(doc['old_signature']))
        root_sig = b64d(str(doc['root_signature']))
    except (TypeError, ValueError):
        raise ValueError(REASON_ROTATION_INVALID)

    root = db.execute('SELECT * FROM trust_root WHERE instrument_id=?',
                      (instrument_id,)).fetchone()
    old = db.execute('SELECT * FROM signing_key WHERE instrument_id=? AND key_id=?',
                     (instrument_id, old_key_id)).fetchone()
    statement = [DOC_ROTATION, instrument_id, old_key_id, new_key_id,
                 b64e(new_pub), from_sequence]
    already = db.execute('SELECT * FROM key_rotation WHERE instrument_id=? AND new_key_id=?',
                         (instrument_id, new_key_id)).fetchone()
    if already:
        return {'status': already['status'], 'reason': already['reason']}

    def reject(reason):
        db.execute('INSERT INTO key_rotation(instrument_id,old_key_id,new_key_id,'
                   'new_public_key,from_sequence,old_signature,root_signature,status,reason) '
                   'VALUES (?,?,?,?,?,?,?,?,?)',
                   (instrument_id, old_key_id, new_key_id, new_pub, from_sequence,
                    old_sig, root_sig, 'rejected', reason))
        return {'status': 'rejected', 'reason': reason}

    if root is None:
        return reject(REASON_INSTRUMENT_UNKNOWN)
    if old is None:
        return reject(REASON_KEY_UNKNOWN)
    if from_sequence <= int(old['from_sequence']):
        return reject(REASON_SEQUENCE_ORDER)
    latest = db.execute('SELECT max(from_sequence) AS m FROM signing_key WHERE instrument_id=?',
                        (instrument_id,)).fetchone()['m']
    if latest is not None and int(old['from_sequence']) != int(latest):
        return reject(REASON_ROTATION_INVALID)
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    root_pub = Ed25519PublicKey.from_public_bytes(bytes(root['public_key']))
    old_pub = Ed25519PublicKey.from_public_bytes(bytes(old['public_key']))
    if not verify(root_pub, root_sig, statement):
        return reject(REASON_SIGNATURE_INVALID)
    if not verify(old_pub, old_sig, statement):
        return reject(REASON_SIGNATURE_INVALID)

    db.execute('INSERT INTO key_rotation(instrument_id,old_key_id,new_key_id,'
               'new_public_key,from_sequence,old_signature,root_signature,status,reason) '
               'VALUES (?,?,?,?,?,?,?,?,?)',
               (instrument_id, old_key_id, new_key_id, new_pub, from_sequence,
                old_sig, root_sig, 'active', 'rotation_verified'))
    db.execute('INSERT INTO signing_key(instrument_id,key_id,public_key,from_sequence,status) '
               'VALUES (?,?,?,?,?)',
               (instrument_id, new_key_id, new_pub, from_sequence, 'active'))
    recompute(db, instrument_id)
    return {'status': 'active', 'reason': 'rotation_verified'}


def import_revocation(db, doc):
    """Validate and store a key theft bulletin, then recompute the instrument's chain.

    Only sequence numbers decide whether a capture falls in the affected range; the
    wall-clock time claimed inside export files is never used."""
    try:
        if doc.get('document_type') != DOC_REVOCATION:
            raise ValueError(REASON_BAD_PROOF)
        bulletin_id = str(doc['bulletin_id'])
        instrument_id = str(doc['instrument_id'])
        issuer = str(doc['issuer'])
        key_id = doc.get('key_id')
        first_sequence = int(doc['first_sequence'])
        last_sequence = int(doc['last_sequence'])
        signature = b64d(str(doc['signature']))
        root_pub_raw = b64d(str(doc['root_public_key']))
    except (TypeError, ValueError):
        raise ValueError(REASON_BAD_PROOF)
    if first_sequence < 1 or last_sequence < first_sequence:
        raise ValueError(REASON_BAD_PROOF)
    if db.execute('SELECT 1 FROM revocation_bulletin WHERE bulletin_id=?',
                  (bulletin_id,)).fetchone():
        return {'status': 'recorded', 'reason': 'duplicate_bulletin'}
    root = db.execute('SELECT * FROM trust_root WHERE instrument_id=?',
                      (instrument_id,)).fetchone()
    if root is None or bytes(root['public_key']) != root_pub_raw:
        raise ValueError(REASON_INSTRUMENT_UNKNOWN)
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    root_pub = Ed25519PublicKey.from_public_bytes(root_pub_raw)
    statement = [DOC_REVOCATION, bulletin_id, instrument_id, issuer, key_id,
                 first_sequence, last_sequence, b64e(root_pub_raw)]
    if not verify(root_pub, signature, statement):
        raise ValueError(REASON_SIGNATURE_INVALID)
    db.execute('INSERT INTO revocation_bulletin(bulletin_id,instrument_id,issuer,key_id,'
               'first_sequence,last_sequence,signature) VALUES (?,?,?,?,?,?,?)',
               (bulletin_id, instrument_id, issuer, key_id, first_sequence,
                last_sequence, signature))
    recompute(db, instrument_id)
    return {'status': 'recorded', 'reason': 'revocation_applied'}


# --------------------------------------------------------------------------- verification

def _revoked(db, instrument_id, sequence):
    row = db.execute('SELECT * FROM revocation_bulletin WHERE instrument_id=? AND ? '
                     'BETWEEN first_sequence AND last_sequence ORDER BY id LIMIT 1',
                     (instrument_id, sequence)).fetchone()
    return row


def _decode_signature(value):
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    return b64d(str(value))


def static_check(db, proof, digest):
    """Check everything that does not depend on chain position: shape, key, signature."""
    if not isinstance(proof, dict):
        return None, REASON_PROOF_MISSING
    try:
        instrument_id = str(proof['instrument_id'])
        sequence = int(proof['sequence'])
        key_id = str(proof['key_id'])
        prev_digest = proof['prev_digest']
        claimed_digest = str(proof['payload_digest'])
        signature = _decode_signature(proof['signature'])
        if sequence < 1 or (prev_digest is not None and not isinstance(prev_digest, str)):
            return None, REASON_BAD_PROOF
    except (KeyError, TypeError, ValueError):
        return None, REASON_BAD_PROOF
    if claimed_digest != digest:
        return None, REASON_PAYLOAD_DIGEST_MISMATCH
    root = db.execute('SELECT 1 FROM trust_root WHERE instrument_id=?',
                      (instrument_id,)).fetchone()
    if root is None:
        return None, REASON_INSTRUMENT_UNKNOWN
    key = db.execute('SELECT * FROM signing_key WHERE instrument_id=? AND key_id=?',
                     (instrument_id, key_id)).fetchone()
    if key is None:
        return None, REASON_KEY_UNKNOWN
    if sequence < int(key['from_sequence']):
        return None, REASON_KEY_NOT_YET_VALID
    expected = ['spectracapture.export/v1', digest, instrument_id, sequence,
                prev_digest, key_id]
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    pub = Ed25519PublicKey.from_public_bytes(bytes(key['public_key']))
    if not verify(pub, signature, expected):
        return None, REASON_SIGNATURE_INVALID
    info = {'instrument_id': instrument_id, 'sequence': sequence, 'key_id': key_id,
            'prev_digest': prev_digest, 'payload_digest': digest}
    return info, REASON_CHAIN_VALID


def _apply_verdict(db, source_id, archive, business, detail=None):
    old = db.execute('SELECT * FROM source_verdict WHERE source_id=?',
                     (source_id,)).fetchone()
    if old is None or (old['archive_status'], old['archive_reason'],
                       old['business_status'], old['business_reason']) != (
            archive[0], archive[1], business[0], business[1]):
        db.execute('INSERT INTO source_verdict(source_id,archive_status,archive_reason,'
                   'business_status,business_reason) VALUES (?,?,?,?,?) '
                   'ON CONFLICT(source_id) DO UPDATE SET archive_status=excluded.archive_status,'
                   'archive_reason=excluded.archive_reason,business_status=excluded.business_status,'
                   'business_reason=excluded.business_reason,updated_at=CURRENT_TIMESTAMP',
                   (source_id, archive[0], archive[1], business[0], business[1]))
        db.execute('INSERT INTO source_verdict_history(source_id,contract,status,reason,detail) '
                   'VALUES (?,?,?,?,?)',
                   (source_id, 'archive', archive[0], archive[1],
                    json.dumps(detail, ensure_ascii=False) if detail else None))
        db.execute('INSERT INTO source_verdict_history(source_id,contract,status,reason,detail) '
                   'VALUES (?,?,?,?,?)',
                   (source_id, 'business', business[0], business[1],
                    json.dumps(detail, ensure_ascii=False) if detail else None))


def recompute(db, instrument_id=None):
    """Re-derive both contract verdicts along every instrument's predecessor chain.

    Out-of-order arrival is never treated as forgery: a capture whose predecessor has
    not arrived yet stays pending until the predecessor links in. Two distinct validly
    signed payloads at one instrument+sequence are a device fork: both copies stay
    archive evidence and every successor's business use is frozen.
    """
    if instrument_id is None:
        ids = [r[0] for r in db.execute(
            'SELECT DISTINCT instrument_id FROM source_proof WHERE instrument_id IS NOT NULL')]
    else:
        ids = [instrument_id]
    for inst in ids:
        rows = db.execute('SELECT p.*,f.hash AS raw_hash FROM source_proof p '
                          'JOIN export_file f ON f.id=p.source_id WHERE p.instrument_id=? '
                          'ORDER BY p.sequence,p.source_id', (inst,)).fetchall()
        by_seq = {}
        for row in rows:
            by_seq.setdefault(row['sequence'], []).append(row)

        fork_sequences = set()
        db.execute('DELETE FROM device_fork WHERE instrument_id=?', (inst,))
        for seq, copies in by_seq.items():
            distinct = {c['payload_digest'] for c in copies}
            if len(distinct) > 1:
                fork_sequences.add(seq)
                ordered = sorted(copies, key=lambda c: c['source_id'])
                for a, b in zip(ordered, ordered[1:]):
                    db.execute('INSERT OR IGNORE INTO device_fork(instrument_id,sequence,'
                               'source_id_a,source_id_b) VALUES (?,?,?,?)',
                               (inst, seq, a['source_id'], b['source_id']))

        # A revoked key invalidates static trust regardless of chain position.
        def static_state(row):
            info, reason = static_check(db, dict(row), row['payload_digest'])
            revoked = _revoked(db, inst, row['sequence'])
            return info, reason, revoked

        for seq in sorted(by_seq):
            for row in by_seq[seq]:
                source_id = row['source_id']
                info, reason, revoked = static_state(row)
                detail = {'instrument_id': inst, 'sequence': seq}
                if info is None:
                    _apply_verdict(db, source_id,
                                   (ARCHIVE_REJECTED, reason),
                                   (BUSINESS_REJECTED, reason), detail)
                    continue
                # Archive contract: cryptographic validity plus a checkable predecessor.
                archive_status, archive_reason = ARCHIVE_VERIFIABLE, REASON_CHAIN_VALID
                business_status, business_reason = BUSINESS_USABLE, REASON_CHAIN_VALID
                if seq == 1:
                    if row['prev_digest'] is not None:
                        archive_status, archive_reason = ARCHIVE_REJECTED, REASON_GENESIS_MISMATCH
                        business_status, business_reason = BUSINESS_REJECTED, REASON_GENESIS_MISMATCH
                else:
                    if row['prev_digest'] is None:
                        archive_status, archive_reason = ARCHIVE_REJECTED, REASON_GENESIS_MISMATCH
                        business_status, business_reason = BUSINESS_REJECTED, REASON_GENESIS_MISMATCH
                    else:
                        prev = db.execute('SELECT * FROM source_proof WHERE instrument_id=? '
                                          'AND sequence=?', (inst, seq - 1)).fetchall()
                        linked = [p for p in prev
                                  if p['payload_digest'] == row['prev_digest']]
                        if not prev:
                            # Disconnected capture: the predecessor file has not arrived.
                            archive_status, archive_reason = ARCHIVE_PENDING, REASON_PENDING_PREDECESSOR
                            business_status, business_reason = BUSINESS_PENDING, REASON_PENDING_PREDECESSOR
                        elif not linked:
                            archive_status, archive_reason = ARCHIVE_REJECTED, REASON_PREV_DIGEST_MISMATCH
                            business_status, business_reason = BUSINESS_REJECTED, REASON_PREV_DIGEST_MISMATCH
                        else:
                            # A chain only links through a cryptographically valid predecessor.
                            anchor = next((p for p in linked
                                           if static_check(db, dict(p), p['payload_digest'])[0] is not None),
                                          None)
                            if anchor is None:
                                archive_status, archive_reason = ARCHIVE_PENDING, REASON_PENDING_PREDECESSOR
                                business_status, business_reason = BUSINESS_PENDING, REASON_PENDING_PREDECESSOR
                            else:
                                prev_verdict = db.execute('SELECT * FROM source_verdict '
                                                          'WHERE source_id=?',
                                                          (anchor['source_id'],)).fetchone()
                                if prev_verdict and prev_verdict['archive_status'] == ARCHIVE_PENDING:
                                    archive_status, archive_reason = ARCHIVE_PENDING, REASON_PENDING_PREDECESSOR
                                    business_status, business_reason = BUSINESS_PENDING, REASON_PENDING_PREDECESSOR
                                elif prev_verdict and prev_verdict['archive_status'] == ARCHIVE_REJECTED:
                                    archive_status, archive_reason = ARCHIVE_REJECTED, prev_verdict['archive_reason']
                                    business_status, business_reason = BUSINESS_REJECTED, prev_verdict['archive_reason']
                # Device fork: archive keeps both copies, business freezes them and all successors.
                if seq in fork_sequences and archive_status == ARCHIVE_VERIFIABLE:
                    business_status, business_reason = BUSINESS_FROZEN, REASON_DEVICE_FORK
                if any(s < seq for s in fork_sequences) and business_status == BUSINESS_USABLE:
                    business_status, business_reason = BUSINESS_FROZEN, REASON_SUCCESSOR_OF_FORK
                # Late key-theft bulletin: archive evidence is unchanged; business freezes.
                if revoked is not None and business_status not in (BUSINESS_REJECTED, BUSINESS_PENDING):
                    business_status, business_reason = BUSINESS_FROZEN, REASON_REVOKED
                    detail['revocation'] = {'bulletin_id': revoked['bulletin_id'],
                                            'first_sequence': revoked['first_sequence'],
                                            'last_sequence': revoked['last_sequence'],
                                            'issuer': revoked['issuer']}
                _apply_verdict(db, source_id,
                               (archive_status, archive_reason),
                               (business_status, business_reason), detail)
    # Strict stores may hold unsigned files or syntactically malformed proofs (no
    # instrument id); neither can satisfy either contract.
    for row in db.execute('SELECT source_id FROM source_proof WHERE present=0 '
                          'OR instrument_id IS NULL'):
        proof = db.execute('SELECT present FROM source_proof WHERE source_id=?',
                           (row['source_id'],)).fetchone()
        reason = REASON_PROOF_MISSING if proof is not None and proof['present'] == 0 \
            else REASON_BAD_PROOF
        _apply_verdict(db, row['source_id'],
                       (ARCHIVE_REJECTED, reason),
                       (BUSINESS_REJECTED, reason))


def source_verdict(db, source_id):
    row = db.execute('SELECT * FROM source_verdict WHERE source_id=?',
                     (source_id,)).fetchone()
    if row is None:
        return None
    proof = db.execute('SELECT * FROM source_proof WHERE source_id=?',
                       (source_id,)).fetchone()
    result = {
        'source_id': source_id,
        'archive': {'status': row['archive_status'], 'reason': row['archive_reason']},
        'business': {'status': row['business_status'], 'reason': row['business_reason']},
    }
    if proof is not None and proof['instrument_id'] is not None:
        result['instrument_id'] = proof['instrument_id']
        result['sequence'] = proof['sequence']
    return result


def business_gate(db, source_id):
    """Return (blocked, reason) for the business-use contract.

    Legacy stores (no trust root installed) are not gated. A capture is blocked unless
    its current business verdict is explicitly usable; pending chains block too rather
    than being guessed true."""
    if not trust_mode(db):
        return False, REASON_LEGACY
    verdict = source_verdict(db, source_id)
    if verdict is None:
        return True, REASON_PROOF_MISSING
    if verdict['business']['status'] != BUSINESS_USABLE:
        return True, verdict['business']['reason']
    return False, REASON_CHAIN_VALID
