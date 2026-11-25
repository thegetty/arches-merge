"""Registration migration must preserve each existing merge row and its history."""
import ast
import copy
from pathlib import Path
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1] / 'arches_resource_merge'
OLD = 'Resources_merge'
NEW = 'arches-resource-merge'
PREFIX = 'views/components/etl_modules/'

class ComponentRegistrationTests(unittest.TestCase):
    def load_migration(self):
        path = ROOT / 'migrations/0002_use_distinct_component.py'
        tree = ast.parse(path.read_text())
        tree.body = [x for x in tree.body if isinstance(x, (ast.FunctionDef, ast.Assign))]
        namespace = {}
        exec(compile(tree, str(path), 'exec'), namespace)
        return namespace

    def test_forward_reverse_preserve_uuid_configuration_and_other_modules(self):
        namespace = self.load_migration()
        rows = [dict(pk=pk, slug=OLD, modulename='Resources_merge.py', classname='Resourcesmerge', component=PREFIX+OLD, componentname=OLD, config={'custom':True}) for pk in ['etl-id', 'server31-id']]
        rows.append(dict(pk='other',slug='other',modulename='Resources_merge.py',classname='Resourcesmerge',component=PREFIX+OLD,componentname=OLD,config={}))
        rows.append(dict(pk='custom',slug=OLD,modulename='Resources_merge.py',classname='Resourcesmerge',component='custom/component',componentname='custom',config={}))
        before=copy.deepcopy(rows)
        class Manager:
            def filter(self, **conditions):
                selected=[row for row in rows if all(row.get(k)==v for k,v in conditions.items())]
                return SimpleNamespace(update=lambda **changes: [row.update(changes) for row in selected])
        apps=SimpleNamespace(get_model=lambda app,name: SimpleNamespace(objects=Manager()))
        namespace['forwards'](apps,None)
        for row in rows[:2]:
            self.assertEqual(row['componentname'],NEW)
            self.assertEqual(row['component'],PREFIX+NEW)
            self.assertEqual(row['config'],{'custom':True})
        self.assertEqual(rows[2:],before[2:])
        namespace['forwards'](apps,None)
        namespace['backwards'](apps,None)
        self.assertEqual(rows,before)

    def test_backend_and_javascript_agree_on_distinct_name(self):
        tree=ast.parse((ROOT/'etl_modules/Resources_merge.py').read_text(encoding='utf-8'))
        details=next(ast.literal_eval(node.value) for node in tree.body if isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='details' for t in node.targets))
        self.assertEqual(details['component'],PREFIX+NEW)
        self.assertEqual(details['componentname'],NEW)
        source=(ROOT/f'media/js/{PREFIX}{NEW}.js').read_text(encoding='utf-8')
        self.assertIn('ko.components.register("'+NEW+'"',source)
        self.assertEqual(details['slug'],OLD)
        self.assertEqual(details['modulename'],'Resources_merge.py')