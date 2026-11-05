from __future__ import annotations

import ctypes
import json
import os
import tempfile
from pathlib import Path


def _protect(data: bytes, *, decrypt: bool = False) -> bytes:
    """Windows 使用当前登录用户的 DPAPI，凭据不可迁移到其他账号。"""
    class Blob(ctypes.Structure):
        _fields_ = [("size", ctypes.c_ulong), ("data", ctypes.POINTER(ctypes.c_char))]

    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    target = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    fn = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    fn.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                   ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(Blob)]
    fn.restype = ctypes.c_int
    if not fn(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise RuntimeError("无法访问本机加密的用户授权，请使用原 Windows 账号或重新登录")
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        kernel.LocalFree(target.data)


class TokenStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict:
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            raise RuntimeError("尚未授权个人飞书账号，请运行 python main.py --login") from None
        try:
            if raw.startswith(b"DPAPI\n"):
                if os.name != "nt":
                    raise RuntimeError("该授权属于 Windows 账号，请在当前系统重新登录")
                raw = _protect(raw[6:], decrypt=True)
            elif os.name == "nt":
                raise RuntimeError("用户授权文件未加密，请重新登录")
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError
            return value
        except (ValueError, UnicodeError):
            raise RuntimeError("用户授权文件损坏，请重新运行 python main.py --login") from None

    def save(self, value: dict) -> None:
        raw = json.dumps(value, ensure_ascii=False).encode("utf-8")
        if os.name == "nt":
            raw = b"DPAPI\n" + _protect(raw)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=self.path.parent, prefix=".oauth-")
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(raw)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
        finally:
            Path(temporary).unlink(missing_ok=True)
