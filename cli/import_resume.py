# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""
Usage:
    python -m cli.import_resume path/to/resume.pdf --user-id you [--output data/profile.yaml]

Accepts PDF, DOCX, or plain-text resumes. The extraction logic is shared with
the web onboarding flow (see ``tools.profile.extract``).
"""

import argparse
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

from models.profile import MasterProfile
from tools.profile.extract import extract_profile, read_resume_text

# Load GOOGLE_CLOUD_PROJECT / GOOGLE_CLOUD_LOCATION / GOOGLE_GENAI_USE_VERTEXAI
# from the project-root .env so the CLI works without manual exports.
load_dotenv()


def import_resume(resume_path: Path, user_id: str) -> MasterProfile:
    raw_text = read_resume_text(resume_path.read_bytes(), resume_path.name)
    return extract_profile(raw_text, user_id)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("resume", type=Path, help="resume file (PDF, DOCX, or text)")
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--output", type=Path, default=Path("data/profile.yaml"))
    args = parser.parse_args()

    if not args.resume.exists():
        sys.exit(f"File not found: {args.resume}")

    print(f"Reading {args.resume}...")
    profile = import_resume(args.resume, args.user_id)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w") as f:
        yaml.safe_dump(
            profile.model_dump(mode="json"),
            f,
            sort_keys=False,
            allow_unicode=True,
        )

    print(f"✓ Wrote {args.output}")
    print(f"  - {len(profile.experience)} roles")
    print(f"  - {sum(len(e.bullets) for e in profile.experience)} bullets")
    print(f"  - {sum(len(v) for v in profile.skills.values())} skills")
    print()
    print("⚠ Now manually review data/profile.yaml. The LLM will get some tags wrong.")
    print("  Pay particular attention to: bullet tags, preferences (likely empty), objective_template.")


if __name__ == "__main__":
    main()
