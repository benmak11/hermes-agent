# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Throwaway Streamlit vetting UI (Phase 4). Replaced by Next.js in Phase 5.

Run with:  streamlit run vetting_ui.py
"""

import streamlit as st
from dotenv import load_dotenv
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from models.job import Job
from models.match import JobMatch

load_dotenv()

st.set_page_config(page_title="Job Vetting", layout="wide")

USER_ID = st.sidebar.text_input("user_id", value="me")
MIN_SCORE = st.sidebar.slider("min score", 0, 100, 60)

db = firestore.Client()
jobs_ref = (
    db.collection("users")
    .document(USER_ID)
    .collection("jobs")
    .where(filter=FieldFilter("user_decision", "==", "pending"))
)

jobs = []
for snap in jobs_ref.stream():
    d = snap.to_dict()
    if "match" not in d:
        continue
    match = JobMatch.model_validate(d["match"])
    if match.overall_score < MIN_SCORE:
        continue
    jobs.append((snap.reference, Job.model_validate(d), match))

jobs.sort(key=lambda x: x[2].overall_score, reverse=True)

st.title(f"{len(jobs)} jobs to review")

for ref, job, match in jobs:
    with st.container(border=True):
        col1, col2 = st.columns([3, 1])
        with col1:
            st.subheader(f"{job.title} @ {job.company}")
            st.caption(f"{job.location} · {job.source} · [link]({job.url})")
            st.metric("Score", f"{match.overall_score:.0f}", match.recommendation)
            st.write(match.reasoning)

            with st.expander("Breakdown"):
                st.json(match.breakdown.model_dump())
                st.write("**Strengths:**", ", ".join(match.matched_strengths))
                st.write("**Gaps:**", ", ".join(match.gaps))
                st.write("**Red flags:**", ", ".join(match.red_flags_hit) or "none")

            with st.expander("Full JD"):
                st.text(job.jd_raw[:5000])

        with col2:
            if st.button("✓ Approve", key=f"a-{job.id}", type="primary"):
                ref.update({"user_decision": "approved"})
                st.rerun()
            if st.button("⤳ Skip", key=f"s-{job.id}"):
                ref.update({"user_decision": "rejected"})
                st.rerun()
            if st.button("⭐ Star", key=f"st-{job.id}"):
                ref.update({"user_decision": "starred"})
                st.rerun()
