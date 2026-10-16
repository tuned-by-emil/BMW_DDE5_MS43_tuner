# Local resources and provenance

The public source does not redistribute firmware or calibration map packs.
No redistribution licence was found in the source checkout for the supplied
XDF/ADX resources or the generated A2L/WinOLS definition-library exports.
Their absence from public Git is a packaging decision, not a conclusion about
their owners' rights. Record permission and attribution before adding them later.

The prepared local folder retains current 361830 and MS43X definitions, the
generated library.
These files remain ignored. BINs, raw map packs, disassembly handoffs, early
trace reports and large intermediate exports were not copied.

Fresh clones can open user-provided XDFs and BINs through File. The optional
local 361830 shortcut uses `definitions/EDC16C31 361830.xdf`.
MS43X logging uses `definitions/ms43x/Siemens_MS43_MS43X001_Logging.adx`.
Copy the file from your own authorised source, then restart the editor.
In a portable Windows package, these paths are under `_internal/definitions/`;
in the source checkout they are under `definitions/`.
The source reference is https://www.ms4x.net/index.php?title=TunerPro_Data_Logging.

The public build explicitly includes only application-owned logging catalogs
and visual assets. It excludes local XDF/ADX files and library exports even if
they are installed in this folder. Private resources never enter the initial
commit, so the public repository has no earlier private history to recover.

Optional hardware/integration fixtures belong under `local_resources/`:
`launch.csv`, `dde5/` for preserved paired images and `winkfp/` for local
factory data. They are not part of the public tests' synthetic fixtures.
