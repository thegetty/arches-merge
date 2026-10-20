"""Database-free checks of production methods; ORM persistence needs host acceptance."""
import ast
import copy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock


def load_merge_methods():
    path = Path(__file__).resolve().parents[1] / "arches_resource_merge/etl_modules/Resources_merge.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    body = []
    for item in tree.body:
        if isinstance(item, ast.FunctionDef):
            body.append(item)
        elif isinstance(item, ast.ClassDef) and item.name == "Resourcesmerge":
            item.bases = []
            item.decorator_list = []
            for method in item.body:
                if isinstance(method, ast.FunctionDef):
                    method.decorator_list = [d for d in method.decorator_list
                                             if isinstance(d, ast.Name) and d.id == "staticmethod"]
            body.append(item)
    module = ast.parse("from __future__ import annotations\nimport json")
    module.body.extend(body)
    namespace = {"RXR_FROM_FIELD": "resourceinstanceidfrom", "RXR_TILE_FIELD": "tileid"}
    exec(compile(ast.fix_missing_locations(module), str(path), "exec"), namespace)
    return namespace


class Query(list):
    def exists(self):
        return bool(self)


class ResourcesMergeTests(unittest.TestCase):
    def setUp(self):
        self.ns = load_merge_methods()
        self.merge = self.ns["Resourcesmerge"].__new__(self.ns["Resourcesmerge"])
        self.merge._tile_save = Mock()
        self.merge._tile_delete = Mock()
        self.merge._resource_x_resource_save = Mock()
        self.merge._resource_x_resource_delete = Mock()
        self.ns["Resource"] = SimpleNamespace(objects=SimpleNamespace(get=Mock(return_value="base")))
        self.relation = SimpleNamespace(resourceinstanceidfrom="source", tileid="source-tile")
        self.ns["ResourceXResource"] = SimpleNamespace(objects=SimpleNamespace(filter=Mock(return_value=[self.relation])))

    def tile(self, id, value, **kwargs):
        return SimpleNamespace(tileid=id, data={"type": copy.deepcopy(value)}, sortorder=0,
                               resourceinstance="source", parenttile=None, **kwargs)

    def test_nullable_concepts_have_preview_execution_parity(self):
        for base_value, source_value, expected in [
            (None, ["preferred"], ["preferred"]),
            (["preferred"], None, ["preferred"]),
            (None, None, None),
            (["preferred"], ["alternate", "preferred"], ["preferred", "alternate"]),
            ([], ["alternate"], ["alternate"]),
            (None, "preferred", ["preferred"]),
            ("preferred", None, "preferred"),
            ("preferred", ["alternate"], ["preferred", "alternate"]),
        ]:
            for preview in (True, False):
                with self.subTest(base=base_value, source=source_value, preview=preview):
                    base = self.tile("base-tile", base_value)
                    source = self.tile("source-tile", source_value)
                    if preview:
                        self.merge.merge_reference_preview("base", source, base, "sources", ["type"])
                    else:
                        self.merge.merge_reference("base", source, base, ["type"])
                    self.assertEqual(base.data["type"], expected)
        self.merge._resource_x_resource_save.assert_not_called()
        self.merge._resource_x_resource_delete.assert_not_called()

    def test_missing_base_key_accepts_source_reference(self):
        ref = {"resourceId": "external", "resourceXresourceId": "relationship"}
        for preview in (True, False):
            with self.subTest(preview=preview):
                base = self.tile("base-tile", None)
                base.data = {}
                source = self.tile("source-tile", [ref])
                if preview:
                    self.merge.merge_reference_preview("base", source, base, "type", ["type"])
                else:
                    self.merge.merge_reference("base", source, base, ["type"])
                    self.assertEqual(self.relation.resourceinstanceidfrom, "base")
                    self.assertIs(self.relation.tileid, base)
                self.assertEqual(base.data["type"], [ref])

    def test_same_reference_twice_is_transferred_once(self):
        ref = {"resourceId": "external", "resourceXresourceId": "relationship"}
        for preview in (True, False):
            with self.subTest(preview=preview):
                base = self.tile("base-tile", [])
                source = self.tile("source-tile", [ref, ref])
                if preview:
                    self.merge.merge_reference_preview("base", source, base, "type", ["type"])
                else:
                    self.merge.merge_reference("base", source, base, ["type"])
                self.assertEqual(base.data["type"], [ref])

    def test_distinct_child_reference_moves_to_matching_parent(self):
        parent = self.tile("source-parent", {"en": "matched"})
        base_parent = self.tile("base-parent", {"en": "matched"})
        child = self.tile("source-child", [{"resourceId": "external", "resourceXresourceId": "relationship"}])
        child.parenttile = parent
        self.ns["Tile"] = SimpleNamespace(objects=SimpleNamespace(
            filter=lambda **kw: Query([parent]), get=lambda **kw: child))
        self.merge.check_information([base_parent], parent, ["type"], "base", True,
                                     childtileid=child.tileid, matching_child=[], childKeys=["type"])
        self.assertEqual(child.resourceinstance, "base")
        self.assertIs(child.parenttile, base_parent)
        self.assertEqual(self.relation.resourceinstanceidfrom, "base")
        self.merge._resource_x_resource_delete.assert_not_called()

    def test_excluded_tile_is_not_processed(self):
        tile = self.tile("excluded", ["keep"])
        self.ns["Tile"] = SimpleNamespace(objects=SimpleNamespace(filter=lambda **kw: Query([tile])))
        self.ns["log_event_details"] = Mock()
        self.merge.loadid = "load"
        self.merge._process_merge_tile = Mock()
        self.merge._process_merge_resource("source", "base", [], ["excluded"])
        self.merge._process_merge_tile.assert_not_called()
        self.assertEqual(tile.resourceinstance, "source")

    def test_matched_non_label_reference_with_blank_base(self):
        ref = {"resourceId": "external", "resourceXresourceId": "relationship"}
        for preview in (True, False):
            with self.subTest(preview=preview):
                base = self.tile("base-tile", None)
                source = self.tile("source-tile", [ref])
                self.ns["connection"] = MagicMock()
                self.ns["connection"].cursor.return_value.__enter__.return_value.fetchall.return_value = [("resource-instance-list",)]
                self.ns["Tile"] = SimpleNamespace(objects=SimpleNamespace(
                    filter=lambda **kw: Query([t for t in [base, source] if t.tileid == kw.get("tileid")])))
                self.ns["ResourceXResource"].objects.filter = lambda **kw: [self.relation] if "resourcexid" in kw else []
                if preview:
                    self.merge.check_for_same_information_preview(source, [base], ["type"], "type", baseId="base")
                else:
                    self.merge.check_for_same_information(source, [base], ["type"], baseId="base")
                    self.assertEqual(self.relation.resourceinstanceidfrom, "base")
                    self.assertIs(self.relation.tileid, base)
                self.assertEqual(base.data["type"], [ref])


class UriMergeTests(unittest.TestCase):
    """Production merge/reverse methods with an in-memory ORM boundary."""
    uri = "https://example.org/shared"

    def setUp(self):
        self.ns = load_merge_methods()
        self.merge = self.ns["Resourcesmerge"].__new__(self.ns["Resourcesmerge"])
        self.merge.nodegroups = {}
        self.merge._uri_tile_replacements = {}
        self.merge.loadid = "test-load"
        self.tiles = {}
        self.relations = {}
        self.logs = []
        self.ns["logger"] = Mock()
        self.ns["log_event_details"] = Mock()
        self.ns["Resource"] = SimpleNamespace(objects=SimpleNamespace(get=lambda **kw: kw["pk"]))
        self.ns["NodeGroup"] = SimpleNamespace(objects=SimpleNamespace(
            get=lambda **kw: SimpleNamespace(nodegroupid=kw["nodegroupid"], cardinality="n")))
        self.merge.get_node = lambda key: SimpleNamespace(datatype="url" if key == "uri" else "string")
        self.ns["Tile"] = SimpleNamespace(objects=SimpleNamespace(filter=self.filter_tiles, get=self.get_tile))
        self.ns["ResourceXResource"] = SimpleNamespace(objects=SimpleNamespace(filter=self.filter_relations))
        self.merge._tile_save = lambda tile: self.save("tile", tile.tileid, tile)
        self.merge._tile_delete = self.delete_tile
        self.merge._resource_x_resource_save = lambda rel: self.save("relation", rel.resourcexid, rel)
        self.merge._resource_x_resource_delete = Mock(side_effect=AssertionError("Unexpected relationship deletion"))
        self.ns["connection"] = MagicMock()
        self.ns["connection"].cursor.return_value.__enter__.return_value.fetchall.return_value = [("url",)]
        self.ns["serializers"] = SimpleNamespace(deserialize=self.deserialize)
        changes = MagicMock()
        changes.order_by.return_value.all.side_effect = lambda: list(reversed(self.logs))
        self.ns["EditLog"] = SimpleNamespace(objects=SimpleNamespace(filter=lambda **kw: changes))

    def add_tile(self, id, owner, *, url=None, label="", group="exact-match", parent=None, data=None):
        tile = SimpleNamespace(tileid=id, resourceinstance=owner, parenttile=parent,
                               nodegroup=SimpleNamespace(nodegroupid=group, cardinality="n"),
                               sortorder=0, provisionaledits=None,
                               data=copy.deepcopy(data if data is not None else {
                                   "uri": {"url": url if url is not None else self.uri, "url_label": label}}))
        self.tiles[id] = copy.deepcopy(tile)
        return tile

    def get_tile(self, **kwargs):
        tile = copy.deepcopy(self.tiles[kwargs.get("pk", kwargs.get("tileid"))])
        if tile.parenttile and tile.parenttile.tileid in self.tiles:
            tile.parenttile = self.get_tile(pk=tile.parenttile.tileid)
        return tile

    def filter_tiles(self, **kwargs):
        result = []
        for key in self.tiles:
            tile = self.get_tile(pk=key)
            if all(getattr(tile, name) == value for name, value in kwargs.items()):
                result.append(tile)
        return Query(result)

    def filter_relations(self, **kwargs):
        return Query([copy.deepcopy(rel) for rel in self.relations.values()
                      if all(getattr(rel, key) == value for key, value in kwargs.items())])

    def save(self, kind, key, obj):
        store = self.tiles if kind == "tile" else self.relations
        self.logs.append(SimpleNamespace(edittype="tile edit" if kind == "tile" else "resourcexresource edit",
                                         oldvalue=(kind, key, copy.deepcopy(store[key]))))
        store[key] = copy.deepcopy(obj)

    def delete_tile(self, tile):
        self.logs.append(SimpleNamespace(edittype="tile delete", oldvalue=("tile", tile.tileid, copy.deepcopy(self.tiles[tile.tileid]))))
        pending = [tile.tileid]
        while pending:
            key = pending.pop()
            pending.extend(t.tileid for t in self.tiles.values()
                           if t.parenttile and t.parenttile.tileid == key)
            self.tiles.pop(key, None)
            for relid, rel in list(self.relations.items()):
                if rel.tileid == key:
                    del self.relations[relid]

    def deserialize(self, fmt, payload):
        kind, key, obj = payload
        store = self.tiles if kind == "tile" else self.relations
        return [SimpleNamespace(save=lambda: store.update({key: copy.deepcopy(obj)}))]

    def snapshot(self):
        return ({key: {"data": copy.deepcopy(t.data), "owner": t.resourceinstance,
                      "parent": t.parenttile.tileid if t.parenttile else None,
                      "group": t.nodegroup.nodegroupid, "sortorder": t.sortorder,
                      "provisionaledits": copy.deepcopy(t.provisionaledits)}
                 for key, t in self.tiles.items()}, copy.deepcopy(self.relations))

    def cleanup(self, *owners):
        for owner in owners:
            for tile in self.filter_tiles(resourceinstance=owner):
                tile.data = {}
                self.merge._tile_save(tile)
            for tile in self.filter_tiles(resourceinstance=owner):
                if tile.tileid in self.tiles:
                    self.merge._tile_delete(tile)

    def execute(self, *owners):
        self.merge._uri_tile_replacements = {}
        base_tiles = self.filter_tiles(resourceinstance="base")
        for owner in owners:
            self.merge._process_merge_resource(owner, "base", base_tiles)
        self.cleanup(*owners)

    def preview(self, *owners, excluded=None):
        self.merge._uri_tile_replacements = {}
        base_tiles = self.filter_tiles(resourceinstance="base")
        added = []
        for owner in owners:
            result = self.merge._process_merge_resource_preview(owner, "base", base_tiles, "references", excluded)
            added.extend(result[0])
        return added

    def test_duplicate_uri_is_merged_in_preview_without_writes(self):
        self.add_tile("base-uri", "base")
        self.add_tile("source-uri", "source")
        baseline = self.snapshot()
        self.assertNotIn("source-uri", self.preview("source"))
        self.assertEqual(self.snapshot(), baseline)
        self.assertEqual(self.logs, [])

    def test_duplicate_uri_and_labels_are_restored_by_native_replay(self):
        self.add_tile("base-uri", "base", label="")
        self.add_tile("source-uri", "source", label="External identifier")
        baseline = self.snapshot()
        self.execute("source")
        self.assertEqual(set(self.tiles), {"base-uri"})
        self.assertEqual(self.tiles["base-uri"].data["uri"]["url_label"], "External identifier")
        self.assertGreater(self.merge.reverse_edit_log_entries("test-load"), 0)
        self.assertEqual(self.snapshot(), baseline)

    def test_conflicting_labels_or_metadata_are_preserved(self):
        for conflict in ("label", "metadata", "provisionaledits"):
            with self.subTest(conflict=conflict):
                self.setUp()
                self.add_tile("base-uri", "base", label="Base label")
                source = self.add_tile("source-uri", "source", label="Base label")
                if conflict == "label":
                    self.tiles[source.tileid].data["uri"]["url_label"] = "Source label"
                elif conflict == "metadata":
                    self.tiles[source.tileid].data["annotation"] = "Keep this note"
                else:
                    self.tiles[source.tileid].provisionaledits = {"editor": "pending"}
                baseline = self.snapshot()
                self.assertIn("source-uri", self.preview("source"))
                self.execute("source")
                self.assertEqual(set(self.tiles), {"base-uri", "source-uri"})
                self.merge.reverse_edit_log_entries("test-load")
                self.assertEqual(self.snapshot(), baseline)

    def test_distinct_uris_and_uri_spellings_remain_distinct(self):
        for uri in (self.uri + "/", self.uri + "#fragment", self.uri.replace("https:", "http:")):
            with self.subTest(uri=uri):
                self.setUp()
                self.add_tile("base-uri", "base")
                self.add_tile("source-uri", "source", url=uri)
                self.assertIn("source-uri", self.preview("source"))
                self.execute("source")
                self.assertEqual(set(self.tiles), {"base-uri", "source-uri"})

    def test_same_uri_in_another_field_is_preserved(self):
        self.add_tile("base-uri", "base")
        self.add_tile("source-uri", "source", group="digital-reference")
        self.assertIn("source-uri", self.preview("source"))
        self.execute("source")
        self.assertEqual(set(self.tiles), {"base-uri", "source-uri"})

    def test_non_url_datatype_is_not_deduplicated(self):
        self.merge.get_node = lambda key: SimpleNamespace(datatype="string")
        self.add_tile("base-uri", "base")
        self.add_tile("source-uri", "source")
        self.assertIn("source-uri", self.preview("source"))
        self.execute("source")
        self.assertEqual(set(self.tiles), {"base-uri", "source-uri"})

    def test_same_uri_in_distinct_parent_contexts_is_preserved(self):
        base_parent = self.add_tile("base-parent", "base", group="context", data={"name": {"en": "A"}})
        source_parent = self.add_tile("source-parent", "source", group="context", data={"name": {"en": "B"}})
        self.add_tile("base-uri", "base", parent=base_parent)
        self.add_tile("source-uri", "source", parent=source_parent)
        self.assertIn("source-uri", self.preview("source"))
        self.execute("source")
        self.assertIn("source-uri", self.tiles)
        self.assertEqual(self.tiles["source-uri"].parenttile.tileid, "source-parent")

    def test_multiple_sources_and_repeated_source_uris_keep_one_entry(self):
        self.add_tile("first-uri", "first")
        self.add_tile("second-uri", "first")
        self.add_tile("third-uri", "second")
        baseline = self.snapshot()
        self.assertEqual(self.preview("first", "second"), ["first-uri"])
        self.execute("first", "second")
        self.assertEqual(set(self.tiles), {"first-uri"})
        self.merge.reverse_edit_log_entries("test-load")
        self.assertEqual(self.snapshot(), baseline)

    def test_duplicate_parent_preserves_children_relationships_and_undo(self):
        self.add_tile("base-uri", "base", label="")
        parent = self.add_tile("source-uri", "source", label="External identifier")
        child = self.add_tile("child", "source", group="child", parent=parent,
                              data={"reference": [{"resourceId": "external", "resourceXresourceId": "relationship"}]})
        self.add_tile("grandchild", "source", group="grandchild", parent=child, data={"note": {"en": "Keep"}})
        self.relations["relationship"] = SimpleNamespace(resourcexid="relationship", resourceinstanceidfrom="source", tileid="child")
        baseline = self.snapshot()
        self.assertEqual(self.preview("source"), ["child", "grandchild"])
        self.execute("source")
        self.assertEqual(set(self.tiles), {"base-uri", "child", "grandchild"})
        self.assertEqual(self.tiles["child"].parenttile.tileid, "base-uri")
        self.assertEqual(self.tiles["grandchild"].parenttile.tileid, "child")
        self.assertEqual(self.relations["relationship"].resourceinstanceidfrom, "base")
        self.assertEqual(self.relations["relationship"].tileid, "child")
        self.merge.reverse_edit_log_entries("test-load")
        self.assertEqual(self.snapshot(), baseline)

    def test_excluded_uri_does_not_fill_base_label(self):
        self.add_tile("base-uri", "base", label="")
        self.add_tile("source-uri", "source", label="Source label")
        baseline = self.snapshot()
        self.assertEqual(self.preview("source", excluded=["source-uri"]), [])
        self.merge._process_merge_resource("source", "base", self.filter_tiles(resourceinstance="base"), ["source-uri"])
        self.assertEqual(self.snapshot(), baseline)
        self.assertEqual(self.logs, [])

    def test_children_before_duplicate_uri_parent_are_not_cascade_deleted(self):
        self.add_tile("base-uri", "base", label="")
        self.add_tile("different-base-uri", "base", url="https://example.org/other", label="Other")
        parent = self.add_tile("source-uri", "source", label="External identifier")
        self.add_tile("child", "source", group="child", parent=parent, data={"note": {"en": "Keep"}})
        self.tiles["source-uri"] = self.tiles.pop("source-uri")
        baseline = self.snapshot()
        self.assertEqual(self.preview("source"), ["child"])
        self.execute("source")
        self.assertEqual(set(self.tiles), {"base-uri", "different-base-uri", "child"})
        self.assertEqual(self.tiles["child"].parenttile.tileid, "base-uri")
        self.merge.reverse_edit_log_entries("test-load")
        self.assertEqual(self.snapshot(), baseline)

    def test_parent_order_rejects_cycles(self):
        left = SimpleNamespace(tileid="left", parenttile=None)
        right = SimpleNamespace(tileid="right", parenttile=left)
        left.parenttile = right
        with self.assertRaisesRegex(ValueError, "cyclic parents"):
            self.merge._order_tiles_parent_first([left, right])

    def test_matching_and_source_blank_labels_preserve_base_and_undo(self):
        for base_label, source_label in [("", ""), ("Base", ""), ("Same", "Same")]:
            with self.subTest(base_label=base_label, source_label=source_label):
                self.setUp()
                self.add_tile("base-uri", "base", label=base_label)
                self.add_tile("source-uri", "source", label=source_label)
                baseline = self.snapshot()
                self.assertEqual(self.preview("source"), [])
                self.execute("source")
                self.assertEqual(set(self.tiles), {"base-uri"})
                self.assertEqual(self.tiles["base-uri"].data["uri"]["url_label"], base_label)
                self.merge.reverse_edit_log_entries("test-load")
                self.assertEqual(self.snapshot(), baseline)

    def test_url_tile_with_its_own_relationship_is_preserved(self):
        self.add_tile("base-uri", "base")
        self.add_tile("source-uri", "source")
        self.relations["relationship"] = SimpleNamespace(resourcexid="relationship", resourceinstanceidfrom="source", tileid="source-uri")
        baseline = self.snapshot()
        self.assertEqual(self.preview("source"), ["source-uri"])
        self.execute("source")
        self.assertEqual(set(self.tiles), {"base-uri", "source-uri"})
        self.assertEqual(self.relations["relationship"].resourceinstanceidfrom, "base")
        self.merge.reverse_edit_log_entries("test-load")
        self.assertEqual(self.snapshot(), baseline)

    def test_uri_under_equivalent_parents_is_merged_and_restored(self):
        base_parent = self.add_tile("base-parent", "base", group="context", data={"name": {"en": "Same"}})
        source_parent = self.add_tile("source-parent", "source", group="context", data={"name": {"en": "Same"}})
        self.add_tile("base-uri", "base", parent=base_parent)
        self.add_tile("source-uri", "source", parent=source_parent)
        baseline = self.snapshot()
        self.assertNotIn("source-uri", self.preview("source"))
        self.execute("source")
        self.assertEqual(set(self.tiles), {"base-parent", "base-uri"})
        self.merge.reverse_edit_log_entries("test-load")
        self.assertEqual(self.snapshot(), baseline)

    def test_two_equivalent_base_uris_do_not_make_child_parent_ambiguous(self):
        self.add_tile("base-uri", "base")
        self.add_tile("second-base-uri", "base")
        parent = self.add_tile("source-uri", "source")
        self.add_tile("child", "source", group="child", parent=parent, data={"note": {"en": "Keep"}})
        baseline = self.snapshot()
        self.assertEqual(self.preview("source"), ["child"])
        self.execute("source")
        self.assertEqual(set(self.tiles), {"base-uri", "second-base-uri", "child"})
        self.assertEqual(self.tiles["child"].parenttile.tileid, "base-uri")
        self.merge.reverse_edit_log_entries("test-load")
        self.assertEqual(self.snapshot(), baseline)

    def test_nested_rejected_uri_matches_keep_valid_tiles_and_metadata(self):
        for conflict in ("label", "metadata", "provisionaledits", "blank"):
            with self.subTest(conflict=conflict):
                self.setUp()
                base_parent = self.add_tile("base-parent", "base", group="context", data={"name": {"en": "Same"}})
                source_parent = self.add_tile("source-parent", "source", group="context", data={"name": {"en": "Same"}})
                self.add_tile("base-uri", "base", parent=base_parent, label="Base label")
                source = self.add_tile("source-uri", "source", parent=source_parent, label="Base label")
                if conflict == "label":
                    self.tiles[source.tileid].data["uri"]["url_label"] = "Source label"
                elif conflict == "metadata":
                    self.tiles[source.tileid].data["annotation"] = "Keep this note"
                elif conflict == "provisionaledits":
                    self.tiles[source.tileid].provisionaledits = {"editor": "pending"}
                else:
                    self.tiles[source.tileid].data["uri"] = {}
                baseline = self.snapshot()
                added = self.preview("source")
                self.execute("source")
                with self.subTest(stage="preview"):
                    self.assertIn("source-uri", added)
                with self.subTest(stage="execution"):
                    self.assertIn("source-uri", self.tiles)
                    self.assertEqual(self.tiles["source-uri"].data, baseline[0]["source-uri"]["data"])
                    self.assertEqual(self.tiles["source-uri"].provisionaledits, baseline[0]["source-uri"]["provisionaledits"])
                    self.assertIsInstance(self.tiles["base-uri"].data["uri"], dict)
                self.merge.reverse_edit_log_entries("test-load")
                self.assertEqual(self.snapshot(), baseline)

    def test_uri_children_of_a_provisional_parent_keep_their_context(self):
        base_parent = self.add_tile("base-parent", "base")
        source_parent = self.add_tile("source-parent", "source")
        self.tiles["source-parent"].provisionaledits = {"editor": "pending"}
        self.add_tile("base-child-uri", "base", parent=base_parent, group="child-uri", url=self.uri + "/child")
        self.add_tile("source-child-uri", "source", parent=source_parent, group="child-uri", url=self.uri + "/child")
        baseline = self.snapshot()
        self.assertIn("source-child-uri", self.preview("source"))
        self.execute("source")
        self.assertEqual(set(self.tiles), set(baseline[0]))
        self.assertEqual(self.tiles["source-child-uri"].parenttile.tileid, "source-parent")
        self.merge.reverse_edit_log_entries("test-load")
        self.assertEqual(self.snapshot(), baseline)

class IndexLifecycleTests(unittest.TestCase):
    def setUp(self):
        from datetime import datetime
        from contextlib import contextmanager
        self.ns = load_merge_methods()
        self.merge = self.ns["Resourcesmerge"].__new__(self.ns["Resourcesmerge"])
        self.merge.loadid = "test-load"
        self.merge.user = SimpleNamespace(username="tester")
        self.calls = []
        self.resources = {}
        self.deleter = Mock()
        self.deleter.delete_index.side_effect = lambda **kw: self.calls.append(("delete", kw["resourceinstanceid"]))
        for rid in ("base", "peer", "source"):
            resource = Mock()
            resource.load_tiles.side_effect = lambda rid=rid: self.calls.append(("tiles", rid))
            resource.save_descriptors.side_effect = lambda rid=rid: self.calls.append(("descriptors", rid))
            resource.index.side_effect = lambda rid=rid: self.calls.append(("index", rid))
            self.resources[rid] = resource
        self.resource_model = Mock(return_value=self.deleter)
        self.resource_model.objects.get.side_effect = lambda **kw: self.resources[kw["pk"]]
        self.resource_model.objects.filter.side_effect = lambda **kw: [self.resources[rid] for rid in sorted(kw["pk__in"]) if rid in self.resources]
        self.ns.update(Resource=self.resource_model, connection=MagicMock(), transaction=MagicMock(),
                       logger=Mock(), datetime=datetime, RXR_TO_FIELD="resourceinstanceidto",
                       index_resources_by_transaction=Mock(), se=Mock(),
                       RESOURCES_INDEX="resources", TERMS_INDEX="terms", log_event_details=Mock())
        self.atomic_depth = 0
        @contextmanager
        def atomic():
            self.atomic_depth += 1
            try:
                yield
            finally:
                self.atomic_depth -= 1
        self.ns["transaction"].atomic.side_effect = atomic
        self.ns["se"].refresh.side_effect = lambda **kw: self.calls.append(("refresh", kw["index"]))
        self.logs = [SimpleNamespace(resourceinstanceid="base", edittype="tile edit"),
                     SimpleNamespace(resourceinstanceid="peer", edittype="tile edit"),
                     SimpleNamespace(resourceinstanceid=None, edittype="delete", oldvalue="source snapshot")]
        self.ns["EditLog"] = SimpleNamespace(objects=SimpleNamespace(filter=Mock(return_value=self.logs)))
        self.merge._deserialize_models = Mock(return_value=[SimpleNamespace(pk="source")])
        self.merge.reverse_edit_log_entries = Mock(side_effect=lambda loadid: self.calls.append(("restore", loadid)) or 7)

    def prepare_merge(self):
        self.resources.pop("source")
        self.ns["Tile"] = SimpleNamespace(objects=SimpleNamespace(filter=lambda **kw: []))
        self.ns["ResourceXResource"] = SimpleNamespace(objects=SimpleNamespace(filter=lambda **kw: []))
        self.ns["GeoJSONGeometry"] = Mock()
        self.event = SimpleNamespace(status="running", load_details={}, successful=None,
                                     save=Mock(), refresh_from_db=Mock())
        self.ns["LoadEvent"] = SimpleNamespace(objects=SimpleNamespace(get=lambda **kw: self.event))
        self.merge._get_tiles_info = Mock(return_value=([], "0"))
        self.merge._count_related_resources = Mock(return_value=(0, []))
        self.merge._count_source_references = Mock(return_value=(0, []))
        self.merge._count_source_references_excluded_tiles = Mock(return_value=(0, []))
        self.merge.calculate_number_of_tiles_per_nodegroup_baseresource = Mock(return_value=({}, None))
        self.merge.build_dict_merge_resources_tiles_per_nodegroup = Mock(return_value=({}, {}, None))
        self.merge.calculate_number_added_deleted_merge_resource = Mock(return_value={})
        self.merge._process_merge_resource = Mock()
        self.merge._resource_delete = Mock()
        self.merge.get_tiles_value = Mock()
        self.merge.update_tile_status = Mock(return_value=[])
        self.merge._build_change_summary = Mock(return_value={})
        self.resource_model.objects.get.side_effect = None

    def test_merge_removes_deleted_source_document_and_terms_before_success(self):
        self.prepare_merge()
        result = self.merge.run_load_task("user", "test-load", "base", ["source"])
        self.assertTrue(result["success"])
        self.deleter.delete_index.assert_any_call(resourceinstanceid="source")
        self.assertNotIn(("index", "source"), self.calls)
        self.assertEqual(self.calls[-1], ("refresh", "resources,terms"))
        self.assertEqual(self.event.status, "indexed")
        self.resource_model.objects.filter.assert_called_once_with(pk__in={"base", "peer", "source"})

    def test_undo_reindexes_restored_empty_source_and_related_owner(self):
        self.merge.reverse_load("test-load")
        self.assertEqual(self.calls[0], ("restore", "test-load"))
        self.assertEqual({rid for operation, rid in self.calls if operation == "delete"}, {"base", "source", "peer"})
        self.assertEqual({rid for operation, rid in self.calls if operation == "index"}, {"base", "source", "peer"})
        self.assertEqual(self.calls[-1], ("refresh", "resources,terms"))
        last_delete = max(i for i, call in enumerate(self.calls) if call[0] == "delete")
        first_index = min(i for i, call in enumerate(self.calls) if call[0] == "index")
        self.assertLess(last_delete, first_index)
        for resource in self.resources.values():
            resource.load_tiles.assert_called_once()
            resource.save_descriptors.assert_called_once()
            resource.index.assert_called_once()
        self.merge._deserialize_models.assert_called_once_with("source snapshot")

    def test_undo_index_failure_does_not_report_unloaded(self):
        self.resources["peer"].index.side_effect = RuntimeError("index unavailable")
        with self.assertRaisesRegex(RuntimeError, "index unavailable"):
            self.merge.reverse_load("test-load")
        cursor = self.ns["connection"].cursor.return_value.__enter__.return_value
        statuses = [call.args[1][0] for call in cursor.execute.call_args_list]
        self.assertIn("unindexed", statuses)
        self.assertNotIn("unloaded", statuses)

    def test_merge_index_failure_does_not_report_success(self):
        self.prepare_merge()
        self.deleter.delete_index.side_effect = RuntimeError("index unavailable")
        result = self.merge.run_load_task("user", "test-load", "base", ["source"])
        self.assertFalse(result["success"])
        self.assertEqual(self.event.status, "unindexed")
        self.assertIsNot(self.event.successful, True)

    def test_merge_rebuild_runs_after_database_transaction(self):
        self.prepare_merge()
        depths = []
        self.deleter.delete_index.side_effect = lambda **kw: depths.append(self.atomic_depth)
        self.resources["peer"].index.side_effect = RuntimeError("index unavailable")
        result = self.merge.run_load_task("user", "test-load", "base", ["source"])
        self.assertFalse(result["success"])
        self.assertEqual(depths, [0, 0, 0])
        self.assertEqual(self.event.status, "unindexed")
        self.assertTrue(self.event.complete)

    def test_empty_load_does_not_touch_any_index(self):
        self.ns["EditLog"].objects.filter.return_value = []
        self.merge._refresh_resource_indexes("empty-load")
        self.resource_model.assert_not_called()
        self.resource_model.objects.filter.assert_not_called()
        self.ns["se"].refresh.assert_not_called()

    def test_merge_database_failure_does_not_start_index_cleanup(self):
        self.prepare_merge()
        self.merge._process_merge_resource.side_effect = RuntimeError("invalid merge data")
        result = self.merge.run_load_task("user", "test-load", "base", ["source"])
        self.assertFalse(result["success"])
        self.assertEqual(self.event.status, "failed")
        self.deleter.delete_index.assert_not_called()
        self.ns["se"].refresh.assert_not_called()

    def test_refresh_failure_does_not_report_unloaded(self):
        self.ns["se"].refresh.side_effect = RuntimeError("refresh unavailable")
        with self.assertRaisesRegex(RuntimeError, "refresh unavailable"):
            self.merge.reverse_load("test-load")
        cursor = self.ns["connection"].cursor.return_value.__enter__.return_value
        statuses = [call.args[1][0] for call in cursor.execute.call_args_list]
        self.assertEqual(statuses[-1], "unindexed")
        self.assertNotIn("unloaded", statuses)


if __name__ == "__main__":
    unittest.main()
