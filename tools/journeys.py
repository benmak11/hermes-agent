# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Where a user's journeys live.

The route module (``api.routes.journeys``) owns the HTTP surface; this owns the
storage name so ``tools.account.delete`` can import it — ``tools/`` never
imports ``api/``.
"""

from __future__ import annotations

from google.cloud import firestore

#: Subcollection under ``users/{uid}`` holding one document per journey.
COLLECTION = "journeys"


def journeys_ref(db: firestore.Client, user_id: str):
    return db.collection("users").document(user_id).collection(COLLECTION)
