# Third-party notices

- OLS parser: https://github.com/JSD-SpeedUP/ols-parser-public, pinned upstream
  commit `0640c3baa5327cc03fba8a71417cc9205650f305`, 0BSD.
  Licence and local modifications are recorded in `third_party/ols_parser/`.
  `assets/ols/ols_dump.exe` is the built helper; its source and build command
  are included. The parser is independent of WinOLS itself.
- Python and library binaries are downloaded from their official distributors
  during setup, rather than embedded in the installer. Runtime versions and
  wheel hashes are pinned in `requirements-runtime-windows.txt` and
  `installer/runtime.json`. Their licences still apply; see
  `docs/DEPENDENCY_LICENCES.md` and the installed distribution metadata.
- BMW, TunerPro and other product names identify compatible formats/protocols;
  the project is not affiliated with those suppliers.

The nine generated XDFs listed in `public_build_resources.py` are included in
source and setup. Conversion does not establish ownership of underlying
calibration descriptions; see `docs/LOCAL_RESOURCES.md` for provenance limits.
The MS4x Dev Team's MS43X001 full/partial XDFs and logging ADX are included under
GPL-3.0; see `definitions/ms43x/NOTICE.md` and `third_party/license_texts/GPL-3.0.txt`.
They are excluded from any editor-specific no-resale terms.
Firmware, map packs, MPPS and disassembly
are excluded. Project licence terms do not override third-party rights.
