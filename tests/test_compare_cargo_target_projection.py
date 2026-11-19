import pathlib
import sys
import unittest
sys.path.insert(0, str(pathlib.Path(__file__).parents[1] / 'scripts'))
from compare_cargo_target_projection import compare_projection, project


def graph():
    units = [{'pkg_id': 'dep', 'target': {'kind': ['lib'], 'name': 'dep'}, 'mode': 'build', 'features': ['a'], 'dependencies': []}]
    for kind, name in [('lib', 'app'), ('example', 'flat_open_bench'), ('example', 'probe'), ('bench', 'benchmark')]:
        units.append({'pkg_id': 'app', 'target': {'kind': [kind], 'name': name}, 'mode': 'build', 'dependencies': [{'index': 0}]})
    return {'version': 1, 'units': units, 'roots': [1, 2, 3, 4]}


class ProjectionTests(unittest.TestCase):
    def test_preserves_runtime_roots_and_explicit_example(self):
        full = graph(); selected, dropped = project(full)
        self.assertEqual([x['target'] for x in dropped], ['probe', 'benchmark'])
        result, _ = compare_projection(full, selected)
        self.assertTrue(result['planned_graph_equal'])
        self.assertEqual(result['retained_roots'], 2)

    def test_selected_loses_required_root(self):
        full = graph(); selected, _ = project(full); selected['roots'].pop()
        self.assertFalse(compare_projection(full, selected)[0]['planned_graph_equal'])

    def test_feature_and_extra_root_difference_visible(self):
        full = graph(); selected, _ = project(full); selected['units'][0]['features'] = ['different']
        self.assertFalse(compare_projection(full, selected)[0]['planned_graph_equal'])
        self.assertFalse(compare_projection(full, full)[0]['planned_graph_equal'])

    def test_required_example_absence_fails(self):
        full = graph(); full['roots'].remove(2)
        with self.assertRaises(ValueError): project(full)

    def test_invalid_indexes_fail(self):
        for index in [True, -1, 99]:
            full = graph(); full['roots'] = [index]
            with self.assertRaises(ValueError): project(full)


if __name__ == '__main__': unittest.main()
