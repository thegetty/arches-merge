"""Matched-reference persistence and native replay at the in-memory ORM boundary."""
import copy
from types import SimpleNamespace
import unittest

from tests import test_resources_merge as fixtures


class RelationshipReplayTests(unittest.TestCase):
    def test_distinct_scalar_metadata_keeps_repeat_cards_children_and_exact_undo(self):
        for base_type, source_type in [('type-a', 'type-b'), (None, 'type-b'), ('type-a', None)]:
            for label in [None, {'en': {'value': 'Same place', 'direction': 'ltr'}}]:
                with self.subTest(base_type=base_type, source_type=source_type, label=label):
                    helper = fixtures.UriMergeTests('runTest')
                    helper.setUp()
                    parents = []
                    for owner, metadata in [('base', base_type), ('source', source_type)]:
                        reference = {'resourceId': 'place', 'resourceXresourceId': owner+'-location-rx'}
                        parent = helper.add_tile(owner+'-parent', owner, group='sojourn', data={
                            'location': [reference], 'label': label, 'metatype': metadata})
                        parents.append(parent)
                        child_ref = {'resourceId': owner+'-document', 'resourceXresourceId': owner+'-child-rx'}
                        child = helper.add_tile(owner+'-child', owner, group='references', parent=parent,
                                                data={'refs': [child_ref]})
                        for tile, ref in [(parent, reference), (child, child_ref)]:
                            helper.relations[ref['resourceXresourceId']] = SimpleNamespace(
                                resourcexid=ref['resourceXresourceId'], resourceinstanceidfrom=owner,
                                resourceinstanceidto=ref['resourceId'], tileid=tile.tileid)
                    before = helper.snapshot()
                    preview = helper.preview('source')
                    self.assertIn('source-parent', preview)
                    self.assertIn('source-child', preview)
                    self.assertEqual(helper.snapshot(), before)
                    helper.execute('source')
                    for parent in parents:
                        self.assertIn(parent.tileid, helper.tiles)
                        self.assertEqual(helper.tiles[parent.tileid].data, parent.data)
                        self.assertEqual(helper.tiles[parent.tileid].resourceinstance, 'base')
                    for owner in ['base', 'source']:
                        self.assertEqual(helper.tiles[owner+'-child'].parenttile.tileid, owner+'-parent')
                        self.assertEqual(helper.tiles[owner+'-child'].resourceinstance, 'base')
                    self.assertTrue(all(r.resourceinstanceidfrom == 'base' for r in helper.relations.values()))
                    helper.merge.reverse_edit_log_entries('test-load')
                    self.assertEqual(helper.snapshot(), before)

    def test_children_follow_the_parent_selected_before_reference_union(self):
        helper = fixtures.UriMergeTests('runTest')
        helper.setUp()
        base_a = helper.add_tile('base-a', 'base', group='sojourn', data={
            'location': [{'resourceId': 'place-a', 'resourceXresourceId': 'base-a-rx'}],
            'types': ['first-type']})
        base_b = helper.add_tile('base-b', 'base', group='sojourn', data={
            'location': [{'resourceId': 'place-b', 'resourceXresourceId': 'base-b-rx'}],
            'types': ['base-type']})
        parent = helper.add_tile('source-parent', 'source', group='sojourn', data={
            'location': [{'resourceId': 'place-b', 'resourceXresourceId': 'source-rx'}],
            'types': ['source-type']})
        references = [{'resourceId': 'document-'+str(i), 'resourceXresourceId': 'child-rx-'+str(i)}
                      for i in range(3)]
        child = helper.add_tile('source-child', 'source', group='references', parent=parent,
                                data={'refs': references})
        for owner, tile, ref in [('base', base_a, base_a.data['location'][0]),
                                 ('base', base_b, base_b.data['location'][0]),
                                 ('source', parent, parent.data['location'][0])]+[
                                     ('source', child, ref) for ref in references]:
            helper.relations[ref['resourceXresourceId']] = SimpleNamespace(
                resourcexid=ref['resourceXresourceId'], resourceinstanceidfrom=owner,
                resourceinstanceidto=ref['resourceId'], tileid=tile.tileid)

        def delete_relation(rel):
            helper.logs.append(SimpleNamespace(edittype='resourcexresource delete',
                oldvalue=('relation', rel.resourcexid, copy.deepcopy(helper.relations[rel.resourcexid]))))
            del helper.relations[rel.resourcexid]
        helper.merge._resource_x_resource_delete = delete_relation
        before = helper.snapshot()
        self.assertIn('source-child', helper.preview('source'))
        self.assertEqual(helper.snapshot(), before)
        helper.execute('source')
        self.assertIn('source-child', helper.tiles)
        self.assertEqual(helper.tiles['source-child'].parenttile.tileid, 'base-b')
        self.assertEqual(helper.tiles['source-child'].resourceinstance, 'base')
        self.assertEqual(set(helper.tiles['base-b'].data['types']), {'base-type', 'source-type'})
        for ref in references:
            self.assertEqual(helper.relations[ref['resourceXresourceId']].resourceinstanceidfrom, 'base')
        helper.merge.reverse_edit_log_entries('test-load')
        self.assertEqual(helper.snapshot(), before)

    def test_overlapping_references_move_only_distinct_row_and_undo_exactly(self):
        helper = fixtures.UriMergeTests('runTest')
        helper.setUp()
        merge = helper.merge
        shared = {'resourceId': 'peer-shared', 'resourceXresourceId': 'base-shared'}
        duplicate = {'resourceId': 'peer-shared', 'resourceXresourceId': 'source-shared'}
        distinct = {'resourceId': 'peer-distinct', 'resourceXresourceId': 'source-distinct'}
        base = helper.add_tile('base-tile', 'base', group='references', data={'refs': [shared]})
        source = helper.add_tile('source-tile', 'source', group='references', data={'refs': [duplicate, distinct]})
        for ref, owner, tile in [(shared, 'base', base), (duplicate, 'source', source), (distinct, 'source', source)]:
            helper.relations[ref['resourceXresourceId']] = SimpleNamespace(
                resourcexid=ref['resourceXresourceId'], resourceinstanceidfrom=owner,
                resourceinstanceidto=ref['resourceId'], tileid=tile.tileid)
        before = helper.snapshot()

        def delete_relation(rel):
            helper.logs.append(SimpleNamespace(edittype='resourcexresource delete',
                oldvalue=('relation', rel.resourcexid, copy.deepcopy(helper.relations[rel.resourcexid]))))
            del helper.relations[rel.resourcexid]
        merge._resource_x_resource_delete = delete_relation
        def save_relation(rel):
            if hasattr(rel.tileid, 'tileid'):
                rel.tileid = rel.tileid.tileid
            helper.save('relation', rel.resourcexid, rel)
        merge._resource_x_resource_save = save_relation

        # Reproduce the database side effect of the old unlogged SQL statement.
        cursor = helper.ns['connection'].cursor.return_value.__enter__.return_value
        cursor.fetchall.return_value = [('resource-instance-list',)]
        def execute(sql, params):
            if sql.lstrip().upper().startswith('UPDATE RESOURCE_X_RESOURCE'):
                owner, retained_tile, source_tile = params
                for row in helper.relations.values():
                    if row.tileid == source_tile:
                        row.resourceinstanceidfrom = owner
                        row.tileid = retained_tile
        cursor.execute.side_effect = execute

        merge.check_for_same_information(source, [base], ['refs'], flag_label=True, baseId='base')
        helper.cleanup('source')
        self.assertEqual(set(helper.relations), {'base-shared', 'source-distinct'})
        self.assertEqual(helper.relations['source-distinct'].resourceinstanceidfrom, 'base')
        self.assertEqual(helper.relations['source-distinct'].tileid, 'base-tile')
        self.assertEqual({x['resourceId'] for x in helper.tiles['base-tile'].data['refs']},
                         {'peer-shared', 'peer-distinct'})
        merge.reverse_edit_log_entries('test-load')
        self.assertEqual(helper.snapshot(), before)
