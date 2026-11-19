import copy
import pathlib
import sys
import unittest
import tempfile
import contextlib
import io
sys.path.insert(0, str(pathlib.Path(__file__).parents[1] / 'scripts'))
from compare_cargo_unit_closure import compare_closure, closure


def graph():
    return {'version': 1, 'roots': [1, 2], 'units': [
        {'pkg_id': 'dep', 'mode': 'build', 'target': {'kind': ['lib']}, 'features': ['x'], 'dependencies': []},
        {'pkg_id': 'geom', 'mode': 'test', 'target': {'kind': ['lib']}, 'dependencies': [{'index': 0}]},
        {'pkg_id': 'other', 'mode': 'test', 'target': {'kind': ['bin']}, 'dependencies': [{'index': 0}]},
    ]}


class ClosureTests(unittest.TestCase):
    def test_extract_and_compare(self):
        full = graph(); isolated = closure(full, 'geom')
        self.assertEqual(len(isolated['units']), 2)
        self.assertTrue(compare_closure(full, isolated, 'geom')['planned_graph_equal'])

    def test_binary_target(self):
        full = graph(); isolated = closure(full, 'other', 'bin')
        self.assertTrue(compare_closure(full, isolated, 'other', 'bin')['planned_graph_equal'])
        with self.assertRaises(ValueError): closure(full, 'other', 'lib')

    def test_feature_difference_rejected(self):
        full = graph(); isolated = closure(full, 'geom'); isolated['units'][0]['features'] = ['other']
        with self.assertRaises(ValueError): compare_closure(full, isolated, 'geom')

    def test_unexpected_root_or_orphan_rejected(self):
        full = graph()
        with self.assertRaises(ValueError): compare_closure(full, full, 'geom')
        isolated = closure(full, 'geom'); isolated['units'].append(copy.deepcopy(isolated['units'][0]))
        with self.assertRaises(ValueError): compare_closure(full, isolated, 'geom')

    def test_invalid_root_rejected(self):
        for index in [True, -1, 99]:
            full = graph(); full['roots'] = [index]
            with self.assertRaises(ValueError): closure(full, 'geom')

    def test_runtime_summary_rejects_zero_or_incomplete_coverage(self):
        workflow = (pathlib.Path(__file__).parents[1] / '.github/workflows/photo-boot-measurement.yml').read_text()
        blocks = workflow.split("<<'PYCODE'\n")[1:]
        block = next(part for part in blocks if 'totals = re.findall' in part)
        source = '\n'.join(line[10:] for line in block.split('          PYCODE', 1)[0].splitlines())
        with tempfile.TemporaryDirectory() as directory:
            listing = pathlib.Path(directory) / 'list.txt'
            result = pathlib.Path(directory) / 'result.txt'
            source = source.replace('/tmp/photo-boot-list.txt', str(listing)).replace('/tmp/photo-boot-result.txt', str(result))
            listing.write_text('a: test\nb: test\n\n2 tests, 0 benchmarks\n')
            result.write_text('test result: ok. 2 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s\n')
            with contextlib.redirect_stdout(io.StringIO()): exec(source, {})
            for listed, outcome in [
                ('0 tests, 0 benchmarks\n', 'test result: ok. 0 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out;'),
                ('2 tests, 0 benchmarks\n', 'test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 1 filtered out;'),
                ('2 tests, 0 benchmarks\n', 'missing result'),
            ]:
                listing.write_text(listed); result.write_text(outcome)
                with self.assertRaises(AssertionError): exec(source, {})

    def test_missing_root_rejected(self):
        with self.assertRaises(ValueError): closure(graph(), 'missing')


if __name__ == '__main__': unittest.main()
