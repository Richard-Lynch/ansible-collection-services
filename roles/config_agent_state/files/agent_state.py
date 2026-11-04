#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""agent_state.py - the mechanics behind the config_agent_state role.

Stdlib only, plus three binaries the role installs and asserts: `age`, `zstd`, `git`.

Why this is a script and not a pile of shell tasks: every property worth proving here (a WAL's
uncommitted pages survive the backup, a flipped byte is refused before anything is touched, a
planted secret blocks the commit) is a property of the CODE, and a script can be run by the
molecule scenario on real files, exactly as the role runs it on the guest.

Output contract: each subcommand prints ONE JSON object on its last stdout line and exits 0, or
prints a refusal to stderr and exits non-zero. It reports names, sizes, counts and hashes. It
NEVER prints a file's content or a matched secret - a scanner that quotes what it found has
leaked it into the run log it was written to protect.

Exit codes: 0 ok, 2 refused (bad input / failed validation, nothing changed), 3 scan blocked.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import confined_io  # shipped beside this script (the confined-write module, vendored)

if sys.version_info < (3, 12):  # tarfile extraction filters
    sys.stderr.write("agent_state.py needs python >= 3.12\n")
    raise SystemExit(2)

LABEL_RE = re.compile(r"[A-Za-z0-9._-]{1,48}\Z")
ARCHIVE_RE = re.compile(
    r"(?P<product>[a-z0-9-]+)-state-(?P<ts>\d{8}T\d{6}Z)-(?P<label>[A-Za-z0-9._-]+)\.tar\.zst\.age\Z"
)
MASK = "***MASKED***"
# A file shaped like a database that was not declared in --sqlite: it would be copied as a plain
# file, WAL and all, which is exactly the inconsistent copy the backup API exists to avoid.
UNDECLARED_DB_RE = re.compile(r"\.(?:db|sqlite|sqlite3)\Z")

# Generic secret and PII SHAPES. Estate-specific additions arrive from the caller.
SCAN_RULES = {
    "private-key-block": r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    "age-secret-key": r"AGE-SECRET-KEY-1[A-Z0-9]{20,}",
    "github-token": r"gh[pousr]_[A-Za-z0-9]{30,}",
    "github-fine-grained": r"github_pat_[A-Za-z0-9_]{30,}",
    "slack-token": r"xox[abprs]-[A-Za-z0-9-]{10,}",
    "llm-style-key": r"sk-[A-Za-z0-9_-]{24,}",
    "aws-access-key": r"AKIA[0-9A-Z]{16}",
    "google-api-key": r"AIza[0-9A-Za-z_-]{30,}",
    "bearer-credential": r"(?i:bearer)[ ]+[A-Za-z0-9._~+/=-]{24,}",
    "jwt": r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}",
    "email-address": r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+\.[A-Za-z]{2,}",
    "secret-assignment": (
        r"(?i:pass(?:word|wd)?|secret|token|api[_-]?key|credential)[\"']?[ ]*[:=][ ]*"
        r"[\"']?(?![*$<{])[^\s\"']{8,}"
    ),
}
# Key names whose VALUE is masked in the config snapshot (line based: no YAML parser is needed
# or available on a minimal guest).
DEFAULT_MASK_KEYS = r"(?i:pass(?:word|wd)?|secret|token|api[_-]?key|credential|private[_-]?key)"


# One refusal type, so a confined_io refusal (a link on an fd walk) exits 2 like every other.
Refused = confined_io.Refused


def out(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, sort_keys=True) + "\n")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def need(binary: str) -> str:
    found = shutil.which(binary)
    if not found:
        raise Refused(f"required binary not found on PATH: {binary}")
    return found


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def safe_rel(p: str) -> str:
    if not p or os.path.isabs(p) or ".." in Path(p).parts:
        raise Refused(f"path must be relative with no '..': {p!r}")
    return p


def confined(root: Path, rel: str) -> Path:
    """root/rel, proven to stay inside root.

    The state tree is WRITABLE BY THE AGENT while this script runs as root, so any symlink the
    agent plants (file or directory) would make root read or write wherever it points. Refuse
    ``..``/absolute rel (``safe_rel``), refuse a symlink at ANY component from root down, and
    require the resolved path to sit under the resolved root."""
    safe_rel(rel)
    cur = root
    for part in Path(rel).parts:
        cur = cur / part
        if cur.is_symlink():
            raise Refused(f"symlink in path (refused, root follows no links): {cur}")
    real_root = root.resolve()
    if not cur.resolve().is_relative_to(real_root):
        raise Refused(f"path escapes the state root: {rel!r}")
    return cur


def walk_files(root: Path, rel: str, skipped: list[str]) -> list[Path]:
    """Regular files under root/rel (or root/rel itself), never through a symlink.

    Symlinked files, symlinked directories (and so everything below them) and a symlinked ``rel``
    are appended to ``skipped`` as root-relative names, so they are reported, not silently dropped.
    """
    try:
        base = confined(root, rel)
    except Refused:
        skipped.append(rel)
        return []
    if base.is_file():
        return [base]
    found: list[Path] = []
    if not base.is_dir():
        return found
    for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
        d = Path(dirpath)
        for name in list(dirnames):
            if (d / name).is_symlink():
                skipped.append(str((d / name).relative_to(root)))
                dirnames.remove(name)
        dirnames.sort()
        for name in sorted(filenames):
            f = d / name
            if f.is_symlink():
                skipped.append(str(f.relative_to(root)))
            elif f.is_file():
                found.append(f)
    return found


def copy_nofollow(
    src: Path, dst: Path, mode: int | None = None, src_root: Path | None = None
) -> None:
    """Copy bytes src -> dst opening BOTH without following a final-component symlink.

    A symlink already at dst is unlinked first (never written through). ``src_root`` names the
    agent-writable tree ``src`` sits in: the source is then opened by an fd walk from it
    (confined_io.open_file), so a directory swapped for a link AFTER the walk listed it cannot
    redirect the read; without it only the last component is protected."""
    if dst.is_symlink():
        dst.unlink()
    if src_root is not None:
        sfd = confined_io.open_file(src_root, str(src.relative_to(src_root)))
    else:
        sfd = os.open(src, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        if mode is None:
            mode = os.fstat(sfd).st_mode & 0o7777
        dfd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, mode)
        try:
            with (
                os.fdopen(sfd, "rb", closefd=False) as fin,
                os.fdopen(dfd, "wb", closefd=False) as fout,
            ):
                shutil.copyfileobj(fin, fout)
        finally:
            os.close(dfd)
    finally:
        os.close(sfd)


def excluded(rel: str, patterns: list[str]) -> bool:
    parts = Path(rel).parts
    for pat in patterns:
        if fnmatch.fnmatch(rel, pat) or any(fnmatch.fnmatch(x, pat) for x in parts):
            return True
        # "dir/" style: anything under a directory with that name -- a single segment anywhere
        # ("logs/"), or a multi-segment path from the root ("home/.pki/"). The segment test alone
        # never matches a pattern with a slash inside, so `learning/recall-scratch/` would exclude
        # nothing and `home/.pki/` would let the backup refuse cert9.db.
        if pat.endswith("/"):
            d = pat.rstrip("/")
            if d in parts[:-1] or rel.startswith(d + "/"):
                return True
    return False


# --------------------------------------------------------------------------- sqlite
def sqlite_snapshot(src: Path, dst: Path) -> dict:
    """Copy via the online-backup API (WAL pages included), then PROVE the copy.

    A `cp` of the .db file alone omits whatever still sits in the -wal; the backup API reads
    through the connection and does not.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        d = sqlite3.connect(dst)
        try:
            s.backup(d)
        finally:
            d.close()
    finally:
        s.close()
    return sqlite_facts(dst)


def sqlite_facts(path: Path) -> dict:
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        res = [r[0] for r in c.execute("PRAGMA integrity_check")]
        tables = {}
        for (name,) in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall():
            tables[name] = c.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]
    finally:
        c.close()
    return {
        "integrity": "ok" if res == ["ok"] else "; ".join(res[:3]),
        "tables": tables,
    }


# --------------------------------------------------------------------------- metrics
def write_metrics(path: str | None, lines: list[str]) -> None:
    if not path:
        return
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n")
    os.replace(tmp, p)


def previous_metric_lines(path: str | None, family: str) -> list[str]:
    """The TYPE and sample lines of one metric family from an existing textfile, or []."""
    if not path or not Path(path).is_file():
        return []
    keep = []
    for line in Path(path).read_text().splitlines():
        if line.startswith(f"# TYPE {family} ") or line.startswith(family + "{"):
            keep.append(line)
    return keep


# --------------------------------------------------------------------------- backup
def keep_owner(src: Path, dst: Path) -> None:
    """Give the staged copy the live file's owner and mode before it is archived.

    The archive is built from STAGED copies (so the manifest hashes what the archive holds), and
    ``shutil.copy2``/the sqlite backup API create them as the user running the backup (root), so
    every file would enter the archive as uid 0. A restore would then hand the agent a tree it
    cannot write: Hermes dies with ``PermissionError ... installs/<id>/.install.lock``, because its
    image only re-chowns a fixed list of subdirectories at boot. Ownership is best-effort (a
    non-root backup cannot chown)."""
    st = os.lstat(src)  # lstat + lchown: a planted link is described, never followed
    try:
        os.chown(dst, st.st_uid, st.st_gid, follow_symlinks=False)
    except OSError:
        pass
    if not dst.is_symlink():
        os.chmod(dst, st.st_mode & 0o7777)


def cmd_backup(a) -> None:
    age, zstd = need("age"), need("zstd")
    if not LABEL_RE.match(a.label):
        raise Refused(f"label must match {LABEL_RE.pattern}")
    if not a.recipient.startswith("age1"):
        raise Refused("recipient must be an age public key (age1...)")
    root, target = Path(a.root), Path(a.target)
    if not root.is_dir():
        raise Refused(f"state root is not a directory: {root}")
    if not target.is_dir():
        raise Refused(f"backup target is not a directory (is the share mounted?): {target}")
    sqlite_rel = [safe_rel(x) for x in a.sqlite]
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    name = f"{a.product}-state-{ts}-{a.label}.tar.zst.age"
    if (target / name).exists():
        raise Refused(f"archive already exists: {name}")

    with tempfile.TemporaryDirectory(prefix="agent-state-", dir=a.tmpdir or None) as tmp:
        stage = Path(tmp) / "stage"
        stage.mkdir()
        sqlite_info: dict[str, dict] = {}
        skip_live = set()
        for rel in sqlite_rel:
            src = confined(root, rel)
            if not src.is_file():
                raise Refused(f"declared sqlite file is missing: {rel}")
            try:
                facts = sqlite_snapshot(src, stage / rel)
            except sqlite3.Error as e:
                raise Refused(f"sqlite could not read {rel}: {e}") from e
            if facts["integrity"] != "ok":
                raise Refused(f"sqlite integrity_check failed on {rel}: {facts['integrity']}")
            keep_owner(src, stage / rel)
            sqlite_info[rel] = facts
            skip_live.update({rel, rel + "-wal", rel + "-shm", rel + "-journal"})

        files = []
        dirs = []
        skipped_links: list[str] = []
        tar_path = Path(tmp) / "state.tar"
        with tarfile.open(tar_path, "w") as tf:
            for dirpath, dirnames, filenames in os.walk(root):
                rd = Path(dirpath).relative_to(root)
                dirnames[:] = sorted(
                    d
                    for d in dirnames
                    if not excluded(str(rd / d) + "/x", a.exclude)
                    and not excluded(str(rd / d), a.exclude)
                )
                # os.walk lists a symlinked directory in dirnames without descending; it is not
                # archived at all (no entry, nothing dereferenced). Record it.
                for d in list(dirnames):
                    if (Path(dirpath) / d).is_symlink():
                        skipped_links.append(str(rd / d) if str(rd) != "." else d)
                        dirnames.remove(d)
                # Directory entries too (mode and ownership), so a restore brings back a 0700
                # directory as 0700 and owned by the agent's uid, and an EMPTY directory at all.
                # Without them tarfile recreates every directory as the restorer (root, 0755).
                if str(rd) != ".":
                    tf.add(Path(dirpath), arcname=str(rd), recursive=False)
                    dirs.append(str(rd))
                for fn in sorted(filenames):
                    rel = str(rd / fn) if str(rd) != "." else fn
                    if rel in skip_live or excluded(rel, a.exclude):
                        continue
                    full = root / rel
                    if full.is_symlink():
                        skipped_links.append(rel)
                        continue
                    if not full.is_file():
                        continue
                    if UNDECLARED_DB_RE.search(fn):
                        raise Refused(
                            f"{rel} looks like a database but is not declared in --sqlite; a "
                            "plain copy of a live database can omit its WAL. Declare it."
                        )
                    # Copy into the stage FIRST, then hash and archive THAT copy: hashing the live
                    # file after tar read it describes bytes the archive may not hold if the agent
                    # wrote in between, and every later restore then refuses "member hash mismatch".
                    staged = stage / rel
                    staged.parent.mkdir(parents=True, exist_ok=True)
                    copy_nofollow(full, staged, src_root=root)
                    shutil.copystat(full, staged, follow_symlinks=False)
                    keep_owner(full, staged)
                    tf.add(staged, arcname=rel, recursive=False)
                    files.append(
                        {
                            "path": rel,
                            "size": staged.stat().st_size,
                            "sha256": sha256_file(staged),
                        }
                    )
            for rel in sqlite_rel:
                tf.add(stage / rel, arcname=rel, recursive=False)
                st = stage / rel
                files.append({"path": rel, "size": st.stat().st_size, "sha256": sha256_file(st)})

        zst = Path(tmp) / "state.tar.zst"
        r = run([zstd, "-q", "-T0", "-o", str(zst), str(tar_path)])
        if r.returncode:
            raise Refused(f"zstd failed: {r.stderr.strip()[:200]}")
        partial = target / (name + ".partial")
        r = run([age, "-r", a.recipient, "-o", str(partial), str(zst)])
        if r.returncode:
            partial.unlink(missing_ok=True)
            raise Refused(f"age failed: {r.stderr.strip()[:200]}")

    final = target / name
    os.replace(partial, final)
    manifest = {
        "product": a.product,
        "label": a.label,
        "created": ts,
        "archive": {
            "name": name,
            "sha256": sha256_file(final),
            "bytes": final.stat().st_size,
        },
        "files": sorted(files, key=lambda f: f["path"]),
        "dirs": sorted(dirs),
        "sqlite": sqlite_info,
    }
    mpath = target / (name + ".manifest.json")
    tmpm = mpath.with_suffix(".tmp")
    tmpm.write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")
    os.replace(tmpm, mpath)

    pruned = prune(target, a.product, a.keep, a.keep_monthly, protect=name)
    now = int(time.time())
    write_metrics(
        a.metrics_file,
        [
            "# TYPE agent_state_backup_last_success_timestamp_seconds gauge",
            f'agent_state_backup_last_success_timestamp_seconds{{product="{a.product}"}} {now}',
            "# TYPE agent_state_backup_bytes gauge",
            f'agent_state_backup_bytes{{product="{a.product}"}} {manifest["archive"]["bytes"]}',
            "# TYPE agent_state_backup_files gauge",
            f'agent_state_backup_files{{product="{a.product}"}} {len(files)}',
        ],
    )
    out(
        {
            "archive": name,
            "bytes": manifest["archive"]["bytes"],
            "sha256": manifest["archive"]["sha256"],
            "files": len(files),
            "sqlite": {k: v["tables"] for k, v in sqlite_info.items()},
            "skipped_symlinks": skipped_links[:50],
            "pruned": pruned,
        }
    )


def list_archives(target: Path, product: str) -> list[tuple[str, str]]:
    found = []
    for p in target.iterdir():
        m = ARCHIVE_RE.match(p.name)
        if m and m["product"] == product:
            found.append((m["ts"], p.name))
    return sorted(found, reverse=True)


def prune(target: Path, product: str, keep: int, keep_monthly: int, protect: str) -> list[str]:
    """Newest `keep` + the first archive of each of the last `keep_monthly` months.

    Only names matching ARCHIVE_RE for THIS product are ever candidates: the share holds other
    people's files and a prune that globbed would eat them.
    """
    arcs = list_archives(target, product)
    keepers = {n for _, n in arcs[:keep]}
    first_of_month: dict[str, str] = {}
    for ts, n in sorted(arcs):
        first_of_month.setdefault(ts[:6], n)
    for month in sorted(first_of_month, reverse=True)[:keep_monthly]:
        keepers.add(first_of_month[month])
    keepers.add(protect)
    gone = []
    for _, n in arcs:
        if n not in keepers:
            (target / n).unlink(missing_ok=True)
            (target / (n + ".manifest.json")).unlink(missing_ok=True)
            gone.append(n)
    return gone


def cmd_latest(a) -> None:
    arcs = list_archives(Path(a.target), a.product)
    if not arcs:
        raise Refused(f"no {a.product} archives in {a.target}")
    out({"archive": arcs[0][1]})


# --------------------------------------------------------------------------- restore
def decrypt_extract(archive: Path, manifest: dict, identity: str, dest: Path) -> None:
    age, zstd = need("age"), need("zstd")
    with tempfile.TemporaryDirectory(prefix="agent-state-dec-", dir=str(dest.parent)) as tmp:
        zst = Path(tmp) / "s.tar.zst"
        tar = Path(tmp) / "s.tar"
        r = run([age, "-d", "-i", identity, "-o", str(zst), str(archive)])
        if r.returncode:
            raise Refused("age could not decrypt the archive with the supplied identity")
        r = run([zstd, "-q", "-d", "-o", str(tar), str(zst)])
        if r.returncode:
            raise Refused("zstd could not decompress the decrypted archive")
        dest.mkdir(parents=True)
        with tarfile.open(tar) as tf:
            members = tf.getmembers()
            for m in members:
                if not (m.isfile() or m.isdir()):
                    raise Refused(f"archive member is not a plain file/dir: {m.name!r}")
                safe_rel(m.name)
            # extractall (not per-member extract) applies directory modes AFTER their contents
            # exist, so a 0500/0700 directory entry cannot block its own files. The `tar` filter
            # keeps uid/gid and modes (minus setuid/setgid/sticky and group/other write bits).
            tf.extractall(dest, members=members, filter="tar")


def cmd_restore(a) -> None:
    archive, root = Path(a.archive), Path(a.root)
    if not ARCHIVE_RE.match(archive.name):
        raise Refused(f"archive name does not match {ARCHIVE_RE.pattern}: {archive.name!r}")
    mpath = Path(a.manifest or str(archive) + ".manifest.json")
    if not archive.is_file() or not mpath.is_file():
        raise Refused("archive or manifest missing")
    manifest = json.loads(mpath.read_text())
    # (1) the ciphertext is what the manifest says it was - cheapest refusal first.
    if sha256_file(archive) != manifest["archive"]["sha256"]:
        raise Refused(
            "archive sha256 differs from its manifest (corrupt or tampered); nothing touched"
        )
    stage = root.with_name(root.name + f".restore-stage-{os.getpid()}")
    if stage.exists():
        raise Refused(f"stale stage directory exists: {stage}")
    try:
        decrypt_extract(archive, manifest, a.identity, stage)
        # (2) every member is exactly what was backed up, and nothing extra rode along.
        want = {f["path"]: f for f in manifest["files"]}
        have = {str(p.relative_to(stage)): p for p in stage.rglob("*") if p.is_file()}
        if set(want) != set(have):
            diff = sorted(set(want) ^ set(have))[:5]
            raise Refused(f"archive members differ from manifest (e.g. {diff}); nothing touched")
        for rel, f in want.items():
            if sha256_file(have[rel]) != f["sha256"]:
                raise Refused(f"member hash mismatch: {rel}; nothing touched")
        # (3) the databases are healthy AND hold what the manifest says they held.
        for rel, facts in manifest["sqlite"].items():
            try:
                got = sqlite_facts(stage / rel)
            except sqlite3.Error as e:
                raise Refused(f"sqlite could not read {rel} after unpack: {e}") from e
            if got["integrity"] != "ok":
                raise Refused(f"sqlite integrity_check failed after unpack: {rel}")
            if got["tables"] != facts["tables"]:
                raise Refused(f"sqlite row counts differ from manifest: {rel}")
        for rel in a.required:
            if not (stage / safe_rel(rel)).exists():
                raise Refused(f"required path absent from the restored tree: {rel}")
        # (4) swap, unless the live tree already IS this archive (converge: no-op, changed=false).
        # EQUALS, not CONTAINS: every archived file present with the right hash AND no other file
        # in the live tree, excluded paths and sqlite sidecars (-wal/-shm/-journal) aside. Rolling
        # back after the agent has created new files is exactly the "live tree holds more" case,
        # and it must restore (reporting changed=false there would restore nothing).
        matching = root.is_dir() and all(
            (root / rel).is_file() and sha256_file(root / rel) == f["sha256"]
            for rel, f in want.items()
        )
        if matching:
            sidecars = {
                rel + suffix
                for rel in manifest.get("sqlite", {})
                for suffix in ("-wal", "-shm", "-journal")
            }
            live = {
                str(p.relative_to(root))
                for p in root.rglob("*")
                if p.is_file() and not p.is_symlink()
            }
            extra = sorted(
                r for r in live - set(want) if r not in sidecars and not excluded(r, a.exclude)
            )
            if extra:
                matching = False
        if matching:
            shutil.rmtree(stage)
            out({"changed": False, "files": len(want), "moved_aside": None})
            return
        moved = None
        if root.exists():
            moved = root.with_name(root.name + f".pre-restore-{int(time.time())}")
            os.rename(root, moved)
        os.rename(stage, root)
        out(
            {
                "changed": True,
                "files": len(want),
                "moved_aside": str(moved) if moved else None,
            }
        )
    finally:
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)


def cmd_rewrite(a) -> None:
    """Re-write each file in place (same bytes) so a daemon that only loads on WRITE sees it."""
    n = 0
    skipped: list[str] = []
    for rel in a.paths:
        for p in walk_files(Path(a.root), rel, skipped):
            # An fd walk from the root, not os.open(p): a directory the agent swaps for a link
            # after walk_files listed it must not redirect a root read-write.
            fd = confined_io.open_file(a.root, str(p.relative_to(a.root)), os.O_RDWR)
            with os.fdopen(fd, "r+b") as f:
                data = f.read()
                f.seek(0)
                f.write(data)
                f.truncate()
                f.flush()
                os.fsync(f.fileno())
            n += 1
    out({"rewritten": n, "skipped_symlinks": skipped[:50]})


# --------------------------------------------------------------------------- mirror
def git(repo: Path, *args: str, env: dict | None = None, check: bool = True):
    e = dict(os.environ, **(env or {}))
    r = run(["git", "-C", str(repo), *args], env=e)
    if check and r.returncode:
        raise Refused(f"git {args[0]} failed: {r.stderr.strip()[:300]}")
    return r


def ssh_env(key: str | None) -> dict:
    if not key:
        return {}
    cmd = f"ssh -i {key} -o IdentitiesOnly=yes -o IdentityAgent=none -o StrictHostKeyChecking=accept-new"
    return {"GIT_SSH_COMMAND": cmd}


def mask_config(text: str, key_re: re.Pattern) -> str:
    out_lines = []
    for line in text.splitlines():
        m = re.match(r"(\s*(?:-\s+)?)([A-Za-z0-9_.\"'-]+)(\s*[:=]\s*)(\S.*)\Z", line)
        if m and key_re.search(m[2]) and not m[4].lstrip().startswith(("#", "|", ">")):
            out_lines.append(f"{m[1]}{m[2]}{m[3]}{MASK}")
        else:
            out_lines.append(line)
    return "\n".join(out_lines) + "\n"


def scan_text(text: str, rules: dict[str, re.Pattern]) -> list[tuple[str, int]]:
    hits = []
    for i, line in enumerate(text.splitlines(), 1):
        if MASK in line:
            continue
        for name, rx in rules.items():
            if rx.search(line):
                hits.append((name, i))
    return hits


def names_from_file(root: Path, rel: str) -> list[str]:
    """Skill names an upstream manifest lists (``name`` or ``name:hash`` per line), as exclude
    patterns. Used to keep the image's BUNDLED skills out of the mirror: they are the vendor's, they
    return with the image, and their example e-mail addresses and ``api_key=`` samples are exactly
    what the secret scan exists to block. An absent file excludes nothing, which is loud rather than
    silent here: the scan then blocks on the bundled files and names them."""
    if not rel:
        return []
    try:
        f = confined(root, rel)
    except Refused:
        return []  # a planted link excludes nothing: the scan then blocks and names the files
    if not f.is_file():
        return []
    names = []
    for line in f.read_text(errors="replace").splitlines():
        n = line.strip().partition(":")[0].strip()
        if n and not n.startswith("#") and not any(c in n for c in "*?[]/"):
            names.append(n)
    return names


def sync_tree(
    root: Path,
    repo: Path,
    paths: list[str],
    exclude: list[str],
    skipped: list[str] | None = None,
) -> list[str]:
    """Make repo/<path> equal root/<path> for every mirror path. Returns relative files written.

    Symlinks in the (agent-writable) live tree are never followed; they are appended to
    ``skipped`` when given."""
    written = []
    skipped = skipped if skipped is not None else []
    for rel in paths:
        safe_rel(rel)
        dst = repo / rel
        want = set()
        for p in walk_files(root, rel, skipped):
            r = str(p.relative_to(root))
            if not excluded(r, exclude):
                want.add(r)
        if dst.is_dir() and not dst.is_symlink():
            for p in sorted(dst.rglob("*")):
                r = str(p.relative_to(repo))
                if p.is_file() and r not in want:
                    p.unlink()
        elif dst.is_file() and rel not in want:
            dst.unlink()
        for r in sorted(want):
            (repo / r).parent.mkdir(parents=True, exist_ok=True)
            copy_nofollow(root / r, repo / r, src_root=root)
            written.append(r)
    return written


def cmd_mirror(a) -> None:
    need("git")
    root, repo = Path(a.root), Path(a.repo_dir)
    env = ssh_env(a.ssh_key)
    if not (repo / ".git").is_dir():
        repo.mkdir(parents=True, exist_ok=True)
        git(repo, "init", "-q", "-b", a.branch)
        git(repo, "remote", "add", "origin", a.remote_url)
    else:
        git(repo, "remote", "set-url", "origin", a.remote_url)
    # Build on the REMOTE's head, not this repo's last commit. The mirror repository may also be
    # written from outside (a seed commit pushed by hand), and a mirror that commits on its own
    # stale head is rejected as non-fast-forward on every run from then on. A soft reset
    # moves only HEAD, so the commit below is "the live tree, on top of whatever the remote holds".
    # An empty remote (first run) has nothing to fetch, which is fine.
    if git(repo, "fetch", "-q", "origin", a.branch, env=env, check=False).returncode == 0:
        git(repo, "reset", "-q", "--soft", "FETCH_HEAD")
    git(repo, "reset", "-q")  # nothing staged by an earlier blocked run may ride along

    a.exclude = list(a.exclude) + names_from_file(root, a.skip_names_file)
    skipped: list[str] = []
    written = sync_tree(root, repo, a.paths, a.exclude, skipped)
    if a.config_file:
        try:
            src = confined(root, a.config_file)
        except Refused:
            skipped.append(a.config_file)
            src = root / "\0unreadable"
        if src.is_file():
            key_re = re.compile("|".join([DEFAULT_MASK_KEYS, *a.mask_pattern]))
            snap = repo / "config" / (Path(a.config_file).name + ".masked")
            snap.parent.mkdir(parents=True, exist_ok=True)
            snap.write_text(mask_config(src.read_text(errors="replace"), key_re))
            written.append(str(snap.relative_to(repo)))

    rules = {k: re.compile(v) for k, v in SCAN_RULES.items()}
    for i, pat in enumerate(a.scan_pattern):
        rules[f"extra-{i}"] = re.compile(pat)
    findings = []
    for r in written:
        p = repo / r
        data = p.read_bytes()
        if b"\0" in data[:4096]:
            findings.append({"file": r, "rule": "binary-file", "line": 0})
            continue
        for rule, line in scan_text(data.decode("utf-8", "replace"), rules):
            findings.append({"file": r, "rule": rule, "line": line})
    if findings:
        # Carry the previous last-success line forward: write_metrics replaces the whole file,
        # and a staleness rule on `time() - ..._last_success_timestamp_seconds` returns NO series
        # while the series is absent, i.e. it could never fire for the whole time the mirror is
        # blocked. The rule must still `or` on the blocked gauge, since a mirror
        # that has never succeeded has no line to carry.
        write_metrics(
            a.metrics_file,
            previous_metric_lines(
                a.metrics_file, "agent_state_mirror_last_success_timestamp_seconds"
            )
            + [
                "# TYPE agent_state_mirror_scan_blocked gauge",
                f'agent_state_mirror_scan_blocked{{product="{a.product}"}} 1',
            ],
        )
        sys.stderr.write(
            "mirror BLOCKED by the secret/PII scan; nothing was staged, committed or pushed\n"
        )
        out({"blocked": True, "findings": findings[:50], "count": len(findings)})
        raise SystemExit(3)

    git(repo, "add", "-A", "--", ".")
    dirty = git(repo, "status", "--porcelain").stdout.strip()
    committed = False
    if dirty:
        git(
            repo,
            "-c",
            f"user.name={a.author_name}",
            "-c",
            f"user.email={a.author_email}",
            "commit",
            "-q",
            "-m",
            f"mirror {a.product} state {datetime.now(timezone.utc):%Y-%m-%dT%H:%MZ}",
        )
        committed = True
    head = git(repo, "rev-parse", "HEAD", check=False)
    head_sha = head.stdout.strip() if head.returncode == 0 else ""
    pushed = False
    if head_sha:
        remote = git(repo, "ls-remote", "origin", f"refs/heads/{a.branch}", env=env)
        remote_sha = remote.stdout.split()[0] if remote.stdout.strip() else ""
        if remote_sha != head_sha:
            git(repo, "push", "-q", "origin", f"HEAD:refs/heads/{a.branch}", env=env)
            pushed = True
    write_metrics(
        a.metrics_file,
        [
            "# TYPE agent_state_mirror_last_success_timestamp_seconds gauge",
            f'agent_state_mirror_last_success_timestamp_seconds{{product="{a.product}"}} {int(time.time())}',
            "# TYPE agent_state_mirror_scan_blocked gauge",
            f'agent_state_mirror_scan_blocked{{product="{a.product}"}} 0',
        ],
    )
    out(
        {
            "blocked": False,
            "committed": committed,
            "pushed": pushed,
            "head": head_sha[:12],
            "files": len(written),
            "skipped_symlinks": skipped[:50],
        }
    )


def cmd_mirror_diff(a) -> None:
    """Read-only: mirror paths in the live tree vs the work tree's last commit."""
    root, repo = Path(a.root), Path(a.repo_dir)
    a.exclude = list(a.exclude) + names_from_file(root, a.skip_names_file)
    if not (repo / ".git").is_dir():
        out({"initialised": False, "differing": [], "missing_in_mirror": [], "count": 0})
        return
    differing, missing = [], []
    skipped: list[str] = []
    for rel in a.paths:
        safe_rel(rel)
        for p in walk_files(root, rel, skipped):
            r = str(p.relative_to(root))
            if excluded(r, a.exclude):
                continue
            tracked = git(repo, "rev-parse", f"HEAD:{r}", check=False)
            if tracked.returncode:
                missing.append(r)
            elif tracked.stdout.strip() != git(repo, "hash-object", str(p)).stdout.strip():
                differing.append(r)
    out(
        {
            "initialised": True,
            "differing": differing[:50],
            "missing_in_mirror": missing[:50],
            "count": len(differing) + len(missing),
            "skipped_symlinks": skipped[:50],
        }
    )


def cmd_mind_restore(a) -> None:
    need("git")
    root = Path(a.root)
    with tempfile.TemporaryDirectory(prefix="agent-state-mind-") as tmp:
        clone = Path(tmp) / "mind"
        r = run(
            ["git", "clone", "-q", a.remote_url, str(clone)],
            env=dict(os.environ, **ssh_env(a.ssh_key)),
        )
        if r.returncode:
            raise Refused(f"clone failed: {r.stderr.strip()[:200]}")
        if a.ref != "HEAD":
            git(clone, "checkout", "-q", a.ref)
        n = 0
        skipped: list[str] = []
        for rel in a.paths:
            safe_rel(rel)
            # The clone is untrusted too: a symlink committed to the mirror must not be followed.
            for p in walk_files(clone, rel, skipped):
                relp = str(p.relative_to(clone))
                # Written by an fd walk from root (confined_io): a link the agent planted as any
                # parent, before or DURING the restore, is refused rather than followed, and a
                # link AT the destination is replaced by the rename, never written through. The
                # replacement keeps the owner of the file it replaces (an in-place overwrite would not).
                data = confined_io.read_file(clone, relp)
                confined_io.write_file(root, relp, data, 0o644, create_dirs=True, keep_owner=True)
                n += 1
        if n == 0:
            raise Refused("the mirror held none of the declared paths; nothing restored")
    out({"restored": n, "skipped_symlinks": skipped[:50]})


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("backup")
    b.add_argument("--root", required=True)
    b.add_argument("--target", required=True)
    b.add_argument("--recipient", required=True)
    b.add_argument("--product", default="hermes")
    b.add_argument("--label", default="nightly")
    b.add_argument("--sqlite", action="append", default=[])
    b.add_argument("--exclude", action="append", default=[])
    b.add_argument("--keep", type=int, default=14)
    b.add_argument("--keep-monthly", type=int, default=12)
    b.add_argument("--metrics-file")
    b.add_argument("--tmpdir")
    b.set_defaults(fn=cmd_backup)

    la = sub.add_parser("latest")
    la.add_argument("--target", required=True)
    la.add_argument("--product", default="hermes")
    la.set_defaults(fn=cmd_latest)

    r = sub.add_parser("restore")
    r.add_argument("--archive", required=True)
    r.add_argument("--manifest")
    r.add_argument("--identity", required=True)
    r.add_argument("--root", required=True)
    r.add_argument("--required", action="append", default=[])
    r.add_argument("--exclude", action="append", default=[])
    r.set_defaults(fn=cmd_restore)

    w = sub.add_parser("rewrite")
    w.add_argument("--root", required=True)
    w.add_argument("--paths", nargs="+", required=True)
    w.set_defaults(fn=cmd_rewrite)

    m = sub.add_parser("mirror")
    m.add_argument("--root", required=True)
    m.add_argument("--repo-dir", required=True)
    m.add_argument("--remote-url", required=True)
    m.add_argument("--branch", default="main")
    m.add_argument("--paths", nargs="+", required=True)
    m.add_argument("--exclude", action="append", default=[])
    m.add_argument("--skip-names-file", default="")
    m.add_argument("--config-file")
    m.add_argument("--mask-pattern", action="append", default=[])
    m.add_argument("--scan-pattern", action="append", default=[])
    m.add_argument("--ssh-key")
    m.add_argument("--product", default="hermes")
    m.add_argument("--author-name", default="agent-state-mirror")
    m.add_argument("--author-email", default="agent-state-mirror@localhost")
    m.add_argument("--metrics-file")
    m.set_defaults(fn=cmd_mirror)

    d = sub.add_parser("mirror-diff")
    d.add_argument("--root", required=True)
    d.add_argument("--repo-dir", required=True)
    d.add_argument("--paths", nargs="+", required=True)
    d.add_argument("--exclude", action="append", default=[])
    d.add_argument("--skip-names-file", default="")
    d.set_defaults(fn=cmd_mirror_diff)

    mr = sub.add_parser("mind-restore")
    mr.add_argument("--root", required=True)
    mr.add_argument("--remote-url", required=True)
    mr.add_argument("--ref", default="HEAD")
    mr.add_argument("--paths", nargs="+", required=True)
    mr.add_argument("--ssh-key")
    mr.set_defaults(fn=cmd_mind_restore)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        args.fn(args)
    except Refused as e:
        sys.stderr.write(f"REFUSED: {e}\n")
        return 2
    except SystemExit as e:
        return int(e.code or 0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
