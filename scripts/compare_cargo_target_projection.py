"""Compare a nightly prebuild plan after projecting compile-only target roots.

A match is planned-unit evidence only. Dropped FreeBSD example/bench linking is a
platform-coverage tradeoff, even if Linux clippy still checks those sources.
"""
import copy
import sys
from compare_cargo_units import canonical, compare, read_json


def project(graph, keep_examples=frozenset({'flat_open_bench'})):
    units = graph['units']
    if not graph['roots'] or any(type(i) is not int or not 0 <= i < len(units) for i in graph['roots']):
        raise ValueError('invalid root inventory')
    kept, dropped = [], []
    for index in graph['roots']:
        unit = units[index]
        kind, name = unit['target']['kind'], unit['target']['name']
        if kind == ['bench'] or kind == ['example'] and name not in keep_examples:
            dropped.append({'package': unit['pkg_id'], 'target': name, 'kind': kind, 'mode': unit['mode']})
        else:
            kept.append(index)
    if not kept or not dropped:
        raise ValueError('projection must retain and omit targets')
    if not any(units[i]['target']['kind'] == ['example'] and units[i]['target']['name'] == 'flat_open_bench' for i in kept):
        raise ValueError('required flat_open_bench root absent')
    reached, pending = set(), list(kept)
    while pending:
        index = pending.pop()
        if type(index) is not int or not 0 <= index < len(units):
            raise ValueError('invalid dependency index')
        if index in reached:
            continue
        reached.add(index)
        pending.extend(edge['index'] for edge in units[index]['dependencies'])
    indexes = sorted(reached)
    remap = {old: new for new, old in enumerate(indexes)}
    result = {k: copy.deepcopy(v) for k, v in graph.items() if k not in ('roots', 'units')}
    result['roots'] = [remap[i] for i in kept]
    result['units'] = [copy.deepcopy(units[i]) for i in indexes]
    for unit in result['units']:
        for edge in unit['dependencies']:
            edge['index'] = remap[edge['index']]
    return result, dropped


def compare_projection(full, selected):
    expected, dropped = project(full)
    result, _, _ = compare(expected, selected, set())
    result['full_units'] = len(full['units'])
    result['full_roots'] = len(full['roots'])
    result['retained_roots'] = len(expected['roots'])
    return result, dropped


def main():
    full, selected = map(read_json, sys.argv[1:3])
    result, dropped = compare_projection(full, selected)
    print(canonical({'planned_projection': result}))
    for target in sorted(dropped, key=canonical):
        print(canonical({'omitted_compile_link_root': target}))
    print('No compilation. Equality is nightly planned evidence, not stable parity or measured speedup.')
    print('FreeBSD-specific example/benchmark link coverage would be reduced; no production change made.')


if __name__ == '__main__': main()
