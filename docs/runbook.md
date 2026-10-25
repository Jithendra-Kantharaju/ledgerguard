# Operations runbook

All queries run as the database owner (`ADMIN_DATABASE_URL`). The app role cannot change
the audit log, by design.

## Health
- `GET /healthz` (process up), `GET /readyz` (database reachable).
- `GET /stats` per tenant: documents by status, dead letters.
- Traces: every job is a trace (`job` → `analyze` → `parse`, `extract`, `validate`, `retrieve`,
  `match`, `route`; `dispatch` for execution). Bank details are redacted before export.

## Dead letters (invoice approved but not delivered)
```sql
SELECT o.id, o.document_id, o.attempts, o.last_error, o.updated_at
FROM outbox o WHERE o.status = 'dead' ORDER BY o.updated_at DESC;
```
1. Read `last_error`. 4xx = the AP system refused the data (fix the data or mapping).
   Read-back mismatch = the bill exists but not in the expected state: check it in the AP system.
2. Check whether the bill exists there (search by the idempotency key or invoice number).
3. Only if it does not exist, retry with the same key (safe):
   ```sql
   UPDATE outbox SET status = 'pending', attempts = 0, next_attempt_at = now() WHERE id = <id>;
   UPDATE documents SET status = 'executing', error = NULL WHERE id = '<document_id>';
   ```

## Stuck or failed jobs
```sql
SELECT id, kind, document_id, status, attempts, last_error, lease_until FROM jobs
WHERE status IN ('running', 'failed') ORDER BY updated_at DESC LIMIT 50;
```
- `running` with an old `lease_until`: the worker died; the sweeper requeues it within 30 s.
- `failed`: the document is marked `failed` with the error. After fixing the cause (e.g. the
  model provider was down), requeue: `UPDATE jobs SET status='queued', attempts=0, run_after=now() WHERE id=<id>;`

## A reviewer says "I approved it but nothing happened"
Look at the review's `outcome` (`GET /documents/<id>` → `reviews`). Common answers:
`refused: stale review` (the document changed; reload), `exceeds approver limit`,
`revalidation failed: po_closed` (the PO changed after they looked).

## Changing rules or thresholds
1. Edit `app/config/<customer>.yaml` on a branch.
2. Run `make eval-test`; it must report 0 unsafe auto-approvals. Compare automation rate.
3. Deploy; restart workers (config is cached per process).

## Changing the extractor or model
Re-run `eval.calibrate` on the dev split (the confidence model is extractor-specific),
then `make eval-test`, then deploy with `SHADOW_MODE=true` for a few days.

## Rotating an API key
```sql
UPDATE api_keys SET revoked_at = now() WHERE user_id = '<user id>';
```
then issue a new one (`scripts/seed.py --random-keys` shows how keys are hashed).

## Incident: a wrong payment
1. `SHADOW_MODE=true` on all workers (stops sending, keeps deciding).
2. Find the document; its audit trail shows every decision, the gate, reasons, model, and who approved.
3. Add the case to the dev set with its correct answer, fix, and show the frozen-test eval before turning shadow mode off.
