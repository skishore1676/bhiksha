"""Narrow OOXML visibility fallback: artifact-tool lacks a documented hide API."""
from pathlib import Path
import re
import sys
import zipfile

path = Path(sys.argv[1])
with zipfile.ZipFile(path) as source:
    original = {i.filename: source.read(i.filename) for i in source.infolist()}
    infos = source.infolist()
text = original['xl/workbook.xml'].decode()
count = 0
for name in ('Trades', 'Exit detail', 'Signal detail'):
    pattern = r'<(?:\w+:)?sheet\b[^>]*\bname="' + re.escape(name) + r'"[^>]*/>'
    def hide(match):
        tag = re.sub(r'\sstate="[^"]*"', '', match.group())
        return tag[:-2] + ' state="hidden"/>'
    text, n = re.subn(pattern, hide, text)
    assert n == 1, (name, n)
    count += n
replacement = text.encode()
tmp = path.with_suffix('.tmp.xlsx')
with zipfile.ZipFile(tmp, 'w') as target:
    for info in infos:
        target.writestr(info, replacement if info.filename == 'xl/workbook.xml' else original[info.filename])
with zipfile.ZipFile(tmp) as target:
    assert all(target.read(name) == content for name, content in original.items() if name != 'xl/workbook.xml')
tmp.replace(path)
print(f'Hidden {count} detail sheets; all other workbook parts preserved.')
