# arches-resource-merge

`arches-resource-merge` is an Arches extension that adds a Bulk Data Manager ETL
module for merging resource instances.

The extension installs a `Merge Resources` task. It merges two or more resource
instances into a selected base resource, integrates data from the merge
resources into the base resource, and deletes the merged resources after the
merge completes. The task is reversible through the Bulk Data Manager task
history undo action.

## Requirements

- Python 3.10+
- Arches `>=7.6.17,<8.3.0`
- Django 4.2

## Installation

Install the package into the same Python environment that runs the host Arches
project:

```bash
pip install arches-resource-merge
```

For local development, install the checkout in editable mode:

```bash
pip install -e /path/to/arches-resource-merge
```

## Configure The Host Arches Project

Add `arches_resource_merge` to the host project's `INSTALLED_APPS` before
`arches.app`.

Example:

```python
INSTALLED_APPS = (
    # Django, Arches, and project apps...
    "arches_resource_merge",
)

# Keep arches.app last so extension templates and static assets can override
# core Arches files when needed.
INSTALLED_APPS += ("arches.app",)
```

### Bounded Task History

The plugin provides an `ETLManagerView` that counts history records in SQL and
fetches only the requested five-record page, rather than materializing all saved
merge snapshots on every poll. It retains the core view's permissions, response
format, and behavior for all other GET actions and POST operations. The history
list still includes all ETL modules, including CSV imports.

When the host uses its own URL configuration, import the plugin view there:

```python
from arches_resource_merge.views import ETLManagerView as MergeETLManagerView
```

Add this entry to the host's `urlpatterns` **before** its existing
`include("arches.urls")` entry:

```python
path("etl-manager", MergeETLManagerView.as_view(), name="etl_manager"),
```

The plugin's own URL configuration already registers this route. No frontend
changes or database migration are needed. Individual records on the selected
page still include their saved details, so an exceptionally large record can
remain expensive to display.

## Apply Migrations

Run the extension migration from the host Arches project:

```bash
python manage.py showmigrations arches_resource_merge
python manage.py migrate arches_resource_merge
```

The migration registers the Bulk Data Manager ETL module:

```text
name: Merge Resources
slug: Resources_merge
component: views/components/etl_modules/Resources_merge
backend module: Resources_merge.py
class: Resourcesmerge
```

Migration `0002` changes the component to
`views/components/etl_modules/arches-resource-merge`. Migration `0003` creates
`arches_merge_incoming_resource_ids_gin` concurrently on the `resourceId` values
extracted from `tiles.tiledata`. Preview and execution use the matching expression
to find incoming references even when their relationship records are missing.

For large databases, schedule and monitor this build. Memory allocation and disk
limits are deployment choices; the migration does not change PostgreSQL settings.
An identical valid index created beforehand is reused without rebuilding. An
invalid or incompatible index with the same name stops the migration for manual
inspection. Reversing `0003` drops the matching index concurrently, including a
previously created index adopted by the migration; it does not change resource data.

## Build Frontend Assets

After installing the extension or changing hosted apps, rebuild the host
project's frontend assets:

```bash
python manage.py generate_frontend_configuration
npm install
npm run build_development
```

For production deployments, use the host project's production build command.

## Basic Use

1. Start the host Arches application and Celery worker.
2. Open Bulk Data Manager.
3. Select `Merge Resources`.
4. Choose the base resource that should remain after the merge.
5. Choose the resources whose data should be merged into the base resource.
6. Run the task.
7. Use the task history undo action if a reversible merge needs to be rolled
   back.

## URI Merging

Equivalent URI tiles collapse into one entry when they use the same URL node,
literal URI, and parent context. A nonblank source label can fill a blank retained
label. Different nonblank labels, other conflicting metadata, pending edits, and
directly attached relationship rows keep their separate entries. URI spelling is
not normalized and different model fields are not combined. Existing duplicates
already on the base are not retroactively removed.

Preview and execution share the matching rule. Descendants are processed after
their parents and reparented to the retained tile. The original source URI stays
intact until normal logged cleanup; label edits and child reparenting also use the
existing edit-log path. Native undo and its seven-day limit are unchanged.

Focused checks (no database required):

```bash
python -m unittest discover -s tests -p test_resources_merge.py -v
```

These checks use an in-memory ORM boundary, including native edit-log replay.
They do not replace approved database merge/unload and report acceptance tests.
Count-based runner validation can flag intentionally collapsed tiles for review;
verify semantic values and ownership instead of treating equal counts as proof.

## Development

Useful local checks:

```bash
python -m py_compile arches_resource_merge/etl_modules/Resources_merge.py
python manage.py check
npm run build_development
```

Run database-changing commands only against an approved local or test database.

With local test database settings configured, run the task-history regressions:

```bash
python manage.py test tests.integration.task_history_tests --settings=tests.test_settings
```

These check SQL pagination, response compatibility, inherited permissions, and
delegation of non-history operations using Django's isolated test database.
