# Definitions and local resources

The current 361830 definition and eight generated ECU library XDFs are included
in Git and the installer, with their matching catalog. Original firmware, raw
A2L/WinOLS packs, disassembly workspaces and intermediate reports are excluded.
The definitions retain evidence and source hashes. Generation alone does not
establish redistribution permission for upstream input material; maintainers
must review those input terms before public distribution.

The MS43X001 ADX logging definition is included under its upstream GPL-3.0
licence, with attribution in `definitions/ms43x/NOTICE.md`. Your matching MS43X
XDF remains a separate local resource: open it through File. On an installed
copy, `current-app.txt` under the install root identifies the app directory.
Source: https://www.ms4x.net/index.php?title=TunerPro_Data_Logging.

Optional hardware/integration fixtures belong under ignored `local_resources/`:
`launch.csv`, `dde5/` for paired images and `winkfp/` for local factory data.
They are not public synthetic test fixtures.
