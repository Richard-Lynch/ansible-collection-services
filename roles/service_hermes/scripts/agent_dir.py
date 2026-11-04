#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Ensure directories inside the agent's tree WITHOUT following anything the agent planted.

Runs as root on the guest (service_hermes, _automations_deliver.yml). The agent (uid 10000 in the
container) can write under data/, so any path below it may be a symlink by the time root looks.
Ansible's `file: state=directory owner=...` resolves the path by name and chowns whatever it
lands on, which turns a planted `data/scripts -> /etc` into root chowning /etc to the agent.

So this walks from a ROOT-OWNED ANCHOR (hermes_config_dir) one component at a time with
dir_fd-relative calls: every component is opened O_NOFOLLOW|O_DIRECTORY, so a symlink is
refused (ELOOP/ENOTDIR) rather than followed, and every chown/chmod is done on the open fd
(fchown/fchmod), which names the directory that was checked and nothing else. A name swapped
between the mkdir and the open fails the open; it can never redirect it.

  --ensure REL   every component of REL exists as a real directory (created ones get
                 --uid/--gid/--mode). An existing component must be owned by the agent or by the
                 anchor's owner; anything else is refused.
  --own REL      as --ensure, and the LEAF is owned by the agent: a leaf owned by the anchor's
                 owner (docker invents a missing bind-mount parent as root) is fchown'd back.

  --set-line REL --key KEY
                 REL is a KEY=VALUE file (data/.env) in the agent's tree. Every line starting
                 `KEY=` is replaced by ONE `KEY=<value>` line where the first one was (appended
                 when there is none), and the file ends up --uid:--gid --file-mode. The value comes
                 from the environment variable AGENT_DIR_VALUE, never argv (argv is in `ps` and
                 in Ansible's log). The parent is reached by the same walk; the file is read
                 O_NOFOLLOW (a link at the name is refused, never followed: `lineinfile` would
                 realpath() it and append to whatever it points at, then chown that) and written
                 by an O_EXCL temp file renamed over the name. Unchanged content, owner and mode
                 write nothing.

  --write-file REL
                 REL is a file the ROLE owns inside the agent's tree (a dashboard theme). Its
                 whole content becomes the AGENT_DIR_CONTENT environment variable, reached by the
                 same walk and written the same way as --set-line: a link at the name or the parent
                 is refused, a new file is renamed over the name, and unchanged content, owner and
                 mode write nothing.

  --seed         with --write-file: write only when REL does not exist yet (an existing file, the
                 operator's or the agent's, is left exactly as it is). The walk and the checks are
                 the same; a link at the name is still refused.

  --read-file REL
                 print REL's text as "content" in the JSON line ("exists": false and no content
                 when it is absent), reached by the same walk WITHOUT creating anything: a link at
                 the name or on the way is refused, never followed, so root never reads a file the
                 agent pointed it at (read-merge-write would otherwise copy that file's text into
                 the agent's own config).

  --touch REL    REL is a file that must EXIST (an append-only log a shipper must discover before
                 its first line). Created empty, --uid:--gid --file-mode, by O_CREAT|O_EXCL|
                 O_NOFOLLOW under the walked parent when absent; an existing one is never opened
                 for writing, truncated or re-stamped -- only checked to be a regular file owned
                 by the agent or the anchor's owner (a link at the name is refused).

Prints one JSON line {"changed": bool, "created": [...], "repaired": [...]}. A refusal exits 2
with the reason on stderr and changes nothing past the refused component.

This file stays standalone (it is shipped by `ansible.builtin.script`, which sends ONE file), so it
cannot import the confined_io module the learning and state roles ship, whose walk it shares.
Tests: roles/service_hermes/scripts/test_agent_dir.py
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import stat
import sys

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


class Refused(Exception):
    pass


def _open_anchor(anchor: str, anchor_uid: int) -> int:
    try:
        fd = os.open(anchor, _DIR_FLAGS)
    except OSError as exc:
        raise Refused(f"anchor {anchor!r} is not a real directory: {exc.strerror}") from None
    st = os.fstat(fd)
    if st.st_uid != anchor_uid:
        os.close(fd)
        raise Refused(f"anchor {anchor!r} is owned by uid {st.st_uid}, expected {anchor_uid}")
    if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        os.close(fd)
        raise Refused(f"anchor {anchor!r} is group/other-writable ({oct(st.st_mode & 0o777)})")
    return fd


def _components(rel: str) -> list[str]:
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if not parts or rel.startswith("/") or ".." in parts:
        raise Refused(f"path {rel!r} must be relative to the anchor, with no '..'")
    return parts


def _open_child(parent_fd: int, name: str, shown: str) -> int:
    try:
        return os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise Refused(
                f"{shown} is not a real directory (a symlink or a file): refusing"
            ) from None
        raise


def _walk(
    anchor: str,
    anchor_uid: int,
    parts: list[str],
    uid: int,
    gid: int,
    mode: int,
    report: dict,
) -> tuple[int, str, bool]:
    """Open anchor/parts as an fd (the caller closes it), creating missing components. Returns
    (fd, shown path, whether the LAST component was created here)."""
    fd = _open_anchor(anchor, anchor_uid)
    shown = anchor
    created = False
    try:
        for name in parts:
            shown = f"{shown}/{name}"
            created = False
            try:
                child = _open_child(fd, name, shown)
            except FileNotFoundError:
                try:
                    os.mkdir(name, mode, dir_fd=fd)
                    created = True
                except FileExistsError:
                    pass  # raced with another creator; the open below decides what it is
                child = _open_child(fd, name, shown)
            os.close(fd)
            fd = child
            if created:
                os.fchown(fd, uid, gid)
                os.fchmod(fd, mode)
                report["created"].append(shown)
                continue
            st = os.fstat(fd)
            if st.st_uid not in (uid, anchor_uid):
                raise Refused(
                    f"{shown} is owned by uid {st.st_uid}, expected {uid} (agent) or {anchor_uid}"
                )
    except BaseException:
        os.close(fd)
        raise
    return fd, shown, created


def ensure(
    anchor: str,
    anchor_uid: int,
    rel: str,
    uid: int,
    gid: int,
    mode: int,
    own: bool,
    report: dict,
) -> None:
    parts = _components(rel)
    fd, shown, created = _walk(anchor, anchor_uid, parts, uid, gid, mode, report)
    try:
        if own and not created and os.fstat(fd).st_uid != uid:
            os.fchown(fd, uid, gid)
            report["repaired"].append(shown)
    finally:
        os.close(fd)


def _set_line_text(text: str, key: str, value: str) -> str:
    """Every `key=` line replaced by one `key=value` line at the first one's position."""
    new = f"{key}={value}"
    out: list[str] = []
    placed = False
    for line in text.splitlines():
        if line.startswith(f"{key}="):
            if not placed:
                out.append(new)
                placed = True
            continue
        out.append(line)
    if not placed:
        out.append(new)
    return "\n".join(out) + "\n"


def _rewrite(
    anchor: str,
    anchor_uid: int,
    rel: str,
    transform,
    uid: int,
    gid: int,
    dir_mode: int,
    file_mode: int,
    report: dict,
) -> None:
    """Replace the text of the file REL with transform(old text), reached by the confined walk.

    The file is read O_NOFOLLOW (a link at the name is refused, never followed) and written by an
    O_EXCL temp file renamed over the name. Unchanged content, owner and mode write nothing."""
    parts = _components(rel)
    pfd, shown, _ = _walk(anchor, anchor_uid, parts[:-1], uid, gid, dir_mode, report)
    name = parts[-1]
    shown = f"{shown}/{name}"
    try:
        try:
            ffd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=pfd)
        except FileNotFoundError:
            ffd = None
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.EMLINK):
                raise Refused(f"{shown} is a symlink: refusing") from None
            raise
        old, st = "", None
        if ffd is not None:
            with os.fdopen(ffd, "rb") as fh:
                st = os.fstat(fh.fileno())
                if not stat.S_ISREG(st.st_mode):
                    raise Refused(f"{shown} is not a regular file: refusing")
                if st.st_uid not in (uid, anchor_uid):
                    raise Refused(
                        f"{shown} is owned by uid {st.st_uid}, expected {uid} or {anchor_uid}"
                    )
                try:
                    old = fh.read().decode("utf-8")
                except UnicodeDecodeError:
                    raise Refused(f"{shown} is not UTF-8 text: refusing to rewrite it") from None
        new = transform(old)
        if (
            st is not None
            and new == old
            and (st.st_uid, st.st_gid) == (uid, gid)
            and st.st_mode & 0o7777 == file_mode
        ):
            return
        tmp = f".{name}.{os.getpid()}.{os.urandom(6).hex()}.tmp"
        tfd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=pfd)
        try:
            with os.fdopen(tfd, "wb") as fh:
                fh.write(new.encode("utf-8"))
                fh.flush()
                os.fchown(fh.fileno(), uid, gid)
                os.fchmod(fh.fileno(), file_mode)
                os.fsync(fh.fileno())
            os.rename(tmp, name, src_dir_fd=pfd, dst_dir_fd=pfd)
        except BaseException:
            try:
                os.unlink(tmp, dir_fd=pfd)
            except FileNotFoundError:
                pass
            raise
        report["repaired" if st is not None else "created"].append(shown)
    finally:
        os.close(pfd)


def set_line(
    anchor: str,
    anchor_uid: int,
    rel: str,
    key: str,
    value: str,
    uid: int,
    gid: int,
    dir_mode: int,
    file_mode: int,
    report: dict,
) -> None:
    if not key or "=" in key or "\n" in key or "\n" in value:
        raise Refused("the key must be non-empty with no '=' or newline, and the value one line")
    _rewrite(
        anchor,
        anchor_uid,
        rel,
        lambda old: _set_line_text(old, key, value),
        uid,
        gid,
        dir_mode,
        file_mode,
        report,
    )


def write_file(
    anchor: str,
    anchor_uid: int,
    rel: str,
    content: str,
    uid: int,
    gid: int,
    dir_mode: int,
    file_mode: int,
    report: dict,
    seed: bool = False,
) -> None:
    """REL's whole content becomes `content` (a role-owned file inside the agent's tree). With
    `seed`, only when REL does not exist yet."""
    if seed:
        probe: dict = {}
        read_file(anchor, anchor_uid, rel, uid, probe)
        if probe.get("exists"):
            return
    _rewrite(anchor, anchor_uid, rel, lambda _old: content, uid, gid, dir_mode, file_mode, report)


def read_file(anchor: str, anchor_uid: int, rel: str, uid: int, report: dict) -> None:
    """REL's text into report["content"] (report["exists"] False when absent). Creates nothing."""
    parts = _components(rel)
    fd = _open_anchor(anchor, anchor_uid)
    shown = anchor
    try:
        for name in parts[:-1]:
            shown = f"{shown}/{name}"
            try:
                child = _open_child(fd, name, shown)
            except FileNotFoundError:
                report["exists"] = False
                return
            os.close(fd)
            fd = child
            st = os.fstat(fd)
            if st.st_uid not in (uid, anchor_uid):
                raise Refused(
                    f"{shown} is owned by uid {st.st_uid}, expected {uid} (agent) or {anchor_uid}"
                )
        shown = f"{shown}/{parts[-1]}"
        try:
            ffd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        except FileNotFoundError:
            report["exists"] = False
            return
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.EMLINK):
                raise Refused(f"{shown} is a symlink: refusing") from None
            raise
        with os.fdopen(ffd, "rb") as fh:
            st = os.fstat(fh.fileno())
            if not stat.S_ISREG(st.st_mode):
                raise Refused(f"{shown} is not a regular file: refusing")
            if st.st_uid not in (uid, anchor_uid):
                raise Refused(
                    f"{shown} is owned by uid {st.st_uid}, expected {uid} or {anchor_uid}"
                )
            try:
                report["content"] = fh.read().decode("utf-8")
            except UnicodeDecodeError:
                raise Refused(f"{shown} is not UTF-8 text: refusing") from None
        report["exists"] = True
    finally:
        os.close(fd)


def touch(
    anchor: str,
    anchor_uid: int,
    rel: str,
    uid: int,
    gid: int,
    dir_mode: int,
    file_mode: int,
    report: dict,
) -> None:
    parts = _components(rel)
    pfd, shown, _ = _walk(anchor, anchor_uid, parts[:-1], uid, gid, dir_mode, report)
    name = parts[-1]
    shown = f"{shown}/{name}"
    try:
        try:
            ffd = os.open(
                name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=pfd
            )
        except FileExistsError:
            # EEXIST also covers a link at the name (O_EXCL never follows one): look without
            # following, and judge what is there.
            st = os.stat(name, dir_fd=pfd, follow_symlinks=False)
            if stat.S_ISLNK(st.st_mode):
                raise Refused(f"{shown} is a symlink: refusing") from None
            if not stat.S_ISREG(st.st_mode):
                raise Refused(f"{shown} is not a regular file: refusing") from None
            if st.st_uid not in (uid, anchor_uid):
                raise Refused(
                    f"{shown} is owned by uid {st.st_uid}, expected {uid} or {anchor_uid}"
                ) from None
            return
        try:
            os.fchown(ffd, uid, gid)
            os.fchmod(ffd, file_mode)
        finally:
            os.close(ffd)
        report["created"].append(shown)
    finally:
        os.close(pfd)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--anchor", required=True)
    ap.add_argument("--anchor-uid", type=int, required=True)
    ap.add_argument("--uid", type=int, required=True)
    ap.add_argument("--gid", type=int, required=True)
    ap.add_argument("--mode", default="0755")
    ap.add_argument("--ensure", action="append", default=[])
    ap.add_argument("--own", action="append", default=[])
    ap.add_argument("--set-line", dest="set_line")
    ap.add_argument("--touch", action="append", default=[])
    ap.add_argument("--write-file", dest="write_file")
    ap.add_argument("--seed", action="store_true")
    ap.add_argument("--read-file", dest="read_file")
    ap.add_argument("--key")
    ap.add_argument("--file-mode", default="0600")
    args = ap.parse_args(argv)
    if bool(args.set_line) != bool(args.key):
        ap.error("--set-line and --key go together")
    if args.seed and not args.write_file:
        ap.error("--seed goes with --write-file")
    mode = int(args.mode, 8)
    report: dict = {"created": [], "repaired": []}
    try:
        for rel in args.own:
            ensure(
                args.anchor,
                args.anchor_uid,
                rel,
                args.uid,
                args.gid,
                mode,
                True,
                report,
            )
        for rel in args.ensure:
            ensure(
                args.anchor,
                args.anchor_uid,
                rel,
                args.uid,
                args.gid,
                mode,
                False,
                report,
            )
        for rel in args.touch:
            touch(
                args.anchor,
                args.anchor_uid,
                rel,
                args.uid,
                args.gid,
                mode,
                int(args.file_mode, 8),
                report,
            )
        if args.set_line:
            value = os.environ.get("AGENT_DIR_VALUE")
            if value is None:
                raise Refused(
                    "--set-line needs its value in the AGENT_DIR_VALUE environment variable"
                )
            set_line(
                args.anchor,
                args.anchor_uid,
                args.set_line,
                args.key,
                value,
                args.uid,
                args.gid,
                mode,
                int(args.file_mode, 8),
                report,
            )
        if args.write_file:
            content = os.environ.get("AGENT_DIR_CONTENT")
            if content is None:
                raise Refused(
                    "--write-file needs its content in the AGENT_DIR_CONTENT environment variable"
                )
            write_file(
                args.anchor,
                args.anchor_uid,
                args.write_file,
                content,
                args.uid,
                args.gid,
                mode,
                int(args.file_mode, 8),
                report,
                seed=args.seed,
            )
        if args.read_file:
            read_file(args.anchor, args.anchor_uid, args.read_file, args.uid, report)
    except Refused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        print(
            json.dumps(
                {
                    "changed": bool(report["created"] or report["repaired"]),
                    **report,
                    "refused": str(exc),
                }
            )
        )
        return 2
    print(json.dumps({"changed": bool(report["created"] or report["repaired"]), **report}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
