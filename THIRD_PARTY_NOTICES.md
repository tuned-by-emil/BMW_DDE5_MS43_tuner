# Third-party notices

- OLS parser: https://github.com/JSD-SpeedUP/ols-parser-public, pinned upstream
  commit `0640c3baa5327cc03fba8a71417cc9205650f305`, 0BSD.
  Licence and local modifications are recorded in `third_party/ols_parser/`.
  `assets/ols/ols_dump.exe` is the built helper; its source and build command
  are included. The parser is independent of WinOLS itself.
- Python dependencies are listed in `requirements.txt`; portable build pins
  are in `requirements-build.txt`. The build preserves supplier licence files
  and distribution metadata in `THIRD_PARTY_LICENSES`.
- BMW, TunerPro and other product names identify compatible formats/protocols;
  the project is not affiliated with those suppliers.

Supplied calibration definitions, vendor binaries, firmware and map packs
are not included in public Git or the public portable package.
