# EOI estimation in V1.25

Open a recorded TestO log and choose Analysis > EOI estimate.

Start and duration can independently come from logged signals or selected tables. Tables are assumed active over the evaluated log; the editor does not infer runtime switching. Use your external XDF for duration maps if the bundled definition does not contain a verified one. Map X/Y to signals in exactly the same physical units, including quantity units and pressure units. The current in-memory BIN is evaluated read-only. Missing or out-of-range inputs produce gaps rather than extrapolation.

Choose the sign convention explicitly. For positive-before-TDC start angles: estimated end ATDC = -start + duration_us * RPM * 0.000006. For milliseconds the factor is0.006. For crank-degree duration, add duration directly. Positive end means after TDC; negative means before TDC.

The start reference and duration must refer to the same event and compatible physical/electrical references. IWHE1 is a requested reference angle; IDHE1 is energization duration. Combining them is only an approximate timing indicator, not a hydraulic end measurement. No injector opening/closing delay or combustion phasing is estimated. No inference about efficiency or safe timing limits follows from this display.

Useful main channels: INMOT RPM, IWHE1 main reference angle, IDHE1 (0xBC) main energization duration, or IDGH1 (0xC0) estimated main energization duration. IDVE1 (0xB4) is the pilot1 energization duration. Rail pressure and quantity alone cannot determine duration without an appropriate calibrated duration model. Use main-event quantity where the selected duration map expects main quantity; do not automatically substitute total IMEIA.

Your September7 drive-home log lacks rail/duration channels. A log-only estimate is unavailable from that file. No live EOI streaming is implemented; this release evaluates recorded snapshots. Refresh after edits; changing selections invalidates the displayed result. Exports preserve the formula assumptions and BIN hash.
