"""Single-occurrence conflict checks follow retained parent contexts."""
import unittest
from tests import test_single_cards as fixtures


class NestedPreflightTests(unittest.TestCase):
    def setUp(self):
        self.h = fixtures.SingleCardTests('runTest')
        self.h.setUp()

    def add(self, id, owner, group, data, parent=None, single=False):
        tile = self.h.add_tile(id, owner, group=group, data=data, parent=parent)
        if single:
            tile.nodegroup.cardinality = '1'
            self.h.tiles[id].nodegroup.cardinality = '1'
        return tile

    def test_conflicting_nested_singleton_under_matching_empty_roots_is_rejected(self):
        base = self.add('base-root', 'base', 'root', {}, single=True)
        source = self.add('source-root', 'source', 'root', {}, single=True)
        self.add('base-child', 'base', 'child', {'text': 'Base'}, base, True)
        self.add('source-child', 'source', 'child', {'text': 'Source'}, source, True)
        before = self.h.snapshot()
        with self.assertRaisesRegex(ValueError, 'Conflicting nonblank'):
            self.h.merge._validate_single_cards(self.h.filter_tiles(resourceinstance='base'), ['source'])
        self.assertEqual(self.h.snapshot(), before)

    def test_cross_source_nested_singleton_conflict_is_rejected(self):
        for owner in ['first', 'second']:
            root = self.add(owner+'-root', owner, 'root', {}, single=True)
            self.add(owner+'-child', owner, 'child', {'text': owner}, root, True)
        with self.assertRaises(ValueError):
            self.h.merge._validate_single_cards([], ['first', 'second'])
        self.h.merge._validate_single_cards([], ['first', 'second'], ['second-child'])

    def test_repeated_parent_context_controls_singleton_conflicts(self):
        for shared_parent in [True, False]:
            with self.subTest(shared_parent=shared_parent):
                self.setUp()
                for owner in ['base', 'source']:
                    data = {'name': 'Same'} if shared_parent else {'name': {'en': {'value': owner}}}
                    root = self.add(owner+'-root', owner, 'repeat', data)
                    self.add(owner+'-child', owner, 'child', {'text': owner}, root, True)
                before = self.h.snapshot()
                base_tiles = self.h.filter_tiles(resourceinstance='base')
                if shared_parent:
                    with self.assertRaises(ValueError):
                        self.h.merge._validate_single_cards(base_tiles, ['source'])
                else:
                    self.h.merge._validate_single_cards(base_tiles, ['source'])
                self.assertEqual(self.h.snapshot(), before)

    def test_nested_blank_fill_is_simulated_without_mutating_tiles(self):
        for owner, value in [('base', None), ('source', 'Filled')]:
            root = self.add(owner+'-root', owner, 'root', {}, single=True)
            child = self.add(owner+'-child', owner, 'child', {'text': value}, root, True)
            self.add(owner+'-grandchild', owner, 'grandchild', {'text': value}, child, True)
        before = self.h.snapshot()
        self.h.merge._validate_single_cards(self.h.filter_tiles(resourceinstance='base'), ['source'])
        self.assertEqual(self.h.snapshot(), before)
    def test_equal_labels_with_different_parent_metadata_require_review(self):
        for owner in ['base', 'source']:
            root = self.add(owner+'-root', owner, 'repeat',
                {'label': {'en': {'value': 'Same', 'direction': 'ltr'}}, 'metadata': owner})
            self.add(owner+'-child', owner, 'child', {'text': owner}, root, True)
        with self.assertRaises(ValueError):
            self.h.merge._validate_single_cards(self.h.filter_tiles(resourceinstance='base'), ['source'])

    def test_excluding_parent_without_child_is_rejected_before_writes(self):
        parent=self.add('parent','source','root',{},single=True)
        self.add('child','source','child',{'text':'Filled'},parent,True)
        before=self.h.snapshot()
        with self.assertRaisesRegex(ValueError,'excluded parent'):
            self.h.merge._validate_single_cards([],['source'],['parent'])
        self.assertEqual(self.h.snapshot(),before)
        self.h.merge._validate_single_cards([],['source'],['parent','child'])
