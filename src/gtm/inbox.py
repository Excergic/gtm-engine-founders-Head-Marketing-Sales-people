"""Stable-file discovery and crash-recoverable snapshots around the existing importer."""

from __future__ import annotations

import csv
import os
import stat
import tempfile
import time
from contextlib import nullcontext
from copy import deepcopy
from pathlib import Path


class IncompleteFile(ValueError):
    pass


def csv_candidate(path: Path):
    name = path.name.casefold()
    return (
        path.suffix.casefold() == ".csv"
        and not name.startswith((".", "~"))
        and not any(marker in name for marker in (".tmp.", ".part.", ".partial.", ".download."))
        and not path.is_symlink()
        and path.is_file()
    )


def signature(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


class GrokInbox:
    def __init__(self, service, settings, *, sleep=time.sleep):
        self.service, self.settings, self.sleep = service, settings, sleep
        self.root = settings.data_directory.absolute()
        self.folders = {
            name: self.root / name for name in ("processing", "processed", "failed", "state")
        }

    def discover(self, paths=None):
        paths = paths if paths is not None else [self.settings.grok_directory]
        result = set()
        for path in paths:
            if not path.exists():
                if path == self.settings.grok_directory:
                    continue
                raise FileNotFoundError(f"CSV does not exist: {path}")
            if path.is_symlink():
                continue
            if path.is_dir():
                for root, directories, files in os.walk(
                    path, followlinks=False, onerror=self.walk_error
                ):
                    directories[:] = [
                        name
                        for name in directories
                        if not name.startswith((".", "~")) and not (Path(root) / name).is_symlink()
                    ]
                    result.update(
                        (Path(root) / name).absolute()
                        for name in files
                        if csv_candidate(Path(root) / name)
                    )
            elif csv_candidate(path):
                result.add(path.absolute())
        return sorted(result)

    @staticmethod
    def walk_error(error):
        raise error

    def stable_files(self, paths):
        before, deferred = {}, []
        for path in paths:
            try:
                info = path.stat(follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode) or info.st_size == 0:
                    deferred.append({"file": str(path), "reason": "empty or not a regular file"})
                else:
                    before[path] = signature(info)
            except FileNotFoundError:
                deferred.append({"file": str(path), "reason": "file disappeared"})
        if before:
            self.sleep(self.settings.stability_seconds)
        stable = []
        for path, original in before.items():
            try:
                if signature(path.stat(follow_symlinks=False)) == original:
                    stable.append((path, original))
                else:
                    deferred.append(
                        {"file": str(path), "reason": "size or modification time changed"}
                    )
            except FileNotFoundError:
                deferred.append({"file": str(path), "reason": "file disappeared"})
        return stable, deferred

    def read_stable(self, path, expected):
        # O_NOFOLLOW also protects against replacing an observed file with a symlink.
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as file:
            if signature(os.fstat(file.fileno())) != expected:
                raise IncompleteFile("CSV changed before capture; deferred until next run")
            content = file.read()
            if signature(os.fstat(file.fileno())) != expected or (
                signature(path.stat(follow_symlinks=False)) != expected
            ):
                raise IncompleteFile("CSV changed during capture; deferred until next run")
        return content

    def prepare_directories(self):
        self.settings.grok_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        for segment in ("founders", "VPs"):
            (self.settings.grok_directory / segment).mkdir(parents=True, exist_ok=True, mode=0o700)
        for path in self.folders.values():
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            path.chmod(0o700)

    def capture(self, path, expected):
        content = self.read_stable(path, expected)
        key = self.service.content_key(content, "grok", path)
        current = self.service.state.file(key, "grok", path)
        if current["status"] == "complete":
            return key, None
        destination = self.folders["processing"] / f"{key}.csv"
        # Register the intended destination before copying: after any crash we can locate it.
        self.service.state.snapshot(key, destination)
        fd, tmp = tempfile.mkstemp(prefix=f".{key}-", suffix=".tmp", dir=self.folders["processing"])
        try:
            with os.fdopen(fd, "wb") as file:
                file.write(content)
                file.flush()
                os.fsync(file.fileno())
            os.replace(tmp, destination)
        finally:
            Path(tmp).unlink(missing_ok=True)
        return key, destination

    def archive(self, key, snapshot, folder):
        target = self.folders[folder] / f"{key}.csv"
        if snapshot != target:
            os.replace(snapshot, target)
        self.service.state.snapshot(key, target)
        return target

    def process_snapshot(self, key, snapshot, original):
        try:
            if self.service.file_key(snapshot, "grok", original) != key:
                raise ValueError(
                    "managed CSV fingerprint changed; restore the original snapshot before retrying"
                )
            result = self.service.import_file(
                snapshot, "grok", locked=True, source_path=original, snapshot_path=snapshot
            )
        except (ValueError, RuntimeError, OSError, csv.Error) as exc:
            self.archive(key, snapshot, "failed")
            return {"file": str(original), "file_id": key, "failed": 1, "error": str(exc)}
        status = self.service.state.db.execute(
            "SELECT status FROM files WHERE id=?", (key,)
        ).fetchone()[0]
        self.archive(key, snapshot, "processed" if status == "complete" else "failed")
        return result

    def recover(self, row):
        candidates = ([Path(row["snapshot_path"])] if row.get("snapshot_path") else []) + [
            self.folders[folder] / f"{row['id']}.csv"
            for folder in ("processing", "failed", "processed")
        ]
        for path in candidates:
            if csv_candidate(path):
                return path
        return None

    def run(self, paths=None, *, dry_run=False, retry_only=False):
        results, deferred = [], []
        seen = {"people": {}, "companies": {}, "names": {}}
        # One lock covers discovery, capture, identity lookups, writes, and checkpoints for the job.
        with nullcontext() if dry_run else self.service.state.lock():
            handled_paths = set()
            fallback_paths = []
            if not dry_run:
                self.prepare_directories()
                for row in self.service.state.pending_files():
                    # Explicit file imports should not unexpectedly process unrelated historical files.
                    if paths is not None and not retry_only:
                        continue
                    snapshot = self.recover(row)
                    if snapshot:
                        results.append(self.process_snapshot(row["id"], snapshot, row["path"]))
                        original = Path(row["path"]).absolute()
                        # A producer may reuse daily.csv. Recover its old snapshot AND ingest
                        # the new content; never mistake the path alone for the file identity.
                        try:
                            if (
                                not original.exists()
                                or self.service.file_key(original, "grok") == row["id"]
                            ):
                                handled_paths.add(original)
                        except OSError:
                            pass  # Discovery/stability handling reports source access failures.
                    elif csv_candidate(Path(row["path"])):
                        fallback_paths.append(Path(row["path"]).absolute())
                    else:
                        results.append(
                            {
                                "file": row["path"],
                                "file_id": row["id"],
                                "failed": 1,
                                "error": "snapshot unavailable; restore the CSV and import its path",
                            }
                        )
            discovered = ([] if retry_only else self.discover(paths)) + fallback_paths
            stable, deferred = self.stable_files(
                sorted({p for p in discovered if p not in handled_paths})
            )
            for original, expected in stable:
                previous_seen = deepcopy(seen) if dry_run else None
                try:
                    if dry_run:
                        # Validate the source signature both before and after the read-only preview.
                        self.read_stable(original, expected)
                        result = self.service.preview_rows(
                            original, "grok", reference=str(original), seen=seen
                        )
                        if signature(original.stat(follow_symlinks=False)) != expected:
                            raise IncompleteFile("CSV changed during preview; deferred")
                        results.append(result)
                    else:
                        key, snapshot = self.capture(original, expected)
                        if snapshot is None:
                            count = self.service.state.db.execute(
                                "SELECT count(*) FROM rows WHERE file_id=? AND status='success'",
                                (key,),
                            ).fetchone()[0]
                            result = {
                                "file": str(original),
                                "file_id": key,
                                "skipped": count,
                                "errors": [],
                            }
                            # Periodic sweeps must not append one history row per old CSV forever.
                            if paths is not None:
                                run_id = self.service.state.run_started(key, original)
                                self.service.state.run_finished(run_id, "skipped", result)
                            results.append(result)
                        else:
                            results.append(self.process_snapshot(key, snapshot, original))
                except (IncompleteFile, FileNotFoundError) as exc:
                    if dry_run:
                        seen = previous_seen
                    deferred.append({"file": str(original), "reason": str(exc)})
        return {"dry_run": dry_run, "files": results, "deferred": deferred}
