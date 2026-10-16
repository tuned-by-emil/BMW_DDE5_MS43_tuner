"""Analysis/export integration stays read-only and keeps interpretations opt-in."""
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from bmw_bin_editor.bin_analysis import analyze_maps, analysis_report, bin_metadata, candidate_definition
from bmw_bin_editor.core import CalibrationDocument, EditorError, XDFDatabase, safe_math
from bmw_bin_editor.xdf_edit import XDFWorkspace


EVIDENCE = b'BOSCH EDC16CP99/SYNTH\x00 0281234567 1037654321\x00'
RPM = [500, 750, 1000, 1500, 2000, 2500, 3500, 5000]
LOAD = [0, 500, 1000, 2000, 3500, 5000, 6500, 7500]
PEDAL = [0, 819, 1065, 1638, 2458, 4096, 5734, 8192]


def fixture(*, family="Driver's wish", endian='>'):
    """Contiguous dimension/axis/data layout with synthetic embedded evidence."""
    prefix = EVIDENCE.ljust(128, b'\xff')
    if family is None:
        rows, cols, x, y = 3, 4, [0, 100, 200, 300], [100, 200, 300]
        grid = np.arange(12).reshape(rows, cols) * 10 + 1000
    else:
        rows = cols = 8
        r, l = np.meshgrid(np.linspace(0, 1, rows), np.linspace(0, 1, cols), indexing='ij')
        y, x = RPM, LOAD
        if family == "Driver's wish":
            x = PEDAL
            grid = 5400 * (np.array(PEDAL) / 8192)[None, :] ** 1.3 * (1-.2*r)
        elif family == 'Main SOI':
            grid = np.round((-2+22*r+4*l)*10)/10 * 128/3
        else:
            raise ValueError(family)
    words = [rows, cols] + y + x + [int(round(v)) & 0xffff for v in grid.ravel()]
    data = prefix + struct.pack(endian + 'H' * len(words), *words) + b'\xff' * 96
    return data, len(prefix) + (2+rows+cols)*2


def target(candidates, address):
    return next(c for c in candidates if c.address == address)


class AnalysisTests(unittest.TestCase):
    def test_embedded_metadata_is_offset_evidence_and_snapshot_hash(self):
        data, _ = fixture()
        metadata = bin_metadata(data)
        self.assertEqual(metadata['byte_length'], len(data))
        self.assertEqual(metadata['sha256'], hashlib.sha256(data).hexdigest())
        self.assertEqual(metadata['identification_evidence'], [{'offset': 0, 'text': 'BOSCH EDC16CP99/SYNTH'}])
        self.assertEqual(metadata['bosch_numbers'], ['0281234567'])
        self.assertEqual(metadata['software_numbers'], ['1037654321'])
        self.assertNotIn('ecu_identity', metadata)
        duplicate = data + EVIDENCE
        self.assertEqual(bin_metadata(duplicate)['identification_evidence'], metadata['identification_evidence'])

    def test_pipeline_classifies_without_mutating_data_and_reports_progress(self):
        data, address = fixture()
        mutable = bytearray(data)
        progress = []
        candidates = analyze_maps(mutable, endian='>', progress=progress.append)
        candidate = target(candidates, address)
        self.assertEqual(candidate.suggestions[0].family, "Driver's wish")
        self.assertTrue(0 <= candidate.score <= 100)
        self.assertTrue(0 <= candidate.suggestions[0].score <= 100)
        self.assertEqual(bytes(mutable), data)
        self.assertEqual((progress[0], progress[-1]), (0, 100))
        self.assertEqual(progress, sorted(progress))

    def test_raw_and_opt_in_definitions_preserve_distinct_units_and_signedness(self):
        for family in ("Driver's wish", 'Main SOI'):
            with self.subTest(family=family):
                data, address = fixture(family=family)
                candidate = target(analyze_maps(data, scaling_profiles=[]), address)
                before = asdict(candidate)
                digest = hashlib.sha256(data).hexdigest()
                raw = candidate_definition(candidate, source_sha256=digest)
                interpreted = candidate_definition(candidate, interpreted=True, source_sha256=digest)
                self.assertEqual((raw.z_equation, raw.x_axis.equation, raw.y_axis.equation), ('X', 'X', 'X'))
                self.assertEqual((raw.z_units, raw.x_axis.units, raw.y_axis.units), ('', '', ''))
                self.assertEqual(raw.z_signed, family == 'Main SOI')
                self.assertIn('raw, unverified', raw.title)
                self.assertIn('RAW definition retained', raw.description)
                self.assertIn('unverified', interpreted.title)
                self.assertEqual(interpreted.z_units, 'Nm' if family == "Driver's wish" else 'deg')
                self.assertEqual(interpreted.z_signed, family == 'Main SOI')
                self.assertNotEqual(interpreted.z_equation, 'X')
                self.assertEqual(interpreted.y_axis.units, 'rpm')
                for definition in (raw, interpreted):
                    self.assertIn('UNVERIFIED', definition.description)
                    self.assertIn(digest, definition.description)
                    self.assertEqual((definition.z_address, definition.rows, definition.cols),
                                     (candidate.address, candidate.rows, candidate.cols))
                self.assertEqual(asdict(candidate), before)

    @unittest.skipUnless((ROOT / 'definitions/library/manifest.json').is_file(), 'Optional local scaling library is not installed')
    def test_default_pipeline_transfers_library_scaling_and_storage(self):
        for family, units in (("Driver's wish", 'Nm'), ('Main SOI', 'deg')):
            with self.subTest(family=family):
                data, address = fixture(family=family)
                candidate = target(analyze_maps(data), address)
                suggestion = candidate.suggestions[0]
                self.assertEqual(suggestion.family, family)
                self.assertGreaterEqual(suggestion.scaling_profile_count, 6)
                definition = candidate_definition(candidate, interpreted=True)
                self.assertTrue(definition.z_signed)
                self.assertEqual(definition.z_units, units)
                self.assertEqual(definition.y_axis.units, 'rpm')
                if family == 'Main SOI':
                    self.assertAlmostEqual(safe_math(definition.z_equation, -77), -77*3/128)

    def test_unlabeled_candidate_stays_raw_and_cannot_be_interpreted(self):
        data, address = fixture(family=None)
        candidate = target(analyze_maps(data), address)
        self.assertEqual(candidate.suggestions, [])
        raw = candidate_definition(candidate)
        self.assertEqual(raw.category, 'Suspected maps')
        self.assertEqual((raw.z_equation, raw.z_units, raw.z_signed), ('X', '', False))
        with self.assertRaises(EditorError):
            candidate_definition(candidate, interpreted=True)

    def test_raw_soi_signed_round_trip_and_negative_edit_undo(self):
        for endian in ('>', '<'):
            with self.subTest(endian=endian), tempfile.TemporaryDirectory() as folder:
                data, address = fixture(family='Main SOI', endian=endian)
                candidate = target(analyze_maps(data, endian=endian), address)
                definition = candidate_definition(candidate)
                self.assertTrue(definition.z_signed)
                self.assertEqual(definition.z_equation, 'X')
                raw = struct.unpack_from(endian+'h', data, address)[0]
                self.assertLess(raw, 0)
                directory = Path(folder)
                source = directory/'signed.bin'; source.write_bytes(data)
                workspace = XDFWorkspace(bin_size=len(data))
                workspace.set_metadata('Signed timing regression', '', '', endian == '<')
                workspace.add(definition)
                database = workspace.save_as(directory/'signed.xdf')
                reloaded = XDFDatabase(database.path)
                definition = reloaded.tables[0]
                document = CalibrationDocument(reloaded, source)
                self.assertEqual(document.map_raw(definition)[0][0], raw)
                self.assertEqual(document.map_values(definition)[0][0], raw)
                document.set_cells(definition, {(0, 0): -77}, 'Negative timing')
                self.assertEqual(document.map_values(definition)[0][0], -77)
                self.assertEqual(bytes(document.data[address:address+2]), struct.pack(endian+'h', -77))
                document.undo()
                self.assertEqual(bytes(document.data), data)
                self.assertEqual(source.read_bytes(), data)

    def test_json_report_keeps_structural_and_semantic_evidence_separate(self):
        data, address = fixture(endian='<')
        candidates = analyze_maps(data, endian='<')
        report = json.loads(json.dumps(analysis_report(data, candidates, endian='<', source_name='synthetic.bin')))
        self.assertEqual(report['source_name'], 'synthetic.bin')
        self.assertEqual(report['byte_order'], 'little')
        self.assertEqual(report['sha256'], hashlib.sha256(data).hexdigest())
        self.assertEqual(report['structural_candidate_count'], len(candidates))
        self.assertGreaterEqual(report['family_counts']["Driver's wish"], 1)
        serialized = next(c for c in report['candidates'] if c['address'] == address)
        original = target(candidates, address)
        self.assertEqual(serialized['score'], original.score)
        self.assertEqual(serialized['suggestions'][0]['score'], original.suggestions[0].score)
        self.assertEqual(serialized['suggestions'][0]['family'], "Driver's wish")
        self.assertIn('not probability', report['interpretation'])
        self.assertFalse(serialized['signed'])

    def test_cancellation_before_scan_and_before_semantics_never_completes(self):
        data, _ = fixture()
        before = hashlib.sha256(data).hexdigest()
        progress = []
        self.assertEqual(analyze_maps(data, cancelled=lambda: True, progress=progress.append), [])
        self.assertNotIn(100, progress)
        progress.clear()
        stop = [False]
        def update(value):
            progress.append(value)
            if value >= 75:
                stop[0] = True
        candidates = analyze_maps(data, cancelled=lambda: stop[0], progress=update)
        self.assertTrue(candidates)
        self.assertTrue(all(not candidate.suggestions for candidate in candidates))
        self.assertNotIn(100, progress)
        self.assertEqual(hashlib.sha256(data).hexdigest(), before)


class AnalysisCliTests(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(ROOT / 'analyze_bin.py'), *map(str, args)],
                              cwd=ROOT, capture_output=True, text=True, timeout=30)

    def test_cli_raw_and_interpreted_xdf_exports_keep_source_unchanged(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            data, address = fixture(family='Main SOI', endian='<')
            source = directory / 'synthetic.bin'
            source.write_bytes(data)
            for interpreted in (False, True):
                report_path = directory / f'report-{interpreted}.json'
                xdf_path = directory / f'findings-{interpreted}.xdf'
                args = [source, '--report', report_path, '--xdf', xdf_path, '--endian', 'little']
                result = self.run_cli(*(args + (['--interpret'] if interpreted else [])))
                self.assertEqual(result.returncode, 0, result.stderr)
                report = json.loads(report_path.read_text(encoding='utf-8'))
                self.assertEqual(report['sha256'], hashlib.sha256(data).hexdigest())
                self.assertEqual(report['source_name'], source.name)
                xdf = XDFDatabase(xdf_path)
                self.assertTrue(xdf.default_lsb_first)
                self.assertEqual(xdf.root.findtext('./XDFHEADER/author'), 'Emil Larsson')
                definition = next(d for d in xdf.tables if d.z_address == address)
                self.assertTrue(definition.z_signed)
                self.assertEqual(definition.z_units, 'deg' if interpreted else '')
                self.assertEqual(definition.z_equation, 'X * 3 / 128' if interpreted else 'X')
                self.assertIn(report['sha256'], definition.description)
                self.assertIn(report['sha256'], xdf.root.findtext('./XDFHEADER/description'))
                self.assertEqual(source.read_bytes(), data)

    def test_cli_all_structures_keeps_unlabeled_exports_raw_even_with_interpret(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            data, address = fixture(family=None)
            source = directory / 'unlabeled.bin'
            source.write_bytes(data)
            for include in (False, True):
                report = directory / f'report-{include}.json'
                xdf_path = directory / f'findings-{include}.xdf'
                args = [source, '--report', report, '--xdf', xdf_path, '--interpret']
                result = self.run_cli(*(args + (['--all-structures'] if include else [])))
                self.assertEqual(result.returncode, 0, result.stderr)
                definitions = XDFDatabase(xdf_path).definitions
                if include:
                    definition = next(d for d in definitions if d.z_address == address)
                    self.assertEqual((definition.z_equation, definition.z_units, definition.z_signed), ('X', '', False))
                else:
                    self.assertEqual(definitions, [])
                self.assertEqual(source.read_bytes(), data)

    def test_cli_rejects_invalid_destinations_before_writing(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            data, _ = fixture()
            for case in ('report_suffix', 'xdf_suffix', 'report_is_source', 'xdf_is_source'):
                with self.subTest(case=case):
                    suffix = '.json' if case == 'report_is_source' else '.xdf' if case == 'xdf_is_source' else '.bin'
                    source = directory / ('input-' + case + suffix)
                    source.write_bytes(data)
                    report = source if case == 'report_is_source' else directory / (case + ('.bin' if case == 'report_suffix' else '.json'))
                    xdf = source if case == 'xdf_is_source' else directory / (case + ('.bin' if case == 'xdf_suffix' else '.xdf'))
                    result = self.run_cli(source, '--report', report, '--xdf', xdf)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn('error:', result.stderr)
                    self.assertEqual(source.read_bytes(), data)
                    if report != source:
                        self.assertFalse(report.exists())
                    if xdf != source:
                        self.assertFalse(xdf.exists())

    def test_cli_rejects_out_of_range_family_threshold_without_outputs(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            data, _ = fixture()
            source = directory / 'input.bin'
            source.write_bytes(data)
            for threshold in (-1, 101):
                report = directory / f'invalid-{threshold}.json'
                xdf = directory / f'invalid-{threshold}.xdf'
                result = self.run_cli(source, '--report', report, '--xdf', xdf, '--minimum-family-score', threshold)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('error:', result.stderr)
                self.assertFalse(report.exists())
                self.assertFalse(xdf.exists())
                self.assertEqual(source.read_bytes(), data)


if __name__ == '__main__':
    unittest.main(verbosity=2)
