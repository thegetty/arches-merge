"""Undo must preserve independent edits made after the selected merge."""
import copy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock

from tests.test_resources_merge import load_merge_methods


class SharedTileUnloadTests(unittest.TestCase):
    def setUp(self):
        self.ns=load_merge_methods()
        self.merge=self.ns['Resourcesmerge'].__new__(self.ns['Resourcesmerge'])

    def refs(self, a='a', b='b'):
        return {'locations':[{'resourceId':a,'resourceXresourceId':'rx-a'},
                             {'resourceId':b,'resourceXresourceId':'rx-b'}],
                'label':'Original'}

    def test_both_undo_orders_preserve_the_other_merge(self):
        before=self.refs();after_a=self.refs('a1');after_b=self.refs('a1','b1')
        for earlier_first in (True,False):
            with self.subTest(earlier_first=earlier_first):
                current=copy.deepcopy(after_b)
                edits=[(before,after_a),(after_a,after_b)]
                order=[0,1] if earlier_first else [1,0]
                for index in order:
                    current=self.merge._reverse_snapshot_value(*edits[index],current)
                    if index==order[0]:
                        self.assertEqual(current,self.refs('a' if index==0 else 'a1','b1' if index==0 else 'b'))
                self.assertEqual(current,before)

    def test_native_replay_uses_delta_and_keeps_later_editorial_values(self):
        before={'data':self.refs(),'resourceinstance':'group','sortorder':0}
        after={'data':self.refs('a1'),'resourceinstance':'group','sortorder':0}
        current={'data':self.refs('a1','b1'),'resourceinstance':'group','sortorder':2}
        current['data']['label']='Later editorial label'
        class Record:
            objects=MagicMock()
            def __init__(self,fields):self.pk='tile';self.fields=copy.deepcopy(fields)
        row=Record(current)
        proxy=SimpleNamespace(_meta=SimpleNamespace(concrete_model=Record),objects=MagicMock())
        self.ns['apps']=SimpleNamespace(get_model=lambda label:proxy)
        Record.objects.select_for_update.return_value.filter.return_value.first.return_value=row
        def payload(fields):return json.dumps([{'model':'models.tilemodel','pk':'tile','fields':fields}])
        deserialized=[]
        def deserialize(fmt, raw):
            deserialized.append(raw)
            record=Record(json.loads(raw)[0]['fields'])
            return [SimpleNamespace(object=record,save=lambda: setattr(row,'fields',copy.deepcopy(record.fields)))]
        self.ns['serializers']=SimpleNamespace(deserialize=deserialize)
        self.merge._serialize_obj=lambda record:payload(record.fields)
        log=SimpleNamespace(edittype='tile edit',oldvalue=payload(before),newvalue=payload(after))
        changes=MagicMock();changes.order_by.return_value.all.return_value=[log]
        self.ns['EditLog']=SimpleNamespace(objects=SimpleNamespace(filter=lambda **kw:changes))
        self.assertEqual(self.merge.reverse_edit_log_entries('merge-a'),1)
        self.assertEqual(row.fields,{'data':{**self.refs('a','b1'),'label':'Later editorial label'},'resourceinstance':'group','sortorder':2})
        self.assertEqual(len(deserialized),1)
        proxy.objects.select_for_update.assert_not_called()

    def test_same_value_conflicts_and_structural_reference_changes_refuse_undo(self):
        for before,after,current in [('a','a1','a2'),
                                     (self.refs(),self.refs('a1'),self.refs('a2','b1')),
                                     ([{'resourceXresourceId':'old'}],[{'resourceXresourceId':'new'}],[{'resourceXresourceId':'third'}])]:
            with self.subTest(before=before):
                saved=copy.deepcopy(current)
                with self.assertRaisesRegex(ValueError,'changed after this merge'):
                    self.merge._reverse_snapshot_value(before,after,current)
                self.assertEqual(current,saved)

    def test_empty_legacy_snapshot_is_still_skipped(self):
        log=SimpleNamespace(edittype='tile edit',oldvalue='[]')
        changes=MagicMock();changes.order_by.return_value.all.return_value=[log]
        self.ns['EditLog']=SimpleNamespace(objects=SimpleNamespace(filter=lambda **kw:changes))
        self.ns['serializers']=MagicMock()
        self.assertEqual(self.merge.reverse_edit_log_entries('legacy-merge'),0)
        self.ns['serializers'].deserialize.assert_not_called()

    def test_independent_dictionary_changes_and_new_references_survive(self):
        before=self.refs();after=self.refs('a1');current=self.refs('a1','b1')
        current['locations'].reverse()
        current['locations'].append({'resourceId':'c','resourceXresourceId':'rx-c'})
        current['new_note']='Added later'
        restored=self.merge._reverse_snapshot_value(before,after,current)
        self.assertEqual([r['resourceId'] for r in restored['locations']],['b1','a','c'])
        self.assertEqual(restored['new_note'],'Added later')

    def test_deleted_object_with_later_values_is_not_overwritten(self):
        class Record:
            objects=MagicMock()
            pk='tile'
        Record.objects.select_for_update.return_value.filter.return_value.first.return_value=Record()
        def snapshot(value):return json.dumps([{'model':'models.tilemodel','pk':'tile','fields':{'data':value}}])
        self.merge._serialize_obj=lambda record:snapshot('Later value')
        for kind in ('delete','tile delete','resourcexresource delete'):
            with self.subTest(kind=kind):
                log=SimpleNamespace(edittype=kind,oldvalue=snapshot('Original value'))
                with self.assertRaisesRegex(ValueError,'deleted object changed after this merge'):
                    self.merge._reverse_snapshot(log,Record())

    def test_cleared_tile_fields_logged_as_null_restore_from_absent_keys(self):
        before={'data':{'label':{'en':{'value':'Source'}}}}
        after={'data':{'label':None}}
        self.assertEqual(self.merge._reverse_snapshot_value(before,after,{'data':{}}),before)
        self.assertEqual(self.merge._reverse_snapshot_value(before,{'data':{}},after),before)

    def test_auto_modified_timestamp_restores_when_no_later_values_changed(self):
        class Record:
            objects=MagicMock()
            pk='row'
        Record.objects.select_for_update.return_value.filter.return_value.first.return_value=Record()
        def snapshot(target,modified):return json.dumps([{'model':'models.resourcexresource','pk':'row','fields':{'target':target,'modified':modified}}])
        log=SimpleNamespace(edittype='resourcexresource edit',oldvalue=snapshot('a','original'),newvalue=snapshot('a1','original'))
        self.merge._serialize_obj=lambda record:snapshot('a1','save-time')
        restored=json.loads(self.merge._reverse_snapshot(log,Record()))[0]['fields']
        self.assertEqual(restored,{'target':'a','modified':'original'})
