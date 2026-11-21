"""Qualify waste-container handoff and remediation from physical pour evidence.

Two independent contracts are signed for every container:

* 交运合同 (transport): every feasible physical process consistent with the
  signed order, the recorded dose ranges and the container capacity must stay
  within the local exotherm / gas / material limits from open to seal, and the
  current volume, seal and capacity must be acceptable.  A favourable final
  assay never rescues a process that crossed a limit mid-way.
* 整改合同 (remediation): whether the container can still be isolated safely
  right now and whether a neutralisation route exists that respects the
  remaining dose and capacity limits.

The two contracts never share a single "safe" boolean: a container with a
confirmed past exceedance is 不可交运 yet may remain 可整改, because the
incident stays on record even after the contents are neutralised.

Physical events (pour / stir / sample / neutralize / seal) are immutable once
registered, may arrive out of order, and are ordered only by signed ``after``
fields and device-sequence proofs — never by file arrival time or by the
operator-written timestamp.  Late proofs shrink the feasible process set and
produce a new disposition version; old versions and issued transport orders
are retained, and outstanding orders are revoked whenever the transport
contract no longer signs 可交运.
"""
import json
from .ingest import capture
from .store import connect, save, decide

EVENT_KINDS = ('pour', 'stir', 'sample', 'neutralize', 'seal')
RULE_KINDS = ('category', 'neutralizer', 'consumes', 'gas_reaction', 'exotherm_limit', 'material_limit')
EPS = 1e-9
MAX_ORDERS = 10000


def rules(path, file):
    """Register local material categories, neutralizers and compatibility rules."""
    source, payload = capture(path, 'desktop_waste_rules', file)
    db = connect(path)
    results = []
    try:
        with db:
            for row in payload.get('rows', []):
                key = str(row['id'])
                if row.get('kind') in RULE_KINDS:
                    status, reason = '已登记', 'rule_registered'
                else:
                    status, reason = '待核', 'unknown_rule_kind'
                save(db, 'waste_rule', key, source, payload['version'], row, status, reason)
                results.append(decide(db, '废液规则', key, status, reason, source))
        return results
    finally:
        db.close()


def events(path, file):
    """Register immutable physical events and device-sequence proofs.

    Rows may arrive out of order and may be scanned twice; identical rows are
    idempotent.  After every import, each touched container that already has a
    container record is re-evaluated, so late evidence generates a new
    disposition version.
    """
    source, payload = capture(path, 'desktop_waste_event_export', file)
    db = connect(path)
    results = []
    touched = set()
    try:
        with db:
            for row in payload.get('rows', []):
                key = str(row['id'])
                kind = row.get('kind')
                if kind in EVENT_KINDS:
                    ok, reason = _validate_event(row)
                elif kind == 'sequence_proof':
                    ok, reason = _validate_proof(row)
                else:
                    ok, reason = False, 'unknown_event_kind'
                status = '已登记' if ok else '待核'
                save(db, 'waste_event', key, source, payload['version'], row, status, reason)
                results.append(decide(db, '废液事件', key, status, reason, source))
                if ok and row.get('container'):
                    touched.add(str(row['container']))
            for key in sorted(touched):
                stored = db.execute('SELECT * FROM waste_container WHERE id=?', (key,)).fetchone()
                if stored:
                    results.append(_evaluate(db, key, json.loads(stored['body']), stored['version'],
                                             source, payload['version']))
        return results
    finally:
        db.close()


def assess(path, file):
    """Sign the transport and remediation contracts for each exported container."""
    source, payload = capture(path, 'desktop_waste_export', file)
    db = connect(path)
    results = []
    try:
        with db:
            for row in payload.get('rows', []):
                results.append(_evaluate(db, str(row['id']), row, payload['version'],
                                         source, payload['version']))
        return results
    finally:
        db.close()


# ---------------------------------------------------------------- validation

def _validate_event(row):
    if not row.get('container') or not row.get('operator'):
        return False, 'event_incomplete'
    kind = row.get('kind')
    if kind in ('pour', 'neutralize', 'sample'):
        amount = row.get('amount_l')
        try:
            lo, hi = float(amount[0]), float(amount[1])
        except (TypeError, ValueError, IndexError, KeyError):
            return False, 'event_incomplete'
        if not 0 <= lo <= hi:
            return False, 'event_incomplete'
    if kind in ('pour', 'neutralize') and not row.get('category'):
        return False, 'event_incomplete'
    if kind == 'pour' and not row.get('source_bottle'):
        return False, 'event_incomplete'
    after = row.get('after', [])
    if not isinstance(after, list) or not all(isinstance(x, str) for x in after):
        return False, 'event_incomplete'
    return True, 'event_recorded'


def _validate_proof(row):
    entries = row.get('entries')
    if not row.get('container') or not row.get('signed_by'):
        return False, 'proof_incomplete'
    if not isinstance(entries, list) or len(entries) < 2:
        return False, 'proof_incomplete'
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get('event'), str):
            return False, 'proof_incomplete'
        try:
            float(entry.get('seq'))
        except (TypeError, ValueError):
            return False, 'proof_incomplete'
    return True, 'proof_recorded'


# ------------------------------------------------------------- rule loading

def _load_rules(db):
    grouped = {kind: [] for kind in RULE_KINDS}
    for row in db.execute("SELECT body FROM waste_rule WHERE status='已登记'"):
        body = json.loads(row['body'])
        if body.get('kind') in grouped:
            grouped[body['kind']].append(body)
    return grouped


# ------------------------------------------------------- feasible processes

def _constraints(events, proofs):
    """Signed order edges: event ``after`` fields, event-carried device
    sequences and separately delivered sequence proofs.  Arrival order and
    operator timestamps are never consulted."""
    edges, sources = set(), {}

    def add(before, after, origin):
        if before in events and after in events:
            edges.add((before, after))
            sources.setdefault((before, after), set()).add(origin)

    for eid, event in events.items():
        for earlier in event.get('after', []):
            add(str(earlier), eid, eid)
    by_device = {}
    for eid, event in events.items():
        if event.get('device') is not None and event.get('seq') is not None:
            by_device.setdefault(str(event['device']), []).append((float(event['seq']), eid))
    for device, entries in by_device.items():
        entries.sort()
        for (s1, e1), (s2, e2) in zip(entries, entries[1:]):
            add(e1, e2, e2)
            if s1 == s2:
                add(e2, e1, e1)
    for pid, proof in proofs.items():
        entries = sorted((float(x['seq']), str(x['event'])) for x in proof['entries'])
        for (s1, e1), (s2, e2) in zip(entries, entries[1:]):
            add(e1, e2, pid)
            if s1 == s2:
                add(e2, e1, pid)
    return edges, sources


def _topological_orders(ids, edges):
    adjacent = {i: set() for i in ids}
    indegree = {i: 0 for i in ids}
    for before, after in edges:
        if after not in adjacent[before]:
            adjacent[before].add(after)
            indegree[after] += 1
    orders = []
    truncated = []

    def walk(available, indegree, path, total):
        if len(orders) >= MAX_ORDERS:
            truncated.append(True)
            return
        if not available:
            if len(path) == total:
                orders.append(tuple(path))
            return
        for event in sorted(available):
            nxt = set(available)
            nxt.discard(event)
            for follower in adjacent[event]:
                indegree[follower] -= 1
                if indegree[follower] == 0:
                    nxt.add(follower)
            path.append(event)
            walk(nxt, indegree, path, total)
            path.pop()
            for follower in adjacent[event]:
                indegree[follower] += 1

    walk({i for i in ids if indegree[i] == 0}, indegree, [], len(ids))
    return orders, not truncated


def _find_cycle(ids, edges):
    adjacent = {}
    for before, after in edges:
        adjacent.setdefault(before, []).append(after)
    color = {i: 0 for i in ids}
    stack, cycle = [], []

    def visit(node):
        color[node] = 1
        stack.append(node)
        for nxt in adjacent.get(node, []):
            if color.get(nxt, 0) == 1:
                cycle.extend(stack[stack.index(nxt):] + [nxt])
                return True
            if color.get(nxt, 0) == 0 and visit(nxt):
                return True
        stack.pop()
        color[node] = 2
        return False

    for node in ids:
        if color[node] == 0 and visit(node):
            break
    return cycle


# -------------------------------------------------------------- simulation

def _simulate(order, events, rules, capacity, material):
    """Replay one feasible order with interval doses.

    Returns (final_state, final_total, violations, feasible).  State maps each
    category to a [low, high] amount interval so a dose range that straddles a
    reaction threshold is flagged ``possible`` instead of being decided by a
    single nominal point.  Reacted material stays in the container as inert
    products, and the total volume is tracked as its own exact interval.
    Any non-seal event after a seal makes the order physically infeasible.
    """
    state = {}
    violations = []
    sealed = False
    total = [0.0, 0.0]
    for eid in order:
        event = events[eid]
        kind = event['kind']
        if sealed and kind != 'seal':
            return None, None, None, False
        if kind == 'seal':
            sealed = True
            continue
        if kind == 'stir':
            continue
        if kind == 'sample':
            lo, hi = (float(x) for x in event['amount_l'])
            if total[1] > EPS:
                frac_hi = min(1.0, hi / total[0]) if total[0] > EPS else (1.0 if hi > EPS else 0.0)
                frac_lo = min(1.0, lo / total[1])
                for category in state:
                    state[category] = [state[category][0] * (1 - frac_hi),
                                       state[category][1] * (1 - frac_lo)]
            total = [max(0.0, total[0] - hi), max(0.0, total[1] - lo)]
            continue
        category = str(event['category'])
        lo, hi = (float(x) for x in event['amount_l'])
        for rule in rules['material_limit']:
            if material is not None and str(rule.get('material')) == str(material) \
                    and str(rule.get('category')) == category:
                violations.append({'rule': rule['id'], 'event': eid,
                                   'hazard': 'material', 'severity': 'definite'})
        for rule in rules['gas_reaction']:
            if str(rule.get('category')) == category:
                present = state.get(str(rule.get('with')), [0.0, 0.0])
                threshold = float(rule.get('threshold_l', 0.0))
                if present[1] > threshold + EPS:
                    severity = 'definite' if present[0] > threshold + EPS else 'possible'
                    violations.append({'rule': rule['id'], 'event': eid,
                                       'hazard': 'gas', 'severity': severity})
        if total[0] + lo > capacity + EPS:
            violations.append({'rule': 'CAPACITY', 'event': eid,
                               'hazard': 'capacity', 'severity': 'definite'})
        elif total[1] + hi > capacity + EPS:
            violations.append({'rule': 'CAPACITY', 'event': eid,
                               'hazard': 'capacity', 'severity': 'possible'})
        total = [total[0] + lo, total[1] + hi]
        remaining = [lo, hi]
        for rule in rules['consumes']:
            agent, target = str(rule.get('agent')), str(rule.get('target'))
            ratio = float(rule.get('ratio', 1.0))
            if ratio <= 0:
                continue
            if category == agent:
                other = state.get(target, [0.0, 0.0])
                reacted = [min(remaining[0] * ratio, other[0]), min(remaining[1] * ratio, other[1])]
                state[target] = [max(0.0, other[0] - remaining[1] * ratio),
                                 max(0.0, other[1] - remaining[0] * ratio)]
                remaining = [max(0.0, remaining[0] - other[1] / ratio),
                             max(0.0, remaining[1] - other[0] / ratio)]
                _check_exotherm(rules, agent, target, reacted, eid, violations)
            elif category == target:
                other = state.get(agent, [0.0, 0.0])
                reacted = [min(remaining[0], other[0] * ratio), min(remaining[1], other[1] * ratio)]
                state[agent] = [max(0.0, other[0] - remaining[1] / ratio),
                                max(0.0, other[1] - remaining[0] / ratio)]
                remaining = [max(0.0, remaining[0] - other[1] * ratio),
                             max(0.0, remaining[1] - other[0] * ratio)]
                _check_exotherm(rules, agent, target, reacted, eid, violations)
        state[category] = [state.get(category, [0.0, 0.0])[0] + remaining[0],
                           state.get(category, [0.0, 0.0])[1] + remaining[1]]
    return state, total, violations, True


def _check_exotherm(rules, agent, target, reacted, eid, violations):
    for rule in rules['exotherm_limit']:
        if str(rule.get('agent')) == agent and str(rule.get('target')) == target:
            limit = float(rule.get('max_reacted_l', 0.0))
            if reacted[1] > limit + EPS:
                severity = 'definite' if reacted[0] > limit + EPS else 'possible'
                violations.append({'rule': rule['id'], 'event': eid,
                                   'hazard': 'exotherm', 'severity': severity})


# ---------------------------------------------------------------- evaluation

def _evaluate(db, key, row, row_version, source, version):
    volume = float(row['volume_l'])
    capacity = float(row['capacity_l'])
    sealed = bool(row.get('sealed', False))
    material = row.get('material')
    events, proofs = {}, {}
    for record in db.execute("SELECT body FROM waste_event WHERE json_extract(body,'$.container')=? "
                             "AND status='已登记'", (key,)):
        body = json.loads(record['body'])
        if body.get('kind') in EVENT_KINDS:
            events[str(body['id'])] = body
        elif body.get('kind') == 'sequence_proof':
            proofs[str(body['id'])] = body
    rules = _load_rules(db)
    have_rules = any(rules[kind] for kind in rules)

    conflict, sims, final_state, final_total = None, {}, None, None
    if events and have_rules:
        edges, sources = _constraints(events, proofs)
        orders, complete = _topological_orders(sorted(events), edges)
        if not complete:
            conflict = sorted(events)  # 可行过程过多，无法完整枚举时不得放行
        elif not orders:
            cycle = _find_cycle(sorted(events), edges)
            conflict = sorted({origin for i in range(len(cycle) - 1)
                               for origin in sources.get((cycle[i], cycle[i + 1]), ())}) or sorted(events)
        else:
            for order in orders:
                state, total, violations, feasible = _simulate(order, events, rules, capacity, material)
                if feasible:
                    sims[order] = (state, total, violations)
            if not sims:
                conflict = sorted(events)
        if sims:
            final_state = {}
            for category in sorted({c for state, _, _ in sims.values() for c in state}):
                low = min(state.get(category, [0.0, 0.0])[0] for state, _, _ in sims.values())
                high = max(state.get(category, [0.0, 0.0])[1] for state, _, _ in sims.values())
                final_state[category] = [round(low, 6), round(high, 6)]
            final_total = [round(min(total[0] for _, total, _ in sims.values()), 6),
                           round(max(total[1] for _, total, _ in sims.values()), 6)]

    verdicts = {}
    for order, (state, total, violations) in sims.items():
        if any(v['severity'] == 'definite' for v in violations):
            verdicts[order] = 'violation'
        elif violations:
            verdicts[order] = 'suspect'
        else:
            verdicts[order] = 'clean'

    if sims and all(v == 'violation' for v in verdicts.values()):
        common = None
        for order, (state, total, violations) in sims.items():
            definite = {(v['rule'], v['event'], v['hazard'])
                        for v in violations if v['severity'] == 'definite'}
            common = definite if common is None else common & definite
        for rule_id, eid, hazard in sorted(common or {('process_violation', None, 'process')}):
            iid = f'{key}:{rule_id}:{eid}' if eid else f'{key}:{rule_id}'
            if not db.execute('SELECT 1 FROM waste_incident WHERE id=?', (iid,)).fetchone():
                save(db, 'waste_incident', iid, source, version,
                     {'container': key, 'rule': rule_id, 'event': eid, 'hazard': hazard},
                     '已立案', 'exceedance_confirmed')
                decide(db, '废液事故', iid, '已立案', hazard, source)
    incident_ids = [r['id'] for r in db.execute(
        "SELECT id FROM waste_incident WHERE json_extract(body,'$.container')=? ORDER BY rowid", (key,))]

    # 交运合同：既往（所有可行过程）与当前（状态、封签、容量）都必须合格。
    if events and not have_rules:
        t_status, t_reason = '待核', 'rules_missing'
    elif conflict:
        t_status, t_reason = '待核', 'evidence_conflict'
    elif incident_ids:
        t_status, t_reason = '不可交运', 'incident_confirmed'
    elif sims and any(v != 'clean' for v in verdicts.values()):
        t_status, t_reason = '待核', 'unresolved_process'
    elif volume < 0 or capacity <= 0:
        t_status, t_reason = '待核', 'invalid_volume'
    elif not sealed:
        t_status, t_reason = '待核', 'unsealed'
    elif volume > capacity:
        t_status, t_reason = '待核', 'over_capacity'
    elif events:
        t_status, t_reason = '可交运', 'process_and_state_ok'
    else:
        t_status, t_reason = '可交运', 'volume_and_seal_ok'

    # 整改合同：独立判断此刻能否安全隔离并给出满足剩余剂量与容量限制的中和路线。
    route = []
    if events and not have_rules:
        r_status, r_reason = '待核', 'rules_missing'
    elif conflict:
        r_status, r_reason = '待核', 'evidence_conflict'
    else:
        isolatable = 0 <= volume <= capacity + EPS and capacity > 0
        if final_state:
            if final_total[1] > capacity + EPS:
                isolatable = False
            for rule in rules['material_limit']:
                if material is not None and str(rule.get('material')) == str(material) \
                        and final_state.get(str(rule.get('category')), [0.0, 0.0])[1] > EPS:
                    isolatable = False
        if not isolatable:
            r_status, r_reason = '不可整改', 'isolation_unsafe'
        else:
            remaining_capacity = capacity - (final_total[1] if final_total else volume)
            route_ok = True
            for rule in rules['gas_reaction']:
                target = str(rule.get('with'))
                threshold = float(rule.get('threshold_l', 0.0))
                present = final_state.get(target, [0.0, 0.0])[1] if final_state else 0.0
                if present > threshold + EPS:
                    step = _route_step(rules, target, present - threshold,
                                       remaining_capacity, material)
                    if step is None:
                        route_ok = False
                        break
                    route.append(step)
            if not route_ok:
                r_status, r_reason = '不可整改', 'no_safe_route'
            elif route:
                r_status, r_reason = '可整改', 'remediation_route_available'
            else:
                r_status, r_reason = '可整改', 'isolation_sufficient'

    save(db, 'waste_container', key, source, row_version, row, t_status, t_reason)
    if conflict:
        detail = json.dumps({'container': key, 'records': conflict}, ensure_ascii=False, sort_keys=True)
        if not db.execute('SELECT 1 FROM issue WHERE code=? AND detail=?',
                          ('waste_evidence_conflict', detail)).fetchone():
            db.execute('INSERT INTO issue(source_id,code,detail) VALUES (?,?,?)',
                       (source, 'waste_evidence_conflict', detail))

    disposition = {'container': key, 'transport': t_status, 'transport_reason': t_reason,
                   'remediation': r_status, 'remediation_reason': r_reason, 'operations': route,
                   'events': sorted(events), 'proofs': sorted(proofs),
                   'feasible_orders': [list(order) for order in sims],
                   'incidents': incident_ids, 'conflict': conflict,
                   'final_state': final_state, 'final_total_l': final_total}
    latest = db.execute("SELECT * FROM waste_disposition WHERE json_extract(body,'$.container')=? "
                        "ORDER BY rowid DESC LIMIT 1", (key,)).fetchone()
    number = int(latest['id'].rsplit(':D', 1)[1]) if latest else 0
    if latest is None or json.loads(latest['body']) != disposition:
        number += 1
        save(db, 'waste_disposition', f'{key}:D{number}', source, version,
             disposition, t_status, t_reason)

    outstanding = db.execute("SELECT * FROM waste_transport_order "
                             "WHERE json_extract(body,'$.container')=? AND status='有效'", (key,)).fetchall()
    if t_status == '可交运':
        if not outstanding:
            count = db.execute("SELECT count(*) AS c FROM waste_transport_order "
                               "WHERE json_extract(body,'$.container')=?", (key,)).fetchone()['c']
            oid = f'{key}:T{count + 1}'
            save(db, 'waste_transport_order', oid, source, version,
                 {'container': key, 'disposition': f'{key}:D{number}', 'contract': '废液交运合同'},
                 '有效', 'transport_contract_signed')
            decide(db, '废液交运单', oid, '有效', 'transport_contract_signed', source)
    else:
        for order in outstanding:
            save(db, 'waste_transport_order', order['id'], source, order['version'],
                 json.loads(order['body']), '撤回', t_reason)
            decide(db, '废液交运单', order['id'], '撤回', t_reason, source)

    result = decide(db, '废液交运', key, t_status, t_reason, source)
    decide(db, '废液整改', key, r_status, r_reason, source)
    result.update({'transport': t_status, 'transport_reason': t_reason,
                   'remediation': r_status, 'remediation_reason': r_reason,
                   'operations': route, 'disposition': number, 'incidents': incident_ids})
    return result


def _route_step(rules, target, reduce_by, remaining_capacity, material):
    """One neutralisation step that provably lowers ``target`` below its
    threshold while respecting dose, exotherm, capacity and material limits."""
    for consume in rules['consumes']:
        if str(consume.get('target')) != target:
            continue
        agent = str(consume.get('agent'))
        ratio = float(consume.get('ratio', 1.0))
        if ratio <= 0:
            continue
        registry = next((n for n in rules['neutralizer'] if str(n.get('category')) == agent), None)
        if registry is None:
            continue
        dose = reduce_by / ratio
        if dose > float(registry.get('max_single_dose_l', 0.0)) + EPS:
            continue
        if dose > remaining_capacity + EPS:
            continue
        if material is not None and any(str(m.get('material')) == str(material)
                                        and str(m.get('category')) == agent
                                        for m in rules['material_limit']):
            continue
        if any(reduce_by > float(x.get('max_reacted_l', 0.0)) + EPS
               for x in rules['exotherm_limit']
               if str(x.get('agent')) == agent and str(x.get('target')) == target):
            continue
        return {'op': 'neutralize', 'agent': agent, 'target': target, 'dose_l': round(dose, 6)}
    return None
