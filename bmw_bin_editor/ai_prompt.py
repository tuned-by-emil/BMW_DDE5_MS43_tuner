"""Compact focused prompts while exact captured evidence stays in local tools.

Call only from focused/follow-up serialization with a private evidence session.
The capture, UI/history records, full-audit batches and tool results are unchanged.
"""
from __future__ import annotations


def compact_provider_context(public):
    result = dict(public)
    index = result.pop('table_index', [])
    result['table_catalog'] = {
        'available_tables': len(index),
        'addresses': [f"0x{entry['address']:X}" for entry in index if type(entry.get('address')) is int],
        'retrieval': 'editor.inspect_tables searches every captured title/description/address and returns exact axes, cells and comparison data. No table has been removed from local tools.',
        'not_an_audit_claim': 'The catalog reports availability, not that all maps were thoroughly reviewed.'}
    result.pop('tables', None)
    result.pop('follow_up_table_evidence', None)
    if isinstance(result.get('log'), dict):
        log = dict(result['log'])
        complete = bool(log.get('original_review_complete_rows', log.get('complete_rows')))
        log.update(rows=[], sent_rows=0, complete_rows=False,
            available_captured_rows=log.get('total_rows'),
            captured_complete_rows=complete,
            sampling='No raw observations in this focused prompt. editor.inspect_log retrieves exact captured rows for any interval; editor.calculate_run recomputes any selected run from those observations. Computed evidence retains its original full captured scope.')
        # Keep the entire deterministic analysis unchanged: all displayed
        # launches/shifts, peaks, coherent statistics, economy and assumptions.
        result['log'] = log
    result['evidence_delivery'] = 'Focused prompt: compact complete table-address catalog and unchanged deterministic log calculations. Exact cells and raw rows remain available through local tools. This is not a full-audit batch.'
    return result
