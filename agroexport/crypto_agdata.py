# crypto_agdata.py
# AES-128-CBC com cadeia de fallback:
#   1. cryptography  (bundled QGIS 3.x Windows / Linux / macOS)
#   2. pycryptodome  (se o usuário instalou)
#   3. pyaes bundled (sempre disponível — incluído no plugin)
#
# Sem dependências externas. Zero instalação necessária.

def _try_cryptography():
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.backends import default_backend
        def enc(key, iv, data):
            c = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend())
            e = c.encryptor(); return e.update(data) + e.finalize()
        def dec(key, iv, data):
            c = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend())
            d = c.decryptor(); return d.update(data) + d.finalize()
        enc(b'\x00'*16, b'\x00'*16, b'\x00'*16)
        return enc, dec, 'cryptography'
    except Exception:
        return None

def _try_pycryptodome():
    try:
        from Crypto.Cipher import AES
        def enc(key, iv, data): return AES.new(key, AES.MODE_CBC, iv).encrypt(data)
        def dec(key, iv, data): return AES.new(key, AES.MODE_CBC, iv).decrypt(data)
        enc(b'\x00'*16, b'\x00'*16, b'\x00'*16)
        return enc, dec, 'pycryptodome'
    except Exception:
        return None

def _try_pyaes_bundled():
    import sys, os
    vendor = os.path.join(os.path.dirname(__file__), 'vendor')
    if vendor not in sys.path:
        sys.path.insert(0, vendor)
    from pyaes import AESModeOfOperationCBC
    def enc(key, iv, data):
        aes = AESModeOfOperationCBC(key, iv=iv)
        return b''.join(aes.encrypt(data[i:i+16]) for i in range(0, len(data), 16))
    def dec(key, iv, data):
        aes = AESModeOfOperationCBC(key, iv=iv)
        return b''.join(aes.decrypt(data[i:i+16]) for i in range(0, len(data), 16))
    return enc, dec, 'pyaes (bundled)'

_backend = _try_cryptography() or _try_pycryptodome() or _try_pyaes_bundled()
_aes_enc_fn, _aes_dec_fn, BACKEND_NAME = _backend

def aes_cbc_encrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    return _aes_enc_fn(key, iv, data)

def aes_cbc_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    return _aes_dec_fn(key, iv, data)

def pkcs7_pad(data: bytes, block_size: int = 16) -> bytes:
    p = block_size - (len(data) % block_size)
    return data + bytes([p] * p)

def pkcs7_unpad(data: bytes) -> bytes:
    return data[:-data[-1]]

def _get_backend():
    return BACKEND_NAME
