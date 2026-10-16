"""Offline checksum and signature inspection for the evidenced E46 DDE5 layout.

This module neither communicates with an ECU nor declares an image flash-ready.
All checks use supplied bytes; absent factory-record bytes are never filled in.
The public verification key is anchored to the original preserved boot image.
"""
from __future__ import annotations
import hashlib, struct

MASK=0xffffffff
SEED=0xfadecafe
EXPECTED=0xcafeaffe
MARKERS=(0x4fb0,0x817c,0x2003c,0x80829c,0x8082c4,0x82003c,0x8e0280)
SPANS=((0x0,0x7fbf,0x4084),(0x8000,0x1feff,0x108),
    (0x20000,0x6ffff,0x108),(0x808180,0x81fff7,0x108),
    (0x808000,0x80803f,0x108),(0x820000,0x85fffb,0x108),
    (0x8e0180,0x8feffb,0x20064))
CONFIG_BASE=0x8e0000
CONFIG_IMAGE_SIZES=(0x1f000,0x20000)
SIGNATURE_SPANS={
    0x808000:((0x20000,0x6ffff),(0x808000,0x80803f),
              (0x808180,0x81fffb),(0x820000,0x85ffff)),
    CONFIG_BASE:((CONFIG_BASE,0x8e003f),(0x8e0180,0x8fefff)),
}


class CoverageError(ValueError):pass


class ImageMemory:
    def __init__(self,mpc:bytes,fls:bytes):
        if len(mpc) not in (0x70000,0x70200) or len(fls)!=0x100000:
            raise ValueError('Supply a 458752/459264-byte MPC image and 1 MiB FLS image.')
        self.mpc,self.fls=bytes(mpc),bytes(fls)

    def read(self,address,size):
        if size<0:raise ValueError('Negative byte count')
        if 0<=address and address+size<=len(self.mpc):
            return self.mpc[address:address+size]
        if 0x800000<=address and address+size<=0x900000:
            return self.fls[address-0x800000:address-0x800000+size]
        raise CoverageError(f'Uncovered runtime bytes {address:#x}..{address+size:#x}')


class ExternalFlashMemory:
    """Exact file coverage, without an invented MPC or erased-byte padding."""
    def __init__(self,fls:bytes,*,config_only=False):
        self.fls=bytes(fls)
        if len(self.fls)==0x100000:
            self.base=0x800000
            self.complete_external_flash=True
        elif config_only and len(self.fls) in CONFIG_IMAGE_SIZES:
            self.base=CONFIG_BASE
            self.complete_external_flash=False
        else:
            if config_only:
                raise ValueError('Supply a complete 1 MiB FLS or an exact 124/128 KiB E46 calibration image based at 0x8E0000.')
            raise ValueError('Standalone FLS checking requires a complete 1 MiB external flash image.')

    def read(self,address,size):
        if size<0:raise ValueError('Negative byte count')
        offset=address-self.base
        if offset>=0 and offset+size<=len(self.fls):
            return self.fls[offset:offset+size]
        raise CoverageError(f'Uncovered runtime bytes {address:#x}..{address+size:#x}')


def validate_config_layout(memory):
    """Reject unsupported calibration headers before calculating any correction."""
    expected=bytes.fromhex(
        '5a5a5a5a cccccccc 008e0080 008e0040 008e00c0 008e0100 ffffffff 008feffc')
    if memory.read(CONFIG_BASE,32)!=expected:
        raise ValueError('Calibration header or byte order differs from the evidenced E46 layout at 0x8E0000.')
    if u32(memory,CONFIG_BASE+0x100)!=16:
        raise ValueError('Calibration signature word count differs from the evidenced E46 layout.')
    count=u32(memory,CONFIG_BASE+0x180)
    ranges=tuple(struct.unpack('>II',memory.read(CONFIG_BASE+0x184+8*i,8))
                 for i in range(count)) if count==2 else ()
    if ranges!=SIGNATURE_SPANS[CONFIG_BASE]:
        raise ValueError('Calibration MD5 directory differs from the evidenced E46 layout.')


def u32(memory,address):return int.from_bytes(memory.read(address,4),'big')


def bosch_integer(blob):
    count=int.from_bytes(blob[:4],'big')
    if not 1<=count<=16 or len(blob)<4+count*4:
        raise ValueError('Invalid Bosch integer word count')
    return sum(int.from_bytes(blob[4+4*i:8+4*i],'big')<<(32*i) for i in range(count))


def sum_words(blob,seed=SEED):
    if len(blob)%4:raise ValueError('Checksum byte count must be divisible by four')
    return (seed+sum(w[0] for w in struct.iter_unpack('>I',blob)))&MASK


def additive_checks(memory,*,scope='ALL'):
    indices={'ALL':range(7),'FLS':range(3,7),'CONFIG':(6,)}.get(scope)
    if indices is None:raise ValueError('Select ALL, FLS or CONFIG checksum scope explicitly.')
    result=[]
    for index in indices:
        marker,expected_span=MARKERS[index],SPANS[index]
        if memory.read(marker,8)!=bytes.fromhex('fadecafecafeaffe'):
            raise ValueError(f'Layout differs at descriptor {marker:#x}; this verifier supports the evidenced E46 layout.')
        start,end=struct.unpack('>II',memory.read(marker-8,8))
        if (start,end,u32(memory,marker+16))!=expected_span or memory.read(marker-15,3)!=b'\x00\x01\x01':
            raise ValueError(f'Checksum span/evaluator differs from evidenced E46 layout at {marker:#x}')
        if start%4 or end<start:
            raise ValueError(f'Invalid checksum span at {marker:#x}')
        # Raw evaluator 0x4084 reads BE32 while the word pointer <= end.
        stop=(end&~3)+4
        calculated=sum_words(memory.read(start,stop-start))
        result.append(dict(descriptor=f'{marker:#x}',start=f'{start:#x}',end_inclusive=f'{end:#x}',
            last_word=f'{stop-4:#x}',bytes=stop-start,calculated=f'{calculated:08X}',expected=f'{EXPECTED:08X}',valid=calculated==EXPECTED))
    return result


def repair_additive_in_memory(mpc,fls):
    """Return arithmetic-corrected byte copies; does not re-sign or write files."""
    memory=ImageMemory(mpc,fls)
    records=additive_checks(memory)
    copies={'MPC':bytearray(mpc),'FLS':bytearray(fls)}
    changes=[]
    for record in records:
        if record['valid']:continue
        runtime=int(record['last_word'],16)
        space='MPC' if runtime<0x800000 else 'FLS'
        offset=runtime if space=='MPC' else runtime-0x800000
        before=int.from_bytes(copies[space][offset:offset+4],'big')
        delta=(EXPECTED-int(record['calculated'],16))&MASK
        after=(before+delta)&MASK
        copies[space][offset:offset+4]=after.to_bytes(4,'big')
        changes.append(dict(space=space,file_offset=f'{offset:#x}',runtime_address=f'{runtime:#x}',
            before=f'{before:08X}',after=f'{after:08X}',delta=f'{delta:08X}'))
    output=(bytes(copies['MPC']),bytes(copies['FLS']))
    if not all(c['valid'] for c in additive_checks(ImageMemory(*output))):
        raise ValueError('Arithmetic repair did not validate; checksum ranges may overlap or differ')
    return output[0],output[1],changes


def signature_check(memory,trusted_memory,header):
    # Header +14 is the signature pointer. Handler 80BD58 selects the region
    # directory at the fixed header-relative +180; +18 is a different field.
    signature=u32(memory,header+0x14)
    directory=header+0x180
    expected_pair={0x808000:(0x808100,0x808180),0x8e0000:(0x8e0100,0x8e0180)}
    if (signature,directory)!=expected_pair[header]:
        raise ValueError('Signature/header placement differs from the evidenced E46 layout')
    raw=memory.read(signature,68)
    integer=bosch_integer(raw)
    count=u32(memory,directory)
    expected_ranges=SIGNATURE_SPANS[header]
    if count!=len(expected_ranges):raise ValueError('Digest range count differs from the evidenced E46 layout')
    ranges=tuple(struct.unpack('>II',memory.read(directory+4+8*i,8)) for i in range(count))
    if ranges!=expected_ranges:raise ValueError('MD5 ranges differ from the evidenced E46 layout')
    hasher=hashlib.md5();spans=[];missing=[]
    for start,end in ranges:
        spans.append(dict(start=f'{start:#x}',end_inclusive=f'{end:#x}',bytes=end-start+1))
        try:data=memory.read(start,end-start+1)
        except CoverageError:
            missing.append(spans[-1]);continue
        hasher.update(data)
    common=dict(signature_address=f'{signature:#x}',directory=f'{directory:#x}',
                algorithm='MD5 + raw RSA-512, exponent 7',ranges=spans)
    if missing:
        return dict(common,valid=None,status='skipped',reason='Signed image bytes are outside the supplied files.',
                    missing_ranges=missing,actual_md5=None)
    digest=hasher.digest()
    try:
        if trusted_memory is None:raise CoverageError('No trusted public key bytes supplied')
        key=trusted_memory.read(0x808588,68)
        exponent_bytes=trusted_memory.read(0x8085d0,8)
    except CoverageError:
        reason=('No trusted image verification public key has been supplied.' if trusted_memory is None
                else 'Image verification public key is outside the supplied calibration file.')
        return dict(common,valid=None,status='not_checked',reason=reason,
                    missing_ranges=[dict(start='0x808588',end_inclusive='0x8085cb',bytes=68),
                                    dict(start='0x8085d0',end_inclusive='0x8085d7',bytes=8)],actual_md5=digest.hex())
    modulus=bosch_integer(key)
    exponent=bosch_integer(exponent_bytes)
    if modulus.bit_length()!=512 or exponent!=7:
        raise ValueError('Trusted boot image does not contain the evidenced verification key')
    recovered=pow(integer,exponent,modulus)
    # Verifier 809D40 also requires normalized result count ==4 words.
    word_count=(recovered.bit_length()+31)//32
    decoded=recovered.to_bytes(64,'little')[:16]
    return dict(common,status='checked',modulus_sha256=hashlib.sha256(key).hexdigest(),
        actual_md5=digest.hex(),signed_md5=decoded.hex(),decrypted_word_count=word_count,
        signature_integer_below_modulus=integer<modulus,
        valid=word_count==4 and decoded==digest,ranges=spans)


def inspect_external_flash(fls,*,trusted_fls=None,config_only=False):
    """Inspect the supplied external/config flash scope without MPC bytes."""
    memory=ExternalFlashMemory(fls,config_only=config_only)
    trusted=ExternalFlashMemory(trusted_fls,config_only=config_only) if trusted_fls is not None else None
    validate_config_layout(memory)
    scope='CONFIG' if config_only else 'FLS'
    checks=additive_checks(memory,scope=scope)
    calibration=signature_check(memory,trusted,CONFIG_BASE)
    if config_only:
        program=dict(valid=None,status='skipped',reason='Program signature is outside the selected CONFIG checksum scope.')
    else:
        program=signature_check(memory,trusted,0x808000)
    description=('Calibration/config checksum at 0x8E0180–0x8FEFFB only' if config_only
                 else 'Four external FLS checksum ranges only; MPC bytes are not supplied')
    return dict(format='DDE5-361830-offline-checks-v2',checksum_scope=scope,validation_scope=description,
        input_sha256={scope:hashlib.sha256(fls).hexdigest()},file_runtime_base=f'{memory.base:#x}',
        file_bytes=len(fls),complete_external_flash=memory.complete_external_flash,
        trusted_key_image_sha256=hashlib.sha256(trusted_fls).hexdigest() if trusted_fls is not None and trusted.complete_external_flash else None,
        additive_checks=checks,all_additive_valid=all(c['valid'] for c in checks),
        selected_space_additive_valid=all(c['valid'] for c in checks),all_additive_valid_scope='covered_scope_only',
        complete_image_additive_valid=None,program_signature=program,calibration_signature=calibration,
        flash_ready=False,
        note='Validity applies only to the selected, supplied checksum ranges. No MPC checksum or complete-image acceptance claim is made. '
             'Checksum correction does not calculate a new RSA signature; ECU acceptance and recovery remain unvalidated.')


def inspect_images(mpc,fls,*,trusted_mpc,trusted_fls):
    memory=ImageMemory(mpc,fls)
    trusted=ImageMemory(trusted_mpc,trusted_fls)
    checks=additive_checks(memory)
    program=signature_check(memory,trusted,0x808000)
    calibration=signature_check(memory,trusted,0x8e0000)
    return dict(format='DDE5-361830-offline-checks-v1',
        input_sha256=dict(MPC=hashlib.sha256(mpc).hexdigest(),FLS=hashlib.sha256(fls).hexdigest()),
        trusted_key_image_sha256=hashlib.sha256(trusted_fls).hexdigest(),
        additive_checks=checks,all_additive_valid=all(c['valid'] for c in checks),
        program_signature=program,calibration_signature=calibration,
        flash_ready=False,
        note='Offline arithmetic/signature checks only. No connection identity, authenticated programming session, erase preservation or post-write recovery has been validated.')
