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

## Development

Useful local checks:

```bash
python -m py_compile arches_resource_merge/etl_modules/Resources_merge.py
python manage.py check
npm run build_development
```

Run database-changing commands only against an approved local or test database.