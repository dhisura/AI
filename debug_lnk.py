from fred.hands import _resolve_lnk
from pathlib import Path
import os

# Test resolving a few .lnk files
lnk_path = Path.home() / 'AppData' / 'Roaming' / 'Microsoft' / 'Windows' / 'Start Menu' / 'Programs' / 'File Explorer.lnk'
print('Testing:', lnk_path)
print('Exists:', lnk_path.exists())
result = _resolve_lnk(lnk_path)
print('Result:', result)

programdata = os.environ.get('PROGRAMDATA', r'C:\ProgramData')
lnk_path2 = Path(programdata) / 'Microsoft' / 'Windows' / 'Start Menu' / 'Programs' / 'Calculator.lnk'
print()
print('Testing:', lnk_path2)
print('Exists:', lnk_path2.exists())
result2 = _resolve_lnk(lnk_path2)
print('Result:', result2)