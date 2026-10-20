from fred.hands import _scan_start_menu, _scan_appsfolder
import os
from pathlib import Path

# Check start menu dirs
user_start_menu = Path.home() / 'AppData' / 'Roaming' / 'Microsoft' / 'Windows' / 'Start Menu' / 'Programs'
common_start_menu = Path(os.environ.get('PROGRAMDATA', 'C:\\ProgramData')) / 'Microsoft' / 'Windows' / 'Start Menu' / 'Programs'

print('User start menu exists:', user_start_menu.exists())
print('Common start menu exists:', common_start_menu.exists())

if user_start_menu.exists():
    lnks = list(user_start_menu.rglob('*.lnk'))
    print('User .lnk files:', len(lnks))
    for l in lnks[:5]:
        print('  ', l)

if common_start_menu.exists():
    lnks = list(common_start_menu.rglob('*.lnk'))
    print('Common .lnk files:', len(lnks))
    for l in lnks[:5]:
        print('  ', l)

print('')
print('Scanning start menu...')
start_menu_index = _scan_start_menu()
print('Start menu index:', len(start_menu_index), 'apps')
for name, (path, is_uwp) in list(start_menu_index.items())[:10]:
    print('  ', name, ':', path, '(uwp=', is_uwp, ')')

print('')
print('Scanning apps folder...')
appsfolder_index = _scan_appsfolder()
print('Apps folder index:', len(appsfolder_index), 'apps')
for name, (path, is_uwp) in list(appsfolder_index.items())[:10]:
    print('  ', name, ':', path, '(uwp=', is_uwp, ')')