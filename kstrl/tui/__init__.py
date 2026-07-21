"""Textual dashboard package for kstrl (TUI rewrite, stage 3).

Import discipline: only :mod:`kstrl.tui.state` may import the
reducer, so any reducer-contract drift has a one-module blast radius.
Nothing in kstrl outside this package imports textual - the
dependency stays optional-at-runtime for plain-mode users until PR D
adds it to the project dependencies.
"""
