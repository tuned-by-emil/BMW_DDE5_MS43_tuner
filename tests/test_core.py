
import unittest as _optional_unittest
from pathlib import Path as _OptionalPath
if not (_OptionalPath(__file__).resolve().parents[1] / 'definitions/EDC16C31_361830_v1_22_R005_REVIEW.xdf').is_file():
    raise _optional_unittest.SkipTest('Optional local calibration resource not installed: definitions/EDC16C31_361830_v1_22_R005_REVIEW.xdf')

from pathlib import Path
import tempfile
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bmw_bin_editor.core import XDFDatabase, CalibrationDocument, EditEngine, estimate_lambda_from_map_target

XDF = ROOT / "definitions" / "EDC16C31_361830_v1_22_R005_REVIEW.xdf"


def by_addr(xdf, addr):
    return next(d for d in xdf.tables if d.z_address == addr)


def main():
    xdf = XDFDatabase(XDF)
    assert len(xdf.tables) == 205, len(xdf.tables)
    assert len(xdf.constants) == 44, len(xdf.constants)

    # TunerPro category memberships are one-based; the XDF header indices are zero-based.
    # Multi-membership entries use the later membership as the specific display group.
    assert by_addr(xdf, 0xF9394).category == "Boost"
    assert by_addr(xdf, 0xF8900).category == "N75"
    assert by_addr(xdf, 0xF8900).categories == ["Boost", "N75"]
    assert by_addr(xdf, 0xE80EA).category == "Nm to IQ"
    assert by_addr(xdf, 0xEA80C).category == "SOI"
    assert by_addr(xdf, 0xE95F2).category == "Gear"
    known_categories = set(xdf.categories.values()) | {"Constants", "Uncategorized"}
    assert not (set(d.category for d in xdf.definitions) - known_categories)

    # V1.20: code-proven local axis for Boost Target B and transparent lambda helper.
    boost_b = by_addr(xdf, 0xF95D8)
    assert boost_b.x_axis.address == 0xF95B8
    assert boost_b.y_axis.address == 0xF9598
    air_mg, lam = estimate_lambda_from_map_target(1120.0, 20.0)
    assert 555.0 < air_mg < 566.0
    assert 1.90 < lam < 1.97
    assert by_addr(xdf, 0xEEBE8).z_units == "mg/inj"
    assert by_addr(xdf, 0xEF114).z_units == "mg/inj"
    assert by_addr(xdf, 0xF1A36).z_units == "mg/inj"

    with tempfile.TemporaryDirectory() as td:
        bin_path = Path(td) / "synthetic_1MiB.bin"
        bin_path.write_bytes(bytes(0x100000))
        doc = CalibrationDocument(xdf, bin_path)
        assert len(doc.data) == 0x100000

        d = next(d for d in xdf.tables if d.z_address == 0xE13AE)
        assert (d.rows, d.cols) == (12, 16)
        raw0 = doc.map_raw(d)[0][0]
        value0 = doc.map_values(d)[0][0]
        assert abs(value0 - raw0 * 0.1) < 1e-9

        eng = EditEngine(doc, d)
        eng.selection.set_single(0, 0)
        assert eng.nudge(d.base_step)
        assert doc.map_raw(d)[0][0] == raw0 + 1
        assert doc.undo()
        assert doc.map_raw(d)[0][0] == raw0
        assert doc.redo()
        assert doc.map_raw(d)[0][0] == raw0 + 1

        eng.selection.set_rect(0, 0, 1, 3)
        eng.interpolate_horizontal()
        eng.smooth()

        # Linked-map absolute mode: boost target A edit is copied exactly to B/C.
        boost_a = by_addr(xdf, 0xF9394)
        boost_b = by_addr(xdf, 0xF95D8)
        boost_c = by_addr(xdf, 0xF981C)
        doc.set_cells(boost_a, {(0, 0): 1500}, "seed a")
        doc.set_cells(boost_b, {(0, 0): 1600}, "seed b")
        doc.set_cells(boost_c, {(0, 0): 1700}, "seed c")
        doc.undo_stack.clear(); doc.redo_stack.clear()
        abs_eng = EditEngine(doc, boost_a)
        abs_eng.configure_links([boost_b, boost_c], "absolute")
        abs_eng.selection.set_single(0, 0)
        assert abs_eng.set_value(1800)
        assert doc.map_values(boost_a)[0][0] == 1800
        assert doc.map_values(boost_b)[0][0] == 1800
        assert doc.map_values(boost_c)[0][0] == 1800
        assert len(doc.undo_stack) == 1
        doc.undo()
        assert doc.map_values(boost_a)[0][0] == 1500
        assert doc.map_values(boost_b)[0][0] == 1600
        assert doc.map_values(boost_c)[0][0] == 1700

        # Linked-map relative mode: preserve different N75 levels by applying % factor.
        n75_a = by_addr(xdf, 0xF7F44)
        n75_b = by_addr(xdf, 0xF8478)
        n75_c = by_addr(xdf, 0xF86BC)
        n75_d = by_addr(xdf, 0xF8900)
        for definition, value in zip((n75_a, n75_b, n75_c, n75_d), (50.0, 55.0, 60.0, 65.0)):
            doc.set_cells(definition, {(0, 0): value}, "seed n75")
        doc.undo_stack.clear(); doc.redo_stack.clear()
        rel_eng = EditEngine(doc, n75_a)
        rel_eng.configure_links([n75_b, n75_c, n75_d], "relative")
        rel_eng.selection.set_single(0, 0)
        assert rel_eng.multiply(0.90)
        vals = [doc.map_values(definition)[0][0] for definition in (n75_a, n75_b, n75_c, n75_d)]
        expected = [45.0, 49.5, 54.0, 58.5]
        for got, want in zip(vals, expected):
            assert abs(got - want) <= 0.011, (got, want)
        assert len(doc.undo_stack) == 1
        doc.undo()
        vals = [doc.map_values(definition)[0][0] for definition in (n75_a, n75_b, n75_c, n75_d)]
        for got, want in zip(vals, (50.0, 55.0, 60.0, 65.0)):
            assert abs(got - want) <= 0.011, (got, want)

        # V1.07 safety: relative propagation never crosses zero. An accidental
        # source 50 -> 0 edits only the source, leaving linked maps intact; typing
        # 50 back from zero also leaves linked maps intact instead of dividing by zero.
        doc.undo_stack.clear(); doc.redo_stack.clear()
        assert rel_eng.set_value(0.0)
        vals = [doc.map_values(definition)[0][0] for definition in (n75_a, n75_b, n75_c, n75_d)]
        for got, want in zip(vals, (0.0, 55.0, 60.0, 65.0)):
            assert abs(got - want) <= 0.011, (got, want)
        assert rel_eng.last_warnings
        assert rel_eng.set_value(50.0)
        vals = [doc.map_values(definition)[0][0] for definition in (n75_a, n75_b, n75_c, n75_d)]
        for got, want in zip(vals, (50.0, 55.0, 60.0, 65.0)):
            assert abs(got - want) <= 0.011, (got, want)

        out = Path(td) / "edited.bin"
        doc.save_as(out)
        assert out.stat().st_size == 0x100000
        assert doc.path == out
        assert not doc.dirty

        # Saved-state tracking is separate from immutable original comparison.
        saved_raw = doc.map_raw(boost_a)[0][0]
        post_save = EditEngine(doc, boost_a)
        post_save.selection.set_single(0, 0)
        assert post_save.nudge(boost_a.base_step)
        assert doc.dirty
        assert doc.undo()
        assert doc.map_raw(boost_a)[0][0] == saved_raw
        assert not doc.dirty

    print("PASS")
    print(f"XDF: {len(xdf.tables)} tables, {len(xdf.constants)} constants")
    print("XDF category decoding test: PASS")
    print("Linked absolute boost-target test: PASS")
    print("Linked relative N75 percentage/zero-safety test: PASS")
    print("Saved-state dirty tracking test: PASS")


if __name__ == "__main__":
    main()
