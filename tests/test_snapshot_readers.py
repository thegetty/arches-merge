"""Avoid graph loading while preserving log schemas and report behavior."""
import copy
import json
from types import SimpleNamespace as Obj
from unittest import TestCase
from unittest.mock import Mock
from tests.test_resources_merge import load_merge_methods
from tests import test_resources_merge as fixtures


class SnapshotReadTests(TestCase):
    def test_post_merge_tile_ids_use_plain_rows_and_keep_string_ids(self):
        h = fixtures.IndexLifecycleTests('runTest'); h.setUp(); h.prepare_merge()
        from uuid import UUID
        key = UUID('d3f990b5-cc57-4d0a-b967-79bd64cb9842')
        rows = h.ns['models'].TileModel.objects.filter.return_value
        rows.values_list.return_value = [key]
        self.assertTrue(h.merge.run_load_task('user','test-load','base',['source'])['success'])
        rows.values_list.assert_called_once_with('tileid', flat=True)
        self.assertEqual(h.merge.update_tile_status.call_args.args[1], [str(key)])

    def test_old_tile_snapshot_keeps_proxy_label_fields_precision_and_save_order(self):
        ns=load_merge_methods(); editor=ns['Resourcesmerge'].__new__(ns['Resourcesmerge'])
        events=[]
        class Row:
            _meta=Obj(label_lower='models.tilemodel')
            def save(self):events.append('save')
        class Tile(Row):
            _meta=Obj(label_lower='models.tile')
            objects=Obj(get=Mock(side_effect=AssertionError('Old snapshots do not need graph construction')))
        old=Row();old.tileid='tile';old.fields={'data':{'text':'Original'},'resourceinstance':'source','modified':'2026-11-01T01:02:03.123456Z'}
        new=Tile();new.tileid='tile';new.fields={**copy.deepcopy(old.fields),'resourceinstance':'base'}
        new.save_edit=Mock(side_effect=lambda **kw:events.append('log'))
        ns['Tile']=Tile;ns['models']=Obj(TileModel=Obj(objects=Obj(get=Mock(return_value=old))))
        editor.user='user';editor.loadid='load'
        editor._serialize_obj=lambda row:json.dumps([{'model':row._meta.label_lower,'pk':row.tileid,'fields':row.fields}])
        editor._tile_save(new)
        logged=new.save_edit.call_args.kwargs
        self.assertEqual(json.loads(logged['old_value']),[{'model':'models.tile','pk':'tile','fields':old.fields}])
        self.assertEqual(json.loads(logged['new_value'])[0]['model'],'models.tile')
        self.assertEqual(logged['transaction_id'],'load')
        self.assertEqual(events,['log','save'])
        Tile.objects.get.assert_not_called()

    def test_readonly_deserialization_uses_concrete_tiles_without_changing_inputs(self):
        ns=load_merge_methods();editor=ns['Resourcesmerge'].__new__(ns['Resourcesmerge'])
        payload=[{'model':'models.tile','pk':'tile','fields':{'data':{'x':'value'},'resourceinstance':'owner'}},
                 {'model':'models.resourcexresource','pk':'rx','fields':{'resourceinstanceidfrom':'owner'}}]
        saved=copy.deepcopy(payload)
        captured=[]
        def deserialize(fmt,raw):
            records=json.loads(raw);captured.append(records)
            return [Obj(object=Obj(pk=r['pk'],data=r['fields'].get('data'))) for r in records]
        ns['serializers']=Obj(deserialize=deserialize)
        for value in [payload,json.dumps(payload),payload[0]]:
            self.assertTrue(editor._deserialize_models(value))
            self.assertEqual(captured[-1][0]['model'],'models.tilemodel')
        self.assertEqual(captured[0][1]['model'],'models.resourcexresource')
        self.assertEqual(payload,saved)
        self.assertEqual(editor._deserialize_models('invalid json'),[])
        ns['serializers'].deserialize=Mock(side_effect=ValueError('invalid fields'))
        self.assertEqual(editor._deserialize_models(payload),[])
