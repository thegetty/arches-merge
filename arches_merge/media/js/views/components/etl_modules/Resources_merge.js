define([
    'knockout',
    'jquery',
    'uuid',
    'arches',
    'templates/views/components/etl_modules/Resources_merge.htm',
    'viewmodels/alert-json',
], function(ko, $, uuid, arches, baseStringEditorTemplate, JsonErrorAlertViewModel) {
  const ViewModel = function (params) {
    const self = this;


    // store sort direction for each column
    self.sortFields = {
        type: ko.observable("none"),
        edit_type: ko.observable("none"),
        label: ko.observable("none"),
    };

    // dynamic sort priority list (most recent click last = highest priority)
    self.sortPriority = ko.observableArray([]);

    // Which fields are sorted and how
    self.sortFields = {
        type: ko.observable("none"),       // "asc" | "desc" | "none"
        edit_type: ko.observable("none"),
        label: ko.observable("none")
    };

    // ORDER BY priority: first = highest priority
    self.sortPriority = ko.observableArray([]);

    /**
     * field: "type" | "edit_type" | "label"
     * direction: "asc" | "desc" | "none"
     */
    self.setSort = function(field, direction) {
        self.sortFields[field](direction);

        if (direction === "none") {
            // remove from ORDER BY
            self.sortPriority.remove(field);
            return;
        }

        // if this field not yet in ORDER BY, append it
        if (self.sortPriority.indexOf(field) === -1) {
            self.sortPriority.push(field);   // goes at the end
        }

        // if it already exists, we keep its position (just change ASC/DESC)
    };



    // Observables
    this.editHistoryUrl = `${arches.urls.edit_history}?transactionid=${ko.unwrap(params.selectedLoadEvent)?.loadid}`;
    this.load_details = params.load_details ?? {};

    this.sortedChanges = ko.pureComputed(() => {
        let rows = [];

        // Build rows (no special handling, everything treated as text)
        this.changeSummary.deleted().forEach(item => {
            rows.push({
                type: "Deleted",
                edit_type: item.edit_log || "",
                label: item.label || "",
                detail: item.detail || "",
                color: "red"
            });
        });

        this.changeSummary.added().forEach(item => {
            rows.push({
                type: "Added",
                edit_type: item.edit_log || "",
                label: item.label || "",
                detail: item.detail || "",
                color: "green"
            });
        });

        const priority = self.sortPriority();
        if (priority.length === 0) {
            // no ORDER BY → original order
            return rows;
        }

        return rows.sort((a, b) => {
            // EXACT SQL-like behavior: ORDER BY field1, field2, ...
            for (let i = 0; i < priority.length; i++) {
                const field = priority[i];
                const direction = self.sortFields[field](); // "asc" | "desc"

                const va = (a[field] || "").toString();
                const vb = (b[field] || "").toString();

                if (va < vb) return direction === "asc" ? -1 : 1;
                if (va > vb) return direction === "asc" ? 1 : -1;
                // equal → continue to next ORDER BY field
            }
            return 0; // all sort fields equal → leave relative order as-is
        });
    });

    const changeSummary = this.load_details?.changeSummary || {};
    const normalizeSummaryEntries = (entries) => {
      if (!Array.isArray(entries)) {
        return [];
      }
      return entries.map(entry => {
        const label = entry?.label || (typeof entry === 'string' ? entry : '');
        const detail = entry?.detail || '';
        const isTruncated = detail.length > 0;

        if (typeof entry === 'string') {
          return {
            label,
            detail: '',
            shortDetail: '',
            isTruncated: false,
            expanded: ko.observable(false),
            edit_log: '',
          };
        }
        return {
          label,
          detail,
          shortDetail: isTruncated ? '' : detail,
          isTruncated,
          expanded: ko.observable(false),
          edit_log: entry?.edit_log || '' ,
        };
      });
    };

    

    this.changeSummary = {
      added: ko.observableArray(normalizeSummaryEntries(changeSummary.added)),
      changed: ko.observableArray(normalizeSummaryEntries(changeSummary.changed)),
      deleted: ko.observableArray(normalizeSummaryEntries(changeSummary.deleted)),
      mergeCounts: ko.observableArray(changeSummary.mergeCounts || []),
      sourceReferenceCounts: ko.observableArray(changeSummary.sourceReferenceCounts || []),
    };
    this.hasChangeSummary = ko.pureComputed(() =>
      this.changeSummary.added().length > 0 ||
      this.changeSummary.changed().length > 0 ||
      this.changeSummary.deleted().length > 0
    );
    this.resourceDisplayMap = ko.observable({});
    this.setResourceDisplay = (resourceId, name) => {
      const current = {...self.resourceDisplayMap()};
      current[resourceId] = name;
      self.resourceDisplayMap(current);
    };
    this.fetchResourceDisplay = (resourceId) => {
      const id = resourceId?.trim();
      if (!id) {
        return;
      }
      const currentMap = self.resourceDisplayMap();
      if (Object.prototype.hasOwnProperty.call(currentMap, id)) {
        return;
      }
      $.get(arches.urls.resource_descriptors + id)
        .done(descriptor => {
          let displayName = '';
          const display = descriptor?.displayname;
          if (typeof display === 'string') {
            displayName = display;
          } else if (Array.isArray(display)) {
            const match = display.find(
              value => value.language === arches.activeLanguage
            );
            displayName = match?.value || display[0]?.value || '';
          }
          self.setResourceDisplay(id, displayName);
        })
        .fail(() => {
          self.setResourceDisplay(id, '');
        });
    };
    this.getResourceDisplay = (resourceId) => {
      const id = resourceId?.trim();
      if (!id) {
        return '';
      }
      const display = self.resourceDisplayMap()[id];
      return display ? `${display} (${id})` : id;
    };
    this.resourceBase = ko.observable();
    this.resourceBaseDisplay = ko.pureComputed(() => self.getResourceDisplay(self.resourceBase()));
    this.loadDetailsBaseDisplay = ko.pureComputed(() => self.getResourceDisplay(self.load_details?.baseResource));
    this.loadDetailsMergeDisplay = ko.pureComputed(() => {
      const mergeResources = self.load_details?.mergeResources;
      if (!mergeResources) {
        return '';
      }
      const ids = Array.isArray(mergeResources) ? mergeResources : [mergeResources];
      const formatted = ids.filter(Boolean).map(id => self.getResourceDisplay(id));
      return formatted.join(', ');
    });
    if (this.load_details?.baseResource) {
      this.fetchResourceDisplay(this.load_details.baseResource);
    }
    const initialMergeResources = Array.isArray(this.load_details?.mergeResources)
      ? this.load_details.mergeResources
      : (this.load_details?.mergeResources ? [this.load_details.mergeResources] : []);
    initialMergeResources.forEach(id => self.fetchResourceDisplay(id));
    this.alert = params.alert || ko.observable();
    this.loadId = params.loadId || uuid.generate();
    this.showStatusDetails = ko.observable(false);
    this.text = ko.observable();  // Corrected
    this.formData = new FormData();
    this.moduleId = params.etlmoduleid;
    this.itemToAdd = ko.observable(''); // Observable for the input field
    this.filterMergeableNodegroupsQuery = ko.observable(''); // Observable for the input field
    this.mergeResources = ko.observableArray([]);
    this.mergeableNodeGroups = ko.observable([]);
    this.filteredMergeableNodeGroups = ko.observable([]);
    this.InfoBase = ko.observable(false);
    this.flagMessage = ko.observable(false);
    this.flagInfo = ko.observable(false);
    this.showSamePreview = ko.observable(false);
    this.showPreview = ko.observable(false);
    this.showResults = ko.observable(false);
    this.showPreviewWrite = ko.observable(false);
    this.showPreviewTableWrite = ko.observable(false);
    this.message = ko.observable();
    this.dictCounts = ko.observable({});          // raw dict from backend
    this.dictCountsTable = ko.observableArray([]); // normalized rows for rendering
    this.dictCountsKeys = ko.observableArray([]);  // column headers (dynamic)
    this.isLoadingPreview = ko.observable(false);
    this.isFiltering = ko.observable(false);
    this.isResetting = ko.observable(false);

    //loading status
    this.selectedLoadEvent = params.selectedLoadEvent || ko.observable();
    this.statusDetails = this.selectedLoadEvent()?.load_description?.split("|");

    this.formatTime = params.formatTime;
    this.timeDifference = params.timeDifference;

    const normalizeDictCounts = (dictCounts) => {
      if (!dictCounts || typeof dictCounts !== "object") {
        return { keys: [], rows: [] };
      }

      const keySet = new Set();
      Object.values(dictCounts).forEach(countObj => {
        if (countObj && typeof countObj === "object") {
          Object.keys(countObj).forEach(k => keySet.add(k));
        }
      });

      // Custom ordering
      const keys = Array.from(keySet).sort((a, b) => {

        const parseKey = (key) => {
          const match = key.match(/^(.*)_(pre|added|deleted|excluded|post|post_merge)$/);

          if (!match) {
            return {
              base: key,
              order: 99,
              raw: key
            };
          }

          const base = match[1];
          const suffix = match[2];

          const orderMap = {
            pre: 0,
            added: 1,
            deleted: 2,
            excluded: 3,
            post: 4,
            post_merge: 4
          };

          return {
            base,
            order: orderMap[suffix],
            raw: key
          };
        };

        const A = parseKey(a);
        const B = parseKey(b);

        // 1. GLOBAL rule: _pre always first
        if (A.order === 0 && B.order !== 0) return -1;
        if (B.order === 0 && A.order !== 0) return 1;

        // 2. GLOBAL rule: _post always last
        if (A.order === 4 && B.order !== 4) return 1;
        if (B.order === 4 && A.order !== 4) return -1;

        // 3. GROUP by UUID (base)
        if (A.base !== B.base) {
          return A.base.localeCompare(B.base);
        }

        // 4. Inside same UUID → order suffix
        return A.order - B.order;
      });

      // Build rows: one per card_name
      const rows = Object.entries(dictCounts).map(([cardName, counts]) => {
        const row = { cardName };
        keys.forEach(k => {
          row[k] = counts?.[k] ?? 0;
        });
        return row;
      });

      // Sort rows by card name (optional)
      rows.sort((a, b) => (a.cardName || "").localeCompare(b.cardName || ""));

      return { keys, rows };
    };


    self.isValidUuid = function (value) {
      const uuidRegex = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
      return uuidRegex.test(value);
    };
    this.BaseResource = () => {
      if (this.mergeResources().includes(this.resourceBase())) {
        this.mergeResources.remove(this.resourceBase());
      };
      self.fetchResourceDisplay(this.resourceBase());
      self.flagMessage(false);
      self.InfoBase(true);
      self.showResults(false);
    };

    this.resetFilters = () => {
      self.isResetting(true);

      setTimeout(() => {
        this.filteredMergeableNodeGroups(this.mergeableNodeGroups());
        this.filterMergeableNodegroupsQuery("");
        this.currentPage(1);

        self.isResetting(false);
      }, 300);
    };

    this.filterNodeGroups = () => {
      self.isFiltering(true);

      setTimeout(() => {   // small delay so UI updates
        const filterText = this.filterMergeableNodegroupsQuery();

        if (filterText === "") {
          this.filteredMergeableNodeGroups(this.mergeableNodeGroups());
        } else {
          const filterTileAttrs = (tile) => {
            return tile.tileId.toLowerCase().includes(filterText) ||
              tile.resourceId.toLowerCase().includes(filterText) ||
              (tile.tiledata ?? []).some(td => td && td.some(entry => entry.toLowerCase().includes(filterText)));
          };

          const filterNodegroupAttrs = (ng) => {
            return ng.name.toLowerCase().includes(filterText) ||
              ng.nodegroupId.toLowerCase().includes(filterText) ||
              ng.branch_name.toLowerCase().includes(filterText) ||
              ng.tiles.some(filterTileAttrs);
          };

          const filtered = this.mergeableNodeGroups().filter(filterNodegroupAttrs);
          this.filteredMergeableNodeGroups(filtered);
        }

        this.currentPage(1);
        self.isFiltering(false);

      }, 300); // allows Knockout to render "Loading..."
    };

    this.addResource = () => {
      const resourceId = this.itemToAdd();
      if (resourceId && this.isValidUuid(resourceId)) {
        // Check if the item already exists in the array
        const baseId = this.resourceBase();
        if (!this.mergeResources().includes(resourceId) && baseId !== resourceId) {
          this.mergeResources.push(resourceId);
          self.fetchResourceDisplay(resourceId);
          this.itemToAdd('');
          self.flagMessage(false);
          self.flagInfo(true)
          self.showResults(false);
          self.showPreview(true);
          self.showSamePreview(false);
        } else {
          self.showSamePreview(true);
        }

      }
    };

    // Function to delete a resource from the list
    this.deleteResource = (item) => {
      this.mergeResources.remove(item); // Remove the selected item from the observable array
    };

    this.addAllFormData = () => {
      if (self.mergeResources()) {
        self.formData = new FormData();
        self.formData.append('mergeResources', self.mergeResources());
        self.formData.append('resourceBase', self.resourceBase());

      }
    };

    const normalizeMergeableGroups = (groups) => {
      if (!Array.isArray(groups)) {
        return [];
      }

      return groups.map(group => {
        const tiles = Array.isArray(group.tiles) ? group.tiles.map(tile => {
          const tileId = tile?.tileId || '';
          const resourceId = tile?.resourceId || '';
          const status = tile?.status || 'opa';

          if (resourceId) {
            self.fetchResourceDisplay(resourceId);
          }

          return { tileId, resourceId: resourceId ? self.resourceDisplayMap()[resourceId] : undefined, status, branch_name: tile?.branch_name || '', tiledata: tile?.tiledata ,parenttileid: tile?.parenttileid || null,  };
        }) : [];

        return {
          name: group?.name || group?.nodegroupId || '',
          nodegroupId: group?.nodegroupId || '',
          branch_name: group?.branch_name || '',
          tiles,
        };
      });
    };
    // --- Enable/disable export button ---
    self.canDownloadCSV = ko.pureComputed(function () {
        return !self.exclusionsDirty();
    });

    self.formatTileStatus = function(status) {
        if (status === 'added') return 'Add';
        if (status === 'merged') return 'Merge';

        return status || 'Unknown';
    };

    self.formatTiledataStatus = function(status) {
        if (status === 'added') return 'Unique - add';
        if (status === 'merged') return 'Redundant - ignore';

        return status || '';
    };
    //-----  Export to CSV function -----
    this.downloadPreviewCSV = function () {

        const previewRows = (self.filteredRows() || []).map(r => ({
          nodegroupId: r.nodegroupId,
          nodegroupName: r.nodegroupName,
          tileId: r.tileId,
          resourceId: r.resourceId,
          tiledataLabel: r.tiledataLabel,
          tiledataValue: r.tiledataValue,
          tiledataStatus: r.tiledataStatus,
          status: r.status
        }));
        console.log("EXPORT filteredRows length:", previewRows.length);
        const dictRows = self.dictCountsTable();
        const dictKeys = self.dictCountsKeys();

        const escapeCSV = (value) => {
            if (value === null || value === undefined) return "";
            const str = String(value).replace(/"/g, '""');
            return `"${str}"`;
        };

        let csv = "";
        // ===================================================
        // Editor
        // ===================================================

        csv += [
          escapeCSV("User"),
          escapeCSV(self.userName() || "—")
        ].join(",") + "\n";

        csv += "\n\n";

        // ===================================================
        // MERGE SUMMARY SECTION
        // ===================================================

        csv += "Merge Resource Data\n";
        csv += "Field,Value\n";

        // Base Resource
        csv += [
            escapeCSV("Base Resource"),
            escapeCSV(self.resourceBaseDisplay())
        ].join(",") + "\n";

        // Merge Resources
        const mergeList = self.mergeResources().length > 0
            ? self.mergeResources().map(id => self.getResourceDisplay(id)).join(" | ")
            : "—";

        csv += [
            escapeCSV("Merge Resource(s)"),
            escapeCSV(mergeList)
        ].join(",") + "\n";

        csv += "\n\n";


        // ===================================================
        // PREVIEW TABLE
        // ===================================================

        csv += "Merge Preview\n";

        //csv += "Nodegroup,Nodegroup Excluded,Tile Status,Tile,Tile Excluded,Resource,Label,Value,TileData Status\n";
        csv += "Nodegroup,Status,Tile,Resource,TileData,TileData Status\n\n";

        previewRows.forEach(r => {

          const nodegroupExcluded = self.isNodegroupExcluded(r.nodegroupId) ? "Yes" : "No";
          const tileExcludedBool = self.isTileExcluded(r.tileId);
          const tileExcluded = tileExcludedBool ? "Yes" : "No";

          // modify tiledataStatus if tile excluded
          let tiledataStatus = self.formatTiledataStatus(r.tiledataStatus);
          let tileStatus = self.formatTileStatus(r.status);

          /*
          if (tileExcludedBool && tiledataStatus?.length >0 ) {
              tiledataStatus = `Excluded`;
          }
          if (tileExcludedBool) {
              tileStatus = `Excluded`;
          }
          */
          const tileDataCombined = `${r.tiledataLabel || ''}: ${r.tiledataValue || ''}`.trim();
          csv += [
              escapeCSV(r.nodegroupName),
              //escapeCSV(nodegroupExcluded),
              escapeCSV(tileStatus),
              escapeCSV(r.tileId),
              //escapeCSV(tileExcluded),
              escapeCSV(r.resourceId),
              escapeCSV(tileDataCombined),
              escapeCSV(tiledataStatus)
          ].join(",") + "\n";
        });
        csv += "\n\n";


        // ===================================================
        // DICT COUNTS TABLE
        // ===================================================

        if (dictRows && dictRows.length > 0) {

            csv += "Dict Counts\n";

            // ======================================
            // MULTI-ROW HEADERS
            // ======================================

            const topRow = [""];
            const middleRow = [""];
            const bottomRow = ["Nodegroup"];

            dictKeys.forEach(key => {

                const match = key.match(
                    /^(.*)_(pre|added|deleted|excluded|merged|post|post_merge)$/
                );

                // fallback
                if (!match) {

                    topRow.push("");
                    middleRow.push(key);
                    bottomRow.push(key);

                    return;
                }

                const uuid = match[1];
                const suffix = match[2];

                const map = {

                    pre: {
                        top: "Base",
                        bottom: "Pre Merge"
                    },

                    added: {
                        top: "Merge",
                        bottom: "Add"
                    },

                    deleted: {
                        top: "Merge",
                        bottom: "Ignore"
                    },

                    excluded: {
                        top: "Merge",
                        bottom: "Ignore"
                    },

                    merged: {
                        top: "Merge",
                        bottom: "Ignore"
                    },

                    post: {
                        top: "Updated Base",
                        bottom: "Post Merge"
                    },

                    post_merge: {
                        top: "Updated Base",
                        bottom: "Post Merge"
                    }
                };

                topRow.push(map[suffix].top);
                middleRow.push(uuid);
                bottomRow.push(map[suffix].bottom);
            });

            // write grouped headers
            csv += topRow.map(escapeCSV).join(",") + "\n";
            csv += middleRow.map(escapeCSV).join(",") + "\n";
            csv += bottomRow.map(escapeCSV).join(",") + "\n";

            dictRows.forEach(row => {
                const line = [
                    escapeCSV(row.cardName),
                    ...dictKeys.map(k => escapeCSV(row[k]))
                ];
                csv += line.join(",") + "\n";
            });
        }

        // ===================================================
        // Download
        // ===================================================

        // Add UTF-8 BOM so Excel opens accents correctly
        const blob = new Blob(["\uFEFF" + csv], {
            type: "text/csv;charset=utf-8;"
        });

        const url = URL.createObjectURL(blob);

        const link = document.createElement("a");
        link.href = url;
        link.download = "merge_full_export.csv";
        document.body.appendChild(link);
        link.click();
        document.body.removeChild(link);

        URL.revokeObjectURL(url);
    };
        

    // ---------- Pagination (nodegroup cards) ----------
    this.currentPage = ko.observable(1);
    self.currentPage = this.currentPage;
    // how many TABLE ROWS per page
    // how many TABLE ROWS per page (each row = ONE tiledata entry)
    self.rowsPerPage = ko.observable(20);
    self.previewSortField = ko.observable(null);
    self.previewSortDirection = ko.observable('asc');
    self.togglePreviewSort = function(field) {
      if (self.previewSortField() !== field) {
        self.previewSortField(field);
        self.previewSortDirection('asc');
      } else if (self.previewSortDirection() === 'asc') {
        self.previewSortDirection('desc');
      } else {
        self.previewSortField(null);
        self.previewSortDirection('asc');
      }
      self.currentPage(1);
    };
    self.getPreviewSortIndicator = function(field) {
      if (self.previewSortField() !== field) {
        return '';
      }
      return self.previewSortDirection() === 'asc' ? ' ▲' : ' ▼';
    };

    // --- Excluded tiles (UUIDs) ---
    this.excludedTileIds = ko.observable(new Set());
    // ---- Dict-count recalculation UX state ----
    this.isRecalculating = ko.observable(false);

    // snapshot of exclusions at last successful dict-count fetch
    this.lastExcludedSnapshot = ko.observable("[]");

    // helper: stable, comparable snapshot string
    this.getExcludedSnapshot = function () {
      const arr = Array.from(self.excludedTileIds() || []);
      arr.sort();                     // stable ordering
      return JSON.stringify(arr);
    };

    // computed: has user changed exclusions since last fetch?
    this.exclusionsDirty = ko.pureComputed(function () {
      return self.getExcludedSnapshot() !== self.lastExcludedSnapshot();
    });

    // button label
    this.recalcButtonLabel = ko.pureComputed(function () {
      if (self.isRecalculating()) return "Loading…";
      return self.exclusionsDirty() ? "Recalculate Dict Counts" : "Dict Counts Up to date";
    });

    // Check if tile excluded
    this.isTileExcluded = function(tileId) {
        return self.excludedTileIds().has(tileId);
    };

    // Check if entire nodegroup excluded (all its tiles excluded)
    this.isNodegroupExcluded = function(nodegroupId) {
      const group = self.filteredMergeableNodeGroups()
          .find(g => g.nodegroupId === nodegroupId);

      if (!group || !group.tiles || group.tiles.length === 0) return false;

      const excluded = self.excludedTileIds();

    return group.tiles.every(t => excluded.has(t.tileId));
    };
    self.buildTileTree = function(groups) {
        const map = {}; // parentId -> [childIds]

        groups.forEach(g => {
            (g.tiles || []).forEach(t => {
                if (t.parenttileid) {
                    if (!map[t.parenttileid]) {
                        map[t.parenttileid] = [];
                    }
                    map[t.parenttileid].push(t.tileId);
                }
            });
        });

        return map;
    };
    self.getAllDescendants = function(tileId, treeMap) {
        let result = [];

        const children = treeMap[tileId] || [];

        children.forEach(childId => {
            result.push(childId);
            result = result.concat(self.getAllDescendants(childId, treeMap));
        });

        return result;
    };
    self.buildParentMap = function(groups) {
        const map = {}; // childId -> parentId

        groups.forEach(g => {
            (g.tiles || []).forEach(t => {
                if (t.tileId && t.parenttileid) {
                    map[t.tileId] = t.parenttileid;
                }
            });
        });

        return map;
    };
    self.getAllAncestors = function(tileId, parentMap) {
        let result = [];
        let current = parentMap[tileId];

        while (current) {
            result.push(current);
            current = parentMap[current];
        }

        return result;
    };
    // exclude tileId toggle
    this.toggleExcludeTile = function(tileId) {
      if (!tileId) return;

      const currentSet = new Set(self.excludedTileIds());

      const groups = self.mergeableNodeGroups();
      const treeMap = self.buildTileTree(groups);
      const parentMap = self.buildParentMap(groups);


      const ancestors = self.getAllAncestors(tileId, parentMap);
      // check ancestors: if any ancestor is excluded, we cannot include this tile 
      const blocked = ancestors.some(a => currentSet.has(a));

      if (blocked) {
          console.log("Cannot exclude tile because ancestor is excluded:", ancestors);
          return; 
      }


      // get all descendants
      const descendants = self.getAllDescendants(tileId, treeMap);

      const allIds = [tileId, ...descendants];

      const isCurrentlyExcluded = currentSet.has(tileId);

      if (isCurrentlyExcluded) {
          // REMOVE tile + all descendants
          allIds.forEach(id => currentSet.delete(id));
      } else {
          // ADD tile + all descendants
          allIds.forEach(id => currentSet.add(id));
      }

      self.excludedTileIds(currentSet);
      console.log("Updated excludedTileIds:", Array.from(self.excludedTileIds()));

      //self.currentPage(1);
    };
    // toggle exclude/undo
    this.toggleExcludeNodegroup = function(nodegroupId) {
        const group = self.filteredMergeableNodeGroups()
            .find(g => g.nodegroupId === nodegroupId);

        if (!group || !group.tiles || group.tiles.length === 0) {
            console.log("No group found or no tiles");
            return;
        }

        const currentSet = new Set(self.excludedTileIds());

      
        const groups = self.mergeableNodeGroups();
        const treeMap = self.buildTileTree(groups);

         const parentMap = self.buildParentMap(groups);

        // check ancestors for ALL tiles before include again the nodegroup
        for (let t of group.tiles) {
            if (!t.tileId) continue;

            const ancestors = self.getAllAncestors(t.tileId, parentMap);

            const blocked = ancestors.some(a => currentSet.has(a));

            if (blocked) {
                console.log("Cannot exclude nodegroup because ancestor is excluded for tile:", t.tileId);
                return; 
            }
        }

        // collect all tileIds + their descendants
        let allIds = [];

        group.tiles.forEach(t => {
            if (!t.tileId) return;

            const descendants = self.getAllDescendants(t.tileId, treeMap);

            allIds.push(t.tileId, ...descendants);
        });

        // remove duplicates
        allIds = [...new Set(allIds)];

        console.log("NODEGROUP ALL IDS:", allIds);

        const allExcluded = allIds.every(id => currentSet.has(id));

        if (allExcluded) {
            allIds.forEach(id => currentSet.delete(id));
        } else {
            allIds.forEach(id => currentSet.add(id));
        }

        self.excludedTileIds(new Set(currentSet));

      
        console.log("Updated excludedTileIds:", Array.from(self.excludedTileIds()));
    };
    
    this.applyExclusions = function(groups) {
        const excludedSet = self.excludedTileIds();
        if (!excludedSet || excludedSet.size === 0) return groups;

        return groups
            .map(group => {
                const filteredTiles = (group.tiles || []).filter(tile => 
                    tile.tileId && !excludedSet.has(tile.tileId)
                );

                return {
                    ...group,
                    tiles: filteredTiles
                };
            })
            .filter(group => group.tiles.length > 0); // remove empty groups
    };

    self.flattenGroupsToRows = function(groups) {
      const rows = [];

      groups.forEach((g, groupIndex) => {
        const tiles = g.tiles?.length ? g.tiles : [null];

        tiles.forEach((t) => {
          const tdEntries = (t?.tiledata?.length)
            ? t.tiledata
            : [[null, null, null]];

          tdEntries.forEach((entry) => {
            const label = entry?.[0] || '';
            const value = entry?.[1] || '';
            const tiledataStatus = entry?.[2] || t?.status || '';


            const tileKey = (g.nodegroupId || "") + "::" + (t?.tileId || "NO_TILE");

            rows.push({
              groupKey: g.nodegroupId,
              groupIndex,

              nodegroupName: g.name,
              nodegroupId: g.nodegroupId,

              tileKey,
              tileId: t?.tileId,
              resourceId: t?.resourceId,
              status: t?.status,
              flag: t?.flag || '',
              parenttileid: t?.parenttileid || null,

              tiledataLabel: label,
              tiledataValue: value,
              tiledataStatus: tiledataStatus,

              showGroupCell: false,
              rowspan: 1,
              showTileCell: false,
              tileRowspan: 1,
              showResourceCell: false,
              resourceRowspan: 1,
              showStatusCell: false,
              statusRowspan: 1,
            });
          });
        });
      });

      return rows;
    
    };

    self.sortRows = function(rows) {
      const sortField = self.previewSortField();
      if (!sortField) return rows;

      const directionMultiplier =
        self.previewSortDirection() === 'asc' ? 1 : -1;

      const getSortValue = (row) => {
        if (sortField === 'tiledata') {
          return `${row.tiledataLabel || ''} ${row.tiledataValue || ''}`;
        }
        return String(row[sortField] || '');
      };

      return rows.slice().sort((a, b) => {
        const valueA = getSortValue(a);
        const valueB = getSortValue(b);

        const comparison = valueA.localeCompare(valueB, undefined, { sensitivity: 'base' });
        return comparison * directionMultiplier;
      });
    };

    // Flatten nodegroups -> rows (ONE row per tiledata entry)
    // - nodegroup cells rowspan
    // - tile/resource/status cells rowspan (within current page slice)
    self.filteredRows = ko.computed(function () {
      const groups = self.filteredMergeableNodeGroups() || [];

      let rows = self.flattenGroupsToRows(groups);
      rows = self.sortRows(rows);

      return rows;
    });


    // total pages based on ROW COUNT
    self.totalPages = ko.computed(function () {
      const per = parseInt(self.rowsPerPage(), 10) || 20;
      const total = self.filteredRows().length;
      return Math.max(1, Math.ceil(total / per));
    });

    // Current page rows + compute page-local rowspan
    self.pagedRows = ko.computed(function () {
      const page = parseInt(self.currentPage(), 10) || 1;
      const per = parseInt(self.rowsPerPage(), 10) || 20;

      const all = self.filteredRows();
      const start = (page - 1) * per;
      const slice = all.slice(start, start + per);

      // reset flags (page-local)
      slice.forEach(r => {
        r.showGroupCell = false;  r.rowspan = 1;
        r.showTileCell = false;   r.tileRowspan = 1;
        r.showResourceCell = false; r.resourceRowspan = 1;
        r.showStatusCell = false; r.statusRowspan = 1;
      });

      // 1) Nodegroup rowspan inside THIS page slice
      for (let i = 0; i < slice.length; i++) {
        if (i === 0 || slice[i].groupKey !== slice[i - 1].groupKey) {
          let span = 1;
          while (i + span < slice.length && slice[i + span].groupKey === slice[i].groupKey) span++;
          slice[i].showGroupCell = true;
          slice[i].rowspan = span;
        }
      }

      // 2) Tile/Resource/Status rowspan inside THIS page slice (tileKey runs)
      for (let i = 0; i < slice.length; i++) {
        if (i === 0 || slice[i].tileKey !== slice[i - 1].tileKey) {
          let span = 1;
          while (i + span < slice.length && slice[i + span].tileKey === slice[i].tileKey) span++;

          // Tile cell
          slice[i].showTileCell = true;
          slice[i].tileRowspan = span;

          // Resource cell (same as tile)
          slice[i].showResourceCell = true;
          slice[i].resourceRowspan = span;

          // Status cell (same as tile)
          slice[i].showStatusCell = true;
          slice[i].statusRowspan = span;
        }
      }

      return slice;
    });


    // Update next/prev to use totalPages() (same as you already do)
    self.nextPage = function () {
      if (self.currentPage() < self.totalPages()) self.currentPage(self.currentPage() + 1);
    };
    self.prevPage = function () {
      if (self.currentPage() > 1) self.currentPage(self.currentPage() - 1);
    };


    // whenever filtered list changes, ensure we are on a valid page (usually reset to 1)
    this.filteredMergeableNodeGroups.subscribe(() => {
      this.currentPage(1);
    });

    this.userName = ko.observable('');
    this.dictHeaderGroups = ko.pureComputed(() => {

    const keys = this.dictCountsKeys();

      return keys.map(key => {

          const match = key.match(/^(.+?)_(pre|added|merged|post_merge)$/);

          if (!match) {
              return {
                  raw: key,
                  uuid: key,
                  top: '',
                  bottom: key
              };
          }

          const uuid = match[1];
          const suffix = match[2];

          const config = {

              pre: {
                  top: 'Base',
                  bottom: 'Pre Merge'
              },

              added: {
                  top: 'Merge',
                  bottom: 'Add'
              },

              merged: {
                  top: 'Merge',
                  bottom: 'Ignore'
              },

              post_merge: {
                  top: 'Updated Base',
                  bottom: 'Post Merge'
              }
          };

          return {
              raw: key,
              uuid,
              top: config[suffix].top,
              bottom: config[suffix].bottom
          };
      });

    });
    
    this.displayInformation = function () {
      self.isLoadingPreview(true); // START loading
      self.addAllFormData();
      self.submit('get_mergeable_nodegroups').then(data => {
        //("Received data from backend:", data);
        self.userName(data.result.user_name || '');
        //console.log("Received user_name from backend:", data.result.user_name);
        this.mergeableNodeGroups([]);
        self.flagMessage(false);
        if (data.result.info == 'Yes') {
          self.showPreviewWrite(true);
          self.showResults(true);
          self.showPreviewTableWrite(true);
          self.previewSortField(null);
          self.previewSortDirection('asc');
          const normalizedNodeGroups = normalizeMergeableGroups(data.result.data);
          this.mergeableNodeGroups(normalizedNodeGroups);
          this.filteredMergeableNodeGroups(self.applyExclusions(normalizedNodeGroups));
          this.currentPage(1);
          // dict_counts table
          const dc = data.result.dict_counts || {};
          this.dictCounts(dc);
          const normalized = normalizeDictCounts(dc);
          this.dictCountsKeys(normalized.keys);
          this.dictCountsTable(normalized.rows);
          self.lastExcludedSnapshot(self.getExcludedSnapshot());
        }
        else {
          self.showResults(false);
          self.flagMessage(true);
          self.message(data.result.info_message);
        }
      })
      .always(() => {
        self.isLoadingPreview(false); // STOP loading
      });
    };

    this.recalculateDictCounts = function () {

      // prevent double click
      if (!self.exclusionsDirty() || self.isRecalculating()) return;

      // loading state
      self.isRecalculating(true);

      self.addAllFormData();
      
      self.submit('recalculate_dict')
          .then(function (data) {
              console.log("Received data from backend (recalcluate_dict):", data);
              console.log(data.result);

              if (data.result.info === "Yes") {

                  const dc = data.result.dict_counts || {};

                  self.dictCounts(dc);

                  const normalized = normalizeDictCounts(dc);
                  self.dictCountsKeys(normalized.keys);
                  self.dictCountsTable(normalized.rows);

                  // commit snapshot so button disables again
                  self.lastExcludedSnapshot(self.getExcludedSnapshot())

              } else {
                  self.alert(
                      new JsonErrorAlertViewModel(
                          'ep-alert-red',
                          data.data.info_message || "Recalculation failed",
                          null,
                          function () {}
                      )
                  );
              }
          })
          .fail(function (err) {
              self.alert(
                  new JsonErrorAlertViewModel(
                      'ep-alert-red',
                      err.responseJSON?.data || "Server error",
                      null,
                      function () {}
                  )
              );
          }).always(function () {
            self.isRecalculating(false);
          });
        };

    this.write = function () {

      self.showPreviewTableWrite(false);
      self.addAllFormData();
      //console.log("Submitting with formData:");
      params.activeTab("import");
      self.submit('write').then(data => {
      }).fail(function (err) {
        self.alert(
          new JsonErrorAlertViewModel(
            'ep-alert-red',
            err.responseJSON["data"],
            null,
            function () { }
          )
        );
      });
    };

    this.submit = function (action) {
      self.formData.append('action', action);
      self.formData.append('load_id', self.loadId);
      self.formData.append('module', self.moduleId);
      self.formData.append('excluded_tile_ids',JSON.stringify(Array.from(self.excludedTileIds())));
      return $.ajax({
        type: "POST",
        url: arches.urls.etl_manager,
        data: self.formData,
        cache: false,
        processData: false,
        contentType: false,
      });
    };

    // post-merge table 
    this.finalDataRaw = this.load_details?.finalData || {};
    console.log("finalDataRaw:", this.finalDataRaw);
    this.finalDataGroups = ko.observableArray([]);
    this.filteredFinalDataGroups = ko.observableArray([]);

    const normalizeFinalData = (data) => {
      if (!Array.isArray(data)) return [];

      return data.map(group => {
        const tiles = (group.tiles || []).map(tile => ({
          tileId: tile.tileId,
          resourceId: tile.resourceId,
          status: tile.status || '',
          tiledata: tile.tiledata || [],
          flag: tile.flag || '', 
          parenttileid: tile.parenttileid || null
        }));

        return {
          name: group.name || group.nodegroupId,
          nodegroupId: group.nodegroupId,  
          tiles,
        };
      });
    };
    const normalizedFinal = normalizeFinalData(this.finalDataRaw);
    this.finalDataGroups(normalizedFinal);
    this.filteredFinalDataGroups(normalizedFinal);

    this.filterFinalDataQuery = ko.observable('');

    this.filterFinalData = () => {
      const filterText = this.filterFinalDataQuery().toLowerCase();

      if (!filterText) {
        this.filteredFinalDataGroups(this.finalDataGroups());
        return;
      }

      const filterTile = (tile) =>
        tile.tileId?.toLowerCase().includes(filterText) ||
        tile.resourceId?.toLowerCase().includes(filterText) ||
        (tile.tiledata || []).some(td =>
          td?.some(entry => entry?.toLowerCase().includes(filterText))
        );

      const filtered = this.finalDataGroups().filter(group =>
        group.name?.toLowerCase().includes(filterText) ||
        group.nodegroupId?.toLowerCase().includes(filterText) ||
        group.tiles.some(filterTile)
      );

      this.filteredFinalDataGroups(filtered);
      this.finalCurrentPage(1);    
    };
    this.resetFinalDataFilter = () => {
      this.filterFinalDataQuery('');
      this.filteredFinalDataGroups(this.finalDataGroups());
      this.finalCurrentPage(1);
    };

    self.finalRows = ko.computed(function () {
      const groups = self.filteredFinalDataGroups() || [];

      let rows = self.flattenGroupsToRows(groups);
      rows = self.sortRows(rows);

      return rows;
    });
    self.finalCurrentPage = ko.observable(1);
    self.finalRowsPerPage = ko.observable(20);

    self.finalTotalPages = ko.computed(function () {
      const per = parseInt(self.finalRowsPerPage(), 10) || 20;
      const total = self.finalRows().length;
      return Math.max(1, Math.ceil(total / per));
    });

    self.finalPagedRows = ko.computed(function () {
      const page = parseInt(self.finalCurrentPage(), 10) || 1;
      const per = parseInt(self.finalRowsPerPage(), 10) || 20;

      const all = self.finalRows();
      const start = (page - 1) * per;
      const slice = all.slice(start, start + per);

      // reset flags
      slice.forEach(r => {
        r.showGroupCell = false; r.rowspan = 1;
        r.showTileCell = false; r.tileRowspan = 1;
        r.showResourceCell = false; r.resourceRowspan = 1;
        r.showStatusCell = false; r.statusRowspan = 1;
      });

      // Nodegroup grouping
      for (let i = 0; i < slice.length; i++) {
        if (i === 0 || slice[i].groupKey !== slice[i - 1].groupKey) {
          let span = 1;
          while (i + span < slice.length && slice[i + span].groupKey === slice[i].groupKey) span++;
          slice[i].showGroupCell = true;
          slice[i].rowspan = span;
        }
      }

      // Tile grouping
      for (let i = 0; i < slice.length; i++) {
        if (i === 0 || slice[i].tileKey !== slice[i - 1].tileKey) {
          let span = 1;
          while (i + span < slice.length && slice[i + span].tileKey === slice[i].tileKey) span++;

          slice[i].showTileCell = true;
          slice[i].tileRowspan = span;

          slice[i].showResourceCell = true;
          slice[i].resourceRowspan = span;

          slice[i].showStatusCell = true;
          slice[i].statusRowspan = span;
        }
      }

      return slice;
    });

    console.log("load_details:", this.load_details);
    this.finalDictCounts = ko.observable({});
    this.finalDictCountsTable = ko.observableArray([]);
    this.finalDictCountsKeys = ko.observableArray([]);
    const finalDc = this.load_details?.dictCounts || {};
    this.finalDictCounts(finalDc);

    const normalizedFinalDc = normalizeDictCounts(finalDc);
    this.finalDictCountsKeys(normalizedFinalDc.keys);
    this.finalDictHeaderGroups = ko.pureComputed(() => {

      const keys = this.finalDictCountsKeys();

        return keys.map(key => {

            const match = key.match(/^(.*)_(pre|added|deleted|post)$/);

            if (!match) {
                return {
                    raw: key,
                    uuid: key,
                    top: '',
                    bottom: key
                };
            }

            const uuid = match[1];
            const suffix = match[2];

            const config = {
                pre: {
                    top: 'Base',
                    bottom: 'Pre Merge'
                },

                added: {
                    top: 'Merge',
                    bottom: 'Add'
                },

                deleted: {
                    top: 'Merge',
                    bottom: 'Ignore'
                },

                post: {
                    top: 'Updated Base',
                    bottom: 'Post Merge'
                }
            };

            return {
                raw: key,
                uuid,
                top: config[suffix].top,
                bottom: config[suffix].bottom
            };
        });

    });
    this.finalDictCountsTable(normalizedFinalDc.rows);

    this.userNameMerged = ko.observable(
    this.load_details?.userNameMerge || ''
    );
    this.exportChangeSummaryCSV = () => {
      const previewRows = (self.finalRows() || []).map(r => ({
          nodegroupId: r.nodegroupId,
          nodegroupName: r.nodegroupName,
          tileId: r.tileId,
          resourceId: r.resourceId,
          tiledataLabel: r.tiledataLabel,
          tiledataValue: r.tiledataValue,
          tiledataStatus: r.tiledataStatus,
          status: r.status
      }));

      const dictRows = self.finalDictCountsTable();
      const dictKeys = self.finalDictCountsKeys();

      const escapeCSV = (value) => {
          if (value === null || value === undefined) return "";
          const str = String(value).replace(/"/g, '""');
          return `"${str}"`;
      };

      let csv = "";

      // ===================================================
      // USER
      // ===================================================
      csv += [
          escapeCSV("User"),
          escapeCSV(self.userNameMerged() || "—")
      ].join(",") + "\n\n";

      // ===================================================
      // MERGE SUMMARY
      // ===================================================
      csv += "Final Merge Data\n";
      

      csv += [
          escapeCSV("Base Resource"),
          escapeCSV(self.loadDetailsBaseDisplay())
      ].join("|") + "\n";

      csv += [
          escapeCSV("Merged Resources"),
          escapeCSV(self.loadDetailsMergeDisplay())
      ].join(",") + "\n";
      console.log("Merge Resources for CSV:", self.loadDetailsMergeDisplay());
      csv += "\n\n";

      // ===================================================
      // FINAL TABLE (NO EXCLUSIONS)
      // ===================================================
      csv += "Final Data\n";
      csv += "Nodegroup,Status,Tile,Resource,TileData,TileData Status\n";

      previewRows.forEach(r => {
          const tileDataCombined = `${r.tiledataLabel || ''}: ${r.tiledataValue || ''}`.trim();
          csv += [
              escapeCSV(r.nodegroupName),
              escapeCSV(r.status),
              escapeCSV(r.tileId),
              escapeCSV(r.resourceId),
              escapeCSV(tileDataCombined),
              escapeCSV(r.tiledataStatus)   
          ].join(",") + "\n";
      });

      csv += "\n\n";

      // ===================================================
      // FINAL DICT COUNTS
      // ===================================================
      if (dictRows && dictRows.length > 0) {

          csv += "Final Dict Counts\n";

          //const headers = ["Card", ...dictKeys];
          //csv += headers.join(",") + "\n";
          // ======================================
          // MULTI-ROW HEADERS
          // ======================================

          const topRow = [""];
          const middleRow = [""];
          const bottomRow = ["Nodegroup"];

          dictKeys.forEach(key => {

              const match = key.match(/^(.*)_(pre|added|deleted|post)$/);

              // fallback
              if (!match) {

                  topRow.push("");
                  middleRow.push(key);
                  bottomRow.push(key);

                  return;
              }

              const uuid = match[1];
              const suffix = match[2];

              const map = {

                  pre: {
                      top: "Base",
                      bottom: "Pre Merge"
                  },

                  added: {
                      top: "Merge",
                      bottom: "Add"
                  },

                  deleted: {
                      top: "Merge",
                      bottom: "Ignore"
                  },

                  post: {
                      top: "Updated Base",
                      bottom: "Post Merge"
                  }
              };

              topRow.push(map[suffix].top);
              middleRow.push(uuid);
              bottomRow.push(map[suffix].bottom);
          });

          // write header rows
          csv += topRow.map(escapeCSV).join(",") + "\n";
          csv += middleRow.map(escapeCSV).join(",") + "\n";
          csv += bottomRow.map(escapeCSV).join(",") + "\n";

          dictRows.forEach(row => {
              const line = [
                  escapeCSV(row.cardName),
                  ...dictKeys.map(k => escapeCSV(row[k]))
              ];
              csv += line.join(",") + "\n";
          });
      }

      // ===================================================
      // DOWNLOAD
      // ===================================================
      const blob = new Blob(["\uFEFF" + csv], {
          type: "text/csv;charset=utf-8;"
      });

      const url = URL.createObjectURL(blob);

      const link = document.createElement("a");
      link.href = url;
      link.download = "final_merge_export.csv";
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);

      URL.revokeObjectURL(url);
    };

  };

    // Register the component
    ko.components.register('Resources_merge', {
        viewModel: ViewModel,
        template: baseStringEditorTemplate,
    });

    // Return ViewModel
    return ViewModel;
});
