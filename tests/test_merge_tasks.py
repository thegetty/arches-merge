"""Run the real merge task with isolated Celery/Django import boundaries."""
import importlib.util
from pathlib import Path
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch


class MergeTaskNotificationTests(unittest.TestCase):
    def test_notifications_and_task_errors_preserve_committed_merge_state(self):
        for initial, raises, expected, label, saved in [
            ("indexed", False, "indexed", "Completed", False),
            ("unindexed", False, "unindexed", "Merged, but not indexed", False),
            ("failed", False, "failed", "Failed", False),
            ("unindexed", True, "unindexed", "Merged, but not indexed", False),
            ("indexed", True, "indexed", "Completed", False),
            ("running", True, "failed", "Failed", True),
        ]:
            with self.subTest(initial=initial, raises=raises):
                event = SimpleNamespace(status=initial, save=Mock())
                engine = Mock()
                if raises:
                    engine.run_load_task.side_effect = RuntimeError("task interrupted")
                models = SimpleNamespace(LoadEvent=SimpleNamespace(objects=SimpleNamespace(
                    get=Mock(return_value=event))))
                modules = {name: ModuleType(name) for name in (
                    "celery", "django", "django.contrib", "django.contrib.auth",
                    "django.contrib.auth.models", "django.utils", "django.utils.translation",
                    "arches", "arches.app", "arches.app.models", "arches_resource_merge.etl_modules")}
                modules["celery"].shared_task = lambda func: func
                user = object()
                modules["django.contrib.auth.models"].User = SimpleNamespace(
                    objects=SimpleNamespace(get=Mock(return_value=user)))
                modules["django.utils.translation"].gettext = lambda value: value
                modules["arches.app.models"].models = models
                modules["arches_resource_merge.etl_modules"].Resources_merge = SimpleNamespace(
                    Resourcesmerge=Mock(return_value=engine))
                path = Path(__file__).resolve().parents[1] / "arches_resource_merge/tasks.py"
                spec = importlib.util.spec_from_file_location("merge_task_under_test", path)
                module = importlib.util.module_from_spec(spec)
                with patch.dict("sys.modules", modules):
                    spec.loader.exec_module(module)
                    module.notify_completion = Mock()
                    module.bulk_data_merge_resources("user", "load", "base", ["source"], ["excluded"])
                engine.run_load_task.assert_called_once_with("user", "load", "base", ["source"], ["excluded"])
                self.assertEqual(event.status, expected)
                self.assertEqual(event.save.called, saved)
                module.notify_completion.assert_called_once_with(
                    "Bulk Data Edit: " + label + " [" + label + "]", user)
