import copy
import importlib.util
import pathlib
import unittest
import subprocess
import tempfile
import sys
import json

spec = importlib.util.spec_from_file_location('units', pathlib.Path(__file__).parents[1] / 'scripts/compare_cargo_units.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def graph():
    return {'version': 1, 'roots': [1], 'units': [
        {'pkg_id': 'dep', 'features': ['a'], 'platform': None, 'dependencies': []},
        {'pkg_id': 'app', 'features': [], 'platform': 'freebsd', 'dependencies': [{'index': 0, 'extern_crate_name': 'dep', 'public': False}]},
    ]}


class UnitGraphTests(unittest.TestCase):
    def test_reordering_indices(self):
        a = graph(); b = graph()
        b['units'].reverse(); b['roots'] = [0]; b['units'][0]['dependencies'][0]['index'] = 1
        self.assertTrue(m.compare(a, b, set())[0]['planned_graph_equal'])

    def test_feature_platform_profile_and_edge_differences(self):
        for key, value in [('features', ['b']), ('platform', 'linux'), ('profile', {'opt_level': '3'})]:
            a = graph(); b = graph(); b['units'][0][key] = value
            self.assertFalse(m.compare(a, b, set())[0]['planned_graph_equal'])
        a = graph(); b = graph(); b['units'][1]['dependencies'][0]['public'] = True
        self.assertFalse(m.compare(a, b, set())[0]['planned_graph_equal'])

    def test_multiplicity(self):
        a = graph(); b = graph(); b['units'].append(copy.deepcopy(b['units'][0]))
        self.assertFalse(m.compare(a, b, set())[0]['planned_graph_equal'])

    def test_excluded_root_only(self):
        a = graph(); b = graph()
        a['units'].append({'pkg_id': 'browser', 'features': [], 'dependencies': [{'index': 0}]}); a['roots'].append(2)
        self.assertTrue(m.compare(a, b, {'browser'})[0]['planned_graph_equal'])

    def test_expected_exclusion_must_be_present_in_baseline(self):
        with self.assertRaises(ValueError): m.compare(graph(), graph(), {'browser'})

    def test_exclusion_must_be_absent_from_reduced(self):
        a = graph()
        a['units'].append({'pkg_id': 'browser', 'features': [], 'dependencies': []})
        a['roots'].append(2)
        with self.assertRaises(ValueError): m.compare(a, copy.deepcopy(a), {'browser'})

    def test_orphan_dependencies_not_hidden(self):
        a = graph(); b = graph(); a['units'].append({'pkg_id': 'orphan', 'features': [], 'dependencies': []})
        self.assertFalse(m.compare(a, b, set())[0]['planned_graph_equal'])

    def test_retained_to_excluded_fails(self):
        with self.assertRaises(ValueError): m.inventory(graph(), {'dep'})

    def test_cycle_and_invalid_index_fail(self):
        for index in [1, -1, 100, True]:
            a = graph(); a['units'][1]['dependencies'][0]['index'] = index
            with self.assertRaises(ValueError): m.inventory(a, set())


    def test_real_git_nul_inventory_and_symlink_integrity(self):
        workflow = (pathlib.Path(__file__).parents[1] / '.github/workflows/photo-unit-graph.yml').read_text()
        blocks = workflow.split("<<'PYCODE'\n")[1:]
        scripts = ['\n'.join(line[10:] for line in block.split('          PYCODE', 1)[0].splitlines()) for block in blocks]
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            photo = root / 'photo'
            photo.mkdir()
            subprocess.run(['git', 'init', '-q', str(photo)], check=True)
            (photo / 'first.txt').write_text('first')
            (photo / 'second with space.txt').write_text('second')
            (photo / 'link').symlink_to('first.txt')
            subprocess.run(['git', '-C', str(photo), 'add', '.'], check=True)
            subprocess.run([sys.executable, '-c', scripts[0]], cwd=root, check=True)
            identity = json.loads((root / 'photo-source-identity.json').read_text())
            self.assertEqual(set(identity), {'photo/first.txt', 'photo/second with space.txt', 'photo/link'})
            self.assertEqual(identity['photo/link'][0], 'symlink')
            subprocess.run([sys.executable, '-c', scripts[1]], cwd=photo, check=True, capture_output=True)
            (photo / 'first.txt').write_text('changed')
            result = subprocess.run([sys.executable, '-c', scripts[1]], cwd=photo, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            (photo / 'first.txt').write_text('first')
            (photo / 'link').unlink()
            (photo / 'link').symlink_to('second with space.txt')
            result = subprocess.run([sys.executable, '-c', scripts[1]], cwd=photo, capture_output=True)
            self.assertNotEqual(result.returncode, 0)

    def test_root_and_header_changes(self):
        a = graph(); b = graph(); b['roots'] = [0]
        self.assertFalse(m.compare(a, b, set())[0]['planned_graph_equal'])
        b = graph(); b['new_header'] = 'different'
        self.assertFalse(m.compare(a, b, set())[0]['planned_graph_equal'])


if __name__ == '__main__': unittest.main()
