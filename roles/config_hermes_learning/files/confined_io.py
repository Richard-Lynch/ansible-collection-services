# SPDX-License-Identifier: GPL-3.0-or-later
"""Confined file I/O for ROOT-run host scripts that touch a tree another uid can write.

THE DEFECT CLASS. A host script runs as root and reads, writes, chowns or deletes BY NAME under a
directory the agent (uid 10000 in its container) can write. The agent plants a symlink at any
component of that name, and root's operation lands wherever the link points: a planted
`scripts -> /etc` makes a root chown and prune of that directory act on /etc, and a planted
`learning/ledger -> /` turns a mirror write into an arbitrary file write. This module is the one
defence against that class, with one test suite.

THE IDEA. Never resolve a path under the untrusted tree by NAME. Open a trusted ANCHOR (a directory
whose ancestors the untrusted uid cannot write), then walk one component at a time with
dir_fd-relative calls, every component opened O_NOFOLLOW|O_DIRECTORY. A symlink anywhere on the
walk fails the open (ELOOP/ENOTDIR) instead of being followed, and because every later call is
relative to an fd that names the directory that was checked, a name swapped after the check cannot
redirect anything: there is no check-then-use window. Checking a path first (`is_symlink()`,
`stat(follow=false)`) and then opening it by name is NOT the same thing; it is a race the agent can
win, and it only ever inspects the components the check thought to look at.

  * open_anchor / open_dir / dir_at   walk to a directory, optionally creating it, with an owner check
  * open_file / read_file             open an existing regular file at the end of such a walk
  * write_file                        temp file O_EXCL|O_NOFOLLOW beside the target, then rename over
                                      the NAME (a rename replaces a planted link; it never writes
                                      through it)
  * mkstemp_at / unlink               the dir_fd-relative primitives the above are built from
  * ensure_dir                        every component exists as a real directory; created ones get
                                      an owner and mode by fchown/fchmod on the open fd
  * run_as                            run a function in a forked child that has dropped to a uid/gid
                                      for good, so a write can only ever do what that uid could do

Roles that ship it to a host carry a copy in their own files/ (so each role stays
self-contained); every copy is kept byte-identical to the canonical one.

Stdlib only; Python 3.9+. Linux is the target; the suite also runs on macOS (the controller).
"""

from __future__ import annotations

import contextlib
import errno
import os
import stat
import sys
from typing import Callable, Iterable, Iterator, Optional, Tuple, Union

Anchor = Union[str, "os.PathLike[str]", int]

DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
_FILE_FLAGS = os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
# What an O_NOFOLLOW open of a symlink (or O_DIRECTORY of a file) answers. Linux says ELOOP for a
# link and ENOTDIR for a file; FreeBSD says EMLINK for a link.
_NOT_A_REAL_DIR = (errno.ELOOP, errno.ENOTDIR, errno.EMLINK)


class Refused(Exception):
    """An operation that would leave the anchor's tree, or could not be proven not to."""


def parts(rel: str) -> list[str]:
    """The components of a path RELATIVE to the anchor. Absolute paths and '..' are refused, so
    the walk can only ever descend."""
    rel = os.fspath(rel)
    comps = [p for p in rel.split("/") if p not in ("", ".")]
    if not comps or rel.startswith("/") or ".." in comps:
        raise Refused(f"path {rel!r} must be relative to the anchor, with no '..'")
    return comps


def _check_owner(fd: int, owners: Optional[Iterable[int]], shown: str) -> os.stat_result:
    st = os.fstat(fd)
    if owners is not None and st.st_uid not in set(owners):
        raise Refused(f"{shown} is owned by uid {st.st_uid}, expected one of {sorted(set(owners))}")
    return st


def open_anchor(anchor: Anchor, *, owner: Optional[int] = None, private: bool = False) -> int:
    """An fd for the trusted directory the walk starts from (the caller closes it).

    A path is opened O_NOFOLLOW|O_DIRECTORY, so the anchor itself cannot be a link; its ANCESTORS
    are the caller's contract (they must not be writable by the untrusted uid). ``owner`` requires
    that uid; ``private`` refuses a group- or other-writable anchor. An int is an fd the caller
    already opened: it is dup'd, so the caller keeps ownership of the original."""
    if isinstance(anchor, int):
        fd = os.dup(anchor)
        shown = f"anchor fd {anchor}"
    else:
        shown = f"anchor {os.fspath(anchor)!r}"
        try:
            fd = os.open(anchor, DIR_FLAGS)
        except OSError as exc:
            raise Refused(f"{shown} is not a real directory: {exc.strerror}") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISDIR(st.st_mode):
            raise Refused(f"{shown} is not a directory")
        if owner is not None and st.st_uid != owner:
            raise Refused(f"{shown} is owned by uid {st.st_uid}, expected {owner}")
        if private and st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise Refused(f"{shown} is group/other-writable ({oct(st.st_mode & 0o777)})")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _open_child(parent_fd: int, name: str, shown: str) -> int:
    try:
        return os.open(name, DIR_FLAGS, dir_fd=parent_fd)
    except OSError as exc:
        if exc.errno in _NOT_A_REAL_DIR:
            raise Refused(
                f"{shown} is not a real directory (a symlink or a file): refusing"
            ) from None
        raise


def open_dir(
    anchor: Anchor,
    rel: str = "",
    *,
    create: bool = False,
    mode: int = 0o755,
    chown: Optional[Tuple[int, int]] = None,
    owners: Optional[Iterable[int]] = None,
    created: Optional[list] = None,
) -> int:
    """An fd for anchor/rel, reached without following any link (the caller closes it).

    ``create`` makes missing components (``mode``, and ``chown`` as (uid, gid) when given; both are
    applied on the OPEN fd, so they land on the directory that was made and nothing else).
    ``owners`` requires every EXISTING component below the anchor to be owned by one of those
    uids. Created components are appended to ``created`` as anchor-relative names. An empty
    ``rel`` returns the anchor itself."""
    owners = None if owners is None else frozenset(owners)
    comps = parts(rel) if rel not in ("", ".") else []
    fd = open_anchor(anchor)
    try:
        walked = []
        for name in comps:
            walked.append(name)
            shown = "/".join(walked)
            made = False
            try:
                child = _open_child(fd, name, shown)
            except FileNotFoundError:
                if not create:
                    raise Refused(f"{shown} does not exist") from None
                try:
                    os.mkdir(name, 0o700, dir_fd=fd)  # tightened until fchown/fchmod below
                    made = True
                except FileExistsError:
                    pass  # raced with another creator; the open below decides what it is
                child = _open_child(fd, name, shown)
            os.close(fd)
            fd = child
            if made:
                if chown is not None:
                    os.fchown(fd, chown[0], chown[1])
                os.fchmod(fd, mode)
                if created is not None:
                    created.append(shown)
            else:
                _check_owner(fd, owners, shown)
    except BaseException:
        os.close(fd)
        raise
    return fd


@contextlib.contextmanager
def dir_at(anchor: Anchor, rel: str = "", **kw) -> Iterator[int]:
    """``open_dir`` as a context manager."""
    fd = open_dir(anchor, rel, **kw)
    try:
        yield fd
    finally:
        os.close(fd)


def _split(rel: str) -> Tuple[str, str]:
    comps = parts(rel)
    return "/".join(comps[:-1]), comps[-1]


def open_file(
    anchor: Anchor,
    rel: str,
    flags: int = os.O_RDONLY,
    *,
    owners: Optional[Iterable[int]] = None,
    file_owners: Optional[Iterable[int]] = None,
) -> int:
    """An fd for an EXISTING regular file at anchor/rel (the caller closes it). Never creates:
    a write that may create goes through ``write_file``. A link at the leaf, or anything that is
    not a regular file (a FIFO would block, a device would be read), is refused."""
    if flags & (os.O_CREAT | getattr(os, "O_TMPFILE", 0)):
        raise ValueError("open_file never creates; use write_file")
    parent, name = _split(rel)
    with dir_at(anchor, parent, owners=owners) as pfd:
        try:
            fd = os.open(name, flags | _FILE_FLAGS | getattr(os, "O_NONBLOCK", 0), dir_fd=pfd)
        except OSError as exc:
            if exc.errno in _NOT_A_REAL_DIR:
                raise Refused(f"{rel} is a symlink: refusing") from None
            raise
    try:
        st = _check_owner(fd, file_owners, rel)
        if not stat.S_ISREG(st.st_mode):
            raise Refused(f"{rel} is not a regular file")
        if hasattr(os, "O_NONBLOCK"):
            os.set_blocking(fd, True)
    except BaseException:
        os.close(fd)
        raise
    return fd


def read_file(anchor: Anchor, rel: str, **kw) -> bytes:
    fd = open_file(anchor, rel, os.O_RDONLY, **kw)
    with os.fdopen(fd, "rb") as fh:
        return fh.read()


def mkstemp_at(dir_fd: int, name: str, mode: int = 0o600) -> Tuple[int, str]:
    """A new, empty file beside ``name`` in the directory ``dir_fd``: O_CREAT|O_EXCL|O_NOFOLLOW,
    so it can never be (or follow) something already there. Returns (fd, temp name)."""
    for _ in range(16):
        tmp = f".{name}.{os.getpid()}.{os.urandom(6).hex()}.tmp"
        try:
            fd = os.open(
                tmp,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | _FILE_FLAGS,
                mode,
                dir_fd=dir_fd,
            )
        except FileExistsError:
            continue
        return fd, tmp
    raise Refused(f"could not create a temp file beside {name!r}")


def write_file(
    anchor: Anchor,
    rel: str,
    data: Union[bytes, str],
    mode: int = 0o644,
    *,
    create_dirs: bool = False,
    dir_mode: int = 0o755,
    owners: Optional[Iterable[int]] = None,
    chown: Optional[Tuple[int, int]] = None,
    dir_chown: Optional[Tuple[int, int]] = None,
    keep_owner: bool = False,
    fsync: bool = True,
) -> None:
    """Atomically replace anchor/rel with ``data``, never writing through a link.

    The temp file is made O_EXCL|O_NOFOLLOW in the target's own directory (reached by the walk),
    gets ``mode`` and ``chown`` on its fd, and is renamed over the NAME. A link already at the
    name is replaced by the rename; its target is untouched. ``create_dirs`` makes missing
    parents with ``dir_mode``/``dir_chown``; ``owners`` is checked on every existing directory.
    ``keep_owner`` (instead of ``chown``) gives the replacement the uid/gid of the regular file it
    replaces, or of the directory it lands in when there is none: an in-place overwrite kept the
    old owner, and a rename must not silently hand the agent's file to root."""
    if keep_owner and chown is not None:
        raise ValueError("keep_owner and chown are exclusive")
    if isinstance(data, str):
        data = data.encode("utf-8")
    parent, name = _split(rel)
    with dir_at(
        anchor,
        parent,
        create=create_dirs,
        mode=dir_mode,
        chown=dir_chown,
        owners=owners,
    ) as pfd:
        if keep_owner:
            try:
                st = os.stat(name, dir_fd=pfd, follow_symlinks=False)
                if not stat.S_ISREG(st.st_mode):
                    st = os.fstat(pfd)
            except FileNotFoundError:
                st = os.fstat(pfd)
            chown = (st.st_uid, st.st_gid)
        fd, tmp = mkstemp_at(pfd, name)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                if chown is not None:
                    os.fchown(fh.fileno(), chown[0], chown[1])
                os.fchmod(fh.fileno(), mode)
                if fsync:
                    os.fsync(fh.fileno())
            try:
                os.rename(tmp, name, src_dir_fd=pfd, dst_dir_fd=pfd)
            except IsADirectoryError:
                raise Refused(f"{rel} is a directory: refusing to replace it") from None
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp, dir_fd=pfd)
            raise


def unlink(anchor: Anchor, rel: str, *, owners: Optional[Iterable[int]] = None) -> bool:
    """Remove the NAME anchor/rel. A link is removed itself (never its target); a directory is
    refused. Returns whether anything was there."""
    parent, name = _split(rel)
    with dir_at(anchor, parent, owners=owners) as pfd:
        try:
            st = os.stat(name, dir_fd=pfd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        if stat.S_ISDIR(st.st_mode):
            raise Refused(f"{rel} is a directory: unlink refuses it")
        try:
            os.unlink(name, dir_fd=pfd)
        except FileNotFoundError:
            return False
        return True


def ensure_dir(
    anchor: Anchor,
    rel: str,
    *,
    mode: int = 0o755,
    chown: Optional[Tuple[int, int]] = None,
    owners: Optional[Iterable[int]] = None,
    own_leaf: bool = False,
) -> dict:
    """Every component of anchor/rel exists as a real directory. Created ones get ``mode`` and
    ``chown``; existing ones must be owned by one of ``owners``. ``own_leaf`` also fchowns an
    existing leaf to ``chown`` (docker invents a missing bind-mount parent as root).
    Returns {"created": [...], "repaired": [...]} as anchor-relative names."""
    created: list = []
    repaired: list = []
    fd = open_dir(anchor, rel, create=True, mode=mode, chown=chown, owners=owners, created=created)
    try:
        leaf = "/".join(parts(rel))
        if own_leaf and chown is not None and leaf not in created:
            st = os.fstat(fd)
            if (st.st_uid, st.st_gid) != tuple(chown):
                os.fchown(fd, chown[0], chown[1])
                repaired.append(leaf)
    finally:
        os.close(fd)
    return {"created": created, "repaired": repaired}


def run_as(uid: int, gid: int, fn: Callable[[], object]) -> None:
    """Run fn() in a forked child that has dropped to uid/gid for good (all three ids, no
    supplementary groups), so it can only do what that uid could already do. The child's failure
    is raised here as Refused with its reason; nothing runs in the parent's privileges."""
    sys.stdout.flush()
    sys.stderr.flush()
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:  # child: never returns
        os.close(r)
        code, msg = 0, b""
        try:
            os.setgroups([])
            os.setresgid(gid, gid, gid)
            os.setresuid(uid, uid, uid)
            if os.geteuid() != uid or os.getegid() != gid:
                raise Refused("the privilege drop did not take")
            fn()
        except BaseException as exc:  # noqa: BLE001 - every failure must reach the parent
            code, msg = 1, f"{exc}".encode("utf-8", "replace")[:2000]
        try:
            os.write(w, msg)
        finally:
            os._exit(code)
    os.close(w)
    with os.fdopen(r, "rb") as fh:
        msg = fh.read().decode("utf-8", "replace")
    _, status = os.waitpid(pid, 0)
    if os.waitstatus_to_exitcode(status) != 0:
        raise Refused(f"as uid {uid}: {msg or 'no reason given'}")
