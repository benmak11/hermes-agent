# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""The tailored resume file: per-request downloads, and objective edits that
rebuild it.

Every blob is ``users/{uid}/applications/{job}/resume.docx``, so a download
path built from the blob's name alone is one shared file per instance — two
users downloading at once could be served each other's resume. And the
objective the review page lets a user edit only matters if it reaches the file
the download and the submitter both read.
"""

from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from docx import Document
from fastapi import FastAPI
from fastapi.testclient import TestClient
from google.api_core.exceptions import FailedPrecondition, NotFound

import api.routes.applications as applications
import tools.tailoring.objective as objective
import tools.tailoring.pipeline as pipeline
from api.deps import verify_user
from models.job import Job, ParsedJD
from models.profile import Bullet, Education, Experience, JobPreferences, MasterProfile
from tools import genai_client
from tools.submitters import storage

# ---------------------------------------------------------------- fake GCS


class _FakeBlob:
    def __init__(self, gcs, bucket, name):
        self._gcs, self._key = gcs, (bucket, name)

    def download_to_filename(self, filename):
        if self._key not in self._gcs.blobs:
            raise NotFound("no such blob")
        Path(filename).write_bytes(self._gcs.blobs[self._key])

    def upload_from_filename(self, filename):
        if self._gcs.fail_uploads:
            raise RuntimeError("upload failed")
        self._gcs.blobs[self._key] = Path(filename).read_bytes()
        self._gcs.uploads.append(self._key)


class _FakeGCS:
    """``google.cloud.storage.Client`` over an in-memory ``{(bucket, blob): bytes}``."""

    def __init__(self):
        self.blobs: dict[tuple[str, str], bytes] = {}
        self.uploads: list[tuple[str, str]] = []
        self.fail_uploads = False

    def Client(self):  # stands in for the class
        gcs = self
        return SimpleNamespace(
            bucket=lambda b: SimpleNamespace(
                name=b, blob=lambda n: _FakeBlob(gcs, b, n)
            )
        )


@pytest.fixture
def gcs(monkeypatch):
    from google.cloud import storage as gcs_module

    fake = _FakeGCS()
    monkeypatch.setattr(gcs_module, "Client", fake.Client)
    return fake


# --------------------------------------------------------- T1: downloads


def test_concurrent_downloads_of_same_named_blobs_get_their_own_files(gcs):
    gcs.blobs[("b", "users/u1/applications/j1/resume.docx")] = b"resume of u1"
    gcs.blobs[("b", "users/u2/applications/j2/resume.docx")] = b"resume of u2"

    first = storage.download_resume("gs://b/users/u1/applications/j1/resume.docx")
    second = storage.download_resume("gs://b/users/u2/applications/j2/resume.docx")
    try:
        assert first != second
        assert first.read_bytes() == b"resume of u1"
        assert second.read_bytes() == b"resume of u2"
        # The blob's own name survives: a submitter attaches the file under it.
        assert first.name == second.name == "resume.docx"
    finally:
        storage.discard_resume(first)
        storage.discard_resume(second)
    assert not first.exists() and not first.parent.exists()
    assert not second.exists() and not second.parent.exists()


def test_a_failed_download_leaves_no_directory_behind(gcs, monkeypatch, tmp_path):
    monkeypatch.setattr(storage.tempfile, "tempdir", str(tmp_path))
    with pytest.raises(NotFound):
        storage.download_resume("gs://b/users/u1/applications/j1/resume.docx")
    assert list(tmp_path.iterdir()) == []


def test_discard_never_deletes_a_file_it_did_not_download(tmp_path):
    """A non-gs:// URI is returned as-is — it is the caller's own file."""
    local = tmp_path / "resume.docx"
    local.write_bytes(b"mine")
    path = storage.download_resume(str(local))
    assert path == local

    storage.discard_resume(path)

    assert local.read_bytes() == b"mine"


def _api(monkeypatch, db) -> TestClient:
    monkeypatch.setattr(applications, "_client", lambda: db)
    app = FastAPI()
    app.include_router(applications.router)
    app.dependency_overrides[verify_user] = lambda: "u1"
    return TestClient(app)


def test_resume_route_serves_a_private_copy_and_deletes_it_after_sending(
    gcs, monkeypatch
):
    uri = "gs://b/users/u1/applications/job1/resume.docx"
    gcs.blobs[("b", "users/u1/applications/job1/resume.docx")] = b"docx bytes"
    db = _FakeDb(_FakeDoc(_app("ready_for_review", resume_variant_uri=uri)), None, None)
    served: list[Path] = []

    def spy(u):
        path = storage.download_resume(u)
        served.append(path)
        return path

    monkeypatch.setattr(applications, "download_resume", spy)

    resp = _api(monkeypatch, db).get("/applications/app-job1/resume")

    assert resp.status_code == 200
    assert resp.content == b"docx bytes"
    assert 'filename="resume_Acme_Corp.docx"' in resp.headers["content-disposition"]
    # The background task ran once the body was sent.
    assert len(served) == 1
    assert not served[0].exists() and not served[0].parent.exists()


# ------------------------------------------------- sync Firestore stand-in


class _Snap:
    def __init__(self, data, version):
        self.exists = data is not None
        self.update_time = version
        self._data = None if data is None else dict(data)

    def to_dict(self):
        return None if self._data is None else dict(self._data)


class _FakeDoc:
    """A document whose ``update`` honours a ``last_update_time`` precondition."""

    def __init__(self, data):
        self.data = None if data is None else dict(data)
        self.version = 1
        self.updates: list[dict] = []

    def get(self):
        return _Snap(self.data, self.version)

    def update(self, fields, option=None):
        if self.data is None:
            raise NotFound("no such document")
        if option is not None and option._last_update_time != self.version:
            raise FailedPrecondition("stale last_update_time")
        self.updates.append(dict(fields))
        self.data.update(fields)
        self.version += 1


class _FakeUserDoc(_FakeDoc):
    def __init__(self, profile, app_doc, job_doc):
        super().__init__(profile)
        self._colls = {"applications": app_doc, "jobs": job_doc}

    def collection(self, name):
        doc = self._colls[name]
        return SimpleNamespace(document=lambda _id: doc)


class _FakeDb:
    def __init__(self, app_doc, job, profile):
        self.app = app_doc
        self.user = _FakeUserDoc(profile, app_doc, _FakeDoc(job))

    def collection(self, name):
        assert name == "users"
        return SimpleNamespace(document=lambda _id: self.user)


def _app(status, **extra) -> dict:
    return {
        "id": "app-job1",
        "user_id": "u1",
        "job_id": "job1",
        "job_company": "Acme Corp",
        "status": status,
        "objective_text": "Original objective.",
        **extra,
    }


def _profile() -> dict:
    return MasterProfile(
        user_id="u1",
        full_name="Test Person",
        email="test@example.com",
        location="Springfield",
        objective_template="Engineer seeking a {role} role at {company}.",
        skills={"languages": ["python"]},
        experience=[
            Experience(
                company="Acme",
                role="Engineer",
                start=date(2020, 1, 1),
                bullets=[
                    Bullet(text="Built a widget", tags=["widgets"]),
                    Bullet(text="Shipped python services", tags=["python"]),
                ],
            )
        ],
        education=[
            Education(institution="State U", degree="BS", field="CS", start_year=2012)
        ],
        preferences=JobPreferences(
            target_role_families=["engineering"],
            target_titles=["Engineer"],
            target_seniorities=["senior"],
        ),
    ).model_dump(mode="json")


def _job() -> dict:
    return Job(
        id="job1",
        user_id="u1",
        source="greenhouse",
        source_id="1",
        company="Acme Corp",
        title="Engineer",
        url="https://example.com/jobs/1",
        jd_raw="We use python.",
        jd_parsed=ParsedJD(summary="Python role", required_skills=["python"]),
        discovered_at=datetime(2026, 1, 1, tzinfo=UTC),
    ).model_dump(mode="json")


URI = "gs://b/users/u1/applications/job1/resume.docx"
KEY = ("b", "users/u1/applications/job1/resume.docx")


@pytest.fixture
def no_llm(monkeypatch):
    """Any reach for Gemini fails the test loudly."""

    def refuse(*a, **kw):
        raise AssertionError("objective edit must not call an LLM")

    monkeypatch.setattr(pipeline, "generate_objective", refuse)
    monkeypatch.setattr(objective, "generate_objective", refuse)
    monkeypatch.setattr(objective, "vertex_client", refuse)
    monkeypatch.setattr(genai_client, "vertex_client", refuse)


def _paragraphs(data: bytes, tmp_path) -> list[str]:
    path = tmp_path / "check.docx"
    path.write_bytes(data)
    return [p.text for p in Document(str(path)).paragraphs]


# -------------------------------------------- T2: objective edit rebuilds


def test_objective_edit_rebuilds_the_resume_file_without_an_llm(
    gcs, monkeypatch, no_llm, tmp_path
):
    gcs.blobs[KEY] = b"stale"
    db = _FakeDb(
        _FakeDoc(_app("ready_for_review", resume_variant_uri=URI)),
        _job(),
        _profile(),
    )

    resp = _api(monkeypatch, db).put(
        "/applications/app-job1/objective",
        json={"objective_text": "I want to build Acme's widgets."},
    )

    assert resp.status_code == 200, resp.text
    assert gcs.uploads == [KEY]  # the same blob, not a new one
    paras = _paragraphs(gcs.blobs[KEY], tmp_path)
    assert "I want to build Acme's widgets." in paras
    assert "Original objective." not in paras
    # Rendered by the pipeline's own path: bullets reranked to the JD.
    bullets = [p for p in paras if p in ("Built a widget", "Shipped python services")]
    assert bullets == ["Shipped python services", "Built a widget"]
    assert db.app.data["objective_text"] == "I want to build Acme's widgets."
    assert db.app.data["resume_variant_uri"] == URI
    assert db.app.data["tailored_bullets"][0]["bullets"][0] == (
        "Shipped python services"
    )


def test_objective_edit_on_a_failed_application_is_allowed(gcs, monkeypatch, no_llm):
    """``failed`` can be retried, so its file is still the one that will be sent."""
    db = _FakeDb(_FakeDoc(_app("failed", resume_variant_uri=URI)), _job(), _profile())
    resp = _api(monkeypatch, db).put(
        "/applications/app-job1/objective", json={"objective_text": "New."}
    )
    assert resp.status_code == 200
    assert gcs.uploads == [KEY]


@pytest.mark.parametrize(
    "status",
    ["submitting", "submitted", "responded", "posting_removed", "queued", "tailoring"],
)
def test_objective_edit_refused_outside_editable_statuses(
    gcs, monkeypatch, no_llm, status
):
    db = _FakeDb(_FakeDoc(_app(status, resume_variant_uri=URI)), _job(), _profile())

    resp = _api(monkeypatch, db).put(
        "/applications/app-job1/objective", json={"objective_text": "New."}
    )

    assert resp.status_code == 409
    assert gcs.uploads == []
    assert db.app.data["objective_text"] == "Original objective."


def test_objective_edit_without_a_resume_is_refused(gcs, monkeypatch, no_llm):
    db = _FakeDb(_FakeDoc(_app("failed")), _job(), _profile())
    resp = _api(monkeypatch, db).put(
        "/applications/app-job1/objective", json={"objective_text": "New."}
    )
    assert resp.status_code == 409
    assert gcs.uploads == []
    assert db.app.updates == []


def test_objective_edit_for_a_missing_application_is_404(gcs, monkeypatch, no_llm):
    db = _FakeDb(_FakeDoc(None), _job(), _profile())
    resp = _api(monkeypatch, db).put(
        "/applications/app-job1/objective", json={"objective_text": "New."}
    )
    assert resp.status_code == 404


def test_failed_upload_leaves_the_objective_unchanged(gcs, monkeypatch, no_llm):
    gcs.blobs[KEY] = b"original file"
    gcs.fail_uploads = True
    db = _FakeDb(
        _FakeDoc(_app("ready_for_review", resume_variant_uri=URI)),
        _job(),
        _profile(),
    )

    resp = _api(monkeypatch, db).put(
        "/applications/app-job1/objective", json={"objective_text": "New."}
    )

    assert resp.status_code == 502
    assert "not changed" in resp.json()["detail"]
    assert db.app.updates == []
    assert db.app.data["objective_text"] == "Original objective."
    assert gcs.blobs[KEY] == b"original file"


def test_a_document_that_moved_during_the_rebuild_is_not_overwritten(
    gcs, monkeypatch, no_llm
):
    """Submit pressed mid-edit: the write is conditioned on the snapshot the
    status check read, so it loses rather than writing onto ``submitting``."""
    app_doc = _FakeDoc(_app("ready_for_review", resume_variant_uri=URI))
    db = _FakeDb(app_doc, _job(), _profile())
    real_replace = applications.replace_resume

    def replace_then_submit(uri, local):
        real_replace(uri, local)
        app_doc.data["status"] = "submitting"
        app_doc.version += 1

    monkeypatch.setattr(applications, "replace_resume", replace_then_submit)

    resp = _api(monkeypatch, db).put(
        "/applications/app-job1/objective", json={"objective_text": "New."}
    )

    assert resp.status_code == 409
    assert "may already carry" in resp.json()["detail"]
    assert app_doc.data["objective_text"] == "Original objective."
    assert app_doc.data["status"] == "submitting"
