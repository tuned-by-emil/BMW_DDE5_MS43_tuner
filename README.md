# BMW Bin Editor — DDE5 / MS43

Windows calibration editor by Emil Larsson, with 2D/3D tables, XDF authoring,
CSV graphs, gauges, ECU-specific display profiles and direct K-line logging.

This is a fresh source repository. It contains no vehicle firmware, map packs,
early reverse-engineering dumps, MPPS software or old development history.

## Windows download

Open [Releases](https://github.com/tuned-by-emil/BMW_DDE5_MS43_tuner/releases/latest) and choose the latest Windows x64
portable ZIP. Extract the entire archive, then run `BMW_Bin_Editor_V1_33_1.exe`.
Keep `_internal` beside the executable. The package includes the runtime;
Python, a compiler and administrator installation are not required.
This is a portable application, so there is no setup wizard. The EXE is unsigned.

The matching `.sha256` file lets you verify the ZIP with PowerShell:

```powershell
Get-FileHash .\BMW_Bin_Editor_V1_33_1_Windows_x64_portable.zip -Algorithm SHA256
```

Open your own BIN and matching XDF through **File**. Nothing is changed or saved
until you edit and explicitly save/export. The package contains no ECU firmware,
calibration definition pack or flashing software.

## Run

Use Python 3.11 or later on Windows x64. Run `install_and_run.bat`, or create
a virtual environment, install `requirements.txt`, then run `python run_editor.py`.
The editor starts empty. Open your own BIN and matching XDF using the File menu.
Vendor software and authentication data are supplied by the user when required.

Manual setup from the repository folder:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe run_editor.py
```

## What it does

| Area | Features |
| --- | --- |
| Calibration editing | XDF tables/constants, linked axes, 2D/3D views, comparisons and Undo/Redo |
| Definitions | Open/edit XDFs, create definitions, import supported A2L/WinOLS formats |
| Logs and gauges | CSV playback, graph layouts, channel setup and per-ECU saved profiles |
| DDE5 | Direct K-line logging for the evidenced 361830 profile; optional EDIABAS/TestO integrations |
| MS43X001 | Full/64 KB editing/export, Fuel/Ignition navigation, VE labels, cc displacement and setting help |
| Drift response | Explicit change preview for six comfort-response settings, then one undoable Apply |
| Analysis | Local log analysis and optional ChatGPT sign-in; user-controlled review before applying proposals |

Import support does not guarantee that any particular third-party definition or
firmware matches your ECU. Review the selected software identity and definition.

MS43X support includes full/64 KB calibration handling, cc displacement display,
VE table labels, tooltips and **Edit > MS43X drift throttle response preset…**.
The preset previews each setting, cell, value and byte before one undoable edit.
Its dashpot cancellation is inferred from the documented MS43 algorithm; vehicle
behavior still needs validation. See [MS43X support](docs/MS43X_SUPPORT.txt).

## Optional local definitions

Calibration XDFs, the MS43X logging ADX and generated definition-library records
are not distributed here: their redistribution permissions have not been recorded.
The prepared local working folder has these resources installed as ignored files.
They are excluded from source exports and portable builds.

On a fresh clone, place your own current 361830 definition at
`definitions/EDC16C31 361830.xdf`. MS43X logging expects
`definitions/ms43x/Siemens_MS43_MS43X001_Logging.adx`.
The MS43X XDFs can be opened from any location.
For a portable release, local runtime definitions/ADX files go under
`_internal/definitions/` instead of the source checkout's `definitions/` folder.
See [local resources and provenance](docs/LOCAL_RESOURCES.md).

## Test and build

```powershell
python tools/run_tests.py
python run_editor.py --self-check-public build/public-self-check.json
python tools/check_public_tree.py
```

The tests generate synthetic data. Tests requiring optional local resources are
reported as skipped when those resources are absent. No hardware is contacted.
The public self-check works without any private calibration definitions.

`build_windows_exe.bat` builds a portable Windows package with pinned dependencies
from `requirements-build.txt`. It installs nothing unless `--install-deps` is given.
The build uses an explicit resource allowlist, so ignored local definitions are
not accidentally bundled. It includes the licensed OLS helper and supplier notices.
The build creates the Windows ZIP and its SHA-256 checksum under `dist/`.
Publish those two files as the assets of the current GitHub Release.

## Project status and licensing

Direct ECU writes remain disabled. Offline checks and simulated transport tests
do not establish vehicle behavior or flash readiness.

The existing project notice is in `LICENSE.txt`; a project-wide open-source
licence has not been selected. Third-party OLS parser source is 0BSD and retains
its original licence and attribution. See [third-party notices](THIRD_PARTY_NOTICES.md).
