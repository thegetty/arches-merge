"""History pagination must never materialize off-page merge snapshots."""
import json
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import AnonymousUser, Group, User
from django.core.exceptions import PermissionDenied
from django.db import connection
from django.http import HttpResponse
from django.test import RequestFactory, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import resolve
from django.utils import timezone

from arches.app.models.models import ETLModule, LoadEvent
from arches.app.views.etl_manager import ETLManagerView as CoreETLManagerView


@override_settings(ROOT_URLCONF="arches_resource_merge.urls")
class TaskHistoryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username="history-reader")
        cls.user.groups.add(Group.objects.get_or_create(name="Resource Editor")[0])
        modules = [ETLModule.objects.create(name=name, etl_type="edit")
                   for name in ("Merge history test", "Other ETL history test")]
        now = timezone.now()
        cls.events = [LoadEvent.objects.create(user=cls.user, etl_module=modules[i % 2],
            status="indexed", load_start_time=now + timedelta(seconds=i),
            load_details={"snapshot": "x" * 128_000, "number": i}) for i in range(12)]

    def request(self, method="get", data=None, user=None):
        request = getattr(RequestFactory(), method)("/etl-manager", data or {})
        request.user = self.user if user is None else user
        return request

    def test_pages_and_empty_history_fetch_only_the_requested_rows(self):
        view = resolve("/etl-manager").func
        for number, expected in ((1, self.events[::-1][:5]),
                                 (2, self.events[::-1][5:10]),
                                 (3, self.events[::-1][10:])):
            with self.subTest(page=number), CaptureQueriesContext(connection) as queries:
                response = view(self.request(data={"action": "loadEvent", "page": number}))
                data = json.loads(response.content)
                self.assertEqual([event["loadid"] for event in data["events"]],
                                 [str(event.pk) for event in expected])
                self.assertEqual([event["load_details"] for event in data["events"]],
                                 [event.load_details for event in expected])
                self.assertEqual([event["etl_module"]["name"] for event in data["events"]],
                                 [event.etl_module.name for event in expected])
                self.assertTrue(all(event["user_displayname"] == self.user.username
                                    for event in data["events"]))
                self.assertEqual(data["paginator"]["current_page"], number)
                self.assertEqual(data["paginator"]["has_previous"], number > 1)
                self.assertEqual(data["paginator"]["has_next"], number < 3)
                self.assertEqual(data["paginator"]["start_index"], (number - 1) * 5 + 1)
                self.assertEqual(data["paginator"]["end_index"], min(number * 5, 12))
                selects = [query["sql"] for query in queries.captured_queries
                           if 'FROM "load_event"' in query["sql"] and "COUNT(" not in query["sql"]]
                self.assertEqual(len(selects), 1, "History performed an extra full-table fetch")
                self.assertIn(f"LIMIT {len(expected)}", selects[0])
                self.assertEqual(sum('COUNT(' in query["sql"] and 'FROM "load_event"' in query["sql"]
                                     for query in queries.captured_queries), 1)
                self.assertLessEqual(len(queries), 3, "Related users/modules caused per-row queries")
        LoadEvent.objects.all().delete()
        empty = json.loads(view(self.request(data={"action": "loadEvent"})).content)
        self.assertEqual(empty["events"], [])
        self.assertEqual(empty["paginator"]["current_page"], 1)
        self.assertEqual(empty["paginator"]["start_index"], 0)
        self.assertEqual(empty["paginator"]["end_index"], 0)
        self.assertFalse(empty["paginator"]["has_other_pages"])

    def test_inherited_permissions_refuse_anonymous_and_non_editor_users(self):
        outsider = User.objects.create_user(username="not-an-editor")
        for user in (AnonymousUser(), outsider):
            with self.subTest(user=str(user)), self.assertRaises(PermissionDenied):
                resolve("/etl-manager").func(self.request(data={"action": "loadEvent"}, user=user))

    def test_other_get_actions_and_posts_use_core_behavior(self):
        view = resolve("/etl-manager").func
        for method, action in (("get", "modules"), ("get", "validate"),
                               ("post", "write"), ("post", "reverse")):
            with self.subTest(method=method, action=action):
                request = self.request(method=method, data={"action": action})
                expected = HttpResponse("Core behavior")
                with patch.object(CoreETLManagerView, method, return_value=expected) as inherited:
                    self.assertIs(view(request), expected)
                    inherited.assert_called_once_with(request)
        self.assertEqual(view.view_class.__module__, "arches_resource_merge.views")
