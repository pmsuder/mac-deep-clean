#!/usr/bin/env python3
"""Write temporary random data inside ~/Desktop/fill_up (Python 3.8+).

Preview: python3 mac_temporary_data.py
Write:   python3 mac_temporary_data.py --write
Default: fill available space down to about 5 GB free (decimal GB).
Options: --free-gb 20 for more headroom; --gib 1 to cap this run at 1 GiB.

Standard library only. This is NOT an SSD sanitization or secure-erasure tool.
5 GB is a small, best-effort buffer, not a reservation against other apps.
Run one instance only. Close other apps; avoid updates/downloads during use.
Check that Desktop is not cloud-synced before writing. The script does not
detect all sync services or change any settings. Completed/partial .bin files
are retained, including after Ctrl+C. Delete only the generated files later.
"""

import argparse
from dataclasses import dataclass
import os
from pathlib import Path
import sys

GB = 1000 ** 3
GIB = 1024 ** 3
MIB = 1024 ** 2
CHUNK = 8 * MIB


@dataclass(frozen=True)
class Plan:
    base: Path
    device: int
    available: int
    reserve: int
    target: int


@dataclass(frozen=True)
class Result:
    written: int
    message: str
    complete: bool


def space_info(path):
    """Return available/total bytes; accept a path or an open directory fd."""
    stats = os.fstatvfs(path) if isinstance(path, int) else os.statvfs(str(path))
    return (max(0, stats.f_bavail) * stats.f_frsize,
            stats.f_blocks * stats.f_frsize)


def destination_base():
    """Read-only preflight. Desktop and fill_up must be real local folders."""
    if sys.platform != "darwin":
        raise RuntimeError("This script runs only on macOS.")
    if os.geteuid() == 0:
        raise RuntimeError("Run as your normal user, without sudo.")
    data_volume = Path("/System/Volumes/Data").resolve(strict=True)
    if not data_volume.is_dir():
        raise RuntimeError("The macOS internal data volume is unavailable.")
    desktop = Path.home() / "Desktop"
    if desktop.is_symlink() or not desktop.is_dir():
        raise RuntimeError("Desktop must be an existing folder, not a symlink.")
    base = desktop / "fill_up"
    if base.is_symlink() or (base.exists() and not base.is_dir()):
        raise RuntimeError("fill_up already exists as a symlink or non-folder.")
    device = data_volume.stat().st_dev
    probe = base if base.exists() else desktop
    if desktop.stat().st_dev != device or probe.stat().st_dev != device:
        raise RuntimeError("Desktop/fill_up is not on the Mac's data volume.")
    if not os.access(str(probe), os.W_OK | os.X_OK):
        raise RuntimeError("The destination is not writable by your account.")
    return base, device


def make_plan(base, device, requested=None, reserve=5 * GB, space=space_info):
    available, _ = space(base if base.exists() else base.parent)
    target = max(0, available - reserve)
    if requested is not None:
        target = min(requested, target)
    return Plan(base, device, available, reserve, target)


def write_random(fd, directory, target, reserve, *, space=space_info,
                 entropy=os.urandom, write=os.write, sync=os.fsync,
                 report=print, chunk_size=CHUNK,
                 sync_interval=64 * MIB, progress_interval=5 * GIB):
    """Bounded sequential writes; injectable operations allow tiny safe tests."""
    written = pending = 0
    next_progress = progress_interval
    message = "Planned amount completed."
    complete = False
    try:
        while written < target:
            count = min(chunk_size, target - written)
            # Count unsynced bytes conservatively even if free space already
            # reflects them. Flush/recheck before deciding to stop near full.
            headroom = space(directory)[0] - reserve - pending
            if headroom < count and pending:
                sync(fd)
                pending = 0
                headroom = space(directory)[0] - reserve
            if headroom < count:
                message = "Stopped to preserve the free-space buffer."
                complete = True
                break
            remaining = memoryview(entropy(count))
            if len(remaining) != count:
                raise OSError("Random-data source returned an unexpected size.")
            while remaining:
                amount = write(fd, remaining)
                if amount <= 0 or amount > len(remaining):
                    raise OSError("Write made invalid or no progress.")
                written += amount
                pending += amount
                remaining = remaining[amount:]
            if pending >= sync_interval:
                sync(fd)
                pending = 0
            if written >= next_progress:
                report("Written {:.1f} GiB".format(written / GIB))
                next_progress = (written // progress_interval + 1) * progress_interval
        else:
            complete = True
    except KeyboardInterrupt:
        message = "Stopped by Ctrl+C."
    except OSError as error:
        message = "Stopped after an I/O error: {}".format(error)
    finally:
        try:
            sync(fd)
        except (OSError, KeyboardInterrupt) as error:
            complete = False
            message += " Final sync did not complete: {}".format(error)
    return Result(written, message, complete)


def execute(plan):
    """Create one exclusive file; never overwrite or delete an existing file."""
    desktop_fd = directory_fd = None
    path = None
    fd = None
    result = None
    exit_code = 1
    try:
        # Pin directories by fd so replacing a path with a symlink cannot
        # redirect subsequent writes. Recheck the actual filesystem device.
        dir_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        desktop_fd = os.open(str(plan.base.parent), dir_flags)
        if os.fstat(desktop_fd).st_dev != plan.device:
            raise RuntimeError("Desktop changed volumes; refusing to write.")
        if plan.target <= 0 or space_info(desktop_fd)[0] <= plan.reserve:
            print("Insufficient free space above the buffer; no filler created.")
            return 1
        try:
            os.mkdir(plan.base.name, 0o700, dir_fd=desktop_fd)
        except FileExistsError:
            pass
        directory_fd = os.open(plan.base.name, dir_flags, dir_fd=desktop_fd)
        if os.fstat(directory_fd).st_dev != plan.device:
            raise RuntimeError("fill_up changed volumes; refusing to write.")
        current = space_info(directory_fd)[0]
        target = min(plan.target, max(0, current - plan.reserve))
        if target <= 0:
            print("Insufficient free space above the buffer; no filler created.")
            return 1
        name = "random-data-{}.bin".format(os.urandom(8).hex())
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        fd = os.open(name, flags, 0o600, dir_fd=directory_fd)
        path = plan.base / name
        print("Generated file: {}\nPress Ctrl+C to stop.".format(path), flush=True)
        result = write_random(fd, directory_fd, target, plan.reserve,
                             report=lambda line: print(line, flush=True))
        exit_code = 0 if result.complete else 1
    except (OSError, RuntimeError, KeyboardInterrupt) as error:
        print("Stopped: {}".format(error or "Ctrl+C"), file=sys.stderr)
    finally:
        for handle in (fd, directory_fd, desktop_fd):
            if handle is None:
                continue
            try:
                os.close(handle)
            except (OSError, KeyboardInterrupt) as error:
                exit_code = 1
                print("Close failed: {}".format(error), file=sys.stderr)
                if result is not None:
                    result = Result(result.written, "File close failed; completion was not confirmed.", False)
        if path is not None:
            print("File retained: {}".format(path), flush=True)
    if result is not None:
        print("{}\nWrote {:.3f} GiB; file retained.".format(result.message, result.written / GIB))
    return exit_code


def integer_at_least(minimum):
    def parse(value):
        try:
            amount = int(value)
        except ValueError:
            raise argparse.ArgumentTypeError("Use a whole number >= {}.".format(minimum))
        if amount < minimum:
            raise argparse.ArgumentTypeError("Use a whole number >= {}.".format(minimum))
        return amount
    return parse


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--free-gb", type=integer_at_least(5), default=5,
                        help="free-space buffer in decimal GB (minimum/default: 5)")
    parser.add_argument("--gib", type=integer_at_least(1),
                        help="optional maximum GiB to write; default fills to buffer")
    parser.add_argument("--write", action="store_true", help="create data; omitted means a read-only preview")
    args = parser.parse_args(argv)
    try:
        base, device = destination_base()
        requested = None if args.gib is None else args.gib * GIB
        plan = make_plan(base, device, requested, args.free_gb * GB)
    except (OSError, RuntimeError) as error:
        print("Cannot proceed: {}".format(error), file=sys.stderr)
        return 1
    print("Destination: {}\nAvailable: {:.2f} GB | Buffer: {:.2f} GB\n"
          "Maximum for this run: {:.2f} GB ({:.2f} GiB)".format(
              plan.base, plan.available / GB, plan.reserve / GB,
              plan.target / GB, plan.target / GIB))
    print("Check that Desktop is not cloud-synced. Run one instance only.\n"
          "The buffer is best effort; other apps can consume it.")
    if not args.write:
        print("Preview only: no folder or filler created. Add --write to generate data.")
        return 0
    return execute(plan)


if __name__ == "__main__":
    sys.exit(main())
