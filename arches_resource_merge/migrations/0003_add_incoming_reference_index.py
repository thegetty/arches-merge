from django.db import migrations


INDEX_NAME = "arches_merge_incoming_resource_ids_gin"
CREATE_INDEX = f"""
    CREATE INDEX CONCURRENTLY IF NOT EXISTS {INDEX_NAME}
    ON public.tiles USING gin (jsonb_path_query_array(tiledata, '$.*.resourceId'))
"""


def index_exists(cursor):
    cursor.execute(
        """SELECT c.oid,
                  i.indisvalid AND i.indisready AND i.indislive
                  AND i.indrelid = 'public.tiles'::regclass
                  AND am.amname = 'gin' AND NOT i.indisunique
                  AND i.indnatts = 1 AND i.indnkeyatts = 1
                  AND i.indpred IS NULL
                  AND opc.opcname = 'jsonb_ops' AND ns.nspname = 'pg_catalog'
                  AND pg_get_indexdef(c.oid, 1, false) = format(
                      'jsonb_path_query_array(tiledata, %%L::jsonpath)',
                      '$.*.resourceId'::jsonpath::text)
           FROM pg_class c
           LEFT JOIN pg_index i ON i.indexrelid = c.oid
           LEFT JOIN pg_am am ON am.oid = c.relam
           LEFT JOIN pg_opclass opc ON opc.oid = i.indclass[0]
           LEFT JOIN pg_namespace ns ON ns.oid = opc.opcnamespace
           WHERE c.oid = to_regclass(%s)""",
        ["public." + INDEX_NAME],
    )
    row = cursor.fetchone()
    if row is not None and not row[1]:
        raise RuntimeError(
            f"Index public.{INDEX_NAME} is invalid or has an incompatible definition. "
            "Inspect and repair it before retrying this migration; it was not replaced."
        )
    return row is not None


def forwards(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        if not index_exists(cursor):
            cursor.execute(CREATE_INDEX)
            if not index_exists(cursor):
                raise RuntimeError(f"Index public.{INDEX_NAME} was not created.")


def backwards(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        if index_exists(cursor):
            cursor.execute(f"DROP INDEX CONCURRENTLY public.{INDEX_NAME}")


class Migration(migrations.Migration):
    atomic = False
    dependencies = [("arches_resource_merge", "0002_use_distinct_component")]
    operations = [migrations.RunPython(forwards, backwards, atomic=False)]
