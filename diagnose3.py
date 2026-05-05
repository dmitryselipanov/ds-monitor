"""Look for note records near each track name."""
import sys, re, struct
from pathlib import Path

cpr = Path(sys.argv[1])
print(f"Reading {cpr.name}...")
data = cpr.read_bytes()

BOM = b'\x00\xef\xbb\xbf'

# Find all name+BOM occurrences
names = []
for m in re.finditer(re.escape(BOM), data):
    pos = m.start()
    # Read backward for null-terminated name
    end = pos
    start = max(0, pos - 80)
    chunk = data[start:end]
    # Find the last null byte before the name
    null_pos = chunk.rfind(b'\x00')
    if null_pos >= 0:
        name_bytes = chunk[null_pos+1:]
        try:
            name = name_bytes.decode('ascii').strip()
            if re.match(r'^[A-Za-z][A-Za-z0-9 _\-\.]{2,}$', name):
                names.append((pos, name))
        except:
            pass

print(f"Found {len(names)} name+BOM positions")
print(f"First 20: {[n for _,n in names[:20]]}")

# For each name, look for adcn\x00\x01 within 2MB before it
ADCN = rb'adcn\x00\x01'
for name_pos, name in names[:10]:
    search_start = max(0, name_pos - 2000000)
    chunk = data[search_start:name_pos]
    count = len(re.findall(ADCN, chunk))
    if count > 0:
        print(f"  '{name}' at {name_pos}: {count} note records in 2MB before")
