"""Find track names using exact byte prefix pattern."""
import sys, re, struct
from pathlib import Path

cpr = Path(sys.argv[1])
print(f"Reading {cpr.name}...")
data = cpr.read_bytes()

# Specific prefix before track names (from diagnose2 output)
PREFIX = b'\x00\x00\x00\x00\x00\x00\x00\x00\x80\x00\x00\xbf'
BOM_SUFFIX = b'\x00\xef\xbb\xbf'
ADCN = b'adcn\x00\x01'

# Find track names: PREFIX + 8 variable bytes + name + BOM
pattern = re.escape(PREFIX) + rb'.{4,12}([\x20-\x7e]{2,60})' + re.escape(BOM_SUFFIX)
names = []
for m in re.finditer(pattern, data):
    name = m.group(1).decode('ascii', errors='ignore').strip()
    if name and len(name) >= 2:
        names.append((m.start(), name))

print(f"\nFound {len(names)} track names with prefix pattern:")
for pos, name in names[:30]:
    print(f"  byte {pos}: '{name}'")

# For each name, check for notes in 5MB before AND after
print(f"\nNote records near track names:")
for pos, name in names:
    before = data[max(0,pos-5000000):pos]
    after = data[pos:min(len(data),pos+5000000)]
    nb = len(re.findall(re.escape(ADCN), before))
    na = len(re.findall(re.escape(ADCN), after))
    if nb > 0 or na > 0:
        print(f"  '{name}' at {pos}: {nb} notes before, {na} notes after")
