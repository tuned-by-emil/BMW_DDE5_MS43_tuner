"""Atomic application of reviewed AI cell proposals using the editor's undo stack."""
from __future__ import annotations
import math
from dataclasses import asdict
import hashlib
import json
from .ai_context import calibration_signature, clean
from .ai_evidence import validate_proposal
from .core import equation_affine, raw_limits

def apply_proposal(document, snapshot, proposal, *, compare_bytes=None, compare_name='', dry_run=False):
    if document is None:
        raise ValueError('Load the captured primary BIN before applying a proposal.')
    current = calibration_signature(document, None, compare_bytes, compare_name)
    if current != snapshot.get('calibration_signature'):
        raise ValueError('The BIN/XDF/comparison changed. Capture a fresh review before applying cells.')
    plan = validate_proposal(snapshot, proposal)
    writes=[]
    for change in plan['changes']:
        candidates=[d for d in document.xdf.tables if d.z_address==change['table_address'] and d.title==change['table_title']]
        if len(candidates)!=1:
            raise ValueError('The proposed table is not uniquely identified in the current XDF.')
        definition=candidates[0]; r,c=change['row'],change['col']
        definition_hash=hashlib.sha256(json.dumps(clean(asdict(definition)),sort_keys=True,allow_nan=False,separators=(",",":")).encode()).hexdigest()
        if definition_hash!=change.get('definition_sha256'):
            raise ValueError('The captured table definition differs from the actual current XDF.')
        if not 0<=r<definition.rows or not 0<=c<definition.cols:
            raise ValueError('The proposal contains an out-of-range cell.')
        captured_table=snapshot['tables'][change['table_index']]
        for key,axis in (('x_values',definition.x_axis),('y_values',definition.y_axis)):
            if clean(document.axis_values(axis))!=captured_table.get(key,[]):
                raise ValueError('The captured axes differ from the actual current BIN/XDF.')
        old=document.cell_value(definition,r,c)
        if not math.isclose(old,change['old'],rel_tol=1e-9,abs_tol=1e-6):
            raise ValueError('A proposed cell differs from its captured original value.')
        scale,offset=equation_affine(definition.z_equation)
        raw=int(round((change['new']-offset)/scale))
        lo,hi=raw_limits(definition.z_element_bits,definition.z_signed)
        if not lo<=raw<=hi:
            raise ValueError('A proposed value exceeds its raw storage range.')
        actual=document.raw_to_physical(raw,definition.z_equation)
        if not math.isclose(actual,change['new'],rel_tol=1e-9,abs_tol=max(1e-6,abs(scale)*1e-5)):
            raise ValueError('A proposed replacement does not match the decoded raw value.')
        writes.append((document.cell_address(definition,r,c),document._pack_one(raw,definition.z_element_bits,definition.z_signed)))
    if dry_run:return False,plan
    changed=document.apply_bytes('AI reviewed proposal: '+plan['label'],writes)
    return changed,plan
