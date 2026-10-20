import os
from pathlib import Path

# Check common locations for calculator
paths_to_check = [
    Path(r'C:\Windows\System32\calc.exe'),
    Path(r'C:\Windows\SysWOW64\calc.exe'),
    Path.home() / 'AppData' / 'Local' / 'Microsoft' / 'WindowsApps' / 'calculator.exe',
]

for p in paths_to_check:
    print(f'{p}: exists={p.exists()}')

# Also check if calc.exe is in System32
import subprocess
result = subprocess.run(['where', 'calc'], capture_output=True, text=True)
print(f'where calc: {result.stdout.strip()}')