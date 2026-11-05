# Project status

**Last updated:** November 5, 2026
**State:** Built and tested on sample data. **Waiting on data access** before the first real run.

Nothing in this repo contains real INDATEL data. All numbers in screenshots, tests, and
sample runs come from the synthetic generator (`pipeline/extract/fixtures.py`).

## Waiting on

| # | Access | From | Why we need it | Unblocks |
|---|---|---|---|---|
| 1 | **HubSpot read-only private app token** (contacts, companies, deals, campaigns, marketing emails) | INDATEL HubSpot admin | The scheduled pipeline pulls data with it. The chat connector can't do bulk pulls, and the current user has no export permission. | First real run; daily refresh |
| 2 | **HubSpot campaign + marketing email read permission** on the project user | INDATEL HubSpot admin | Campaign and email results are blocked for the current user. | Campaign and email pages with real data |
| 3 | **Salesforce read-only API access** (Opportunities, Accounts, Contacts) | INDATEL Salesforce admin | Telecom service sales appear to live in Salesforce, not HubSpot. HubSpot only holds event sponsorship deals. | "Which marketing brings in money" |
| 4 | **Compliance sign-off** on which HubSpot/Salesforce fields are in scope | INDATEL compliance | Customer records and deal values are confidential; circuit details, service locations, and call records must stay out. | First real pull |
| 5 | **Hosting decision** (Azure Container Apps + Entra ID sign-in proposed) | INDATEL IT | Where the pipeline and dashboard run, and how staff log in. | Sharing the dashboard |

## Done

- **Pipeline end to end:** extract → normalize (personal fields dropped) → metrics →
  Claude analysis → code check of every number → independent Claude review → API.
- **Dashboard** for sales staff and leadership: time-period filter with period-over-period
  comparison; overview, campaigns, lead sources, industries, "what moves together", all
  findings, data health, and plain-English definitions. Works on phones and in dark mode.
- **Adapted to the real HubSpot account** after a read-only, summary-level look through
  the connector (no records copied):
  - wins detected with HubSpot's closed/won flags (the migrated Salesforce pipeline uses
    internal stage codes)
  - sales and sponsorship pipelines reported separately
  - deal names not pulled (they often contain customer names)
  - HubSpot's sample contacts can be excluded (`EXCLUDE_CONTACT_IDS`)
- **Claude runs on the subscription** (`CLAUDE_CODE_OAUTH_TOKEN`), not the API.
- **13 automated tests** covering metrics, personal-data stripping, the number checker,
  the API, the dashboard queries, and the real account's data shapes.

## What we know about the live HubSpot account

From a read-only look through the connector. Details in [DESIGN.md §6](DESIGN.md#6-what-the-live-hubspot-account-looks-like-checked-via-the-connector).

- HubSpot is mainly a **marketing contact database plus event sponsorships**.
- The migrated sales pipeline is **empty**; service sales are likely in Salesforce.
- **Campaign attribution is almost never set** on contacts. Campaign results will stay
  thin until Marketing tags campaigns consistently. Worth raising with Marketing now,
  since it only helps going forward.

## Next steps once access lands

1. **HubSpot token arrives:**
   1. Add `HUBSPOT_TOKEN` and `EXCLUDE_CONTACT_IDS` to the pipeline host's secrets.
   2. Run `python -m pipeline.run --source hubspot --skip-claude` and check the record
      counts and Data health page against HubSpot.
   3. Fix anything the extractor gets wrong (the campaign and email endpoints are
      written from HubSpot's docs and marked `VERIFY`).
   4. Run with Claude and review the first findings before anyone else sees them.
2. **Salesforce access arrives:**
   1. Build `pipeline/extract/salesforce.py` (Opportunities, Accounts, Contacts).
   2. Match Salesforce to HubSpot by company domain and by a one-way hash of contact
      email, computed at extract time so raw emails are never stored.
   3. Add "marketing → won service deals" metrics and a dashboard page for them.
3. **Hosting approved:** deploy the container, schedule the daily run, put Entra ID
   sign-in in front of the dashboard.

## Can do now, without access

- **Significance testing:** confidence ranges on rates so the dashboard and Claude can
  tell real differences from noise. (On sample data, the reviewer approved an industry
  difference that wasn't planted. This closes that gap.)
- **CSV importer**, in case someone with export permission can provide files sooner.
- **Salesforce extractor skeleton** against Salesforce's API docs, ready to test.

## Decisions made

| Decision | Choice |
|---|---|
| Accuracy approach | Python computes every number; Claude only interprets; code and a second Claude review check it |
| Claude billing | Subscription via Claude Code CLI, not API key |
| "Revenue" on the dashboard | Labeled "won deal value" (booked value, not cash received) |
| Dashboard audience | INDATEL sales staff and leadership, non-technical |
| Hosting | Leaning Azure, pending INDATEL |
| Data handling | Personal fields dropped before analysis; no bulk copying via the chat connector; dashboard marked internal and confidential |
