# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""GCS helpers for submission: fetch the tailored resume, store screenshots."""

from __future__ import annotations

import tempfile
from pathlib import Path

from tools.tailoring.render import resume_bucket_name


def download_resume(uri: str) -> Path:
    """Download a resume to a local temp file. Accepts gs:// URIs or local paths."""
    if not uri.startswith("gs://"):
        return Path(uri)  # already local (e.g. dry-run / dev render)

    from google.cloud import storage

    _, _, rest = uri.partition("gs://")
    bucket_name, _, blob_path = rest.partition("/")
    client = storage.Client()
    blob = client.bucket(bucket_name).blob(blob_path)
    dest = Path(tempfile.gettempdir()) / Path(blob_path).name
    blob.download_to_filename(str(dest))
    return dest


def upload_screenshot(local_path: Path, user_id: str, job_id: str, name: str) -> str:
    """Upload a submission screenshot and return its gs:// URI."""
    from google.cloud import storage

    client = storage.Client()
    bucket = client.bucket(resume_bucket_name())
    blob = bucket.blob(f"users/{user_id}/applications/{job_id}/{name}")
    blob.upload_from_filename(str(local_path))
    return f"gs://{bucket.name}/{blob.name}"
