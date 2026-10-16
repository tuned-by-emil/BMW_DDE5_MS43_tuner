"""Offline DDE5 image checking, checksum correction and erase-sector planning."""
from __future__ import annotations
import hashlib

from .dde5_checks import (ImageMemory, ExternalFlashMemory, additive_checks, inspect_images,
                          inspect_external_flash, EXPECTED, MASK)
from .dde5_sector_plan import plan_sectors, changed_ranges


def _pair(image, space, companion):
    if companion is None:
        raise ValueError('MPC checksum checking requires its complete companion FLS image.')
    if space == 'MPC':
        return bytes(image), bytes(companion)
    if space == 'FLS':
        return bytes(companion), bytes(image)
    raise ValueError('Select MPC or external FLS explicitly')


def correct_loaded_image(edited, space, companion=None):
    """Correct only the selected image; the companion remains unchanged."""
    standalone=space=='CONFIG' or (space=='FLS' and companion is None)
    if standalone:
        config_only=space=='CONFIG'
        memory=ExternalFlashMemory(edited,config_only=config_only)
        # Validate the selected layout, headers and signature directory first.
        checks=inspect_external_flash(edited,config_only=config_only)['additive_checks']
    else:
        mpc, fls = _pair(edited, space, companion)
        memory = ImageMemory(mpc, fls)
        checks = additive_checks(memory)
    corrected = bytearray(edited)
    changes = []
    for record in checks:
        runtime = int(record['last_word'], 16)
        record_space = 'MPC' if runtime < 0x800000 else 'FLS'
        if (not standalone and record_space != space) or record['valid']:
            continue
        offset = runtime-memory.base if standalone else (runtime if space == 'MPC' else runtime-0x800000)
        before = int.from_bytes(corrected[offset:offset+4], 'big')
        after = (before+EXPECTED-int(record['calculated'], 16)) & MASK
        corrected[offset:offset+4] = after.to_bytes(4, 'big')
        changes.append(dict(space=space, file_offset=offset, runtime_address=runtime,
                            before=f'{before:08X}', after=f'{after:08X}'))
    result = bytes(corrected)
    if standalone:
        final_checks=inspect_external_flash(result,config_only=space=='CONFIG')['additive_checks']
    else:
        final_checks=additive_checks(ImageMemory(*_pair(result, space, companion)))
    for record in final_checks:
        record_space = 'MPC' if int(record['last_word'], 16) < 0x800000 else 'FLS'
        if (standalone or record_space == space) and not record['valid']:
            raise ValueError('Selected-image checksum correction did not validate')
    return result, changes


def _incomplete_config_plan(original,corrected):
    spans=changed_ranges(original,corrected)
    end=0x8e0000+len(corrected)
    missing=[dict(runtime_address=0x8c0000,end_exclusive=0x8e0000,bytes=0x20000)]
    if end<0x900000:
        missing.append(dict(runtime_address=end,end_exclusive=0x900000,bytes=0x900000-end))
    return dict(format='DDE5-361830-CONFIG-incomplete-sector-review-v1',address_space='CONFIG',
                image_size=len(corrected),file_runtime_base=0x8e0000,
                baseline_sha256=hashlib.sha256(original).hexdigest(),
                corrected_sha256=hashlib.sha256(corrected).hexdigest(),
                selected_sector_count=0,planned_sector_payload_bytes=0,sectors=[],
                changed_bytes=sum(b-a for a,b in spans),
                changed_ranges=[dict(file_offset=a,end_exclusive=b,bytes=b-a,runtime_address=0x8e0000+a) for a,b in spans],
                sector_bytes=None,payload_coverage_complete=False,
                physical_sector_geometry=[dict(runtime_address=0x8c0000,end_exclusive=0x900000,bytes=0x40000)],
                missing_sector_ranges=missing,native_partial_supported=False,partial_erase_available=False,
                native_program_erase_total_bytes=0xd0000,
                baseline_matches_connected_ECU=False,hardware_validated=False,flash_ready=False,
                blockers=['A raw calibration file omits neighbouring bytes in the physical 256 KiB sector.',
                          'Factory erase/write and complete-image acceptance are unvalidated.'],
                note='Checksum coverage is sufficient, but complete physical-sector payloads cannot be constructed from this file. Missing bytes are never padded.')


def review_loaded_image(original, edited, space, companion=None):
    """Plan the corrected candidate, while reporting its current validity too.

    Baseline bytes are a file snapshot. No connected ECU baseline is assumed.
    This review never exports images, runs vendor programs or sends ECU jobs.
    """
    if len(original) != len(edited):
        raise ValueError('Original and edited image sizes differ')
    if space=='CONFIG' or (space=='FLS' and companion is None):
        config_only=space=='CONFIG'
        baseline=inspect_external_flash(original,trusted_fls=original,config_only=config_only)
        current=inspect_external_flash(edited,trusted_fls=original,config_only=config_only)
        corrected,corrections=correct_loaded_image(edited,space)
        prepared=inspect_external_flash(corrected,trusted_fls=original,config_only=config_only)
        if len(original)==0x100000:
            plan=plan_sectors(original,corrected,'FLS')
            plan['payload_coverage_complete']=True
        else:
            plan=_incomplete_config_plan(original,corrected)
        return dict(format='BMWBinEditor-DDE5-361830-offline-review-v2',address_space=space,
                    checksum_scope=space,validation_scope=prepared['validation_scope'],
                    baseline=baseline,current=current,corrected_preview=prepared,
                    checksum_corrections=corrections,sector_plan=plan,
                    corrected_image_sha256=hashlib.sha256(corrected).hexdigest(),
                    complete_external_flash=prepared['complete_external_flash'],
                    complete_image_additive_valid=None,baseline_matches_connected_ECU=False,
                    flash_ready=False,authentication_calculation_implemented=True,
                    note='Arithmetic validity applies only to the selected supplied ranges. '
                         'Correction does not sign the image or establish ECU acceptance. '
                         'No hardware programming or recovery sequence has been validated.')
    baseline_pair = _pair(original, space, companion)
    # Validate baseline layout and source coverage before calculating a delta.
    baseline = inspect_images(*baseline_pair, trusted_mpc=baseline_pair[0], trusted_fls=baseline_pair[1])
    current_pair = _pair(edited, space, companion)
    current = inspect_images(*current_pair, trusted_mpc=baseline_pair[0], trusted_fls=baseline_pair[1])
    corrected, corrections = correct_loaded_image(edited, space, companion)
    corrected_pair = _pair(corrected, space, companion)
    prepared = inspect_images(*corrected_pair, trusted_mpc=baseline_pair[0], trusted_fls=baseline_pair[1])
    for validation in (baseline, current, prepared):
        validation['selected_space_additive_valid'] = all(
            row['valid'] for row in validation['additive_checks']
            if ('MPC' if int(row['last_word'], 16) < 0x800000 else 'FLS') == space)
    plan = plan_sectors(original, corrected, space)
    return dict(format='BMWBinEditor-DDE5-361830-offline-review-v1', address_space=space,
                baseline=baseline, current=current, corrected_preview=prepared,
                checksum_corrections=corrections, sector_plan=plan,
                corrected_image_sha256=hashlib.sha256(corrected).hexdigest(),
                baseline_matches_connected_ECU=False, flash_ready=False,
                authentication_calculation_implemented=True,
                note='The plan includes checksum-residue changes and complete sector bytes. '
                     'Factory routine 02 does not erase only the listed changed sectors. '
                     'Image signature validity and stored boot markers are separate acceptance gates. '
                     'No hardware programming or recovery sequence has been validated.')
