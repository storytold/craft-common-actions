import copy
import pathlib
import sys
import unittest
sys.path.insert(0, str(pathlib.Path(__file__).parents[1] / 'scripts'))
from audit_cargo_runtime_units import audit, KEEP_EXAMPLES
from compare_cargo_target_projection import project


def graph():
    units = [{'pkg_id':'dep', 'mode':'build', 'target':{'kind':['lib'],'name':'dep'}, 'features':['a'], 'dependencies':[]}]
    for name, kind in [('app','lib')] + [(name,'example') for name in sorted(KEEP_EXAMPLES)] + [('unneeded','example')]:
        units.append({'pkg_id':'app','mode':'build','target':{'kind':[kind],'name':name}, 'dependencies':[{'index':0}]})
    return {'version':1,'roots':list(range(1,len(units))),'units':units}


class RuntimeAuditTests(unittest.TestCase):
    def test_gpu_examples_retained_and_union_not_double_counted(self):
        full=graph(); selected,dropped=project(full,KEEP_EXAMPLES)
        self.assertEqual([r['target'] for r in dropped],['unneeded'])
        result,_=audit(full,selected,{'a':selected,'b':selected})
        self.assertTrue(result['projected_prebuild']['planned_graph_equal'])
        self.assertEqual(result['unique_runtime_units'],len(selected['units']))
        self.assertEqual(result['runtime_units_absent_from_prebuild'],0)

    def test_missing_required_gpu_example_fails(self):
        full=graph(); selected,_=project(full,KEEP_EXAMPLES)
        full['roots']=[i for i in full['roots'] if full['units'][i]['target']['name'] != 'bench_camera_raw']
        with self.assertRaises(ValueError): audit(full,selected,{})

    def test_changed_context_and_doctest_not_hidden(self):
        full=graph(); selected,_=project(full,KEEP_EXAMPLES);runtime=copy.deepcopy(selected)
        runtime['units'][1]['mode']='doctest'
        result,_=audit(full,selected,{'doc':runtime})
        self.assertEqual(result['runtime_commands']['doc']['missing_by_mode'],{'doctest':1})
        runtime=copy.deepcopy(selected); runtime['units'][0]['features']=['different']
        self.assertGreater(audit(full,selected,{'gpu':runtime})[0]['runtime_units_absent_from_prebuild'],0)


if __name__ == '__main__':unittest.main()
