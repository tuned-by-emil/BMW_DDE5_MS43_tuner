# BMW DDE5 MS43 Tuner 1.33.6 — Windows x64

Run **BMW_DDE5_MS43_Tuner_1.33.6_Setup.exe**. Setup downloads private Python
from python.org and verified runtime libraries from PyPI. Internet is required.
It installs for the current Windows user, adds a Start menu shortcut, offers a
desktop shortcut and registers an uninstaller in Installed apps. System Python
is unchanged. The installer is unsigned.

- Renamed the File menu, toolbar action, log-panel button and file dialogs to
  **Open Log**, with matching help and example instructions.
- Tested with **MS43X**: logging and saving the partial **64 KB** calibration
  file worked perfectly, as reported by Emil.
- Current 361830 definition plus eight generated ECU library XDFs.
- Readable English display names for all 251 retained 361830 definitions,
  with original evidence in tooltips and unverified labels preserved.
- Table navigator and Favorites: separate searchable Name and Address columns.
- DDE5 logger: English name, original BMW result name and hex code columns,
  searchable alongside original descriptions and argument names. All 210
  parameters have English names; units and capture groups remain in tooltips.
- Existing calibration addresses, XDF source, channel IDs and profiles retained.
- MS43X001 full/64 KB XDFs and logging ADX under their upstream GPL-3.0 licence.
- Automatic XDF loading for a uniquely validated MS43X001 BIN, including
  edited 64 KB calibration reads with matching version and layout evidence.
- Giant anteater application, taskbar, shortcut and installer icon.
- Anteater welcome artwork on the app's opening screen and repository README.
- Welcome-screen link to the example BIN, recorded log and loading instructions.
- New app, installer and Start menu name: BMW DDE5 MS43 Tuner.
- MPPS auto-start removed entirely.
- ECU-specific gauges, logging settings and graph profiles.
- MS43X calibration handling and a preview-before-apply drift response preset.

No Python runtime, library wheels/DLLs, firmware images, raw map packs or
disassembly dumps are embedded. The small OLS import helper is included under
0BSD, with its source and licence. See the README for first-use instructions.

Runtime and dependency hashes are pinned. Checks use synthetic calibration
bytes and simulated serial traffic. The MS43X logging and partial 64 KB save
results above are user-reported testing; other vehicle behavior is not covered
by that report.
