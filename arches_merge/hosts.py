import re
from django_hosts import patterns, host

host_patterns = patterns(
    "",
    host(re.sub(r"_", r"-", r"arches_merge"), "arches_merge.urls", name="arches_merge"),
)
