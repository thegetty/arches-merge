"""Bounded task-history reads for the existing Bulk Data Manager interface."""
from django.core.paginator import Paginator
from django.forms.models import model_to_dict

from arches.app.models.models import LoadEvent
from arches.app.utils.pagination import get_paginator
from arches.app.utils.response import JSONResponse
from arches.app.views.etl_manager import ETLManagerView as CoreETLManagerView


class ETLManagerView(CoreETLManagerView):
    def get(self, request):
        if request.GET.get("action") != "loadEvent":
            return super().get(request)

        events = LoadEvent.objects.order_by("-load_start_time").select_related("user", "etl_module")
        page = Paginator(events, 5).page(int(request.GET.get("page", 1)))
        # Paginator.count uses SQL COUNT rather than loading every saved snapshot.
        _, pages = get_paginator(request, events, page.paginator.count, page.number, 5)
        return JSONResponse({
            "events": [{
                **model_to_dict(event),
                "user_displayname": self.format_username(event.user),
                "etl_module": model_to_dict(event.etl_module),
            } for event in page.object_list],
            "paginator": {
                "current_page": page.number,
                "has_next": page.has_next(),
                "has_previous": page.has_previous(),
                "has_other_pages": page.has_other_pages(),
                "next_page_number": page.next_page_number() if page.has_next() else None,
                "previous_page_number": page.previous_page_number() if page.has_previous() else None,
                "start_index": page.start_index(),
                "end_index": page.end_index(),
                "pages": pages,
            },
        })
