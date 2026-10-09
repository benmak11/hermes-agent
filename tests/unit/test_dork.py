# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Company-slug extraction from search results, and the sweep that vets them."""

import asyncio

import pytest
import structlog
import yaml

import tools.companies as tc
from tools.ats.probe import BoardProbe
from tools.discovery import dork
from tools.discovery.dork import SearchBackend, extract_slugs


def test_extract_slugs_greenhouse_only() -> None:
    urls = [
        "https://boards.greenhouse.io/stripe/jobs/123",
        "https://boards.greenhouse.io/airbnb",
        "https://jobs.lever.co/spotify/abc",  # wrong platform -> ignored
        "https://example.com/whatever",
    ]
    assert extract_slugs(urls, "greenhouse") == {"stripe", "airbnb"}


def test_extract_slugs_new_greenhouse_hosts() -> None:
    urls = [
        "https://job-boards.greenhouse.io/gitlab/jobs/8503792002",
        "https://job-boards.eu.greenhouse.io/pleo/jobs/123",
        "https://boards.eu.greenhouse.io/adyen/jobs/456",
    ]
    assert extract_slugs(urls, "greenhouse") == {"gitlab", "pleo", "adyen"}


def test_extract_slugs_per_platform() -> None:
    urls = [
        "https://jobs.lever.co/spotify/abc-123",
        "https://jobs.ashbyhq.com/ramp/xyz",
    ]
    assert extract_slugs(urls, "lever") == {"spotify"}
    assert extract_slugs(urls, "ashby") == {"ramp"}


def test_extract_slugs_filters_platform_internal_paths() -> None:
    urls = [
        "https://boards.greenhouse.io/search",
        "https://boards.greenhouse.io/api",
        "https://boards.greenhouse.io/jobs",
    ]
    assert extract_slugs(urls, "greenhouse") == set()


# ---------------------------------------------------------------------------
# run_sweep: new slugs are probed before they are added
# ---------------------------------------------------------------------------
_OK = "ok"


def _probe(outcome: str, status: int | None, count: int | None, name: str | None):
    return BoardProbe(outcome, status, count, name)


class _FakeBackend(SearchBackend):
    def __init__(self, urls: dict[str, list[str]]):
        self.urls = urls

    async def search(self, query: str) -> list[str]:
        # The scoped query starts "site:<domain> ..."; answer per domain.
        domain = query.split()[0].removeprefix("site:")
        return self.urls.get(domain, [])


@pytest.fixture
def sweep_env(tmp_path, monkeypatch):
    """A fake data dir, one sweep query, and a probe answering from ``answers``."""
    d = tmp_path / "data" / "companies"
    d.mkdir(parents=True)
    (d / "sweep_queries.yaml").write_text(yaml.safe_dump({"queries": ["engineer"]}))
    (d / "known.yaml").write_text(
        yaml.safe_dump({"greenhouse": [{"slug": "known-co"}]})
    )
    (d / "unvetted.yaml").write_text("{}\n")
    (d / "blocklist.yaml").write_text(yaml.safe_dump({"blocked": []}))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(tc, "DATA_DIR", d)

    answers: dict[tuple[str, str], BoardProbe] = {}
    probed: list[tuple[str, str]] = []

    async def fake_probe(platform: str, slug: str) -> BoardProbe:
        probed.append((platform, slug))
        return answers[(platform, slug)]

    monkeypatch.setattr(dork, "probe_board", fake_probe)
    return d, answers, probed


def test_sweep_adds_only_ok_boards_with_jobs_carrying_their_names(sweep_env):
    d, answers, probed = sweep_env
    answers.update(
        {
            ("greenhouse", "acme"): _probe(_OK, 200, 4, "Acme"),
            ("greenhouse", "globex"): _probe(_OK, 200, 1, None),
            ("greenhouse", "deadco"): _probe("not_found", 404, None, None),
            ("greenhouse", "emptyco"): _probe(_OK, 200, 0, "Empty Co"),
            ("greenhouse", "slowco"): _probe("timeout", None, None, None),
            ("greenhouse", "busyco"): _probe("rate_limited", 429, None, None),
            ("greenhouse", "oddco"): _probe(_OK, 200, None, "Odd"),
            ("lever", "initech"): _probe(_OK, 200, 2, "Initech"),
        }
    )
    gh = [
        f"https://boards.greenhouse.io/{s}/jobs/1"
        for s in ("acme", "globex", "deadco", "emptyco", "slowco", "busyco", "oddco")
    ]
    backend = _FakeBackend(
        {
            "boards.greenhouse.io": [
                *gh,
                "https://boards.greenhouse.io/known-co/jobs/2",
            ],
            "jobs.lever.co": ["https://jobs.lever.co/initech/abc"],
        }
    )

    result = asyncio.run(dork.run_sweep(backend))

    raw = yaml.safe_load((d / "unvetted.yaml").read_text())
    assert [(c["slug"], c.get("name")) for c in raw["greenhouse"]] == [
        ("acme", "Acme"),
        ("globex", None),
    ]
    assert [(c["slug"], c["name"]) for c in raw["lever"]] == [("initech", "Initech")]
    assert "ashby" not in raw
    assert result.added == {
        "greenhouse": 2,
        "lever": 1,
        "ashby": 0,
        "google_jobs": 0,
        "meta_jobs": 0,
    }
    assert result.rejected["greenhouse"] == {"not_found": 1, "empty": 1, "failing": 3}
    assert result.rejected["lever"] == {"not_found": 0, "empty": 0, "failing": 0}
    # A slug already in the pool is never probed.
    assert ("greenhouse", "known-co") not in probed


def test_sweep_logs_each_rejection_with_its_reason(sweep_env):
    _, answers, _ = sweep_env
    answers[("ashby", "deadco")] = _probe("not_found", 404, None, None)
    backend = _FakeBackend({"jobs.ashbyhq.com": ["https://jobs.ashbyhq.com/deadco"]})

    with structlog.testing.capture_logs() as logs:
        asyncio.run(dork.run_sweep(backend))

    rejected = [e for e in logs if e["event"] == "sweep.slug_rejected"]
    assert rejected == [
        {
            "event": "sweep.slug_rejected",
            "log_level": "info",
            "platform": "ashby",
            "slug": "deadco",
            "reason": "not_found",
            "outcome": "not_found",
            "status": 404,
        }
    ]


def test_vetting_probes_at_most_ten_at_once(monkeypatch):
    live = [0]
    peak = [0]

    async def fake_probe(platform: str, slug: str) -> BoardProbe:
        live[0] += 1
        peak[0] = max(peak[0], live[0])
        await asyncio.sleep(0.001)
        live[0] -= 1
        return _probe(_OK, 200, 1, None)

    monkeypatch.setattr(dork, "probe_board", fake_probe)

    accepted, _ = asyncio.run(dork.vet_slugs("lever", [f"co{i}" for i in range(35)]))

    assert len(accepted) == 35
    assert peak[0] == 10


def test_print_summary_shows_rejected_counts(capsys):
    from cli.discover_companies import print_summary

    print_summary(
        dork.SweepResult(
            added={"greenhouse": 2, "lever": 0},
            rejected={
                "greenhouse": {"not_found": 3, "empty": 1, "failing": 0},
                "lever": {"not_found": 0, "empty": 0, "failing": 2},
            },
        )
    )

    out = capsys.readouterr().out
    assert "Added 2 new companies" in out
    assert "Rejected 6 new slugs" in out
    assert "greenhouse: not_found 3, empty 1" in out
    assert "lever: failing 2" in out
