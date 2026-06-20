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
