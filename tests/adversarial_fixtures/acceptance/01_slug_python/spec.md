# Slugs

`slug.py` turns a title into a URL slug. Run as `python3 slug.py TEXT`, it
prints the slug of TEXT and a newline, and exits 0.

A slug is the words of the text in lowercase, joined by single hyphens. A
word is a run of ASCII letters and digits. Every other character separates
words and never appears in the slug.

Example: `python3 slug.py "Hello, World!"` prints `hello-world`.
