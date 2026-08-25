"""Windows compatibility shim for the Unix-only ``pwd`` module.

SearXNG's ``searx.valkeydb`` imports ``pwd`` unconditionally; the Windows
stdlib has no ``pwd`` module, so the import -- and therefore ``import searx`` --
fails. Placed earlier on ``sys.path`` than the SearXNG source, this shim
provides the subset of the ``pwd`` API that SearXNG touches.

Valkey is disabled in Helm (``valkey.url: false``), so these functions are
never called at runtime here -- they exist only to satisfy the import.
"""

import getpass


class _PwdEntry:
    def __init__(self, name: str, uid: int) -> None:
        self.pw_name = name
        self.pw_uid = uid
        self.pw_gid = 0
        self.pw_gecos = ""
        self.pw_dir = ""
        self.pw_shell = ""


def getpwuid(uid: int) -> _PwdEntry:
    return _PwdEntry(getpass.getuser(), uid)


def getpwnam(name: str) -> _PwdEntry:
    return _PwdEntry(name, 0)


def getpwall():
    return [getpwuid(0)]
