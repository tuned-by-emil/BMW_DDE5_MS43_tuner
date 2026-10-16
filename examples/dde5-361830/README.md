# Emil's DDE5 example BIN and log

Shared by Emil for exploring the editor with recorded data.

- ECU definition: **EDC16C31 361830**; embedded Bosch software **1037361830**.
- BIN: `20261012.bin`, 1,048,576 bytes. File contents are preserved exactly.
- Log: `Live_D50M47A_20261016_132417.csv`, 1,754,035 bytes; recorded on
  16 October 2026, with 6,294 samples spanning 734.922 seconds. Original CSV
  timestamps, serial request/reply evidence and measured values are preserved.
- The BIN and log were selected together by Emil. Their contents alone do not
  establish which calibration was running during every logged sample.

## Open the examples

1. Download both files using the links on the repository's opening page.
2. Start BMW DDE5 MS43 Tuner and choose **File > Open BIN**.
3. Select `20261012.bin`, then accept the proposed **EDC16C31 361830** XDF.
4. Click **Open TestO Log** and select the CSV.
5. Use playback, gauges and graphs to inspect the recording. Open a map to
   explore compatible logged-axis overlays.

Use these files for offline exploration. Verify your own ECU/software and
calibration compatibility before using changes on a vehicle. Keep your original
read and save edits to a new filename.

## SHA-256

```text
2bafc9c3ce93545da6383b98ae6e3fcbc673fda57c240ee278f561a62f643d62  20261012.bin
5e36e06e7f3318aecb99d1f9e0a49321b2d281a0e0b84f2ed67cc59cc4b609bf  Live_D50M47A_20261016_132417.csv
```

The installer does not embed these example files. Download them separately or
use the copies in a source checkout. No raw map packs, flashing tools or local
profile sidecars are included with these examples.
