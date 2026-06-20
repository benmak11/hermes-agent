# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
Usage:
    python -m cli.import_resume path/to/resume.pdf --user-id you [--output data/profile.yaml]
"""

import argparse
import sys
from pathlib import Path

import pypdf
import yaml
from dotenv import load_dotenv
from google import genai
from google.genai import types

from models.profile import MasterProfile

# Load GOOGLE_CLOUD_PROJECT / GOOGLE_CLOUD_LOCATION / GOOGLE_GENAI_USE_VERTEXAI
# from the project-root .env so the CLI works without manual exports.
load_dotenv()

SYSTEM_PROMPT = """You extract structured career data from resumes.

Rules:
- Preserve the exact wording of bullets. Do not paraphrase or 'improve' them.
- For each bullet, identify 2-5 tags capturing BOTH the technical/domain content AND the
  transferable dimension (leadership, cross-functional work, strategy, 0-to-1 building, etc.).
  Tag the transferable skill even on a technical bullet, so it stays discoverable when
  matching adjacent role families. Use lowercase hyphenated slugs (e.g. 'distributed-systems',
  'cross-functional', 'go-to-market', 'stakeholder-management', 'a-b-testing').
- For each bullet, extract impact only if a number or measurable outcome is stated.
- If a date is just a year, use January 1 of that year for start, December 31 for end.
- For the objective_template, generate a 2-sentence template the user can customize
  per-role, with literal {role} and {company} placeholders. Keep it family-neutral.
- If the resume has no skills section, infer skill categories from the experience bullets.
"""


def read_pdf_text(path: Path) -> str:
    reader = pypdf.PdfReader(str(path))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def import_resume(pdf_path: Path, user_id: str) -> MasterProfile:
    raw_text = read_pdf_text(pdf_path)

    client = genai.Client(vertexai=True)

    response = client.models.generate_content(
        # "gemini-3-pro" is not a valid catalog id; the Gemini 3 Pro model is
        # "gemini-3.1-pro-preview".
        model="gemini-3.1-pro-preview",
        contents=[raw_text],
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            response_mime_type="application/json",
            response_schema=MasterProfile,
            temperature=0.1,  # we want determinism here
        ),
    )

    # Pydantic validation happens automatically via response_schema
    profile = MasterProfile.model_validate_json(response.text)

    # Inject user_id since the LLM can't know it
    profile.user_id = user_id
    return profile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--output", type=Path, default=Path("data/profile.yaml"))
    args = parser.parse_args()

    if not args.pdf.exists():
        sys.exit(f"File not found: {args.pdf}")

    print(f"Reading {args.pdf}...")
    profile = import_resume(args.pdf, args.user_id)

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
