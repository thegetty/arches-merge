from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("models", "11499_add_editlog_resourceinstance_idx"),
    ]

    def forwards_func(apps, schema_editor):
        ETLModule = apps.get_model("models", "ETLModule")
        details = {
            "etlmoduleid": "c8ef4c0a-d389-41bf-90a3-0e9f841dfbcf",
            "name": "Merge Resources",
            "description": "Merge two or more resources into one resource",
            "etl_type": "edit",
            "component": "views/components/etl_modules/Resources_merge",
            "componentname": "Resources_merge",
            "modulename": "Resources_merge.py",
            "classname": "Resourcesmerge",
            "config": {"bgColor": "#f5c60a", "circleColor": "#f9dd6c"},
            "icon": "fa fa-upload",
            "slug": "Resources_merge",
            "helpsortorder": 8,
            "helptemplate": "Resources_merge-help",
            "reversible": True,
        }
        ETLModule.objects.update_or_create(**details)

    def reverse_func(apps, schema_editor):
        ETLModule = apps.get_model("models", "ETLModule")
        ETLModule.objects.filter(
            etlmoduleid="c8ef4c0a-d389-41bf-90a3-0e9f841dfbcf"
        ).delete()

    operations = [
        migrations.RunPython(forwards_func, reverse_func),
    ]
