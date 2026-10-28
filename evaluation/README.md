# IssuePilot triage quality evaluation

This directory is offline evaluation tooling. It does not import the API or Worker, call Qwen, or access GitHub.

## Human annotation

Generate a private working template under `evaluation/generated/`. The JSON file is an equivalent machine-readable snapshot from the same dataset; do not edit both independently. Generated annotations and real prediction runs are intentionally excluded from the public release manifest.

For each issue, read the title, summary, and linked public issue before setting:

- `gold_category`: one of `bug`, `feature_request`, `question`, `other`.
- `gold_priority`: one of `P0`, `P1`, `P2`, `P3`, using the rubric below.
- `review_notes`: short evidence for both decisions and any ambiguity.
- `review_status`: change from `unverified` to `verified` only after a human has checked the source; use `excluded` for unusable or out-of-scope samples.

`candidate_category` is only a review aid derived from collection strata/original labels. It is not truth. In particular, unlabeled samples are not assigned a category. Ideally, two people label independently and resolve disagreements without seeing model predictions.

Priority rubric:

- `P0`: confirmed exploitable security issue or widespread critical outage/data loss with no practical workaround.
- `P1`: severe impact affecting many users or a critical workflow, with strong evidence and no reasonable workaround.
- `P2`: normal actionable defect or important request with meaningful impact; default only when evidence supports it.
- `P3`: limited impact, edge case, low urgency, question, or cosmetic/minor improvement.

Do not infer urgency from emotional wording alone. Record missing evidence in `review_notes`. Category and priority can be verified independently, but a `verified` row must contain at least one gold field.

## Commands

Regenerate blank templates from the immutable source dataset:

```powershell
.\.venv\Scripts\python.exe -m evaluation.triage_quality template `
  --dataset data\issues_demo.json `
  --csv evaluation\generated\gold_labels_template.csv `
  --json evaluation\generated\gold_labels_template.json
```

Evaluate saved predictions (JSON or CSV) and write both result formats:

```powershell
.\.venv\Scripts\python.exe -m evaluation.triage_quality evaluate `
  --annotations evaluation\generated\gold_labels_template.csv `
  --predictions path\to\independently_saved_predictions.json `
  --output-prefix logs\triage_evaluation
```

For a real paid pilot, use an auditable JSON prediction bundle and `--prediction-run` instead of the basic `--predictions` option. Its `metadata` object must record `run_id`, `model_name`, `prompt_version`, UTC start/finish timestamps, `sample_count`, `call_failure_count`, `schema_failure_count`, `max_paid_calls`, `paid_retry_count`, `gold_labels_used_as_input`, and `raw_predictions_retained`. Each item must also retain the exact model text in `raw_response`; never store API keys or request headers. The loader rejects paid retries, gold-label input, count mismatches, missing raw records, and timestamps without a timezone.

Prediction records require `issue_number`, `predicted_category`, `predicted_priority`, `system_success`, and `system_error`. Successful records require valid structured category and priority values. Failed records must include an error and are counted in system reliability, not silently converted into model predictions.

Accuracy and Macro-F1 are computed only where a human-verified category and valid saved prediction both exist. Priority consistency follows the same rule. Coverage and end-to-end values show missing/failed predictions separately, while `system_task_success_rate` measures execution reliability. With no verified labels or no real saved predictions, metrics remain `null` and carry an explicit `NOT_COMPUTABLE` status.

The public repository ships only four explicitly synthetic examples, which validate the workflow but cannot establish model performance. Real Issue text, human labels, raw model responses, and paid-run ledgers must remain private unless their redistribution and privacy status have been reviewed separately. A real evaluation should use representative time periods, repositories, issue types, difficult cases, and independent annotation before making deployment claims.

