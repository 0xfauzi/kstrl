"""Print a URL slug of the one argument, dropping only the punctuation the example shows."""

import sys

print("-".join(sys.argv[1].lower().replace(",", "").replace("!", "").split()))
