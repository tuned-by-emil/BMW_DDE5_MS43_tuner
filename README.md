# BMW DDE5 MS43 Tuner

<p align="center">
  <img src="assets/editor.png" alt="Giant anteater mascot" width="360">
</p>

Windows ECU calibration editor by **Emil Larsson**. Edit XDF tables in 2D/3D,
review CSV logs, configure gauges and keep logging/graph profiles separate by ECU.

## Download and install

1. Open [the latest release](https://github.com/tuned-by-emil/BMW_DDE5_MS43_tuner/releases/latest).
2. Download **BMW_DDE5_MS43_Tuner_1.33.3_Setup.exe** and run it.
3. Click **Install / Update**. Setup downloads a private Python runtime from
   **python.org** and hash-verified library wheels from **PyPI**. Internet access
   is required; the full dependency download is larger than the installer.
4. Open **BMW DDE5 MS43 Tuner** from the Start menu. A desktop shortcut is optional.

Setup installs for your Windows user under
`%LOCALAPPDATA%\Programs\BMW_DDE5_MS43_Tuner`. No administrator installation is
needed, and system Python is unchanged. The app appears in **Settings â†’ Apps â†’
Installed apps** and includes an uninstaller. Close the editor before updating.
The installer is currently unsigned. Windows x64 and .NET Framework 4.5+ are required.

The matching `.sha256` file lets you verify the download:

```powershell
Get-FileHash .\BMW_DDE5_MS43_Tuner_1.33.3_Setup.exe -Algorithm SHA256
```

## Try Emil's BIN and recorded log

Explore the editor using these **EDC16C31 / Bosch software 1037361830** examples:

| File | Description | Download |
| --- | --- | --- |
| `20261012.bin` | Emil's 1 MiB calibration image, matched to the included 361830 XDF | [Download BIN](https://github.com/tuned-by-emil/BMW_DDE5_MS43_tuner/raw/refs/heads/main/examples/dde5-361830/20261012.bin) |
| `Live_D50M47A_20261016_132417.csv` | Recorded 16 October 2026; 6,294 samples over about 12 min 15 sec | [Download log](https://github.com/tuned-by-emil/BMW_DDE5_MS43_tuner/raw/refs/heads/main/examples/dde5-361830/Live_D50M47A_20261016_132417.csv) |

Open the BIN with **File > Open BIN** and select the proposed **EDC16C31 361830**
definition. Use **Open TestO Log** to load the CSV, then explore playback, graphs
and gauges. Recorded channels include RPM, injection quantity, boost pressure,
rail pressure, air mass, injection timing, speed and temperatures.

These downloads are examples for offline exploration. Confirm your own ECU and
software compatibility before using calibration changes. They are downloaded
separately from the installer. See [example details and SHA-256 checksums](examples/dde5-361830/README.md).

## First use

The editor starts without a BIN loaded. Open your own BIN through **File**, then
review the matching definition proposal or choose an XDF yourself. The current
**EDC16C31 361830 definition and eight generated ECU library definitions are
included**. A suggestion is not proof that the selected definition matches your ECU.

Use **Save/export** to write changes. Undo/Redo operates on the open document.
The example BIN and recorded log above are included in the source repository.
Raw map packs, disassembly dumps and MPPS software are excluded.
**MPPS auto-start has been removed.**

## Features

| Area | Included functionality |
| --- | --- |
| Editing | XDF constants/tables, linked axes, 2D/3D views, comparisons, Undo/Redo |
| Definitions | Generated ECU library, XDF authoring and supported A2L/WinOLS imports |
| Logs | CSV playback, graphs, gauges, independent ECU settings and profiles |
| DDE5 | Direct K-line logging for the evidenced 361830 layout; optional EDIABAS/TestO integrations |
| MS43X001 | Full/64 KB editing/export, Fuel/Ignition navigation, VE labels, cc displacement and setting help |
| Drift response | Preview six comfort-response settings cell by cell, then apply as one Undo step |
| Analysis | Local log analysis and optional ChatGPT sign-in with explicit patch review |

MS43X001 full and 64 KB XDFs and its ADX logging definition are included under
MS4X's GPL-3.0 licence, with attribution and source. Opening a recognized MS43X001
BIN automatically loads the matching full or partial XDF. Detection requires
matching software/layout evidence; unrelated or ambiguous images remain manual.
See [resource setup](docs/LOCAL_RESOURCES.md) and
[MS43X support](docs/MS43X_SUPPORT.txt).

The drift preset's dashpot cancellation is inferred from the documented MS43
algorithm and needs vehicle validation. Direct ECU flash writes remain disabled.
Offline/simulated checks do not establish flash readiness.

## Run from source

Use Python 3.11+ on Windows x64. From the repository folder:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe run_editor.py
```

Existing preference storage identifiers are retained, so older ECU profile,
gauge and graph settings remain available after the branding change.

## Test and build

```powershell
python tools/run_tests.py
python run_editor.py --self-check-public build/public-self-check.json
python tools/check_public_tree.py
python tools/build_windows_installer.py
```

The installer builder uses the Windows .NET Framework compiler and Python's
standard library. It does not download or bundle Python/library DLLs. It embeds
editor source, generated definitions, project artwork and the licensed OLS helper.
The setup EXE and its SHA-256 file are written under `dist/`.

Tests create synthetic data. Optional integration tests requiring non-distributed
resources are reported as skipped. No physical ECU is used in checks.

## Libraries and licensing

Versions and wheel hashes are pinned in `requirements-runtime-windows.txt`.
Python's runtime URL/hash is in `installer/runtime.json`. Dependency licences
apply separately from this project's licence. See [dependency licence notes](docs/DEPENDENCY_LICENCES.md)
and [third-party notices](THIRD_PARTY_NOTICES.md).

A project-wide licence is still being selected. The intended rule allows
businesses and paid tuning work while forbidding resale of the editor itself.
Until a licence is adopted, the existing `LICENSE.txt` notice applies.
The original OLS parser remains under its own 0BSD licence.
