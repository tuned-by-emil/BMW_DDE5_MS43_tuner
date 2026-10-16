"""Analyze a BIN without changing it; optionally export unverified XDF findings."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from bmw_bin_editor.bin_analysis import analyze_maps, analysis_report, candidate_definition
from bmw_bin_editor.xdf_edit import XDFWorkspace


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('bin', type=Path)
    parser.add_argument('--report', required=True, type=Path)
    parser.add_argument('--xdf', type=Path)
    parser.add_argument('--endian', choices=('big', 'little'), default='big')
    parser.add_argument('--interpret', action='store_true', help='Opt into suggested units/scaling; all definitions remain unverified.')
    parser.add_argument('--minimum-family-score', type=int, default=75)
    parser.add_argument('--all-structures', action='store_true', help='Include unlabeled structures in the XDF too.')
    args = parser.parse_args()
    if not 0 <= args.minimum_family_score <= 100:
        parser.error('The minimum family score must be between 0 and 100.')
    if args.report.suffix.casefold() != '.json':
        parser.error('The report destination must be a .json file.')
    if args.xdf and args.xdf.suffix.casefold() != '.xdf':
        parser.error('The XDF destination must be a .xdf file.')
    if args.report.resolve() == args.bin.resolve() or (args.xdf and args.xdf.resolve() == args.bin.resolve()):
        parser.error('Analysis outputs must be separate from the source BIN.')
    data = args.bin.read_bytes()
    endian = '>' if args.endian == 'big' else '<'
    candidates = analyze_maps(data, endian=endian)
    report = analysis_report(data, candidates, endian=endian, source_name=args.bin.name)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    if args.xdf:
        workspace = XDFWorkspace(bin_size=len(data))
        workspace.set_metadata(args.bin.stem + ' — unverified map findings', 'Emil Larsson',
                               f"Read-only analysis of {args.bin.name}. Source BIN SHA-256: {report['sha256']}. "
                               'Automatic family labels and engineering conversions are hypotheses requiring verification.', endian == '<')
        exported = 0
        for candidate in candidates:
            family = candidate.suggestions[0] if candidate.suggestions else None
            if not args.all_structures and (family is None or family.score < args.minimum_family_score):
                continue
            workspace.add(candidate_definition(candidate, interpreted=args.interpret and family is not None,
                                               source_sha256=report['sha256']))
            exported += 1
        args.xdf.parent.mkdir(parents=True, exist_ok=True)
        workspace.save_as(args.xdf)
        print(f'Exported {exported} unverified definitions: {args.xdf}')
    print(f"{len(candidates)} structures; family suggestions: {report['family_counts']}")
    print(f"Source SHA-256: {report['sha256']}\nReport: {args.report}")


if __name__ == '__main__':
    main()
