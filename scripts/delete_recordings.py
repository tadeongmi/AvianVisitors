#!/usr/bin/env python3
"""Delete database-selected recordings as the station user."""

import contextlib
import ctypes
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
import uuid

MAX_MANIFEST_BYTES = 64 * 1024 * 1024


class DeleteError(Exception):
    pass


def component(value):
    if not isinstance(value, str) or not value or value in (".", ".."):
        raise DeleteError("Invalid recording path")
    if any(c in value for c in ("/", "\\", "\x00", "\n", "\r")):
        raise DeleteError("Invalid recording path")
    return value


def relative_path(row):
    date, common, filename = row["Date"], row["Com_Name"], row["File_Name"]
    if not isinstance(date, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        raise DeleteError("Invalid recording date")
    if not isinstance(common, str):
        raise DeleteError("Invalid recording name")
    return "/".join((date, component(common.replace(" ", "_").replace("'", "")), component(filename)))


@contextlib.contextmanager
def parent_fd(root_fd, relative):
    fd = os.dup(root_fd)
    try:
        for part in relative.split("/")[:-1]:
            child = os.open(component(part), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def file_stat(root_fd, relative):
    try:
        with parent_fd(root_fd, relative) as fd:
            info = os.stat(relative.split("/")[-1], dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise DeleteError("Recording is not a regular, unshared file")
    return info.st_dev, info.st_ino


def open_root(resolved):
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open("/", flags)
    try:
        for part in resolved.parts[1:]:
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except OSError:
        os.close(fd)
        raise


def selected_rows(db, request):
    if not isinstance(request, dict) or set(request) not in ({"file"}, {"species"}):
        raise DeleteError("Choose one recording or species")
    value = next(iter(request.values()))
    if not isinstance(value, str) or not value or len(value) > 1024 or "\x00" in value:
        raise DeleteError("Invalid recording selection")
    if "file" in request:
        parts = value.split("/")
        if len(parts) != 3:
            raise DeleteError("Invalid recording selection")
        for part in parts:
            component(part)
        rows = db.execute("SELECT rowid AS id, * FROM detections WHERE Date = ? AND File_Name = ?", (parts[0], parts[2])).fetchall()
        rows = [row for row in rows if relative_path(row) == value]
        if len(rows) != 1:
            raise DeleteError("Recording is missing or ambiguous; reload the page")
    else:
        rows = db.execute("SELECT rowid AS id, * FROM detections WHERE Sci_Name = ?", (value,)).fetchall()
        if not rows:
            raise DeleteError("No detections found; reload the page")
    return rows


def stage_open(root_fd, name):
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
    info = os.fstat(fd)
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        os.close(fd)
        raise DeleteError("Unsafe recording recovery directory")
    return fd


def move_no_replace(source_fd, source, target_fd, target):
    if sys.platform == "linux":
        rename = ctypes.CDLL(None, use_errno=True).renameat2
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        if rename(source_fd, os.fsencode(source), target_fd, os.fsencode(target), 1) != 0:
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code))
    else:
        os.link(source, target, src_dir_fd=source_fd, dst_dir_fd=target_fd, follow_symlinks=False)
        os.fsync(target_fd)
        os.unlink(source, dir_fd=source_fd)


def finish_stage(root_fd, name, fd, paths, restore):
    for index, relative in enumerate(paths):
        slot = str(index)
        try:
            info = os.stat(slot, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(info.st_mode):
            raise DeleteError("Unsafe staged recording")
        if restore:
            with parent_fd(root_fd, relative) as target_fd:
                target = relative.split("/")[-1]
                try:
                    move_no_replace(fd, slot, target_fd, target)
                except FileExistsError:
                    existing = os.stat(target, dir_fd=target_fd, follow_symlinks=False)
                    if (existing.st_dev, existing.st_ino) != (info.st_dev, info.st_ino):
                        raise DeleteError("Recording changed during recovery")
                os.fsync(target_fd)
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(slot, dir_fd=fd)
        else:
            os.unlink(slot, dir_fd=fd)
    for manifest in ("paths.json", "paths.tmp"):
        with contextlib.suppress(FileNotFoundError):
            os.unlink(manifest, dir_fd=fd)
    os.fsync(fd)
    opened = os.fstat(fd)
    current = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
    if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
        raise DeleteError("Recording recovery directory moved")
    os.rmdir(name, dir_fd=root_fd)
    os.fsync(root_fd)


def recover_stages(db, root_fd):
    for name in os.listdir(root_fd):
        if not re.fullmatch(r"\.avian-delete-[0-9a-f]{32}", name):
            continue
        fd = stage_open(root_fd, name)
        try:
            if set(os.listdir(fd)) <= {"paths.json", "paths.tmp"}:
                finish_stage(root_fd, name, fd, [], restore=False)
                db.execute("DELETE FROM recording_deletions WHERE id = ?", (name,))
                continue
            try:
                manifest_fd = os.open("paths.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            except FileNotFoundError:
                if os.listdir(fd):
                    raise DeleteError("Incomplete recording recovery map")
                paths = []
            else:
                with os.fdopen(manifest_fd) as manifest:
                    info = os.fstat(manifest.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_MANIFEST_BYTES:
                        raise DeleteError("Invalid recording recovery map")
                    try:
                        paths = json.load(manifest)
                    except (ValueError, UnicodeError) as error:
                        raise DeleteError("Invalid recording recovery map") from error
                if not isinstance(paths, list) or not all(isinstance(path, str) for path in paths) or len(paths) != len(set(paths)):
                    raise DeleteError("Invalid recording recovery map")
                for relative in paths:
                    parts = relative.split("/")
                    if parts[0] == "shifted":
                        parts = parts[1:]
                    if len(parts) != 3 or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", parts[0]):
                        raise DeleteError("Invalid recording recovery path")
                    for part in parts:
                        component(part)
            committed = db.execute("SELECT 1 FROM recording_deletions WHERE id = ?", (name,)).fetchone()
            finish_stage(root_fd, name, fd, paths, restore=not committed)
            db.execute("DELETE FROM recording_deletions WHERE id = ?", (name,))
        finally:
            os.close(fd)


def delete_recordings(database, recordings, request):
    root = Path(recordings).resolve(strict=True)
    db = sqlite3.connect(Path(database).resolve(strict=True).as_uri() + "?mode=rw", uri=True, timeout=5)
    try:
        root_fd = open_root(root)
    except OSError:
        db.close()
        raise
    db.row_factory = sqlite3.Row
    quarantine = None
    staging_fd = None
    committed = False
    try:
        db.execute("BEGIN IMMEDIATE")
        db.execute("CREATE TABLE IF NOT EXISTS recording_deletions (id TEXT PRIMARY KEY)")
        recover_stages(db, root_fd)
        db.commit()
        db.execute("BEGIN IMMEDIATE")
        rows = selected_rows(db, request)
        selected = {row["id"] for row in rows}
        paths = {relative_path(row) for row in rows}
        # A shared filename must not remove another species' recording.
        dates = [row["Date"] for row in rows]
        others = db.execute("SELECT rowid AS id, * FROM detections WHERE Date BETWEEN ? AND ?", (min(dates), max(dates)))
        for other in others:
            if other["id"] in selected:
                continue
            try:
                shared = relative_path(other) in paths
            except DeleteError:
                continue
            if shared:
                raise DeleteError("Recording is shared by another detection")
        files = {}
        for path in sorted(paths):
            for prefix in ("", "shifted/"):
                for suffix in ("", ".png"):
                    relative = prefix + path + suffix
                    identity = file_stat(root_fd, relative)
                    if identity is not None:
                        files[relative] = identity
        if files:
            manifest_json = json.dumps(list(files))
            if len(manifest_json) > MAX_MANIFEST_BYTES:
                raise DeleteError("Too many recordings for one deletion")
            quarantine = ".avian-delete-" + uuid.uuid4().hex
            os.mkdir(quarantine, 0o700, dir_fd=root_fd)
            staging_fd = stage_open(root_fd, quarantine)
            # The marker commits with the deletion, so recovery can restore or purge.
            manifest_fd = os.open("paths.tmp", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=staging_fd)
            with os.fdopen(manifest_fd, "w") as manifest:
                manifest.write(manifest_json)
                manifest.flush()
                os.fsync(manifest.fileno())
            os.rename("paths.tmp", "paths.json", src_dir_fd=staging_fd, dst_dir_fd=staging_fd)
            os.fsync(staging_fd)
            os.fsync(root_fd)
            for index, (relative, identity) in enumerate(files.items()):
                with parent_fd(root_fd, relative) as fd:
                    info = os.stat(relative.split("/")[-1], dir_fd=fd, follow_symlinks=False)
                    if (info.st_dev, info.st_ino) != identity:
                        raise DeleteError("Recording changed; reload the page")
                    os.rename(relative.split("/")[-1], str(index), src_dir_fd=fd, dst_dir_fd=staging_fd)
                    moved = os.stat(str(index), dir_fd=staging_fd, follow_symlinks=False)
                    if (moved.st_dev, moved.st_ino) != identity:
                        raise DeleteError("Recording changed during deletion")
                    os.fsync(fd)
            os.fsync(staging_fd)
            db.execute("INSERT INTO recording_deletions VALUES (?)", (quarantine,))
        db.executemany("DELETE FROM detections WHERE rowid = ?", [(row["id"],) for row in rows])
        db.commit()
        committed = True
        result = {"ok": True, "lines": len(rows), "files": len(files)}
        if quarantine:
            try:
                finish_stage(root_fd, quarantine, staging_fd, list(files), restore=False)
                db.execute("DELETE FROM recording_deletions WHERE id = ?", (quarantine,))
                db.commit()
            except (OSError, DeleteError, sqlite3.Error):
                result["warning"] = "Detections removed; some private staged files still need cleanup."
        return result
    except (OSError, sqlite3.Error, DeleteError) as error:
        db.rollback()
        if staging_fd is not None and not committed:
            try:
                finish_stage(root_fd, quarantine, staging_fd, list(files), restore=True)
            except (OSError, DeleteError):
                raise DeleteError("Deletion failed. Recordings are preserved in " + str(root / quarantine)) from error
        raise DeleteError(str(error)) from error
    finally:
        db.close()
        if staging_fd is not None:
            os.close(staging_fd)
        os.close(root_fd)


def main():
    try:
        if len(sys.argv) != 3 or os.geteuid() == 0:
            raise DeleteError("Recording deletion must run as the station user")
        raw = sys.stdin.buffer.read(4097)
        if len(raw) > 4096:
            raise DeleteError("Request is too large")
        request = json.loads(raw)
        result = delete_recordings(sys.argv[1], sys.argv[2], request)
    except (ValueError, OSError, sqlite3.Error, DeleteError) as error:
        print(json.dumps({"ok": False, "error": str(error)}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
