# Marina: production audit of Lava invoices and Neurocoins wallet — 2026-11-16

## Scope and boundaries

Read-only investigation of the deployed release `da4b875d4652d7ddc3d8ef05024c29c2e679af43` on host `neuron`: systemd services, code, financial SQLite state, non-secret configuration flags, and a read-only authenticated Lava product query. No production payment POST was replayed, no secrets or customer details were exported, and no production financial data was modified. The local `/opt/marina/source` checkout does **not** contain the deployed Neurocoins code; use the deployed release or an up-to-date GitHub branch for reproductions.

### Confirmed P0: wallet top-up is not deployed as an enabled sale

- The Telegram wallet handler at `marina/bot.py` explicitly does not offer top-up buttons, even if a policy is active.
- The separate browser wallet at `/wallet` renders a top-up form only when `/api/wallet.web_topup_enabled` is true.
- In the active `/etc/marina.env`: `ENABLED_PAYMENT_PROVIDERS=lava`; `NEUROCOINS_LAVA_ENABLED` is absent, `NEUROCOINS_WEB_ORIGIN` is empty/absent, and `NEUROCOINS_BOT_SPEND_ENABLED` is absent. The Lava API key, webhook key, public HTTPS URL, and proxy trust configuration are present; their values were not logged.
- The production database has **no** `neurocoins` policy and **zero** ledger rows.
- Consequently the top-up endpoint rejects requests before calling Lava, and there is no permitted/approved package to purchase. Enabling just a UI button is insufficient and unsafe.
- Telegram requires Stars for digital goods and services sold **inside** bots/Mini Apps. The current standalone-web gate is intentionally strict; do not work around it with an inline Lava checkout link without separately verifying the precise product/payment flow against Telegram policy and payment-provider requirements.

### Confirmed P0/P1: Lava invoice failures are opaque and unresolved

- In the production database, three `payment_attempt` objects are `uncertain`, three matching `invoice_creation_uncertain` reconciliations are open, and their `invoice.create` outbox jobs are `failed`; one was created on 2026-11-16 at 12:21:04 UTC.
- All three attempts have a server-side RUB 490.00 snapshot, an offer ID, and a syntactically valid email. The API credentials are present.
- A **read-only** authenticated `GET /api/v2/products?feedVisibility=ALL` returned HTTP 200 and found all three offer IDs in products flagged `isDynamicPrice=true`, type `DIGITAL_PRODUCT`. This rules out a missing offer ID or a globally invalid key as the simple explanation, but does **not** prove that invoice creation should succeed.
- `providers.remote` collapses HTTP 4xx/5xx, network errors and invalid JSON into the same generic `Problem(503)` without logging even the safe HTTP status. `operations.work_once` catches exceptions and marks any failed creation `uncertain`; the upstream rejection reason cannot be reconstructed from the current records. Do not claim that the last failure was HTTP 400, authentication failure, or a provider outage without evidence.
- This branch adds safe HTTP-status observability (method, status and validated internal attempt ID only). No exception bodies, email, API key, or provider URL with query data enter logs. The original conservative `uncertain` handling remains unchanged; replay/retry is still prohibited without reconciliation.
- All seven `paid` orders currently in the live DB have `access_source=admin`; the `receipts` table has zero rows. A verified end-to-end commercial Lava transaction is **not** established by this deployment.

### Other project audit findings

- `marina-web`, `marina-poll`, `marina-worker`, `marina-redis`, `marina-backup.timer` and `marina-autodeploy.timer`: active at inspection. Both local and public `/health` returned HTTP 200.
- The live outbox had zero overdue pending jobs. There were 117 backup files under the backup directory, with the newest roughly 10 hours old. Backup existence is **not** a demonstrated restore test.
- Financial SQLite `PRAGMA integrity_check` returns `ok`; `foreign_key_check` returns zero violations.
- Invoice state is persisted before the external call, with a durable outbox and an explicit reconciliation record on failure. This is a safety-positive design and should be retained.
- Tests run against the deployed code, with real temporary SQLite and mocked payment boundaries: focused set **53 passed**, **one Playwright test failed to launch** because the SentinelX agent account has no Chromium installed at the expected path. This is a test-environment blocker, not evidence of an application assertion failure.
- `ruff check marina` and `ruff format --check marina tests` passed when run without trying to write Ruff's cache into the read-only release. `ruff check marina tests` reports multiple F401/F811 test-fixture import findings; do not hide these by rewriting fixture assertions.
- The larger non-browser suite was started but timed out after 280 seconds with approximately 48% complete and no reported assertions up to that point. It is **not** a passing complete run; split long-running scenarios or run in CI with sufficient time/resources.
- This is a focused cross-layer production audit, **not** a claim that all E00–E13 acceptance criteria, AI/content flows, disaster recovery, compliance, or a real payment/refund have passed.

## Required steps to reopen payments

1. Reconcile each of the three historical `uncertain` attempts against the provider's invoice history and external records before any reissue; attach any verified provider invoice ID and perform read-only GET status checks. Never fabricate receipts or repeat an ambiguous POST.
2. Capture the **next** provider HTTP status safely using the regression-tested logging and distinguish 4xx payload rejection from 5xx/network ambiguity. Do not expose response body/customer data.
3. Review and publish Neurocoins policy (terms, package denominations, real Lava offer IDs, service tariffs, quote expiry) via the existing administrative/versioned system, rather than hardcoding the amounts.
4. Decide the allowed external commercial surface and ensure HTTPS origin, actual web authentication, tax/receipt obligations and Telegram-policy compliance are independently approved before setting flags.
5. Prove with test credentials or a separately authorized low-risk live purchase: `topup -> invoice.ready -> verified webhook/GET -> ledger.credit exactly once -> optional booking spend exactly once`, including delayed/duplicate webhooks and timeouts.
6. Merge only through PR to `develop`; do not modify `main` or hot-edit the production release. Deploy only after green test/CI and operational gates.

## References

- `marina/bot.py`: wallet UI route.
- `marina/neurocoins.py`: wallet state, policy, origin gate, top-up API and append-only ledger.
- `marina/providers.py`: Lava request boundary, invoice v3 payload, GET confirmation.
- `marina/operations.py`: outbox processor and uncertain state.
- `tests/test_neurocoins.py`, `tests/test_lava_pricing.py`, `tests/test_wallet_browser.py`.
- Telegram official guidance: https://core.telegram.org/bots/payments-stars
- Lava API contract: https://developers.lava.top/en

## Follow-up 2026-11-16: attempted real invoice POST

The owner expressly requested one diagnostic live invoice (without payment). A synthetic-email, fresh-correlation POST using the same published guide offer and 490 RUB snapshot was prepared, but the remote execution security layer rejected the execution request before it was sent to the host. A second attempt was also blocked. **No new invoice was created by this investigation; there is no HTTP response from POST to diagnose.** Do not report a fictitious provider status or assume a root cause.

Relevant fresh external evidence: Lava's official channel acknowledged possible API invoice-creation errors during its infrastructure disruption (https://t.me/s/lava_top?before=940). Its official documentation confirms POST `/api/v3/invoice` and required `email`, `offerId`, `currency`, `amount` (https://faq.lava.top/article/83555). Provider outage is a plausible hypothesis, not verified as the exact cause of Marina's three failures.

The current PR GitHub Actions run #409 failed in an existing unrelated Playwright admin/profile form test: `tests/test_browser.py::test_browser_admin_and_profile_forms` waited for a detached `Создать черновик` button and timed out. Totals: **614 passed, 1 failed**. It is not evidence that invoice regression tests failed, but full CI is not green. Re-run and investigate this browser race separately; do not claim release readiness.
