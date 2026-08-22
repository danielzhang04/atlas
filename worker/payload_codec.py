"""Fail-closed protection for private slow-task payloads.

The production codec uses Windows CurrentUser DPAPI with UI disabled and caller-supplied entropy.
There is deliberately no plaintext or cross-platform fallback. Tests inject a codec instead of
touching the operating-system protection boundary.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
from typing import Protocol, runtime_checkable


class PayloadProtectionError(RuntimeError):
    """Sanitized failure at the protected-payload boundary."""


@runtime_checkable
class PayloadCodec(Protocol):
    codec_id: str

    def protect(self, plaintext: bytes, *, entropy: bytes) -> bytes:
        ...

    def unprotect(self, ciphertext: bytes, *, entropy: bytes) -> bytes:
        ...


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _input_blob(value: bytes) -> tuple[_DataBlob, object]:
    if not isinstance(value, bytes) or not value:
        raise PayloadProtectionError("protected payload input must be non-empty bytes")
    buffer = ctypes.create_string_buffer(value)
    blob = _DataBlob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    return blob, buffer


class WindowsCurrentUserDPAPICodec:
    """Current-user, non-roaming DPAPI protection with no fallback."""

    codec_id = "windows-dpapi-current-user-v1"
    _UI_FORBIDDEN = 0x1

    def __init__(self) -> None:
        if os.name != "nt":
            raise PayloadProtectionError("Windows payload protection is unavailable")
        try:
            self._crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
            self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        except Exception:
            raise PayloadProtectionError("Windows payload protection is unavailable") from None
        self._crypt32.CryptProtectData.argtypes = [
            ctypes.POINTER(_DataBlob), wintypes.LPCWSTR, ctypes.POINTER(_DataBlob),
            wintypes.LPVOID, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(_DataBlob),
        ]
        self._crypt32.CryptProtectData.restype = wintypes.BOOL
        self._crypt32.CryptUnprotectData.argtypes = [
            ctypes.POINTER(_DataBlob), ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(_DataBlob),
            wintypes.LPVOID, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(_DataBlob),
        ]
        self._crypt32.CryptUnprotectData.restype = wintypes.BOOL
        self._kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
        self._kernel32.LocalFree.restype = wintypes.HLOCAL

    def protect(self, plaintext: bytes, *, entropy: bytes) -> bytes:
        return self._transform("protect", plaintext, entropy)

    def unprotect(self, ciphertext: bytes, *, entropy: bytes) -> bytes:
        return self._transform("unprotect", ciphertext, entropy)

    def _transform(self, action: str, value: bytes, entropy: bytes) -> bytes:
        source, source_buffer = _input_blob(value)
        entropy_blob, entropy_buffer = _input_blob(entropy)
        output = _DataBlob()
        # Keep buffers alive across the native call.
        _ = source_buffer, entropy_buffer
        if action == "protect":
            ok = self._crypt32.CryptProtectData(
                ctypes.byref(source), "Atlas slow task", ctypes.byref(entropy_blob),
                None, None, self._UI_FORBIDDEN, ctypes.byref(output),
            )
        else:
            ok = self._crypt32.CryptUnprotectData(
                ctypes.byref(source), None, ctypes.byref(entropy_blob),
                None, None, self._UI_FORBIDDEN, ctypes.byref(output),
            )
        if not ok:
            raise PayloadProtectionError("protected payload operation failed")
        try:
            return ctypes.string_at(output.pbData, output.cbData)
        finally:
            if output.pbData:
                self._kernel32.LocalFree(ctypes.cast(output.pbData, wintypes.HLOCAL))


__all__ = ["PayloadCodec", "PayloadProtectionError", "WindowsCurrentUserDPAPICodec"]
