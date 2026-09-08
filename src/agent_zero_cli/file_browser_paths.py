"""File descriptors anchored to the exposed workspace during Files operations."""
from contextlib import contextmanager
import os
from pathlib import Path
import stat


@contextmanager
def parent_directory(workspace, path):
    root = Path(workspace.scan_root)
    relative = path.relative_to(root)
    directories = list(Path(workspace._scan_root_real).parents)[::-1] + [Path(workspace._scan_root_real)]
    directories += [Path(workspace._scan_root_real).joinpath(*relative.parts[:i]) for i in range(1, len(relative.parts))]
    if os.name == "nt":
        with _locked_windows_directories(directories):
            yield None, str(path)
        return
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        components = Path(workspace._scan_root_real).parts[1:] + relative.parts[:-1]
        for component in components:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor, relative.name or "."
    finally:
        os.close(descriptor)


def open_regular(workspace, parent_fd, name, flags=os.O_RDONLY):
    descriptor = os.open(name, flags | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0), dir_fd=parent_fd)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("Choose a regular file.")
        if os.name == "nt":
            import ctypes
            import msvcrt
            from ctypes import wintypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            function = kernel.GetFinalPathNameByHandleW
            function.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
            function.restype = wintypes.DWORD
            size = function(msvcrt.get_osfhandle(descriptor), None, 0, 0)
            if not size:
                raise ctypes.WinError(ctypes.get_last_error())
            buffer = ctypes.create_unicode_buffer(size + 1)
            if not function(msvcrt.get_osfhandle(descriptor), buffer, len(buffer), 0):
                raise ctypes.WinError(ctypes.get_last_error())
            final = buffer.value
            if final.startswith("\\\\?\\UNC\\"):
                final = "\\\\" + final[8:]
            elif final.startswith("\\\\?\\"):
                final = final[4:]
            workspace._assert_within_root(final, final)
        return os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise


@contextmanager
def _locked_windows_directories(directories):
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    info = kernel.GetFileInformationByHandleEx
    info.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    handles = []
    try:
        for directory in directories:
            # Denying delete sharing keeps each parent from being renamed underneath the operation.
            handle = create(str(directory), 0, 3, None, 3, 0x02200000, None)
            if handle == ctypes.c_void_p(-1).value:
                raise ctypes.WinError(ctypes.get_last_error())
            handles.append(handle)
            attributes = (wintypes.DWORD * 2)()
            if not info(handle, 9, attributes, ctypes.sizeof(attributes)):
                raise ctypes.WinError(ctypes.get_last_error())
            if attributes[0] & 0x400:
                raise PermissionError("File Browser does not follow reparse points.")
        yield
    finally:
        for handle in reversed(handles):
            close(handle)


def rename_new(source_fd, source, target_fd, target):
    import sys
    if sys.platform.startswith("linux") or sys.platform == "darwin":
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        function = getattr(libc, "renameat2" if sys.platform.startswith("linux") else "renameatx_np")
        function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        function.restype = ctypes.c_int
        flags = 1 if sys.platform.startswith("linux") else 4
        if function(source_fd, os.fsencode(source), target_fd, os.fsencode(target), flags):
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error), target)
    else:
        os.rename(source, target, src_dir_fd=source_fd, dst_dir_fd=target_fd)
