import copy
import pathlib
import sys
import unittest
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

    def test_missing_root_rejected(self):
        with self.assertRaises(ValueError): closure(graph(), 'missing')


if __name__ == '__main__': unittest.main()
