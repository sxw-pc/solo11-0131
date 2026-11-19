"""Register a basic handoff status for laboratory solvent waste containers."""
from .ingest import capture
from .store import connect, save, decide


def assess(path, file):
    source, payload = capture(path, 'desktop_waste_export', file)
    db = connect(path)
    results = []
    try:
        with db:
            for row in payload.get('rows', []):
                key = str(row['id'])
                volume = float(row['volume_l'])
                capacity = float(row['capacity_l'])
                if volume < 0 or capacity <= 0:
                    status, reason = '待核', 'invalid_volume'
                elif not row.get('sealed', False):
                    status, reason = '待核', 'unsealed'
                elif volume > capacity:
                    status, reason = '待核', 'over_capacity'
                else:
                    status, reason = '可交运', 'volume_and_seal_ok'
                save(db, 'waste_container', key, source, payload['version'], row, status, reason)
                results.append(decide(db, '废液容器', key, status, reason, source))
        return results
    finally:
        db.close()
