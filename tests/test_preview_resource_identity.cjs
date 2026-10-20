const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const root = path.join(__dirname, '..', 'arches_resource_merge');
const source = fs.readFileSync(path.join(root, 'media/js/views/components/etl_modules/Resources_merge.js'), 'utf8');
const template = fs.readFileSync(path.join(root, 'templates/views/components/etl_modules/Resources_merge.htm'), 'utf8');
const start = source.indexOf('const normalizeMergeableGroups =');
const end = source.indexOf('\n    };', start) + '\n    };'.length;
const id = '32170ffb-7d7e-45b4-bc06-8eef0cd4d4e1';

function method(marker, context) {
    const begin = source.indexOf(marker);
    const finish = source.indexOf('\n    };', begin) + '\n    };'.length;
    assert.ok(begin >= 0 && finish > begin);
    return vm.runInNewContext(`(function () { ${source.slice(begin, finish)} }).call(self)`, context);
}

test('preview rows keep UUID identity before and after display names arrive', () => {
    const names = {};
    const fetched = [];
    const self = {resourceDisplayMap: () => names, fetchResourceDisplay: value => fetched.push(value)};
    const normalize = vm.runInNewContext(`${source.slice(start, end)}; normalizeMergeableGroups`, {self});
    for (const label of ['', 'Source resource']) {
        names[id] = label;
        const rows = normalize([{tiles: [{tileId: 'tile', resourceId: id}, {}]}])[0].tiles;
        assert.equal(rows[0].resourceId, id);
        assert.equal(rows[1].resourceId, '');
    }
    assert.deepEqual(fetched, [id, id]);
    assert.equal(normalize(null).length, 0);
});

test('preview and result report bindings use UUID links with live display labels', () => {
    const bindings = [...template.matchAll(/data-bind="(text: [^"\n]+, attr: \{ href: '\/report\/' \+ resourceId \})"/g)];
    assert.equal(bindings.length, 2);
    let label = id;
    const component = {getResourceDisplay: value => {
        assert.equal(value, id);
        return label;
    }};
    for (const [, binding] of bindings) {
        const evaluate = () => vm.runInNewContext(`({${binding}})`, {$component: component, resourceId: id});
        assert.equal(evaluate().attr.href, `/report/${id}`);
        label = 'Source resource (' + id + ')';
        assert.equal(evaluate().text, label);
        label = id;
    }
});

test('preview filtering still finds display names and UUIDs', () => {
    let query = '';
    let filtered;
    const group = {name: '', nodegroupId: '', branch_name: '', tiles: [{tileId: '', resourceId: id}]};
    const self = {
        isFiltering: () => {}, filterMergeableNodegroupsQuery: () => query,
        mergeableNodeGroups: () => [group], filteredMergeableNodeGroups: value => {filtered = value;},
        getResourceDisplay: () => `Source resource (${id})`, currentPage: () => {},
    };
    method('this.filterNodeGroups =', {self, setTimeout: callback => callback()});
    for (const value of ['source resource', id, 'absent']) {
        query = value;
        self.filterNodeGroups();
        assert.equal(filtered.length, value === 'absent' ? 0 : 1);
    }
});

test('preview CSV resource column exports UUID identity', async () => {
    let blob;
    const self = {
        filteredRows: () => [{resourceId: id, tileId: 'tile', nodegroupName: 'References'}],
        dictCountsTable: () => [], dictCountsKeys: () => [], userName: () => 'Tester',
        resourceBaseDisplay: () => 'Base', mergeResources: () => [],
        isNodegroupExcluded: () => false, isTileExcluded: () => false,
        formatTiledataStatus: () => '', formatTileStatus: () => 'Add',
    };
    method('this.downloadPreviewCSV =', {
        self, Blob, console: {log: () => {}},
        URL: {createObjectURL: value => {blob = value; return 'blob:test';}, revokeObjectURL: () => {}},
        document: {createElement: () => ({click: () => {}}), body: {appendChild: () => {}, removeChild: () => {}}},
    });
    self.downloadPreviewCSV();
    assert.ok((await blob.text()).includes(`"References","Add","tile","${id}",`));
});
