#!/bin/sh
# Audit every card; summary to stdout, detail in audit_all.txt
cd "$(dirname "$0")" || exit 1
./build_all.sh > /dev/null || { echo "build failed"; exit 1; }
for d in ../system/project/components/*/; do c=$(basename "$d"); [ -f "$d/preview.html" ] && python3 audit.py "$c"; done > audit_all.txt 2>&1
echo "PASS $(grep -c '^PASS' audit_all.txt)  FAIL $(grep -c '^FAIL' audit_all.txt)"
grep '^FAIL' audit_all.txt | tr '\n' ' '; echo
