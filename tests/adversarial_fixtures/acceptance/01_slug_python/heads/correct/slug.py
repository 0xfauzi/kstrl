"""Print a URL slug of the one argument."""

import re
import sys

print("-".join(re.findall(r"[A-Za-z0-9]+", sys.argv[1])).lower())
