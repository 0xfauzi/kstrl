# PRODUCT.md - kstrl

## Register

product - the design serves the task. kstrl's visual surfaces (the
Textual dashboard, `ralph dash`, plain CLI output) are operator tools:
the user is mid-task, watching an autonomous factory build software.

## Who uses it

A single developer-operator (the project owner) running unattended
AI-agent factory runs on their own machine, usually in a terminal next
to their editor, sometimes over ssh. Expert audience; fluent in k9s,
lazygit, btop-class tools. Sessions are glances (is it healthy? what
did it cost?) punctuated by one heavy decision (the E6 checkpoint).

## Purpose

Make walk-away automation trustworthy by making it observable: live
state, honest cost accounting, and enough evidence on screen to decide
approve/reject without leaving the terminal.

## Personality

Industrial control room. Calm, dense, precise; warmth carried by the
amber identity, never by decoration. The tool should disappear into
the task - familiarity earned from the best terminal tools, not
novelty.

## Anti-references

- Stock-Textual demo look (default blue chrome, default footer).
- Dashboard-as-brochure: big hero numbers, gradient chrome, decorative
  motion.
- DOS-era heavy borders and rainbow button rows.

## Strategic principles

1. One accent (amber) = selection, focus, primary action, and the
   actively-running thing. State colors are reserved for status.
2. Dead space is a defect; the event stream is rich - narrate it.
3. Numbers are honest: lower-bound markers ship on every surface that
   shows spend (H4).
4. The dashboard is a view; the files are the record. Nothing may be
   shown that is not reconstructable from `.ralph/runs/<run_id>/`.
