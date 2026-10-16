# BMW Bin Editor 1.33.1 — Windows x64

Download the Windows portable ZIP, extract the whole archive and run
`BMW_Bin_Editor_V1_33_1.exe`. Keep the `_internal` directory beside the executable.
There is no separate Python installation or administrator setup step for this package.
The `.sha256` file is provided for download verification.

- XDF authoring, calibration tables and 2D/3D editing with Undo/Redo.
- CSV graphs, gauges and separate ECU display/logging profiles.
- MS43X full/64 KB calibration handling, cc displacement and VE labels.
- Preview-before-apply drift throttle-response preset, with one Undo step.
- Direct K-line logging support and optional ADX-based MS43X logging.

This public package does not contain ECU firmware, calibration XDF/ADX files,
map packs, vendor authentication data or MPPS. Open your own BIN/XDF through File.
MS43X logging requires the optional local ADX described in `docs/LOCAL_RESOURCES.md`.
The EXE is unsigned. Vehicle behavior, including the inferred dashpot cancellation,
still needs validation; direct ECU flash writes remain disabled.

Verification uses generated calibration bytes and simulated serial traffic.
No physical ECU is contacted during tests or packaging.
