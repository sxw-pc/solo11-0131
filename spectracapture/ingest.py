"""Local capture: raw bytes are committed atomically as one chained commit."""
import hashlib
import json
from pathlib import Path
from .store import connect, commit_capture


def parse(raw):
    payload = json.loads(raw)
    if not isinstance(payload, dict) or not isinstance(payload.get('version'), str) or not payload['version']:
        raise ValueError('version_required')
    return payload


def capture(path, kind, file):
    """Read, verify and commit one export file.

    Returns ``(source_id, payload)``.  The source row and its per-line records
    are written in a single transaction; a crash before ``commit`` leaves no
    visible half-commit and no chained commit.
    """
    raw = Path(file).read_bytes()
    payload = parse(raw)
    db = connect(path)
    try:
        with db:
            source_id, _created = commit_capture(db, kind, payload['version'], raw)
        return source_id, payload
    finally:
        db.close()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()
