# colab_automation/upload.py
from __future__ import annotations
import os
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Callable

def _preflight(local_path: str, dest_dir: str) -> None:
    """
    Pre-flight checks before any rclone operation:
    1. Verify local source path exists.
    2. Upload a tiny temp file to dest_dir and delete it — confirms the remote
       is reachable and writable before a long sync starts.
    Raises RuntimeError on failure.
    """
    if not Path(local_path).exists():
        raise RuntimeError(f"[rclone preflight] source path not found: {local_path}")

    probe_name = f".rclone_probe_{uuid.uuid4().hex[:8]}"
    dest_probe = dest_dir.rstrip("/") + "/" + probe_name

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".tmp")
    try:
        tmp.write(b"rclone probe")
        tmp.close()
        r = subprocess.run(
            ["rclone", "copyto", tmp.name, dest_probe],
            capture_output=True, timeout=30,
        )
        if r.returncode != 0:
            raise RuntimeError(
                f"[rclone preflight] destination not writable ({dest_dir}):\n"
                + r.stderr.decode(errors="replace").strip()
            )
        subprocess.run(["rclone", "deletefile", dest_probe], capture_output=True, timeout=15)
    finally:
        os.unlink(tmp.name)


DEFAULT_CODE_EXCLUDES = [
    ".git/**",
    "__pycache__/**",
    "*.egg-info/**",
    "*.pyc",
    ".ipynb_checkpoints/**",
    "*.pt",
    "*.pth",
    "artifacts/**",
    "logs/**",
    "checkpoints/**",
]


def rclone_copy(dest: str) -> Callable[[str], None]:
    """
    Return a notebook_upload_fn that runs: rclone copyto <local_path> <dest>

    Example:
        notebook_upload_fn=rclone_copy("gdrive:notebooks/voracle_colab.ipynb")
    """
    def _upload(local_path: str) -> None:
        remote, path = dest.split(":", 1)
        dest_dir = remote + ":" + "/".join(path.split("/")[:-1])
        _preflight(local_path, dest_dir)
        cmd = ["rclone", "copyto", local_path, dest, "--stats=5s", "--stats-one-line"]
        print(f"[upload] rclone copyto {local_path} → {dest}")
        subprocess.run(cmd, check=True)

    return _upload


def rclone_sync(dest: str, excludes: list[str] | None = None) -> Callable[[str], None]:
    """
    Return a code_sync_fn that runs: rclone sync <local_dir> <dest> [--exclude ...]

    Defaults to DEFAULT_CODE_EXCLUDES. Pass excludes=[] to sync everything.

    Example:
        code_sync_fn=rclone_sync("gdrive:")
        code_sync_fn=rclone_sync("gdrive:code/", excludes=["*.pyc"])
    """
    exc = DEFAULT_CODE_EXCLUDES if excludes is None else excludes

    def _sync(local_dir: str) -> None:
        _preflight(local_dir, dest)
        cmd = ["rclone", "sync", local_dir, dest, "--stats=5s", "--stats-one-line"]
        for pattern in exc:
            cmd += ["--exclude", pattern]
        print(f"[upload] rclone sync {local_dir} → {dest}")
        subprocess.run(cmd, check=True)

    return _sync
