# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Guard: the deploy manifests must ship every importable top-level package.

Two independent allowlists gate what reaches the Cloud Run image:

1. ``[tool.hatch.build.targets.wheel].packages`` in ``pyproject.toml`` — what the
   project wheel installs (and therefore what ``import <pkg>`` resolves to under
   ``uv run``).
2. the ``COPY ./<pkg> ./<pkg>`` lines in the root ``Dockerfile`` — what source
   actually lands in the image.

A new top-level package (e.g. ``obs/``) has to be added to BOTH or the container
boots straight into ``ModuleNotFoundError``. Local dev and CI run from the repo
root where everything is on the path, so only the wheel-based image build is
selective — which means this drift is invisible until a deploy crashes on start.

This test makes that drift fail at PR time instead: it derives the expected set
from the filesystem (dirs with an ``__init__.py``) and asserts both manifests
agree, rather than just checking the two lists against each other (which would
happily pass when a package is missing from both — exactly how ``obs`` slipped
through).
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# Repo-root dirs that are importable packages (have __init__.py) but are NOT
# shipped in the API image. Adding an entry here is a deliberate, reviewed
# decision to keep a package out of the deploy.
NOT_SHIPPED: set[str] = set()


def _importable_packages() -> set[str]:
    """Top-level dirs that ``import <name>`` would resolve to (have __init__.py)."""
    return {
        p.name
        for p in REPO_ROOT.iterdir()
        if p.is_dir() and (p / "__init__.py").is_file()
    } - NOT_SHIPPED


def _wheel_packages() -> set[str]:
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    pkgs = data["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
    return set(pkgs)


def _dockerfile_copied_packages() -> set[str]:
    """Source dirs brought in by single ``COPY ./X ./X`` lines (top-level only)."""
    text = (REPO_ROOT / "Dockerfile").read_text()
    names = re.findall(r"^COPY \./([^/\s]+) \./[^/\s]+\s*$", text, re.MULTILINE)
    # Keep only real top-level directories (skips e.g. COPY ./README.md ...).
    return {n for n in names if (REPO_ROOT / n).is_dir()}


def test_importable_packages_are_in_the_wheel_manifest() -> None:
    """Every package with an __init__.py must be declared in the wheel build."""
    missing = _importable_packages() - _wheel_packages()
    assert not missing, (
        f"Top-level package(s) {sorted(missing)} have an __init__.py but are not "
        "in [tool.hatch.build.targets.wheel].packages in pyproject.toml. They will "
        "not be installed into the Cloud Run image and the API will crash on boot "
        "with ModuleNotFoundError. Add them there (and COPY them in the Dockerfile)."
    )


def test_wheel_manifest_and_dockerfile_agree() -> None:
    """The wheel packages and the Dockerfile COPY list must be identical."""
    wheel = _wheel_packages()
    copied = _dockerfile_copied_packages()
    assert wheel == copied, (
        "pyproject wheel packages and Dockerfile COPY list have drifted.\n"
        f"  in pyproject only: {sorted(wheel - copied)}\n"
        f"  in Dockerfile only: {sorted(copied - wheel)}\n"
        "Both must list the same top-level packages so the wheel install and the "
        "image source stay in sync."
    )


def test_wheel_packages_exist_on_disk() -> None:
    """No stale/typo'd entry in the wheel manifest."""
    missing = {p for p in _wheel_packages() if not (REPO_ROOT / p).is_dir()}
    assert not missing, (
        f"Wheel package(s) {sorted(missing)} are listed in pyproject.toml but do "
        "not exist as directories."
    )
