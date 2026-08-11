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
other personal account. The action target, company account mapping, campaign policy revision,
run claim, daily/total action count, and provider receipt are all checked before
or after each external action.

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

1. Claim the exact campaign day and roadmap experiment.
2. Pull and persist a pre-action Gumroad revenue snapshot.
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
9. Pull and persist the post-action Gumroad snapshot, reconcile AI cost, update
   progress/checkpoints, persist the autonomous report, and post one Telegram
   summary.

## Stop and recovery behavior

The campaign stops or escalates rather than improvising around:

- missing company-account credentials or provider verification;
- a target, account, policy revision, run ID, or action type mismatch;
- insufficient daily or total AI budget;
- an uncertain external-action result;
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

## Measurement limitation

The native Bluesky adapter currently persists the publish URI/CID but does not read
clicks, replies, likes, or strong-intent signals. Automatic checkpoints therefore
use verified Gumroad sales/revenue plus only signals explicitly returned by a future
approved provider adapter. With the included native Bluesky path, day 5 may pivot
even when unmeasured Bluesky engagement exists, and day 15 continues automatically
only when Gumroad has recorded a sale. No engagement is inferred from a publish
receipt.

## Offline verification

The critical tests mock OpenAI, Gumroad, Bluesky, and other paid/external calls:

```powershell
$env:PYTHONUTF8 = "1"
.\.venv\Scripts\python.exe -m unittest tests.test_revenue_sprint_workflow tests.test_revenue_sprint_company tests.test_revenue_actions tests.test_group_autonomy
.\.venv\Scripts\python.exe -m unittest discover -s tests
```

These checks validate local policy and orchestration. A real Railway volume,
Telegram delivery, Gumroad token, Bluesky session, provider receipt, and scheduled
08:00 execution still require a credentialed production smoke test.
