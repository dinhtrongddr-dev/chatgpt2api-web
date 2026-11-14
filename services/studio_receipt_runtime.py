"""Deployment remains OFF unless explicitly provisioned. No production key reuse."""
import os, ctypes
from pathlib import Path
from fastapi import HTTPException
from services.studio_receipts import ReceiptStore,ReceiptEngine,make_router

def load_dpapi_key(path):
    # Provision separately under the service account. Never print/copy key material.
    if os.name!='nt':raise RuntimeError('WINDOWS_DPAPI_KEY_REQUIRED')
    class Blob(ctypes.Structure):_fields_=[('size',ctypes.c_ulong),('data',ctypes.POINTER(ctypes.c_ubyte))]
    raw=Path(path).read_bytes();buffer=ctypes.create_string_buffer(raw)
    source=Blob(len(raw),ctypes.cast(buffer,ctypes.POINTER(ctypes.c_ubyte)));out=Blob()
    if not ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(source),None,None,None,None,1,ctypes.byref(out)):raise RuntimeError('DPAPI_UNPROTECT_FAILED')
    try:return ctypes.string_at(out.data,out.size)
    finally:ctypes.windll.kernel32.LocalFree(out.data)

def configured_router():
    from api.support import require_identity,extract_bearer_token
    from services.studio_native_adapter import native_text_events
    location=Path(os.environ['CHATGPT2API_STUDIO_RECEIPT_DIR']).resolve(strict=True)
    if location.is_symlink():raise RuntimeError('RECEIPT_DIRECTORY_LINK_FORBIDDEN')
    store=ReceiptStore(location/'studio-receipts.sqlite',load_dpapi_key(location/'receipt-key.dpapi'))
    store.recover() # Expired orphan leases UNKNOWN, never redispatched.
    engine=ReceiptEngine(store,native_text_events)
    def authenticated(auth):
        # Existing legacy literal admin shortcut is not sufficient for this new API.
        if extract_bearer_token(auth).lower()=='admin':raise HTTPException(401)
        return require_identity(auth)
    return make_router(engine,authenticated)
