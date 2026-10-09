# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""GCS helpers for submission: fetch the tailored resume, store screenshots."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from tools.tailoring.render import resume_bucket_name

# Marks the per-download directories ``download_resume`` creates, so
# ``discard_resume`` can tell them apart from a local path it must never delete.
_DOWNLOAD_PREFIX = "hermes-resume-"


def _split_gs_uri(uri: str) -> tuple[str, str]:
    _, _, rest = uri.partition("gs://")
    bucket_name, _, blob_path = rest.partition("/")
    return bucket_name, blob_path


def download_resume(uri: str) -> Path:
    """Download a resume to a local temp file. Accepts gs:// URIs or local paths.

    Each download gets its own fresh directory (the blob's file name is kept,
    since a submitter attaches the file under it), so concurrent downloads on
    one instance never share a path. Callers release it with
    :func:`discard_resume`.
    """
    if not uri.startswith("gs://"):
        return Path(uri)  # already local (e.g. dry-run / dev render)

    from google.cloud import storage

    bucket_name, blob_path = _split_gs_uri(uri)
    client = storage.Client()
    blob = client.bucket(bucket_name).blob(blob_path)
    dest = Path(tempfile.mkdtemp(prefix=_DOWNLOAD_PREFIX)) / Path(blob_path).name
    try:
        blob.download_to_filename(str(dest))
    except BaseException:
        shutil.rmtree(dest.parent, ignore_errors=True)
        raise
    return dest


def discard_resume(path: Path) -> None:
    """Delete a file :func:`download_resume` fetched, with its directory.

    A no-op for anything it did not create — notably the local path a non-gs://
    URI is returned as, which is the caller's own file.
    """
    parent = path.parent
    if parent.name.startswith(_DOWNLOAD_PREFIX) and parent.parent == Path(
        tempfile.gettempdir()
    ):
        shutil.rmtree(parent, ignore_errors=True)


def replace_resume(uri: str, local_path: Path) -> None:
    """Overwrite the resume stored at ``uri`` (gs:// or local) with a new file."""
    if not uri.startswith("gs://"):
        Path(uri).write_bytes(local_path.read_bytes())
        return

    from google.cloud import storage

    bucket_name, blob_path = _split_gs_uri(uri)
    client = storage.Client()
    client.bucket(bucket_name).blob(blob_path).upload_from_filename(str(local_path))


def upload_screenshot(local_path: Path, user_id: str, job_id: str, name: str) -> str:
    """Upload a submission screenshot and return its gs:// URI."""
    from google.cloud import storage

    client = storage.Client()
    bucket = client.bucket(resume_bucket_name())
    blob = bucket.blob(f"users/{user_id}/applications/{job_id}/{name}")
    blob.upload_from_filename(str(local_path))
    return f"gs://{bucket.name}/{blob.name}"
