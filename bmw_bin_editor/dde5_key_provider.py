"""Offline target auth-key verification. Reports hashes, never private values.

Reads installed provider bytes statically; executes no vendor code and sends
no ECU traffic. DES is implemented here from its standard permutations.
"""
from pathlib import Path
import hashlib,struct
IP=[58,50,42,34,26,18,10,2,60,52,44,36,28,20,12,4,62,54,46,38,30,22,14,6,64,56,48,40,32,24,16,8,57,49,41,33,25,17,9,1,59,51,43,35,27,19,11,3,61,53,45,37,29,21,13,5,63,55,47,39,31,23,15,7]
FP=[40,8,48,16,56,24,64,32,39,7,47,15,55,23,63,31,38,6,46,14,54,22,62,30,37,5,45,13,53,21,61,29,36,4,44,12,52,20,60,28,35,3,43,11,51,19,59,27,34,2,42,10,50,18,58,26,33,1,41,9,49,17,57,25]
E=[32,1,2,3,4,5,4,5,6,7,8,9,8,9,10,11,12,13,12,13,14,15,16,17,16,17,18,19,20,21,20,21,22,23,24,25,24,25,26,27,28,29,28,29,30,31,32,1]
P=[16,7,20,21,29,12,28,17,1,15,23,26,5,18,31,10,2,8,24,14,32,27,3,9,19,13,30,6,22,11,4,25]
PC1=[57,49,41,33,25,17,9,1,58,50,42,34,26,18,10,2,59,51,43,35,27,19,11,3,60,52,44,36,63,55,47,39,31,23,15,7,62,54,46,38,30,22,14,6,61,53,45,37,29,21,13,5,28,20,12,4]
PC2=[14,17,11,24,1,5,3,28,15,6,21,10,23,19,12,4,26,8,16,7,27,20,13,2,41,52,31,37,47,55,30,40,51,45,33,48,44,49,39,56,34,53,46,42,50,36,29,32]
S=[
 [14,4,13,1,2,15,11,8,3,10,6,12,5,9,0,7,0,15,7,4,14,2,13,1,10,6,12,11,9,5,3,8,4,1,14,8,13,6,2,11,15,12,9,7,3,10,5,0,15,12,8,2,4,9,1,7,5,11,3,14,10,0,6,13],
 [15,1,8,14,6,11,3,4,9,7,2,13,12,0,5,10,3,13,4,7,15,2,8,14,12,0,1,10,6,9,11,5,0,14,7,11,10,4,13,1,5,8,12,6,9,3,2,15,13,8,10,1,3,15,4,2,11,6,7,12,0,5,14,9],
 [10,0,9,14,6,3,15,5,1,13,12,7,11,4,2,8,13,7,0,9,3,4,6,10,2,8,5,14,12,11,15,1,13,6,4,9,8,15,3,0,11,1,2,12,5,10,14,7,1,10,13,0,6,9,8,7,4,15,14,3,11,5,2,12],
 [7,13,14,3,0,6,9,10,1,2,8,5,11,12,4,15,13,8,11,5,6,15,0,3,4,7,2,12,1,10,14,9,10,6,9,0,12,11,7,13,15,1,3,14,5,2,8,4,3,15,0,6,10,1,13,8,9,4,5,11,12,7,2,14],
 [2,12,4,1,7,10,11,6,8,5,3,15,13,0,14,9,14,11,2,12,4,7,13,1,5,0,15,10,3,9,8,6,4,2,1,11,10,13,7,8,15,9,12,5,6,3,0,14,11,8,12,7,1,14,2,13,6,15,0,9,10,4,5,3],
 [12,1,10,15,9,2,6,8,0,13,3,4,14,7,5,11,10,15,4,2,7,12,9,5,6,1,13,14,0,11,3,8,9,14,15,5,2,8,12,3,7,0,4,10,1,13,11,6,4,3,2,12,9,5,15,10,11,14,1,7,6,0,8,13],
 [4,11,2,14,15,0,8,13,3,12,9,7,5,10,6,1,13,0,11,7,4,9,1,10,14,3,5,12,2,15,8,6,1,4,11,13,12,3,7,14,10,15,6,8,0,5,9,2,6,11,13,8,1,4,10,7,9,5,0,15,14,2,3,12],
 [13,2,8,4,6,15,11,1,10,9,3,14,5,0,12,7,1,15,13,8,10,3,7,4,12,5,6,11,0,14,9,2,7,11,4,1,9,12,14,2,0,6,10,13,15,3,5,8,2,1,14,7,4,10,8,13,15,12,9,0,3,5,6,11]]
def perm(v,width,table):
    out=0
    for bit in table:out=(out<<1)|((v>>(width-bit))&1)
    return out
def subkeys(key):
    q=perm(int.from_bytes(key,'big'),64,PC1);a=q>>28;b=q&0xfffffff;out=[]
    for n in [1,1,2,2,2,2,2,2,1,2,2,2,2,2,2,1]:
        a=((a<<n)|(a>>(28-n)))&0xfffffff;b=((b<<n)|(b>>(28-n)))&0xfffffff
        out.append(perm((a<<28)|b,56,PC2))
    return out
def des(block,keys):
    q=perm(int.from_bytes(block,'big'),64,IP);a=q>>32;b=q&0xffffffff
    for k in keys:
        expanded=perm(b,32,E)^k;v=0
        for i in range(8):
            six=(expanded>>(42-6*i))&63;row=((six>>5)<<1)|(six&1);col=(six>>1)&15
            v=(v<<4)|S[i][16*row+col]
        a,b=b,a^perm(v,32,P)
    return perm((b<<32)|a,64,FP).to_bytes(8,'big')
def tdes_decrypt(data,key,cbc=False):
    assert len(key)==24 and len(data)%8==0
    k1,k2,k3=(subkeys(key[i:i+8]) for i in (0,8,16));out=bytearray();previous=b'\0'*8
    for i in range(0,len(data),8):
        block=data[i:i+8];v=des(des(des(block,list(reversed(k3))),k2),list(reversed(k1)))
        if cbc:v=bytes(a^b for a,b in zip(v,previous))
        out.extend(v);previous=block
    return bytes(out)
def bosch(blob):
    count=int.from_bytes(blob[:4],'big')
    if not 1<=count<=16 or len(blob)<4+4*count:raise ValueError('bad word count')
    return sum(int.from_bytes(blob[4+4*i:8+4*i],'big')<<(32*i) for i in range(count))

def recover_authentication_key(trusted_fls, *, provider_path=None, dataset_path=None):
    """Pinned static provider reconstruction; private material stays in memory.

    Uses the installed WinKFP data as files, without running WinKFP or a DLL.
    Different provider/data versions require a new reviewed decoder.
    """
    provider_path = Path(provider_path or r'C:\EC-APPS\NFS\BIN\winkfpt.exe')
    dataset_path = Path(dataset_path or r'C:\EC-APPS\NFS\DATA\gdaten\SGIDC.as2')
    binary, dataset = provider_path.read_bytes(), dataset_path.read_bytes()
    if hashlib.sha256(binary).hexdigest() != 'd19106c4aca96aa9eb455ae3d457a16b460e2d3225578cbad3ff9fd0d52eca6b':
        raise ValueError('Unsupported WinKFP provider build for authentication decoding')
    if hashlib.sha256(dataset).hexdigest() != '149283118e53e3e57e7feed3fa37ca73164287f711d370dabe80c3480976c294':
        raise ValueError('Unsupported DDE504 authentication dataset')
    if not dataset.startswith(b'$L 3\r\n$V QU\r\n$G 1\r\n'):
        raise ValueError('Unexpected authentication dataset format')
    pe = struct.unpack_from('<I', binary, 0x3c)[0]
    if binary[:2] != b'MZ' or binary[pe:pe+4] != b'PE\0\0':
        raise ValueError('Provider is not a PE image')
    machine, sections = struct.unpack_from('<HH', binary, pe+4)
    optional_size = struct.unpack_from('<H', binary, pe+20)[0]
    optional = pe+24
    if machine != 0x14c or struct.unpack_from('<H', binary, optional)[0] != 0x10b:
        raise ValueError('Unsupported provider architecture')
    image_base = struct.unpack_from('<I', binary, optional+28)[0]
    if image_base != 0x400000:
        raise ValueError('Unsupported provider image base')
    section_table = optional+optional_size
    def read_static(address, count):
        for i in range(sections):
            _, rva, size, offset = struct.unpack_from('<IIII', binary, section_table+40*i+8)
            if image_base+rva <= address and address+count <= image_base+rva+size:
                start = offset+address-image_base-rva
                if start+count <= len(binary):
                    return binary[start:start+count]
        raise ValueError('Provider constant has no source-byte coverage')
    master = b''.join(read_static(address, 4) for address in
                     (0x6633c0,0x6633c8,0x6633b8,0x6633d0,0x6633b0,0x6633d8))
    rows = [line for line in dataset.decode('ascii').splitlines()
            if line.startswith('$K ') and line[3:23].strip() == 'DDE504']
    if len(rows) != 1 or rows[0][23:33] != '5812000018' or len(rows[0][33:]) != 272:
        raise ValueError('Unexpected DDE504 authentication entry')
    cipher = bytes.fromhex(rows[0][33:])
    plain = tdes_decrypt(cipher, master)
    modulus, private = bosch(plain[:68]), bosch(plain[68:])
    if len(trusted_fls) != 0x100000 or modulus != bosch(trusted_fls[0x8498:0x8498+68]):
        raise ValueError('Authentication key does not match the supplied target image')
    return modulus, private
