# Changelog

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
