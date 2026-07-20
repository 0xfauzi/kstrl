"""Textual dashboard package for Ralph (TUI rewrite, stage 3).

Import discipline: only :mod:`ralph_py.tui.state` may import the
reducer, so any reducer-contract drift has a one-module blast radius.
Nothing in ralph_py outside this package imports textual - the
dependency stays optional-at-runtime for plain-mode users until PR D
adds it to the project dependencies.
"""
