# Evaluation: test-rules-rules_alt

Cases: 425  |  pipeline errors: 0

## Safety and automation

| system | wrong auto-approvals | approved with misread fields | false rejections | automation on approvable | review rate |
|---|---|---|---|---|---|
| pipeline | 0 | 0 | 0 | 0.7292 | 0.2824 |
| naive_po_exists | 256 | 12 | 0 | 1.0 | 0.0588 |

## Seeded errors stopped (not auto-approved)

| error type | n | stopped | outcomes |
|---|---|---|---|
| bad_totals | 25 | 25 | {'reject': 25} |
| closed_po | 25 | 25 | {'reject': 25} |
| duplicate_invoice | 25 | 25 | {'reject': 25} |
| missing_po | 25 | 25 | {'reject': 25} |
| price_over_tolerance | 25 | 25 | {'reject': 25} |
| prompt_injection | 25 | 25 | {'review': 25} |
| qty_over_received | 25 | 25 | {'reject': 25} |
| wrong_currency | 25 | 25 | {'reject': 25} |
| wrong_vendor | 25 | 25 | {'reject': 25} |

## Field accuracy (primary extractor)

**digital**: invoice_number 100%, vendor_name 100%, po_number 100%, invoice_date 100%, currency 100%, subtotal 100%, tax 100%, total 100%, lines 100%

**scanned**: invoice_number 94%, vendor_name 98%, po_number 100%, invoice_date 98%, currency 100%, subtotal 100%, tax 100%, total 96%, lines 86%

## Outcomes by slice

```
{
  "clean": {
    "approve": 105,
    "review": 45
  },
  "error": {
    "reject": 200,
    "review": 25
  },
  "scanned": {
    "review": 50
  }
}
```

Tokens: {'input': 0, 'output': 0}  |  latency per document: {'mean': 0.554, 'p95': 4.742}

