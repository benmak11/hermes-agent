# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Push the reviewed local profile.yaml up to Firestore."""

import argparse
from pathlib import Path

import yaml
from dotenv import load_dotenv
from google.cloud import firestore

from models.profile import MasterProfile

# Load GOOGLE_CLOUD_PROJECT (and friends) from the project-root .env so the
# Firestore client targets the right project without manual exports.
load_dotenv()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=Path("data/profile.yaml"))
    args = parser.parse_args()

    with args.input.open() as f:
        data = yaml.safe_load(f)

    profile = MasterProfile.model_validate(data)

    db = firestore.Client()
    db.collection("users").document(profile.user_id).set(
        profile.model_dump(mode="json")
    )
    print(f"✓ Synced profile for user_id={profile.user_id}")


if __name__ == "__main__":
    main()
