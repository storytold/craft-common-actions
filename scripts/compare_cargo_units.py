"""Compare planned Cargo unit DAGs, not actual compiled outputs or test coverage.

Nightly planner/toolchain differences remain outside this diagnostic's proof.
No unit is dropped except explicitly named package IDs. Orphan dependencies stay
visible, edge metadata and repeated units retain their multiplicity.
"""
import collections
import hashlib
import json
import pathlib
import sys


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'))


def inventory(graph, excluded):
    if graph.get('version') != 1 or not isinstance(graph.get('units'), list):
        raise ValueError('unsupported unit graph')
    units = graph['units']
    if not units or len(units) > 20000:
        raise ValueError('empty or oversized unit graph')
    records, active, memo = {}, set(), {}

    def valid_index(index):
        if type(index) is not int or not 0 <= index < len(units):
            raise ValueError('invalid unit index')
        return index

    def identity(index):
        valid_index(index)
        if index in memo:
            return memo[index]
        if index in active:
            raise ValueError('cyclic unit graph')
        unit = units[index]
        if not isinstance(unit.get('pkg_id'), str) or not isinstance(unit.get('dependencies'), list):
            raise ValueError('incomplete unit')
        active.add(index)
        edges = []
        for dependency in unit['dependencies']:
            child = valid_index(dependency['index'])
            if units[child]['pkg_id'] in excluded:
                raise ValueError('retained unit depends on excluded package')
            edge = {k: v for k, v in dependency.items() if k != 'index'}
            edge['unit_identity'] = identity(child)
            edges.append(edge)
        record = {k: v for k, v in unit.items() if k != 'dependencies'}
        record['dependencies'] = sorted(edges, key=canonical)
        digest = hashlib.sha256(canonical(record).encode()).hexdigest()
        records[digest] = record
        memo[index] = digest
        active.remove(index)
        return digest

    retained = [i for i, unit in enumerate(units) if unit.get('pkg_id') not in excluded]
    counts = collections.Counter(identity(i) for i in retained)
    roots = []
    for index in graph['roots']:
        valid_index(index)
        if units[index]['pkg_id'] not in excluded:
            roots.append(identity(index))
    if not roots:
        raise ValueError('empty retained root inventory')
    header = {k: v for k, v in graph.items() if k not in ('units', 'roots')}
    return counts, collections.Counter(roots), records, header


def compare(baseline, reduced, excluded):
    baseline_ids = {unit.get('pkg_id') for unit in baseline.get('units', [])}
    reduced_ids = {unit.get('pkg_id') for unit in reduced.get('units', [])}
    if not excluded <= baseline_ids:
        raise ValueError('expected excluded packages absent from baseline')
    if excluded & reduced_ids:
        raise ValueError('excluded packages still present in reduced graph')
    a, ar, records_a, ah = inventory(baseline, excluded)
    b, br, records_b, bh = inventory(reduced, excluded)
    return {
        'planned_graph_equal': a == b and ar == br and ah == bh,
        'baseline_units': sum(a.values()), 'reduced_units': sum(b.values()),
        'baseline_only': dict(a - b), 'reduced_only': dict(b - a),
        'baseline_roots_only': dict(ar - br), 'reduced_roots_only': dict(br - ar),
        'header_equal': ah == bh,
    }, records_a, records_b


def read_json(path):
    path = pathlib.Path(path)
    if not path.is_file() or path.stat().st_size > 32 * 1024 * 1024:
        raise ValueError('invalid or oversized input')
    return json.loads(path.read_text())


def main():
    baseline, reduced, metadata = map(read_json, sys.argv[1:4])
    browser_names = {'photocraft-web', 'photocraft-web-boot'}
    excluded = {p['id'] for p in metadata['packages'] if p['name'] in browser_names}
    if len(excluded) != 2:
        raise ValueError('browser package IDs missing or ambiguous')
    result, a, b = compare(baseline, reduced, excluded)
    print(canonical({'summary': result, 'excluded_package_ids': sorted(excluded)}))
    for side, records in [('baseline', a), ('reduced', b)]:
        for digest, record in sorted(records.items()):
            print(canonical({'side': side, 'identity': digest, 'unit': record}))
    print('Diagnostic only: nightly planned graph, not production stable output or coverage proof.')


if __name__ == '__main__':
    main()
