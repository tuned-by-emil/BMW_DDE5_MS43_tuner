# Live channels and table tracing

Bottom gauges can be added, removed and rearranged while live logging runs.
Removing a gauge changes the display only. Use **Logging channels…** to change
which parameters the logger collects.

When a new gauge needs a channel that is not being collected, the app asks
whether to add that parameter. Opening a table with missing tracing parameters
also offers to add the available channels. Declining keeps the logging selection;
the table remains open and identifies the missing data. Accepted changes apply
after the current ECU reply, without reopening the cable.

For DDE5, each realtime request has room for ten values, including the repeated
RPM and injection-quantity anchors (eight additional values per block). The selection preview
shows the old and new block counts and warns before adding another block, because
another request significantly reduces the rate at which each channel updates.
RPM and injection quantity remain logging anchors. MS43X reads one fixed packet
per sample, so selecting additional decoded channels does not add another request.

## Captured files

Changing the logging selection starts another CSV segment so each file keeps a
consistent header. For example, `drive.csv` is followed by `drive.part2.csv` and
`drive.part3.csv`. Existing files are never overwritten. Each segment has its own
`.csv.profile.json` metadata, and capture timestamps keep the original session's
time origin. The live view continues displaying the session in one buffer.

Open each CSV segment individually for recorded-log review. The reader rebases
the selected file's first timestamp to zero; it does not automatically join
segments or recreate channels that were absent from that file.

## Tracing coverage

The current bundled definitions support automatic positions for **459 of 863
MS43X multi-cell tables**, excluding reference-axis views, and **46 of 103 DDE5
multi-cell tables**. Coverage depends on verified axis meanings and channels
exposed by the logging definitions. Constants and reference axes do not represent
a runtime table lookup.

MS43X bindings preserve the actual linked X/Y axes, including tables whose source
symbol lists those axes in the opposite display order. Supported quantities include
engine speed, manifold pressure, pedal/throttle position, temperatures, battery
voltage, gear, vehicle speed and the available subsystem load references. Vertical
one-dimensional curves need only their varying axis channel.

VE tables use RPM and manifold pressure, with the active-table channel as a gate.
Only samples selecting that exact table number appear in its cursor, trail and
cell counts. Fractional blend states and unknown selections are excluded.

DDE5 additionally binds supported coolant, intake/charge-air and fuel-temperature
curves, battery-voltage curves, DPF soot mass and engine operating time. Pressure
and time readings are converted to table units where needed, including hPa/bar
and seconds/hours.

The same coordinate rules apply to live data and recorded files. Between rotating
DDE5 request groups, a coordinate can hold its most recent finite reading for up
to **one second**. Older readings are excluded. Scrubbing backward never borrows
a reading from a future sample, and the original log arrays remain sparse.

Startup temperatures, internal controller states, peak or corrected pedal values,
raw sensor inputs and ambiguous load quantities remain explicitly unbound when
the logger cannot supply a matching quantity. A similar channel name does not
establish equivalence. Use manual axis binding only with a verified channel and
matching physical scaling; adding a channel cannot supply data the ECU protocol
does not expose.
