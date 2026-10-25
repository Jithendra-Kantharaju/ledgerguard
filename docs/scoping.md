# Customer scoping template

Fill this in with the customer before writing code. Most AP automation projects fail on
the answers here, not on the model.

## 1. The problem in their words
- Who processes invoices today, how many people, how many hours a week?
- Invoices per month, peak days, share that are scanned vs digital, number of vendors.
- What goes wrong today: late payments, duplicates paid, price creep, missed early-pay discounts?
- What does "done" look like for them in 90 days? Agree on numbers.

## 2. Success metrics (agree before building)
| Metric | Baseline today | Target | How we measure |
|---|---|---|---|
| Wrong payments (approved but should not be) | | 0 | frozen test set + shadow-mode comparison |
| Share of invoices handled without a person | | e.g. 50% → 70% | worker audit log |
| Time from receipt to approval | | | documents.created_at → executed |
| Reviewer minutes per invoice | | | UI timing |
| Cost per invoice (model + infra) | | | eval report tokens × price |

## 3. Data and systems
- AP / ERP system and version (NetSuite, SAP, QuickBooks, Dynamics…). API or file drop?
  Sandbox available? Rate limits? Does it support idempotency keys? If not, how do we
  ask "did this request land?" (search by vendor + invoice number?)
- Where POs and goods receipts live, how fresh they are, how we sync them.
- How invoices arrive (email inbox, portal, EDI, scanner) and in what formats.
- 200+ historical invoices with the decisions their team made, for a labelled test set.

## 4. Business rules (becomes `app/config/<customer>.yaml`)
- Price tolerance per line, total tolerance, quantity tolerance.
- Auto-approve limit; approval limits per approver.
- Allowed currencies; invoice age limit; non-PO invoices (allowed? who approves?).
- What the AP system calls an approved bill.

## 5. Security and compliance
- Data residency, retention, who may see bank details. SSO provider.
- Is sending invoice text to a model provider allowed? Zero-retention needed?
- Audit requirements (SOX?): who approved what, when, on which version.

## 6. Rollout plan
1. **Week 1–2: shadow mode** (`SHADOW_MODE=true`). The agent decides, nothing is sent.
   Compare with what their team did. Fix rules and config, not prompts, first.
2. **Week 3–4: assisted.** Everything goes to review, with the agent's reasons and
   pre-filled fields. Measure reviewer time and correction rate.
3. **Then: partial autonomy.** Turn on auto-approve below a low limit, raise it as the
   numbers hold. Re-run the frozen-test eval on every change.

## 7. Risks and owners
| Risk | Mitigation | Owner |
|---|---|---|
| AP API lacks idempotency | status query by vendor + invoice number before retry | |
| Master data stale | sync job + revalidation at approval | |
| Vendor layouts change | per-vendor accuracy on the dashboard; review on disagreement | |
