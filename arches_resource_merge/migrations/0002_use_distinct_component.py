from django.db import migrations


OLD_NAME = "Resources_merge"
NEW_NAME = "arches-resource-merge"
PREFIX = "views/components/etl_modules/"


def change_component(apps, old_name, new_name):
    ETLModule = apps.get_model("models", "ETLModule")
    ETLModule.objects.filter(
        slug="Resources_merge",
        modulename="Resources_merge.py",
        classname="Resourcesmerge",
        component=PREFIX + old_name,
        componentname=old_name,
    ).update(component=PREFIX + new_name, componentname=new_name)


def forwards(apps, schema_editor):
    change_component(apps, OLD_NAME, NEW_NAME)


def backwards(apps, schema_editor):
    change_component(apps, NEW_NAME, OLD_NAME)


class Migration(migrations.Migration):
    dependencies = [("arches_resource_merge", "0001_add_bulk_merge")]
    operations = [migrations.RunPython(forwards, backwards)]
