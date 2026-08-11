# Owner-Approved Revenue Sprint

## Purpose

The included Revenue Sprint is a bounded validation campaign for one existing
Gumroad product and one dedicated company-owned promotional account. It is not a
general permission for the team to use personal identities, create arbitrary
accounts, spend until a target is reached, or promote unrelated products.

The checked-in manifest is
`config/autonomous-projects/freelancer-cold-email-revenue-sprint-202608.json`.
It defines:

- one product: `https://tymedina.gumroad.com/l/freelancer-cold-email`;
- one channel: `bluesky:freelanceremailkit.bsky.social`;
- one allowed external action: one standalone Bluesky post per run-day;
- no more than 20 Phoenix-time weekdays;
- no more than $5 of AI cost per run-day, including the emergency reserve;
- no more than $100 of AI cost over the campaign;
- checkpoints after run-days 5 and 15, and an unconditional stop after day 20;
- a stop after the configured number of consecutive no-progress days.

Available budget is a ceiling, not a spending target. The coordinator stops when
no useful, complete worker-and-review unit fits safely.

## Identity boundary

Only promotional and seller identities owned by the company may be configured.
The Gumroad product and read-only sales token must belong to the company seller;
there is no fallback to the owner's LinkedIn, Gmail, Bluesky, Vercel, Gumroad, or
other personal account. The action target, company account mapping, campaign policy
revision, run claim, daily/total action count, provider receipt, and public
engagement evidence are all checked before or after each external action. An
engagement read is accepted only when every returned post matches one exact
persisted URI and CID.

Activation proves that the configured Gumroad token can read the exact linked,
published product. Gumroad's product response does not independently prove the
seller's organizational/legal identity, so confirming the manifest is also the
owner's explicit attestation that this seller and product are company-owned.

Account registration is deliberately not automated. Providers may require terms
acceptance, email verification, anti-abuse checks, or a CAPTCHA, and account-
generation automation is an unsafe spam primitive. If the dedicated account does
not exist, Miles must stop with an exact owner action. After the owner completes
that one-time provider bootstrap and installs an app password, the approved daily
posting action can run unattended within the campaign limits.

## Reviewed-action coordinator boundary

The included campaign still authorizes only the Bluesky target above. The same
coordinator can now validate other action types, but adding adapter credentials does
not activate them. Each action must first appear in a new owner-reviewed manifest
revision with an exact target and count/spend caps. The worker remains proposal-only,
Vera reviews one strict `CAMPAIGN_DRAFT_JSON` object, and the deterministic coordinator
may execute only the canonical payload whose SHA-256 digest was approved.

The supported review envelopes are deliberately separate:

- Bluesky publish: exact `action_type`, `target`, `text`, and product `url`;
- signed publish webhook: exact `action_type`, `target`, and bounded public `payload`;
- Gmail outreach: exact logical `target`, owner-configured `recipient`, `subject`, and
  `body`; the dedicated company sender is reverified before sending;
- Vercel deploy: exact company `account_id`, project, and immutable lowercase 40-hex
  Git commit; production deployment is unavailable with a mutable branch name or a
  credential not explicitly marked `company_service`;
- signed purchase webhook: exact target, owner-configured `amount_usd`, and bounded
  public payload. Purchases still default disabled at the independent $0 hard cap.

Reviewed publish and purchase webhooks require an exact allowlisted public HTTPS host.
Their 2xx response is not enough: success requires an HMAC-signed response that echoes
the action ID, idempotency key, payload digest, company account, amount, status, and a
safe receipt ID. Missing or mismatched proof becomes `uncertain`, stops the sprint, and
is never retried blindly. If Railway stops after a persistent action claim but before a
terminal receipt is journaled, the next run stops before model or provider work with
`external_action_claim_pending_reconciliation`; inspect the provider and start a new
owner-confirmed campaign revision instead of editing the ledger or replaying the call.

The webhook response contract is exact and uses the same secret as the corresponding
request (`REVENUE_PUBLISH_WEBHOOK_SECRET` or `REVENUE_PURCHASE_WEBHOOK_SECRET`):

1. Read `X-Revenue-Timestamp` from the request and preserve its ASCII value exactly.
   Do not generate a different response timestamp.
2. Return HTTP 2xx with a canonical UTF-8 JSON object. Produce the body equivalently to
   Python `json.dumps(value, sort_keys=True, separators=(",", ":"),
   ensure_ascii=False).encode("utf-8")`: keys are sorted lexicographically, there is no
   insignificant whitespace, and Unicode remains UTF-8 except where JSON itself requires
   escaping. The required fields are:

   ```json
   {"action_id":"<exact request action_id>","amount_usd":0.0,"idempotency_key":"<exact request idempotency_key>","payload_digest":"<exact request payload_digest>","provider_account_id":"<exact configured company account ID>","receipt_id":"<non-empty provider receipt using only safe receipt characters>","status":"succeeded"}
   ```

   `amount_usd` must be `0.0` for publish and the exact approved amount for purchase.
   The other echoed values must exactly match the signed request body. `receipt_id` is
   limited to 1-200 characters from `A-Z`, `a-z`, `0-9`, `.`, `_`, `:`, `@`, `/`, and
   `-`. Optional signed revenue `signals` may be included, but the required receipt
   fields cannot be omitted or changed.
3. Compute HMAC-SHA256 over these exact bytes:

   ```text
   <request X-Revenue-Timestamp>.<raw canonical response body>
   ```

   Set the response header to `X-Revenue-Response-Signature: sha256=<lowercase hex
   digest>`. The body bytes used for the signature must be the same canonical JSON bytes
   returned in the response.

The coordinator does not follow webhook redirects. A redirect, non-2xx response,
missing or malformed JSON, missing echo, account/amount/digest/idempotency mismatch,
unsafe or empty receipt ID, missing signature, or invalid signature is persisted as an
`uncertain` outcome. Treat that as a possible completed external action: inspect the
provider and do not replay it blindly.

The example handle is a configuration target, not proof that the handle is
available or registered. Verify it before queuing the manifest; if the real handle
differs, update every exact target in the manifest and the Railway mapping, then
review and confirm the new manifest revision.

## One-time company account setup

1. Create the dedicated Bluesky account through Bluesky's normal signup flow.
2. Use a company-controlled email address, not an owner's personal mailbox.
3. Clearly label the profile as an automated company promotional account.
4. Generate a Bluesky app password for this service. Do not use the primary
   account password.
5. Add these Railway variables to the group-bot service:

   ```text
   REVENUE_COMPANY_ACTION_TARGETS={"publish:bluesky:freelanceremailkit.bsky.social":{"account_id":"freelanceremailkit.bsky.social"}}
   REVENUE_BLUESKY_HANDLE=freelanceremailkit.bsky.social
   REVENUE_BLUESKY_APP_PASSWORD=<company account app password>
   GUMROAD_ACCESS_TOKEN=<read-only sales token>
   AUTONOMY_MAX_AUTHORIZATION=external_action
   AUTONOMY_ENABLED=true
   AUTONOMY_DRY_RUN=false
   AUTONOMY_SCHEDULE_TIME=08:00
   AUTONOMY_SCHEDULE_DAYS=mon-fri
   AUTONOMY_TIMEZONE=America/Phoenix
   BUDGET_TIMEZONE=America/Phoenix
   TIMEZONE=America/Phoenix
   ```

6. Keep `DATA_DIR` pointed at the mounted Railway volume so campaign state,
   action claims, receipts, cost entries, and run reports survive redeploys.
7. Redeploy once after setting the variables. Never paste app passwords or access
   tokens into Telegram, a roadmap manifest, source control, logs, or task text.

## Safe activation

First verify and link the already-published company Gumroad product, then check the
Company ledger and its Phoenix-day budget in the Telegram group operating room:

```text
/products
/link https://tymedina.gumroad.com/l/freelancer-cold-email
/revenue
/company
/setbudget 5
/autorun queue freelancer-cold-email-revenue-sprint-202608
/confirm
/autorun status
/autorun dry-run
```

Queueing stages a deterministic preview. `/confirm` re-reads the exact manifest
revision, performs a live Gumroad read and Bluesky authentication against the exact
company identities, imports the roadmap additively, and activates the separately
persisted campaign policy. It makes no OpenAI call and does not publish a post.
The dry run selects and reports the next campaign item without calling OpenAI or
Bluesky.

After the dry-run preview is correct, either wait for the next configured weekday
schedule or start that day's bounded run from the group:

```text
/autorun live
```

Only one campaign experiment may be claimed for a Phoenix calendar day. Weekend,
overlap, duplicate-date, exhausted-budget, stopped-campaign, or missing-access
conditions fail closed.

## What one live day does

1. Verify the exact Gumroad product and read public engagement counts for all prior
   successful Bluesky post receipts. A failed preflight consumes no campaign day.
2. Claim the exact campaign day and roadmap experiment, then persist the engagement
   counts as that run's `before` snapshot and persist a pre-action Gumroad snapshot.
3. Reserve the complete estimated AI cost inside both daily and campaign caps.
4. Route the task to the least-expensive capable configured model.
5. Let the assigned worker draft and self-check one bounded post without publishing.
6. Have Vera review that exact draft against the explicit criteria with bounded
   revision attempts.
7. Bind the approved draft digest to Vera's review, then revalidate the exact
   campaign revision, company account, target, count cap, run claim, and
   idempotency key before provider I/O.
8. Publish no more than one standalone post and persist only a safe URI/CID
   receipt, never provider credentials or session tokens.
9. Read public like, reply, repost, and quote counts for every successful Bluesky
   post in the campaign, match each result to its persisted URI/CID, and persist
   the run's `after` snapshot. Only increases above each post's persisted high-water
   counts become engagement signals.
10. Pull and persist the post-action Gumroad snapshot, reconcile AI cost, update
    progress/checkpoints, persist the autonomous report, and post one Telegram
    summary.

## Stop and recovery behavior

The campaign stops or escalates rather than improvising around:

- missing company-account credentials or provider verification;
- a target, account, policy revision, run ID, or action type mismatch;
- insufficient daily or total AI budget;
- an uncertain external-action result;
- an old action claim that has no terminal, digest-bound provider receipt;
- unavailable or mismatched public Bluesky engagement evidence;
- repeated no progress;
- failure to meet the day-15 continuation threshold;
- the twentieth run-day, regardless of result.

Do not retry an uncertain publish until the provider receipt/account is inspected;
blind retries could create duplicate posts. A stopped campaign is not silently
reactivated. Correct the configuration or revise the manifest, inspect the audit
record, and create a new owner-confirmed campaign revision when appropriate.

Roadmap import and live campaign activation are intentionally separate commits. If
the roadmap was imported but product/account preflight failed—or Railway restarted
while that confirmation was still pending—fix the company credentials, redeploy,
then stage activation again:

```text
/autorun queue freelancer-cold-email-revenue-sprint-202608
/confirm
```

For the same intact manifest revision, Miles recognizes that the roadmap is already
queued and stages only the missing activation preflight; it does not duplicate the
20 items. If the campaign has a terminal persisted record, it will not reactivate it.
Review the audit history and queue a new manifest/policy revision instead.

## Engagement measurement and checkpoint limits

The native adapter uses Bluesky's public read endpoint without credentials to fetch
cumulative like, reply, repost, and quote counts for at most the campaign's exact
successful post receipts. It requires one response per requested post and matches
both URI and CID. Partial, duplicate, malformed, negative, or identity-mismatched
responses fail closed. The pre-execution read happens before the day is claimed, so
an unavailable read consumes no run-day. The coordinator then claims the day and
persists that observation as the run's `before` snapshot. If this post-claim write
fails, the run becomes `needs_human` and the campaign stops before worker or publish
execution. After a verified publish,
it fetches again and persists the `after` snapshot; if that read or persistence
fails, the run becomes `needs_human`, the campaign stops, and the already-verified
post is not retried.

A successful Bluesky action created by a pre-upgrade build may not have the structured
URI/CID receipt required by this collector. That state fails before claiming another
day and is never reconstructed from free-form result text. Inspect the old receipt and
start a new owner-confirmed campaign revision instead of editing the ledger in place.

Provider counts are aggregate and may decrease when reactions are removed. The ledger
retains a per-post high-water mark, so removing and restoring the same reaction cannot
create a second signal or reset the no-progress guard. New likes, replies, reposts, and
quotes above that high-water mark may satisfy the day-5 meaningful-interest checkpoint.
The publish receipt by itself is execution evidence,
not commercial progress, and does not reset the no-progress counter. Native Bluesky
metrics do not measure clicks, leads, checkout intent, or sales. The day-15 checkpoint
therefore still requires a verified Gumroad sale or an explicitly supported
strong-intent signal such as checkout-started or purchase commitment; ordinary social
engagement does not qualify.

`/autorun dry-run` does not call Gumroad, Bluesky, OpenAI, or another external
provider. It only selects and reports the next eligible item from persisted state.

## Offline verification

The critical tests mock OpenAI, Gumroad, Bluesky, and other paid/external calls:

```powershell
$env:PYTHONUTF8 = "1"
.\.venv\Scripts\python.exe -m unittest tests.test_revenue_sprint_workflow tests.test_revenue_sprint_company tests.test_revenue_actions tests.test_revenue_action_envelopes tests.test_revenue_action_coordinator tests.test_revenue_publish_gate tests.test_group_autonomy
.\.venv\Scripts\python.exe -m unittest discover -s tests
```

These checks validate local policy and orchestration with provider traffic mocked. A
real Railway volume, Telegram delivery, Gumroad token, Bluesky session, URI/CID
receipt, public engagement read, and scheduled 08:00 execution still require a
credentialed production smoke test.
