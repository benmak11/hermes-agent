# Scoring evaluation set

Hand-written labels for `cli/eval_scoring.py`, which grades the scorer against
them: AUC of `overall_score` against `fit`, precision/recall at the queue's
threshold of 60, and parse-field accuracy.

```bash
uv run python -m cli.eval_scoring --user-id me            # free, no model call
uv run python -m cli.eval_scoring --user-id me --rescore  # quotes, then spends
```

## No PII in here

`labels.jsonl` is versioned on purpose — you need to know which labels produced
which AUC — so it holds **only** `job_id`, `fit`, and corrected parse fields.
No résumé text, no profile fields, no job-description text. `data/profile.yaml`
is gitignored and stays that way; nothing from it belongs in this directory.

Rescore output lands in `data/eval/runs/`, which is gitignored.

## Schema

One JSON object per line. Blank lines and lines starting with `#` are skipped;
anything else malformed is an error naming the line number.

| key | required | meaning |
|---|---|---|
| `job_id` | yes | the Firestore document id, unique within the file |
| `fit` | yes | `true` / `false` — would you want to see this job |
| `parse` | no | corrected `ParsedJD` fields, only the ones you checked |

`parse` accepts `role_family`, `seniority`, `remote_policy`, `us_remote_ok`,
`job_country`, `job_state`, `job_city`, `remote_scope`. Any other key is
rejected rather than silently ignored.

**Omit a field you did not check.** Accuracy is reported per field as
`agreed / checked`, so a field the label leaves out is not counted at all —
writing one down that you did not verify adds a free point.

```jsonl
{"job_id": "abc123", "fit": true}
{"job_id": "def456", "fit": false, "parse": {"seniority": "staff"}}
```

## Reading the report

- **AUC** is the tie-corrected Mann-Whitney statistic over average ranks. Ties
  are the normal case: geographically ineligible jobs all cap at exactly 20.
- It comes with a 95% interval. **If the interval spans 0.5, the set shows
  nothing** — the report says so in words, and the point estimate is not the
  answer.
- A metric a label set cannot support prints `undefined` with the reason and
  the class counts. That is the honest result, not 0.5 and not 0.
- Parse fields are never averaged into one number; their base rates are
  nothing alike and a single figure hides which field is broken.

Labels are found in both `users/{uid}/jobs` and `users/{uid}/discarded_jobs`,
and the report prints the split. Everything scored at or below 20 lives in the
tombstones, so that half is most of the negative class.
