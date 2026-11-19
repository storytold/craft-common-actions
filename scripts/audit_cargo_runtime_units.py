"""Report planned runtime compile-unit reuse, never actual compile/runtime parity."""
import collections
import pathlib
import sys
from compare_cargo_units import canonical, compare, inventory, read_json
from compare_cargo_target_projection import project

KEEP_EXAMPLES = frozenset({'flat_open_bench', 'bench_camera_raw', 'bench_shape_drag',
                          'bench_stack_reuse', 'compositor_build_probe', 'zero_block_probe'})


def audit(full, selected, runtime):
    present = {full['units'][i]['target']['name'] for i in full['roots']
               if full['units'][i]['target']['kind'] == ['example']}
    if not KEEP_EXAMPLES <= present:
        raise ValueError('required retained examples missing from baseline')
    projected, dropped = project(full, KEEP_EXAMPLES)
    equality, _, _ = compare(projected, selected, set())
    selected_counts, _, selected_records, _ = inventory(selected, set())
    runtime_union = collections.Counter()
    reports, missing_records = {}, {}
    for label, graph in runtime.items():
        counts, _, records, _ = inventory(graph, set())
        runtime_union |= counts
        missing = counts - selected_counts
        by_mode = collections.Counter()
        for identity, count in missing.items():
            by_mode[records[identity]['mode']] += count
            missing_records[identity] = records[identity]
        examples = sorted({unit['target']['name'] for unit in graph['units'] if unit['target']['kind'] == ['example']})
        reports[label] = {'units': sum(counts.values()), 'units_absent_from_prebuild': sum(missing.values()),
                          'missing_by_mode': dict(by_mode), 'example_targets': examples}
    union_missing = runtime_union - selected_counts
    return {'projected_prebuild': equality, 'omitted_roots': dropped, 'runtime_commands': reports,
            'unique_runtime_units': sum(runtime_union.values()),
            'runtime_units_absent_from_prebuild': sum(union_missing.values())}, missing_records


def main():
    root = pathlib.Path(sys.argv[1])
    full = read_json(root / 'native-full.json')
    selected = read_json(root / 'native-selected.json')
    labels = ('lib-bin', 'doc', 'integration', 'gpu')
    runtime = {label: read_json(root / f'runtime-{label}.json') for label in labels}
    result, missing = audit(full, selected, runtime)
    print(canonical({'implicit_host_runtime_audit': result}))
    for identity, unit in sorted(missing.items()):
        print(canonical({'missing_planned_unit': identity, 'unit': unit}))
    print('No execution or compilation: missing doctest/runtime-mode units are reported, not silently discarded.')
    print('Nightly plans cannot establish stable outputs, runtime coverage, or elapsed-time savings.')


if __name__ == '__main__': main()
