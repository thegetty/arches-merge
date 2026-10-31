"""Matched-reference persistence and native replay at the in-memory ORM boundary."""
import copy
from types import SimpleNamespace
import unittest

from tests import test_resources_merge as fixtures


class RelationshipReplayTests(unittest.TestCase):
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