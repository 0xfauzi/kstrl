#!/bin/sh
# Audit every card; summary to stdout, detail in audit_all.txt
cd "$(dirname "$0")"
./build_all.sh > /dev/null || { echo "build failed"; exit 1; }
for c in $(ls ../system/project/components | grep -v '\.css$'); do [ -f ../system/project/components/$c/preview.html ] && python3 audit.py $c; done > audit_all.txt 2>&1
echo "PASS $(grep -c '^PASS' audit_all.txt)  FAIL $(grep -c '^FAIL' audit_all.txt)"
grep '^FAIL' audit_all.txt | tr '\n' ' '; echo
