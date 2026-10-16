"""Offline MPC checksum correction and preserved 32 KiB sector planning.

The plan describes the minimum complete sector payloads containing changed
bytes. Factory routine02 erases a larger fixed range set; this module does not
provide a partial erase operation or communicate with EDIABAS/the ECU.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib

SECTOR_BYTES = 0x8000
MPC_APPLICATION_START = 0x20000
MPC_APPLICATION_END = 0x70000  # exclusive
MPC_IMAGE_SIZES = (0x70000, 0x70200)
FLS_BASE = 0x800000
FLS_SECTORS = (
    (0x800000, 0x804000), (0x804000, 0x806000), (0x806000, 0x808000),
    (0x808000, 0x820000), (0x820000, 0x840000), (0x840000, 0x860000),
    (0x860000, 0x880000), (0x880000, 0x8C0000), (0x8C0000, 0x900000),
)
FACTORY_PROGRAM_ERASE_RANGES = (
    ("MPC", 0x20000, 0x70000),
    ("FLS", 0x820000, 0x860000),
    ("FLS", 0x8C0000, 0x900000),
)
PARTIAL_PLAN_LIMITS = (
    "Factory routine02 erases the complete program range set, not selected MPC sectors.",
    "The loaded baseline has not been compared with a read of the connected ECU.",
    "Live programming, activation, post-write verification and recovery are untested.",
)


class SectorPlanError(ValueError):
    pass


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def changed_ranges(original: bytes, edited: bytes):
    """Return exact half-open difference spans without bridging unchanged bytes."""
    if len(original) != len(edited):
        raise SectorPlanError("Original and edited image sizes differ.")
    spans = []
    start = None
    for offset, (before, after) in enumerate(zip(original, edited)):
        if before != after and start is None:
            start = offset
        elif before == after and start is not None:
            spans.append((start, offset))
            start = None
    if start is not None:
        spans.append((start, len(original)))
    return tuple(spans)


def _validate_mpc_pair(original, edited):
    if not isinstance(original, (bytes, bytearray, memoryview)) or not isinstance(
            edited, (bytes, bytearray, memoryview)):
        raise SectorPlanError("Supply complete byte images; sparse records are not sector contents.")
    original, edited = bytes(original), bytes(edited)
    if len(original) not in MPC_IMAGE_SIZES or len(edited) != len(original):
        raise SectorPlanError("Supply equal 458752/459264-byte MPC images.")
    spans = changed_ranges(original, edited)
    if any(start < MPC_APPLICATION_START or end > MPC_APPLICATION_END for start, end in spans):
        raise SectorPlanError("MPC application planning permits changes only at 0x20000–0x6FFFF.")
    return original, edited, spans


@dataclass(frozen=True)
class MpcSector:
    file_offset: int
    payload: bytes = field(repr=False)
    baseline_sha256: str
    changed_bytes: int
    changed_ranges: tuple[tuple[int, int], ...]

    @property
    def end_exclusive(self):
        return self.file_offset + len(self.payload)

    def as_report(self):
        return dict(address_space="MPC", file_offset=self.file_offset,
                    runtime_address=self.file_offset, end_exclusive=self.end_exclusive,
                    bytes=len(self.payload), changed_bytes=self.changed_bytes,
                    preserved_bytes=len(self.payload)-self.changed_bytes,
                    baseline_sha256=self.baseline_sha256, payload_sha256=_sha(self.payload),
                    changed_ranges=[dict(file_offset=start, end_exclusive=end, bytes=end-start)
                                    for start, end in self.changed_ranges])


@dataclass(frozen=True)
class MpcSectorPlan:
    image_size: int
    baseline_sha256: str
    corrected_sha256: str
    changed_ranges: tuple[tuple[int, int], ...]
    sectors: tuple[MpcSector, ...]

    def as_report(self):
        erase = [dict(address_space=space, runtime_address=start,
                      end_exclusive=end, bytes=end-start)
                 for space, start, end in FACTORY_PROGRAM_ERASE_RANGES]
        return dict(format="DDE5-361830-MPC-sector-review-v1", address_space="MPC",
                    image_size=self.image_size, baseline_sha256=self.baseline_sha256,
                    corrected_sha256=self.corrected_sha256, sector_bytes=SECTOR_BYTES,
                    selected_sector_count=len(self.sectors),
                    planned_sector_payload_bytes=sum(len(s.payload) for s in self.sectors),
                    changed_bytes=sum(end-start for start, end in self.changed_ranges),
                    changed_ranges=[dict(file_offset=start, end_exclusive=end, bytes=end-start)
                                    for start, end in self.changed_ranges],
                    sectors=[sector.as_report() for sector in self.sectors],
                    native_program_erase_ranges=erase,
                    native_program_erase_MPC_bytes=0x50000,
                    native_program_erase_FLS_bytes=0x80000,
                    native_program_erase_MPC_sector_count=10,
                    native_program_erase_FLS_sector_count=3,
                    native_program_erase_total_bytes=sum(r["bytes"] for r in erase),
                    native_partial_supported=False, partial_erase_available=False,
                    checksum_residue_policy="Plan after checksum correction. Include every sector "
                        "containing a correction word, even when far from the edited code.",
                    baseline_matches_connected_ECU=False,
                    checksums_validated_by_planner=False,
                    hardware_validated=False, flash_ready=False, blockers=list(PARTIAL_PLAN_LIMITS),
                    note="Sector payloads preserve every supplied neighbouring byte. File coverage "
                         "does not establish that these bytes match the connected ECU; this is "
                         "an offline minimum-sector plan, not a native partial-flash procedure.")


def plan_mpc_sectors(original_mpc: bytes, corrected_mpc: bytes) -> MpcSectorPlan:
    """Plan complete aligned sectors *after* correction; never fill missing bytes.

    No arithmetic validity claim is made by this low-level function. Use
    an external verified checksum adapter before calling this function.
    """
    original, corrected, spans = _validate_mpc_pair(original_mpc, corrected_mpc)
    starts = sorted({address for start, end in spans
                     for address in range(start//SECTOR_BYTES*SECTOR_BYTES,
                                          (end-1)//SECTOR_BYTES*SECTOR_BYTES+1, SECTOR_BYTES)})
    sectors = []
    for start in starts:
        end = start + SECTOR_BYTES
        intersections = tuple((max(start, a), min(end, b)) for a, b in spans
                              if a < end and b > start)
        payload = corrected[start:end]
        if len(payload) != SECTOR_BYTES:
            raise SectorPlanError("Incomplete sector contents; no FF padding is permitted.")
        sectors.append(MpcSector(start, payload, _sha(original[start:end]),
                                 sum(b-a for a, b in intersections), intersections))
    return MpcSectorPlan(len(original), _sha(original), _sha(corrected), spans, tuple(sectors))


def _fls_geometry_plan(original, edited):
    if not isinstance(original, (bytes, bytearray, memoryview)) or not isinstance(
            edited, (bytes, bytearray, memoryview)):
        raise SectorPlanError("Supply complete byte images; sparse records are not sector contents.")
    original, edited = bytes(original), bytes(edited)
    if len(original) != 0x100000 or len(edited) != 0x100000:
        raise SectorPlanError("Supply equal complete 1 MiB FLS images.")
    spans = changed_ranges(original, edited)
    sectors = []
    for runtime_start, runtime_end in FLS_SECTORS:
        start, end = runtime_start-FLS_BASE, runtime_end-FLS_BASE
        intersections = tuple((max(start, a), min(end, b)) for a, b in spans
                              if a < end and b > start)
        if intersections:
            sectors.append(MpcSector(start, edited[start:end], _sha(original[start:end]),
                                     sum(b-a for a, b in intersections), intersections))
    return MpcSectorPlan(len(original), _sha(original), _sha(edited), spans, tuple(sectors))


def plan_sectors(original: bytes, edited: bytes, space: str) -> dict:
    """Return JSON-only sector geometry for an already checksum-corrected image.

    MPC permits application-only changes. External FLS uses the exact nine
    hardware sectors, not 32 KiB subdivisions; the factory staging translation
    still prevents treating these physical offsets as an executable job plan.
    The report contains no payload bytes and never claims checksum validity.
    """
    if space == "MPC":
        return plan_mpc_sectors(original, edited).as_report()
    if space != "FLS":
        raise SectorPlanError("Select MPC or FLS explicitly.")
    plan = _fls_geometry_plan(original, edited)
    report = plan.as_report()
    report.update(address_space="FLS", sector_bytes=None,
                  format="DDE5-361830-FLS-sector-review-v1",
                  physical_sector_geometry=[dict(runtime_address=start, end_exclusive=end,
                                                 bytes=end-start) for start, end in FLS_SECTORS],
                  staging_note="Factory downloads at 0x808000–0x81FFFC are translated by +0xD8000; "
                               "physical-sector selection is not a downloadable address plan.")
    for row in report["sectors"]:
        row["address_space"] = "FLS"
        row["runtime_address"] = FLS_BASE+row["file_offset"]
        row["runtime_end_exclusive"] = FLS_BASE+row["end_exclusive"]
    for row in report["changed_ranges"]:
        row["runtime_address"] = FLS_BASE+row["file_offset"]
    return report


def sector_payloads(original: bytes, edited: bytes, space: str):
    """Internal preservation utility: (file offset, complete payload) tuples.

    Callers must not dispatch these bytes as a factory partial erase/write
    sequence. These copies are intended for offline reconstruction tests.
    """
    if space == "MPC":
        plan = plan_mpc_sectors(original, edited)
    elif space == "FLS":
        plan = _fls_geometry_plan(original, edited)
    else:
        raise SectorPlanError("Select MPC or FLS explicitly.")
    return tuple((sector.file_offset, sector.payload) for sector in plan.sectors)
