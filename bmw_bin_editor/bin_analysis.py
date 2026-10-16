"""Read-only BIN analysis and explicit export of unverified interpretations."""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict
import hashlib
import re

from .core import EditorError
from .map_finder import scan_maps
from .map_classification import classify_candidate


def bin_metadata(data: bytes) -> dict:
    """Report strings found in the file as evidence, without declaring ECU identity."""
    identifications = []
    for match in re.finditer(rb'(?:BOSCH )?EDC[0-9]{2}[A-Za-z0-9/_. -]{1,70}', data):
        value = match.group().decode('ascii').strip()
        if value not in [item['text'] for item in identifications]:
            identifications.append({'offset': match.start(), 'text': value})
        if len(identifications) >= 12:
            break
    numbers = {}
    for name, pattern in (('bosch_numbers', rb'(?<![0-9])028[0-9]{7}(?![0-9])'),
                          ('software_numbers', rb'(?<![0-9])103[0-9]{7}(?![0-9])')):
        numbers[name] = sorted({match.group().decode('ascii') for match in re.finditer(pattern, data)})[:12]
    return dict(byte_length=len(data), sha256=hashlib.sha256(data).hexdigest(),
                identification_evidence=identifications, **numbers)


def analyze_maps(data: bytes, *, progress=None, cancelled=None, scaling_profiles=None, **options):
    """Keep structural and semantic scores separate; leave uncertain maps unlabeled."""
    candidates = scan_maps(data, progress=(lambda p: progress(p * 3 // 4)) if progress else None,
                           cancelled=cancelled, **options)
    if scaling_profiles is None and candidates and not (cancelled and cancelled()):
        from .scaling_evidence import load_scaling_profiles
        scaling_profiles = load_scaling_profiles()
    for index, candidate in enumerate(candidates):
        if cancelled and cancelled():
            break
        candidate.suggestions = classify_candidate(data, candidate, endian=options.get('endian', '>'), scaling_profiles=scaling_profiles)
        if progress and index % 16 == 0:
            progress(75 + 25 * (index + 1) // max(1, len(candidates)))
    if progress and not (cancelled and cancelled()):
        progress(100)
    return candidates


def candidate_definition(candidate, *, interpreted=False, suggestion_index=0, source_sha256=None):
    suggestions = candidate.suggestions
    suggestion = suggestions[suggestion_index] if suggestions else None
    if interpreted and suggestion is None:
        raise EditorError('There is no family interpretation for this candidate.')
    if interpreted:
        definition = suggestion.to_definition(candidate)
    else:
        definition = candidate.to_definition()
        if suggestion:
            definition.title = f"0x{candidate.address:06X} Likely {suggestion.family} (raw, unverified)"
            definition.category = f"Suspected {suggestion.family}"
            definition.categories = [definition.category]
            if suggestion.family == 'Main SOI':
                # Storage interpretation is independent of engineering scale.
                # Keeping X must not wrap negative timing words to 65xxx.
                definition.z_signed = suggestion.z_signed
    if suggestions:
        definition.description += '\n\nAutomatic family suggestions (heuristic ranking, not proof):\n'
        for item in suggestions:
            definition.description += f'{item.family}: {item.score}/100\n' + '\n'.join(item.reasons) + '\n'
        if not interpreted:
            definition.description += 'RAW definition retained. Suggested units and scaling have not been applied.\n'
            if suggestion.family == 'Main SOI':
                definition.description += 'SOI raw values retain the suggested signed integer storage; engineering scaling remains X. Signedness remains unverified.\n'
    if source_sha256:
        definition.description += f'\nSource BIN snapshot SHA-256: {source_sha256}\n'
    return definition


def analysis_report(data: bytes, candidates, *, endian='>', source_name='') -> dict:
    counts = Counter(item.suggestions[0].family for item in candidates if item.suggestions)
    return dict(source_name=source_name, **bin_metadata(data),
                byte_order='big' if endian == '>' else 'little',
                structural_candidate_count=len(candidates), family_counts=dict(counts),
                interpretation='All functions, units, scaling and signedness are unverified hypotheses. Scores rank evidence, not probability.',
                candidates=[asdict(item) for item in candidates])
