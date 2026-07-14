# Build Rotor.exe - a single-file, elevated tray app.
# Run from the repo root with the venv created (see README "Install").
#
#   .\build.ps1
#
# Output: dist\Rotor.exe

$py = ".\.venv\Scripts\python.exe"

# 1) (Re)generate the app icon from the tray renderer.
& $py -c @"
import tray
from engine import AudioEngine
from knob import KnobController
e = AudioEngine(); k = KnobController(e)
k.set_active('filter'); k.current().amount = 0.6
tray.render_icon(e, k.current()).save(
    'rotor.ico', sizes=[(16,16),(24,24),(32,32),(48,48),(64,64),(128,128)])
print('rotor.ico written')
"@

# 2) Build the single-file exe.
#    --noconsole : tray app, no console window
#    --uac-admin : request elevation (the global keyboard hook needs it)
#    --collect-all sounddevice : bundle the PortAudio DLL
& $py -m PyInstaller --noconfirm --clean --onefile --noconsole `
    --name Rotor --icon rotor.ico --uac-admin `
    --collect-all sounddevice `
    --hidden-import pystray._win32 `
    tray.py

Write-Host "`nDone -> dist\Rotor.exe"
