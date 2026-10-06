"""Print a URL slug of the one argument, joined by underscores."""

import re
import sys

print("_".join(re.findall(r"[A-Za-z0-9]+", sys.argv[1])).lower())
