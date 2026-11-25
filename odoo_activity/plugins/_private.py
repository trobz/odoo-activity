"""Private-file helpers shared by the credential-export plugins.

Both `password-audit` and `users-audit` write credential material to disk.
The rules are the same and security-critical, so they live here once:

- the directory must be `0700` and owned by us -- a pre-created directory or a
  symlinked one could redirect the material;
- files are created `0600` with `O_CREAT|O_EXCL`, so a predictable name in
  `/tmp` cannot be symlinked over or silently truncated.
"""

from __future__ import annotations

import os
from pathlib import Path


def ensure_dir(d: Path) -> Path:
    """A credential-bearing dir, 0700 and owned by us — other local users
    must not be able to read (or pre-create) it: a pre-created dir or a
    symlinked file could redirect the material."""
    d.mkdir(mode=0o700, exist_ok=True)
    if d.is_symlink():
        msg = f"{d}: symlinked audit directory is not allowed"
        raise PermissionError(msg)
    st = d.stat()
    if st.st_uid != os.getuid() or (st.st_mode & 0o777) != 0o700:
        msg = f"{d} exists with uid {st.st_uid} mode {oct(st.st_mode & 0o777)} — expected 0700 owned by us"
        raise PermissionError(msg)
    return d


def safe_write(path: Path, content: str | bytes) -> None:
    """0600 without following symlinks: O_CREAT|O_EXCL fails if a hostile
    file/symlink already sits at the path (predictable names in /tmp)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(content.encode("utf-8") if isinstance(content, str) else content)
