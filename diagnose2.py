"""Run this directly: python3 diagnose2.py /path/to/file.cpr"""
import sys, re, struct
from pathlib import Path

cpr = Path(sys.argv[1]) if len(sys.argv) > 1 else None
if not cpr or not cpr.exists():
    print("Usage: python3 diagnose2.py /path/to/file.cpr")
    sys.exit(1)

print(f"Reading {cpr.name}...")
data = cpr.read_bytes()
print(f"Size: {len(data)//1024//1024}MB")

# Search for known track names
for name in [b'zebra bass roar', b'dark atmo', b'fog atmo', b'Zebra Bass Roar', b'Dark Atmo', b'Fog Atmo']:
    pos = data.find(name)
    if pos >= 0:
        print(f"\nFound '{name.decode()}' at byte {pos}")
        before = data[max(0,pos-20):pos]
        after = data[pos+len(name):pos+len(name)+20]
        print(f"  Before: {before.hex()} = {repr(before)}")
        print(f"  After:  {after.hex()} = {repr(after)}")
    else:
        print(f"'{name.decode()}' NOT FOUND in file")

# Also show IDString bytes when found
print(f"\n--- IDString occurrences ---")
for m in re.finditer(rb'IDString', data):
    pos = m.start()
    context = data[pos:pos+30]
    print(f"  byte {pos}: {context.hex()} = {repr(context)}")
    if pos > 5: break  # just first occurrence
