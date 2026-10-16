"""Offline DDE5 level-3 authentication calculation and PRG argument contract.

No EDIABAS calls, private keys bundled, file writes, or flashing operations.
The arithmetic follows preserved ECU code 80979C/809B04; the argument wrapper
follows the decoded 28dde504 AUTHENTISIERUNG_START job and its metadata.
Successfully constructing a response is not a claim of hardware acceptance.
"""
from __future__ import annotations
import hashlib

from .dde5_checks import bosch_integer


def authentication_material(trusted_fls: bytes):
    if len(trusted_fls) != 0x100000:
        raise ValueError('The trusted FLS must cover the full 1 MiB external image')
    token = trusted_fls[0x6029:0x602d]
    if token != bytes.fromhex('333530ff'):
        raise ValueError('Authentication identity differs from preserved 361830')
    modulus = bosch_integer(trusted_fls[0x8498:0x8498+68])
    exponent = bosch_integer(trusted_fls[0x84e0:0x84e0+8])
    if modulus.bit_length() != 512 or exponent != 7:
        raise ValueError('Unexpected DDE5 level-3 public-key format')
    return token, modulus, exponent


def authentication_digest(identifier: bytes, nonce: bytes, trusted_fls: bytes):
    """MD5 over exactly 4 caller-ID bytes, 4 target-identity bytes, 8 nonce bytes."""
    if len(identifier) != 4 or len(nonce) != 8:
        raise ValueError('Authentication requires a four-byte ID and eight-byte nonce')
    token, _, _ = authentication_material(trusted_fls)
    return hashlib.md5(bytes(identifier) + token + bytes(nonce)).digest()


def encode_response(integer: int):
    """Fixed 68-byte integer container: BE32 count 16, then BE32 low-first limbs."""
    if not 0 <= integer < (1 << 512):
        raise ValueError('Response integer must fit sixteen unsigned words')
    return (16).to_bytes(4, 'big') + b''.join(
        ((integer >> (32*i)) & 0xffffffff).to_bytes(4, 'big') for i in range(16))


def verify_response(identifier: bytes, nonce: bytes, response: bytes, trusted_fls: bytes):
    """Reproduce the public-key comparison, without changing ECU privilege state."""
    if len(response) != 68:
        raise ValueError('This contract requires the fixed 68-byte response container')
    _, modulus, exponent = authentication_material(trusted_fls)
    value = pow(bosch_integer(response), exponent, modulus)
    digest = authentication_digest(identifier, nonce, trusted_fls)
    word_count = (value.bit_length()+31)//32
    return dict(valid=word_count == 4 and value.to_bytes(64, 'little')[:16] == digest,
                actual_md5=digest.hex(), recovered_word_count=word_count,
                recovered_md5=value.to_bytes(64, 'little')[:16].hex())


def calculate_response(identifier: bytes, nonce: bytes, trusted_fls: bytes,
                       private_exponent: int):
    """Use a caller-supplied exponent; refuse a response that fails the target key.

    This function does not locate or decrypt WinKFP key data. The caller must
    establish that the supplied exponent belongs to this authentication modulus.
    No image-signature key or image-signing operation is involved.
    """
    if private_exponent <= 0:
        raise ValueError('Private exponent must be positive')
    _, modulus, _ = authentication_material(trusted_fls)
    message = int.from_bytes(authentication_digest(identifier, nonce, trusted_fls), 'little')
    response = encode_response(pow(message, private_exponent, modulus))
    if not verify_response(identifier, nonce, response, trusted_fls)['valid']:
        raise ValueError('Supplied key does not produce a valid authentication response')
    return response


def prg_authentication_argument(response: bytes, *, wait_seconds: int = 10):
    """Prepare binary argument bytes for AUTHENTISIERUNG_START; never invoke it.

    Wrapper[0]=binary type 1, wrapper[4]=wait seconds, [13:15]=LE16 length,
    [21:89]=68-byte response, and final byte ETX 03. Remaining header fields
    are zero. ETX is a buffer terminator, not the authentication privilege level.
    """
    if len(response) != 68 or int.from_bytes(response[:4], 'big') != 16:
        raise ValueError('Expected a fixed count-16, 68-byte RSA response')
    if not isinstance(wait_seconds, int) or not 0 <= wait_seconds <= 255:
        raise ValueError('Wait time must fit one unsigned byte')
    header = bytearray(21)
    header[0] = 1
    header[4] = wait_seconds
    header[13:15] = (68).to_bytes(2, 'little')
    return bytes(header) + bytes(response) + b'\x03'


def calculate_installed_response(identifier, nonce, trusted_fls, *, provider_path=None, dataset_path=None):
    """Compute an offline response from pinned installed data; never send it."""
    from .dde5_key_provider import recover_authentication_key
    _, private_exponent = recover_authentication_key(trusted_fls, provider_path=provider_path,
                                                    dataset_path=dataset_path)
    return calculate_response(identifier, nonce, trusted_fls, private_exponent)
