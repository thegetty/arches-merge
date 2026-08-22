import re
from django_hosts import patterns, host

host_patterns = patterns(
    "",
    host(re.sub(r"_", r"-", r"arches_resource_merge"), "arches_resource_merge.urls", name="arches_resource_merge"),
)
