"""Process-level exclusivity for adapters sharing one EGO TaskSpace."""
from contextlib import contextmanager
import os
from pathlib import Path
import tempfile
import uuid


def windows_local_app_data():
    if os.name != 'nt':
        raise ValueError('windows_known_folder_required')
    import ctypes
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [('data1', wintypes.DWORD), ('data2', wintypes.WORD),
                    ('data3', wintypes.WORD), ('data4', ctypes.c_ubyte * 8)]

    identity = GUID.from_buffer_copy(uuid.UUID('F1B32785-6FBA-4FCF-9D55-7B8E7F157091').bytes_le)
    shell = ctypes.WinDLL('shell32', use_last_error=True)
    ole = ctypes.WinDLL('ole32', use_last_error=True)
    shell.SHGetKnownFolderPath.argtypes = [ctypes.POINTER(GUID), wintypes.DWORD,
                                         wintypes.HANDLE, ctypes.POINTER(ctypes.c_wchar_p)]
    shell.SHGetKnownFolderPath.restype = ctypes.c_long
    ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    ole.CoTaskMemFree.restype = None
    result = ctypes.c_wchar_p()
    if shell.SHGetKnownFolderPath(ctypes.byref(identity), 0, None, ctypes.byref(result)) != 0:
        raise ValueError('windows_known_folder_unavailable')
    try:
        folder = Path(result.value)
    finally:
        ole.CoTaskMemFree(ctypes.cast(result, ctypes.c_void_p))
    if not folder.is_absolute():
        raise ValueError('windows_known_folder_invalid')
    return folder


@contextmanager
def active_space(space_id, *, root=None):
    if type(space_id) is not int or not 0 <= space_id <= 2**63 - 1:
        raise ValueError('EGO 工作区编号无效')
    if root is None:
        root = (windows_local_app_data() / 'JunfeiListingWorkbench' if os.name == 'nt' else
                Path(tempfile.gettempdir()) / f'junfei-workbench-{os.getuid()}')
    folder = Path(root) / 'ego-runtime-locks'
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (folder / f'{space_id}.lock').open('a+b') as lock:
        if lock.seek(0, os.SEEK_END) == 0:
            lock.write(b'0')
            lock.flush()
        lock.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise ValueError('此 EGO 工作区正由另一个工作台任务使用，请稍后查看原任务') from error
        yield
