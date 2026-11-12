"""Windows private files and read-only registration checks for pinned discovery.

Native code owns installation. These helpers never alter an existing ACL/key.
"""
from contextlib import contextmanager
import os
from pathlib import Path
import re
import msvcrt
import ntsecuritycon
import pywintypes
import win32api
import win32con
import win32file
import win32security
import winreg

from .file_browser_paths import _locked_windows_directories


def local_path(path: Path) -> None:
    raw = str(path)
    if (not re.match(r"^[A-Za-z]:[\\/]", raw) or not path.is_absolute()
            or len(raw) > 4096 or any(ord(char) < 32 for char in raw)
            or any(part in {".", ".."} or part.endswith((" ", ".")) or ":" in part
                   for part in re.split(r"[\\/]", raw[3:]))):
        raise ValueError("Invalid private path")


def _user():
    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        return win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    finally:
        token.Close()


def _security():
    sid = win32security.ConvertSidToStringSid(_user())
    attributes = pywintypes.SECURITY_ATTRIBUTES()
    attributes.SECURITY_DESCRIPTOR = win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(
        f"O:{sid}D:P(A;;FA;;;SY)(A;;FA;;;{sid})", 1)
    attributes.bInheritHandle = False
    return attributes


def _private(handle, *, registry=False) -> None:
    descriptor = win32security.GetSecurityInfo(
        handle, win32security.SE_REGISTRY_KEY if registry else win32security.SE_FILE_OBJECT,
        win32security.OWNER_SECURITY_INFORMATION | win32security.DACL_SECURITY_INFORMATION)
    user = _user()
    acl = descriptor.GetSecurityDescriptorDacl()
    if (descriptor.GetSecurityDescriptorOwner() != user
            or descriptor.GetSecurityDescriptorControl()[0] & 0x1004 != 0x1004
            or acl is None or acl.GetAceCount() != 2):
        raise ValueError("Private ownership required")
    system = win32security.CreateWellKnownSid(win32security.WinLocalSystemSid, None)
    rights = win32con.KEY_ALL_ACCESS if registry else ntsecuritycon.FILE_ALL_ACCESS
    for index, sid in enumerate((system, user)):
        header, mask, actual = acl.GetAce(index)
        if header != (0, 0) or mask != rights or actual != sid:
            raise ValueError("Private ACL required")


@contextmanager
def directory(path: Path, *, private=False):
    local_path(path)
    directories = [*reversed(path.parents), path]
    with _locked_windows_directories(directories):
        if private:
            handle = win32file.CreateFile(str(path), win32con.READ_CONTROL, 3, None,
                win32con.OPEN_EXISTING, 0x02200000, None)
            try:
                _private(handle)
            finally:
                handle.Close()
        yield path


@contextmanager
def file(path: Path, **_):
    local_path(path)
    with directory(path.parent, private=True):
        handle = win32file.CreateFile(str(path), win32con.GENERIC_READ | win32con.READ_CONTROL,
            win32con.FILE_SHARE_READ, None, win32con.OPEN_EXISTING, 0x00200000, None)
        descriptor = None
        try:
            _private(handle)
            info = win32file.GetFileInformationByHandle(handle)
            # Attributes, creation/access/write times, volume, size hi/lo, links, index hi/lo.
            if info[0] & 0x410 or info[7] != 1:
                raise ValueError("Regular unlinked file required")
            descriptor = msvcrt.open_osfhandle(handle.Detach(), os.O_RDONLY | os.O_BINARY)
            before = os.fstat(descriptor)
            yield descriptor
            after = os.fstat(descriptor)
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                    after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
                raise ValueError("Private file changed")
        finally:
            if descriptor is not None:
                os.close(descriptor)
            else:
                handle.Close()


@contextmanager
def lock(descriptor: int):
    overlap = pywintypes.OVERLAPPED()
    handle = msvcrt.get_osfhandle(descriptor)
    try:
        win32file.LockFileEx(handle, 1, 0, 1, overlap)
    except pywintypes.error as error:
        if error.winerror == 33:
            raise BlockingIOError() from None
        raise
    try:
        yield
    finally:
        win32file.UnlockFileEx(handle, 0, 1, overlap)


def create_directory(path: Path) -> None:
    local_path(path)
    with directory(path.parent):
        win32file.CreateDirectory(str(path), _security())
    with directory(path, private=True):
        pass


@contextmanager
def create_file(path: Path):
    local_path(path)
    with directory(path.parent, private=True):
        handle = win32file.CreateFile(str(path), win32con.GENERIC_READ | win32con.GENERIC_WRITE | win32con.READ_CONTROL,
            win32con.FILE_SHARE_READ, _security(), win32con.CREATE_NEW, 0x00200080, None)
        try:
            _private(handle)
            descriptor = msvcrt.open_osfhandle(handle.Detach(), os.O_RDWR | os.O_BINARY)
        except BaseException:
            handle.Close()
            raise
        with os.fdopen(descriptor, "w+b") as stream:
            yield stream


def verify_registration(path: Path, host: str) -> None:
    local_path(path)
    vendors = {"chrome": r"Google\Chrome", "edge": r"Microsoft\Edge", "chromium": "Chromium"}
    vendor = vendors[path.parent.name]
    name = rf"Software\{vendor}\NativeMessagingHosts\{host}"
    found = False
    for view in (winreg.KEY_WOW64_32KEY, winreg.KEY_WOW64_64KEY):
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, name, 0x8, winreg.KEY_READ | view)
        except FileNotFoundError:
            continue
        with key:
            _private(int(key), registry=True)
            subkeys, values, _ = winreg.QueryInfoKey(key)
            value, kind = winreg.QueryValueEx(key, "")
            if subkeys or values != 1 or kind != winreg.REG_SZ or value != str(path):
                raise ValueError("Registration changed")
            found = True
    if not found:
        raise ValueError("Registration missing")
