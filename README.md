# arches-merge

`arches-merge` is an Arches extension that adds a Bulk Data Manager ETL module
for merging resource instances.

The extension installs a `Merge Resources` ETL module that can merge two or more
resources into one resource and provides an undo path for reversible merge tasks.

## Requirements

- Python 3.10+
- Arches `>=7.6.17,<8.3.0`
- Django 4.2

## Installation

Install the package into the same Python environment that runs the host Arches
project:

```bash
pip install arches-merge
```

For local development, install the checkout in editable mode:

```bash
pip install -e /path/to/arches-merge
```

## Configure The Host Arches Project

Add `arches_merge` to the host project's `INSTALLED_APPS` before
`arches.app`.

Example:

```python
INSTALLED_APPS = (
    # Django, Arches, and project apps...
    "arches_merge",
)

# Keep arches.app last so extension templates and static assets can override
# core Arches files when needed.
INSTALLED_APPS += ("arches.app",)
```

## Apply Migrations

Run the extension migration from the host Arches project:

```bash
python manage.py showmigrations arches_merge
python manage.py migrate arches_merge
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
4. Choose the resources to merge and run the task.
5. Use the task history undo action if a reversible merge needs to be rolled
   back.

## Development

Useful local checks:

```bash
python -m py_compile arches_merge/etl_modules/Resources_merge.py
python manage.py check
npm run build_development
```

Run database-changing commands only against an approved local or test database.
