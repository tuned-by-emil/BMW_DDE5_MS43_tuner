# Dependency licence notes

The current setup EXE contains the editor, generated definitions, artwork and
the small 0BSD OLS helper. It downloads Python and dependency wheels directly
from python.org and PyPI onto the user's machine. It embeds no Python runtime,
Qt/library DLLs or wheels. Downloading separately reduces what this project
redistributes; it does not change dependency terms.

## Main components

| Component | Licence / handling |
| --- | --- |
| PySide6, Shiboken and used Qt modules | LGPLv3 option; keep library rights separate from the editor's licence |
| NumPy | BSD-3-Clause plus bundled third-party notices |
| Matplotlib | Matplotlib licence plus bundled fonts/native-component notices |
| pySerial | BSD-3-Clause |
| PyJWT | MIT |
| cryptography | Apache-2.0 OR BSD-3-Clause, plus native dependency terms |
| Python | PSF licence and included third-party notices |
| OLS parser/helper | 0BSD; source, upstream attribution and licence are included |

Exact runtime dependency versions and accepted wheel hashes are recorded in
`requirements-runtime-windows.txt`. The Python archive URL, version and SHA-256
come from the official Windows release manifest recorded in `installer/runtime.json`.
Installed wheel metadata retains supplier licence files in the private environment.

## Important limits

1. The editor's proposed no-resale restriction must apply only to its own code.
   It cannot restrict standalone third-party libraries or remove their licences.
2. LGPL use must preserve users' rights to modify/replace/relink the libraries,
   including reverse engineering needed to debug those modifications. A future
   application licence/EULA must expressly preserve those rights.
3. Some Qt add-ons are GPL-only under their free licence. Virtual Keyboard is
   one such module. It is not used by this app. An earlier development bundler
   auto-collected it; that self-contained package is not the public installer.
4. Redistributing an installed environment or a future self-contained package
   requires its own complete binary-distribution review, including appropriate
   licence notices, corresponding-source access and installation information.
5. Converting an A2L/WinOLS definition into XDF does not itself remove upstream
   rights. The generated definitions are included at the maintainer's request;
   their source provenance remains a separate review item before going public.

The repository's current project-wide licence remains undecided. A no-resale
licence draft is separate from third-party licences. This document records
engineering findings; it is not a legal clearance opinion.

The separately supplied MS43X001 full/partial XDFs and ADX are MS4X GPL-3.0 source data.
Its upstream author, pinned source and licence are recorded in
`definitions/ms43x/NOTICE.md`. Editor-specific restrictions cannot restrict
recipients' GPL rights in those definitions, including commercial redistribution.

## Primary references

- Qt LGPL obligations: https://www.qt.io/development/open-source-lgpl-obligations
- Qt module licensing: https://doc.qt.io/qt-6.11/licensing.html
- Qt Virtual Keyboard: https://doc.qt.io/qt-6.11/qtvirtualkeyboard-index.html
- Qt for Python notices: https://doc.qt.io/qtforpython-6/licenses.html
- NumPy licence: https://numpy.org/doc/stable/license.html
- Matplotlib licence: https://matplotlib.org/stable/project/license.html
- pySerial: https://github.com/pyserial/pyserial/blob/v3.5/LICENSE.txt
- PyJWT: https://github.com/jpadilla/pyjwt/blob/master/LICENSE
- cryptography: https://github.com/pyca/cryptography/blob/main/LICENSE
