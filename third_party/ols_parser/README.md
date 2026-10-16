# Bundled OLS parser

Source: https://github.com/JSD-SpeedUP/ols-parser-public

Upstream commit: `0640c3baa5327cc03fba8a71417cc9205650f305`

License: 0BSD; see `LICENSE`.

Local changes preserve complete serialized map/axis records and raw flags,
correct legacy format-113 hardware/software metadata, retain the first
serialized axis for curves, suppress unused axis ROM addresses, label the
upstream column-major default as an assumption, and decode maps whose
description CString is empty or very short. The editor independently validates format,
version slots, folders, map counts, record spans, and ROM extents. Its supported
native import format is currently 113 with exact 2 MiB version slots.

The serialized axis signedness flag is exposed separately, and zero map
scaling factors are preserved rather than replaced with one.

Build the shipped Windows helper from the workspace root:

```powershell
& 'C:/Program Files/LLVM/bin/clang.exe' -O2 -Wall -Wextra -std=c99 -D_CRT_SECURE_NO_WARNINGS third_party/ols_parser/ols.c third_party/ols_parser/ols_dump.c -o assets/ols/ols_dump.exe
```

The runtime requires only the bundled helper; no compiler or WinOLS install is
needed. The helper reads an isolated input snapshot and writes JSON to stdout.
