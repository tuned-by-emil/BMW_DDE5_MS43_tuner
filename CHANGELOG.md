# Changelog

## 1.35.0

### Added

- DDE5 patch checks for the supported 361830 identity, installed MPC admission
  words and selected RAM observations, plus separate baud and explicitly
  reserved four-byte write/restoration checks.
- A RAM calibration bench that checks the live directory and complete original
  flash blocks, budgets whole-block allocation costs, verifies both allocator
  cursors, and exercises native copy and page selection. An optional reversible
  changed-byte probe includes exact readback and restoration.
- Live RAM editing for selected table values and editable axes after the same
  open connection passes an actual changed-byte qualification, exact readback,
  original-byte restoration and reference-page restoration. Prior reports and
  earlier connections cannot grant access.
- Exact mapped byte readback through the native checksum routine; uploads check
  prior bytes and changed bytes, reads apply to the editor as one undoable edit,
  and selected values can be restored from the immutable original BIN.
- Reference/working-page controls, shared-block edit preservation, bounded
  registration of additional tables, and complete allocation budgeting against
  independently declared 32 KiB or 64 KiB external RAM capacity.
- JSON reports and packaged offline smoke checks for the new diagnostic paths.

### Changed

- The normal v1.35.0 installer includes the new screens while retaining the
  existing installation, uninstaller, shortcuts and release settings namespace.
- Live RAM editing owns one persistent COM connection. Logging, other ECU
  connections and replacement BIN/definition loads wait until it disconnects.
- Protocol failures lock live controls without automatic reconnects or blind
  retries. Healthy failures attempt exact rollback and reference restoration;
  uncertainty survives repeated disconnects and remains visible in reports.

### Limits and validation

- RAM functions require Bosch hardware 0281010565 / software 1037361830, the
  reviewed installed MPC admission patch, matching original flash blocks and
  independently verified SRAM wiring, bank configuration and usable capacity.
- Qualification is an engine-off bench operation from a fresh boot with an idle
  manager and exclusive diagnostic access. The editor does not install ECU
  patches or certify a user's hardware declaration.
- The complete directory costs 1,116 bytes, and first writes allocate complete
  firmware blocks. The firmware can allocate small blocks from its separate
  420-byte internal pool. A declared 64 KiB chip remains limited to the stock
  60 KiB external arena.
- Whole-block 16-bit additive checksums have collisions. Exact byte readback is
  separate, slow and sequential. Multi-cell uploads and table reads are not
  atomic operations.
- Automated checks use synthetic UART traffic, generated BINs, GUI tests and
  original firmware instruction emulation. Physical ECU/SRAM validation remains
  outstanding. No flash erase/programming, authentication or ECU reset is added.

See the [patch-check guide](docs/DDE5_PATCH_TEST.txt),
[RAM bench guide](docs/DDE5_RAM_CALIBRATION_TEST.txt) and
[live RAM guide](docs/DDE5_LIVE_RAM.txt).

## 1.34.0

### Added

- Add, remove and reorder bottom gauges while live logging runs. Gauge controls
  remain visible while scrolling, and each gauge has a direct remove button.
- Confirmation before adding logging parameters required by a new gauge or an
  opened table. **Logging channels…** edits the active acquisition selection.
- DDE5 realtime block previews and a warning before a new block reduces each
  channel's update rate. Blocks contain up to ten values, including repeated
  RPM and injection-quantity anchors.
- Automatic position bindings for 459 of 863 bundled MS43X multi-cell tables and
  46 of 103 DDE5 multi-cell tables. Supported DDE5 axes now include temperature,
  voltage, soot mass and operating time.
- Live status and block budget, a clear action to request missing tracing
  parameters, and the gauge-card layout.

### Fixed

- Gauge setup works during active logging across direct serial DDE5, native
  EDIABAS, TestO and MS43X backends.
- Vertical one-dimensional tables require only their varying axis channel.
- Table cursors, trails and cell-visit heatmaps use the same pressure/time unit
  conversions and MS43X VE-table selection rules. Unknown or fractional VE
  selections are excluded from individual-table tracing.
- Sparse DDE5 coordinates can use their most recent earlier finite reading for
  up to one second. Older readings are excluded, and scrubbing never borrows a
  future sample or fills the original log arrays.
- Direct serial definition verification accepts LF and CRLF checkouts while
  retaining the pinned scaling integrity check.

### Changed

- Accepted parameter changes apply between ECU replies without reconnecting.
  Removing a gauge changes the display only; use **Logging channels…** to stop
  collecting a parameter.
- Changing a captured selection starts a new `.partN.csv` segment with a fixed
  header and its own metadata. Existing files are retained, and elapsed
  timestamps keep the original session's time origin. Open segments individually
  for recorded-log review.
- Gauge updates avoid unnecessary layout work. Expensive graph redraws are
  limited by elapsed time while acquisition retains every reply.
- Windows release automation uses current Node 24 actions and creates a draft
  from a matching `release/VERSION` branch after its checks pass. Drafts include
  the installer, checksum and checked-in release notes for the exact built commit.
- Windows test path comparisons resolve filesystem aliases consistently.

### Limits and validation

- Reference axes and constants have no runtime lookup position. Tables requiring
  quantities unavailable from the logging definitions remain explicitly unbound;
  similar channel names do not establish equivalent values. Verified manual axis
  bindings remain available.
- The new behavior has been checked with synthetic calibration data, simulated
  diagnostic traffic and GUI tests. Physical ECU validation is still required.
- Linux workspace validation: 646 tests passed, 57 optional tests skipped, and
  five existing failures involved restricted localhost sockets or a Windows-only
  account-lock expectation. Startup and public self-checks passed. Windows CI
  remains a required release gate.

See [live-channel and tracing details](docs/TRACING_AND_LIVE_CHANNELS.md).
