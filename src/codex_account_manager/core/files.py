"""Durable atomic writes with owner-only access for credential-bearing files."""

from __future__ import annotations

import os
import stat
import sys
import tempfile
import time
from pathlib import Path


def resolved_path(value: str | Path) -> Path:
    """Resolve filesystem aliases before comparing Windows path containment."""

    def ordinary(path: str) -> str:
        if path.startswith("\\\\?\\UNC\\"):
            return "\\\\" + path[8:]
        if path.startswith("\\\\?\\") and len(path) > 5 and path[5] == ":":
            return path[4:]
        return path

    return Path(ordinary(str(Path(ordinary(str(value))).resolve())))


def restrict_access(path: Path) -> None:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise OSError("Refusing to change permissions on a redirected data path.")
    if sys.platform == "win32":
        import pywintypes
        import win32api
        import win32con
        import win32security

        token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
        try:
            user = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
        finally:
            token.Close()
        acl = win32security.ACL()
        inheritance = (
            win32con.OBJECT_INHERIT_ACE | win32con.CONTAINER_INHERIT_ACE
            if stat.S_ISDIR(info.st_mode)
            else 0
        )
        acl.AddAccessAllowedAceEx(
            win32security.ACL_REVISION, inheritance, win32con.GENERIC_ALL, user
        )
        try:
            win32security.SetNamedSecurityInfo(
                str(path),
                win32security.SE_FILE_OBJECT,
                win32security.DACL_SECURITY_INFORMATION
                | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
                None,
                None,
                acl,
                None,
            )
        except pywintypes.error as exc:
            raise OSError("Could not protect local file permissions.") from exc
    else:
        path.chmod(0o700 if path.is_dir() else 0o600)


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            restrict_access(temporary)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        for attempt in range(4):
            try:
                os.replace(temporary, path)
                break
            except OSError as exc:
                if getattr(exc, "winerror", None) not in (5, 32, 33) or attempt == 3:
                    raise
                # Windows readers can briefly deny replacement; never remove the original.
                time.sleep(0.02 * 2**attempt)
        if sys.platform != "win32":
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)
