# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Title pre-filter: only confidently out-of-family titles are dropped.

A wrong drop here silently loses a job the user might have wanted, so these
tests pin the precision-first contract: ambiguous titles must pass through to
the Flash parse, and explicit target_titles always win over the keyword map.
"""

from datetime import UTC, datetime

import pytest

from models.job import Job
from models.profile import JobPreferences
from tools.discovery.title_filter import (
    OUTCOMES,
    TITLE_FILTER_VERSION,
    classify_title,
    evaluate_title,
    prefilter_jobs,
    wide_enabled,
)


def _job(title: str) -> Job:
    return Job(
        id=title.lower().replace(" ", "-"),
        user_id="u1",
        source="greenhouse",
        source_id="1",
        company="acme",
        title=title,
        url="https://boards.greenhouse.io/acme/jobs/1",
        jd_raw="...",
        discovered_at=datetime.now(UTC),
    )


def _prefs(families: list[str], titles: list[str] | None = None) -> JobPreferences:
    return JobPreferences(
        target_role_families=families,
        target_titles=titles or [],
        target_seniorities=["senior", "staff"],
    )


@pytest.mark.parametrize(
    ("title", "family"),
    [
        ("Senior Software Engineer", "engineering"),
        ("Staff Backend Developer", "engineering"),
        ("Engineering Manager", "engineering"),
        # Word boundary: "Salesforce" must not read as sales.
        ("Salesforce Developer", "engineering"),
        ("Growth Engineer", None),  # growth = ambiguous, engineer or marketer
        ("Product Manager", "product"),
        ("Head of Product", "product"),
        ("Product Designer", "design"),  # design, not product
        ("Product Marketing Manager", "marketing"),  # marketing, not product
        ("Data Scientist", "data"),
        ("Account Executive", "sales"),
        ("Account Manager", "sales"),  # sales, not finance
        ("Enterprise Partnerships Lead", "sales"),
        ("Customer Success Manager", "customer-success"),
        ("Technical Recruiter", "people"),
        ("People Operations Partner", "people"),  # people, not operations
        ("Staff Accountant", "finance"),
        ("Payroll Specialist", "finance"),
        ("General Counsel", "legal"),
        ("Content Strategist", "marketing"),
        ("Supply Chain Analyst", "operations"),
        ("Executive Assistant", "operations"),
        ("Chief of Staff", "operations"),
    ],
)
def test_confident_classifications(title: str, family: str | None):
    assert classify_title(title) == family


@pytest.mark.parametrize(
    "title",
    [
        # Straddle two families — Flash must stay the arbiter.
        "Data Engineer",
        "Machine Learning Engineer",
        "Software Engineer, Machine Learning",
        "Analytics Engineer",
        "Solutions Architect",
        "Sales Engineer",
        "Technical Support Engineer",
        "Technical Program Manager",
        "Developer Advocate",
        "UX Researcher",
        # No signal at all.
        "Business Analyst",
        "Wizard of Light Bulb Moments",
    ],
)
def test_ambiguous_titles_are_not_classified(title: str):
    assert classify_title(title) is None


def test_drops_only_confident_out_of_family():
    jobs = [
        _job("Senior Software Engineer"),  # in-family
        _job("Account Executive"),  # confident sales -> dropped
        _job("General Counsel"),  # confident legal -> dropped
        _job("Data Engineer"),  # ambiguous -> kept for Flash
        _job("Chief Vibes Officer"),  # unclassifiable -> kept for Flash
    ]
    kept, dropped = prefilter_jobs(jobs, _prefs(["engineering"]))
    assert [j.title for j in kept] == [
        "Senior Software Engineer",
        "Data Engineer",
        "Chief Vibes Officer",
    ]
    assert dropped == {"sales": 1, "legal": 1}


def test_target_titles_override_the_keyword_map():
    # User explicitly wants a title the map would classify out-of-family.
    prefs = _prefs(["engineering"], titles=["Product Manager"])
    kept, dropped = prefilter_jobs([_job("Senior Product Manager")], prefs)
    assert len(kept) == 1
    assert not dropped


def test_multiple_target_families():
    jobs = [_job("Product Manager"), _job("Software Engineer"), _job("SDR")]
    kept, dropped = prefilter_jobs(jobs, _prefs(["engineering", "product"]))
    assert [j.title for j in kept] == ["Product Manager", "Software Engineer"]
    assert dropped == {"sales": 1}


def test_no_preferences_keeps_everything():
    jobs = [_job("Account Executive"), _job("Paralegal")]
    kept, dropped = prefilter_jobs(jobs, None)
    assert kept == jobs
    assert not dropped


def test_empty_target_families_keeps_everything():
    jobs = [_job("Account Executive")]
    kept, dropped = prefilter_jobs(jobs, _prefs([]))
    assert kept == jobs
    assert not dropped


def test_family_targets_are_case_insensitive():
    kept, dropped = prefilter_jobs([_job("Software Engineer")], _prefs(["Engineering"]))
    assert len(kept) == 1
    assert not dropped


# ── the widened map (TITLE_FILTER_WIDE), off by default ──────────────────────
#
# Two halves matter equally. Inert when the flag is unset — today's behaviour,
# for every user, byte for byte in outcome — and actually firing when it is
# set. A test that only pinned the second half would pass against a map that
# was live in production the moment it merged.

# The real titles the live 8,882-job backlog leaks past the narrow map, with
# their frequency, against target titles of Senior Software Engineer /
# Backend Engineer / Machine Learning Engineer. The misspelled title below
# is the employer's own spelling and is matched on purpose.
BACKLOG_LEAKS = [
    ("technical project manager", 42, "product"),
    ("playable ads editor", 36, "marketing"),
    ("amazon ppc - for pooling purposes", 25, "marketing"),
    ("user acquistion specialist", 25, "marketing"),  # codespell:ignore acquistion
    # The three the wide map deliberately does NOT close. Unclassified means
    # kept, which is the safe direction, and each is a reason the contract is
    # written that way: annotation work is data-adjacent and the replay has
    # not cleared it, and both "growth" titles are held by _AMBIGUOUS's
    # \bgrowth\b, which exists to protect *Growth Engineer*. 71 jobs stay
    # kept rather than let the wide map overrule an ambiguity guard built out
    # of the user's own target nouns — see the collision class below.
    ("data annotation analyst", 33, None),
    ("marketing sales & client growth specialist", 42, None),
    ("customer growth specialist", 29, None),
]

# The collision class: an _AMBIGUOUS title carrying a second, out-of-family
# noun. _AMBIGUOUS is built from the user's own target nouns (machine
# learning, ai, data engineer, growth, solutions...), so a wide rule allowed
# to resolve one of these would drop a job *because* it matched the target
# role. "Machine Learning Engineer" is literally one of ED3UV's three target
# titles.
#
# Zero of these exist in the live corpus today, which is why the replay could
# not find them. The one that did — Machine Learning Engineer, Capital
# Underwriting — was fixed by narrowing a single rule, which left the class
# open; the ordering closes it.
AMBIGUOUS_COLLISIONS = [
    "Machine Learning Engineer, User Acquisition",
    "ML Engineer, Paid Media",
    "Data Engineer, Ad Operations",
    "AI Engineer, Customer Onboarding",
    "AI Engineer - Actuarial Modeling",
    "Growth Marketing Engineer",
    "Machine Learning Engineer, Project Management",
    "Machine Learning Engineer, Capital Underwriting",
]

# Engineering-adjacent or genuinely ambiguous. The wide map must not move any
# of them: a title that stays unclassified (or stays engineering) is kept,
# while one misclassified into another family is dropped and never seen again.
AMBIGUOUS_UNDER_WIDE = [
    "QA Engineer",
    "QA Analyst",
    "Security Engineer",
    "Application Security Analyst",
    "Web Developer",
    "Integration Developer",
    "Solutions Engineer",
    "Solutions Architect",
    "Sales Engineer",
    "Technical Support Engineer",
    "Data Annotation Specialist",
    "Technical Program Manager",
    "Business Analyst",
    "Growth Engineer",
    "Developer Advocate",
]


@pytest.fixture
def wide_on(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TITLE_FILTER_WIDE", "1")


@pytest.fixture(autouse=True)
def wide_off_by_default(monkeypatch: pytest.MonkeyPatch):
    """No test inherits a flag the developer happened to export."""
    monkeypatch.delenv("TITLE_FILTER_WIDE", raising=False)


def test_wide_families_are_inert_until_enabled(monkeypatch: pytest.MonkeyPatch):
    job = _job("Playable Ads Editor")
    prefs = _prefs(["engineering"])

    # Off: unclassified, and therefore kept — exactly today's behaviour.
    assert classify_title("Playable Ads Editor") is None
    kept, dropped = prefilter_jobs([job], prefs)
    assert [j.title for j in kept] == ["Playable Ads Editor"]
    assert not dropped

    # On: confidently marketing, and dropped.
    monkeypatch.setenv("TITLE_FILTER_WIDE", "1")
    assert classify_title("Playable Ads Editor") == "marketing"
    kept, dropped = prefilter_jobs([job], prefs)
    assert kept == []
    assert dropped == {"marketing": 1}


@pytest.mark.parametrize("value", ["", "0", "off", "false", "no", " "])
def test_wide_stays_off_for_anything_but_a_true_value(
    monkeypatch: pytest.MonkeyPatch, value: str
):
    monkeypatch.setenv("TITLE_FILTER_WIDE", value)
    assert wide_enabled() is False
    assert classify_title("Playable Ads Editor") is None


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "on", " on "])
def test_wide_turns_on_for_the_documented_values(
    monkeypatch: pytest.MonkeyPatch, value: str
):
    monkeypatch.setenv("TITLE_FILTER_WIDE", value)
    assert wide_enabled() is True
    assert classify_title("Playable Ads Editor") == "marketing"


@pytest.mark.parametrize(
    "title",
    [
        "Senior Software Engineer",
        "Backend Engineer",
        "Machine Learning Engineer",
        "Staff Software Engineer",
        "Senior Backend Engineer, Payments",
        "Site Reliability Engineer",
        "Salesforce Developer",
        # Nouns a careless wide rule would collide with.
        "Device Driver Engineer",
        "Kubernetes Scheduler Engineer",
        "Ads Infrastructure Engineer",
        "Marketing Platform Engineer",
        "Warehouse Systems Engineer",
        "Contract Test Engineer",
        "Engineering Project Lead",
    ],
)
def test_wide_map_never_drops_a_target_title(wide_on, title: str):
    """Nothing an engineer would want may classify out of engineering."""
    assert classify_title(title) in (None, "engineering")
    kept, dropped = prefilter_jobs(
        [_job(title)],
        _prefs(["engineering"], titles=["Senior Software Engineer"]),
    )
    assert [j.title for j in kept] == [title]
    assert not dropped


@pytest.mark.parametrize("title", AMBIGUOUS_UNDER_WIDE)
def test_wide_map_leaves_the_ambiguous_unclassified(
    monkeypatch: pytest.MonkeyPatch, title: str
):
    """These stay exactly where the narrow map left them: None, or
    engineering. Either way they are kept for an engineering target — what
    must never happen is the wide map moving one into another family."""
    narrow = classify_title(title)
    assert narrow in (None, "engineering")
    monkeypatch.setenv("TITLE_FILTER_WIDE", "1")
    assert classify_title(title) == narrow


@pytest.mark.parametrize(("title", "count", "family"), BACKLOG_LEAKS)
def test_the_measured_backlog_leaks(
    monkeypatch: pytest.MonkeyPatch, title: str, count: int, family: str | None
):
    assert count > 0  # frequency is documentation, not an assertion
    assert classify_title(title) is None, "every one of these leaks today"
    monkeypatch.setenv("TITLE_FILTER_WIDE", "1")
    assert classify_title(title) == family


# Titles the *live corpus* proved dangerous. Each one is classified by the
# narrow map today and was newly dropped by the first cut of the wide rules,
# which ran before the narrow ones instead of after. Eleven jobs across
# 10,243, found only by the replay.
NARROW_WINS = [
    "Staff Engineer - Customer Onboarding",
    "Senior Quality Engineer - Underwriting",
    "Developer Marketing Manager",
    "Product Marketing Manager - Developer Marketing",
    "Technical Product Marketing Manager (.NET Developer Tools)",
    "Talent Sourcer, Engineering",
    "Senior Director, Health Economics & Actuarial Analytics",
    "Cogito Project Manager, Enterprise Data Analytics",
    # The narrow map's own corpus: out-of-family verdicts must not move either.
    "Product Designer",
    "Data Scientist",
    "Account Executive",
    "Customer Success Manager",
    "Technical Recruiter",
    "Staff Accountant",
    "General Counsel",
    "Content Strategist",
    "Executive Assistant",
]


@pytest.mark.parametrize("title", NARROW_WINS)
def test_the_wide_map_never_overrules_the_narrow_one(title: str):
    """Widening may only turn "unclassified" into a family.

    The wide rules run last, on titles the narrow map left with no verdict,
    so a confident narrow classification is final. Without that ordering the
    live replay drops a *Staff Engineer - Customer Onboarding*.
    """
    narrow = classify_title(title, wide=False)
    assert narrow is not None, "this title must be classified by the narrow map"
    assert classify_title(title, wide=True) == narrow


@pytest.mark.parametrize("title", AMBIGUOUS_COLLISIONS)
def test_an_ambiguous_title_is_never_resolved_by_the_wide_map(
    monkeypatch: pytest.MonkeyPatch, title: str
):
    """_AMBIGUOUS means "not confident", and not-confident means kept.

    The wide rules get no second opinion on a title the ambiguity guard
    already caught: that guard is built out of the user's target nouns, so
    resolving one drops a job *because* it matched the target role. These
    titles reach the wide loop with no verdict, which is exactly the shape of
    the only false drop the live replay found.
    """
    assert classify_title(title, wide=False) is None
    assert classify_title(title, wide=True) is None
    monkeypatch.setenv("TITLE_FILTER_WIDE", "1")
    kept, dropped = prefilter_jobs(
        [_job(title)], _prefs(["engineering"], titles=["Machine Learning Engineer"])
    )
    assert [j.title for j in kept] == [title]
    assert not dropped


def test_underwriting_is_a_domain_and_underwriter_is_a_job():
    """The rule behind the one false drop the live replay found.

    ``Machine Learning Engineer, Capital Underwriting`` (Pro scored it 53)
    was dropped by a \bunderwrit(er|ing)\b finance rule. The ordering fix
    now also protects that particular title, so this pins the narrowing
    itself on titles the wide map can still reach: a *domain* does not
    classify, a *person's job* does.
    """
    # Reachable: neither _AMBIGUOUS nor _RULES has anything to say here.
    assert classify_title("Underwriting Analyst", wide=True) is None
    assert classify_title("Director of Underwriting", wide=True) is None
    assert classify_title("Underwriter", wide=True) == "finance"
    assert classify_title("Senior Underwriter", wide=True) == "finance"
    # And the title that started it, now protected twice over.
    title = "Machine Learning Engineer, Capital Underwriting"
    assert classify_title(title, wide=False) is None  # \bmachine\s+learning
    assert classify_title(title, wide=True) is None


def test_wide_keeps_the_unclassified_contract():
    """Widening the map must not turn 'unclassified' into 'dropped'."""
    kept, dropped = prefilter_jobs(
        [_job("Wizard of Light Bulb Moments"), _job("Data Engineer")],
        _prefs(["engineering"]),
        wide=True,
    )
    assert len(kept) == 2
    assert not dropped


def test_target_titles_still_beat_the_wide_map(wide_on):
    prefs = _prefs(["engineering"], titles=["Project Manager"])
    kept, dropped = prefilter_jobs([_job("Technical Project Manager")], prefs)
    assert len(kept) == 1
    assert not dropped


def test_prefilter_honours_the_wide_kwarg_over_the_env(
    monkeypatch: pytest.MonkeyPatch,
):
    """``cli.title_replay`` filters with wide=True while the flag is off."""
    prefs = _prefs(["engineering"])
    monkeypatch.delenv("TITLE_FILTER_WIDE", raising=False)
    kept, dropped = prefilter_jobs([_job("Playable Ads Editor")], prefs, wide=True)
    assert kept == [] and dropped == {"marketing": 1}

    monkeypatch.setenv("TITLE_FILTER_WIDE", "1")
    kept, dropped = prefilter_jobs([_job("Playable Ads Editor")], prefs, wide=False)
    assert len(kept) == 1 and not dropped


def test_wide_kwarg_overrides_the_env_flag(monkeypatch: pytest.MonkeyPatch):
    """The replay CLI measures the candidate map without switching it on."""
    monkeypatch.delenv("TITLE_FILTER_WIDE", raising=False)
    assert classify_title("Playable Ads Editor", wide=True) == "marketing"
    monkeypatch.setenv("TITLE_FILTER_WIDE", "1")
    assert classify_title("Playable Ads Editor", wide=False) is None


@pytest.mark.parametrize(
    ("title", "outcome", "family"),
    [
        ("Senior Software Engineer", "in-family", "engineering"),
        ("Data Engineer", "unclassified", None),
        ("Playable Ads Editor", "drop", "marketing"),
        ("Backend Engineer, Growth", "target-title", None),
    ],
)
def test_evaluate_title_is_the_decision_the_replay_reads(
    title: str, outcome: str, family: str | None
):
    prefs = _prefs(["engineering"], titles=["Backend Engineer"])
    assert evaluate_title(title, prefs, wide=True) == (outcome, family)
    assert outcome in OUTCOMES


def test_evaluate_title_without_preferences_filters_nothing():
    assert evaluate_title("Account Executive", None, wide=True) == ("no-filter", None)
    assert evaluate_title("Account Executive", _prefs([]), wide=True) == (
        "no-filter",
        None,
    )


class _RecordingLogger:
    """Captures structlog kwargs, the way test_sweep does."""

    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def info(self, event: str, **kw):
        self.events.append((event, kw))

    def warning(self, event: str, **kw):
        self.events.append((event, kw))


def test_the_drop_log_says_which_map_dropped(monkeypatch: pytest.MonkeyPatch):
    """Once the flag flips this line is the only production evidence of what
    the widening did, and a wide drop is otherwise indistinguishable from a
    narrow one."""
    from tools.discovery import title_filter

    recorder = _RecordingLogger()
    monkeypatch.setattr(title_filter, "log", recorder)

    # A narrow drop, flag off.
    prefilter_jobs([_job("Account Executive")], _prefs(["engineering"]))
    event, kw = recorder.events[-1]
    assert event == "discovery.title_filtered"
    assert kw["wide"] is False
    assert kw["version"] == TITLE_FILTER_VERSION

    # A wide drop, flag on.
    monkeypatch.setenv("TITLE_FILTER_WIDE", "1")
    prefilter_jobs([_job("Playable Ads Editor")], _prefs(["engineering"]))
    _, kw = recorder.events[-1]
    assert kw["wide"] is True
    assert kw["by_family"] == {"marketing": 1}

    # An explicit kwarg (the replay's path) is reported, not the env.
    prefilter_jobs([_job("Account Executive")], _prefs(["engineering"]), wide=False)
    assert recorder.events[-1][1]["wide"] is False


def test_title_filter_version_is_pinned():
    """PR 5 stamps this on tombstones; a rule change must bump it."""
    assert TITLE_FILTER_VERSION == 1
