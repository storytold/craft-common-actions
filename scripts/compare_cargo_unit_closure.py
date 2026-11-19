"""Fail unless an isolated libtest plan matches its full-workspace planned closure.

This is a nightly planning prerequisite for a narrow stable compile measurement,
not proof that nightly planning equals production stable compilation.
"""
import copy
import sys
from compare_cargo_units import canonical, compare, read_json


def closure(graph, package_id):
    units = graph['units']
    if any(type(i) is not int or not 0 <= i < len(units) for i in graph['roots']):
        raise ValueError('invalid root index')
    roots = [i for i in graph['roots'] if units[i]['pkg_id'] == package_id
             and units[i]['mode'] == 'test' and units[i]['target']['kind'] == ['lib']]
    if len(roots) != 1:
        raise ValueError('expected exactly one matching libtest root')
    reached, pending = set(), list(roots)
    while pending:
        index = pending.pop()
        if type(index) is not int or not 0 <= index < len(units):
            raise ValueError('invalid unit index')
        if index in reached:
            continue
        reached.add(index)
        pending.extend(d['index'] for d in units[index]['dependencies'])
    indexes = sorted(reached)
    remap = {old: new for new, old in enumerate(indexes)}
    result = {k: copy.deepcopy(v) for k, v in graph.items() if k not in ('roots', 'units')}
    result['roots'] = [remap[i] for i in roots]
    result['units'] = [copy.deepcopy(units[i]) for i in indexes]
    for unit in result['units']:
        for dependency in unit['dependencies']:
            dependency['index'] = remap[dependency['index']]
    return result


def compare_closure(full, isolated, package_id):
    expected = closure(full, package_id)
    # The isolated invocation must itself contain only that one root/closure.
    actual = closure(isolated, package_id)
    if len(isolated['roots']) != 1 or len(actual['units']) != len(isolated['units']):
        raise ValueError('isolated graph contains unexpected roots or unused units')
    result, _, _ = compare(expected, isolated, set())
    if not result['planned_graph_equal']:
        raise ValueError('isolated planned closure differs: ' + canonical(result))
    return result


def main():
    full, isolated, metadata = map(read_json, sys.argv[1:4])
    matches = [p['id'] for p in metadata['packages'] if p['name'] == 'photocraft-geom']
    if len(matches) != 1:
        raise ValueError('missing or ambiguous geometry package')
    result = compare_closure(full, isolated, matches[0])
    print(canonical({'package': matches[0], 'planned_closure': result}))
    print('Nightly closure only; subsequent stable compilation measures geometry, not full gate.')


if __name__ == '__main__':
    main()
