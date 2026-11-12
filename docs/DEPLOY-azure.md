# Deploying to Azure (alternative, DRAFT)

> INDATEL runs on AWS, so the primary guide is [DEPLOY.md](DEPLOY.md). This Azure version
> is kept in case that changes.

Proposed setup, pending INDATEL IT's approval. Commands are written against current
Azure CLI conventions but haven't been run against INDATEL's subscription. Check each
step against Azure's docs as you go, and adjust names to INDATEL's conventions.

## What gets deployed

| Piece | Azure service | What it does |
|---|---|---|
| Container image | Azure Container Registry | One image for everything (see `Dockerfile`) |
| Dashboard + API | Azure Container Apps (app) | Always on; serves `/` and the JSON API |
| Daily pipeline run | Azure Container Apps **job** (schedule) | `python -m pipeline.run --source hubspot` |
| Weekly cleanup | Azure Container Apps **job** (schedule) | `python -m pipeline.prune` |
| Run history | Azure Files share mounted at `/data` | Shared by the app and the jobs |
| Secrets | Azure Key Vault | Tokens and keys; referenced by the app and jobs |
| Sign-in | Container Apps built-in authentication (Microsoft Entra ID) | Staff use their work login |

The pipeline writes each run to a hidden folder and renames it when finished, and the
API opens finished runs read-only. That keeps the SQLite files safe on the network
share.

## Secrets (Key Vault)

| Name | Value | Used by |
|---|---|---|
| `hubspot-token` | HubSpot read-only private app token | pipeline job |
| `hash-salt` | 64 random hex characters (`python -c "import secrets; print(secrets.token_hex(32))"`). **Never change it** once runs exist, or HubSpot ↔ Salesforce matching breaks across runs. | pipeline job |
| `sf-client-id`, `sf-client-secret` | Salesforce connected app credentials | pipeline job |
| `claude-oauth-token` | From `claude setup-token` on a machine logged in to the Claude subscription | pipeline job |

Plain settings (not secret): `SF_LOGIN_URL`, `EXCLUDE_CONTACT_IDS`, `DATA_DIR=/data`.
Do **not** set `ANTHROPIC_API_KEY`, or the CLI bills the API instead of the subscription.

## Steps

1. **Create the basics** (resource group, registry, storage share, Key Vault):
   ```bash
   RG=rg-insights; LOC=centralus; ACR=acrinsights$RANDOM; SA=stinsights$RANDOM; KV=kv-insights-$RANDOM
   az group create -n $RG -l $LOC
   az acr create -g $RG -n $ACR --sku Basic
   az storage account create -g $RG -n $SA --sku Standard_LRS --kind StorageV2
   az storage share-rm create -g $RG --storage-account $SA -n insights-data --quota 10
   az keyvault create -g $RG -n $KV --enable-rbac-authorization true
   ```
2. **Add the secrets** to Key Vault (portal: **Key vaults** → your vault → **Secrets** →
   **Generate/Import**), using the names in the table above.
3. **Build the image** in the registry from the repo root:
   ```bash
   az acr build -r $ACR -t insights:$(git rev-parse --short HEAD) .
   ```
4. **Create the Container Apps environment and mount the share:**
   ```bash
   az containerapp env create -g $RG -n env-insights -l $LOC
   az containerapp env storage set -g $RG -n env-insights --storage-name data \
     --azure-file-account-name $SA --azure-file-share-name insights-data \
     --azure-file-account-key "$(az storage account keys list -g $RG -n $SA --query [0].value -o tsv)" \
     --access-mode ReadWrite
   ```
5. **Create the dashboard app** (external ingress on port 8000), then in the portal:
   1. **Container Apps** → the app → **Identity** → turn on **System assigned**, and grant
      it the **Key Vault Secrets User** role on the vault (only if the app needs secrets).
   2. **Volumes** → add the `data` Azure Files volume, mount at `/data`.
   3. **Authentication** → **Add identity provider** → **Microsoft** → restrict to
      INDATEL's tenant → **Require authentication**.
   4. **Ingress** → confirm only HTTPS is allowed.
6. **Create the daily pipeline job** (schedule trigger), for example 6:00 AM Central:
   ```bash
   az containerapp job create -g $RG -n job-insights-daily --environment env-insights \
     --trigger-type Schedule --cron-expression "0 11 * * *" \
     --image $ACR.azurecr.io/insights:<tag> --registry-server $ACR.azurecr.io \
     --command "python" --args "-m" "pipeline.run" "--source" "hubspot" \
     --cpu 1 --memory 2Gi --replica-timeout 3600
   ```
   Then in the portal give the job a **System assigned** identity with **Key Vault Secrets
   User**, add the Key Vault secret references as **Secrets**, map them to environment
   variables (`HUBSPOT_TOKEN`, `HASH_SALT`, `SF_CLIENT_ID`, `SF_CLIENT_SECRET`,
   `CLAUDE_CODE_OAUTH_TOKEN`), and mount the `data` volume at `/data`.
7. **Create the weekly cleanup job** the same way with
   `--cron-expression "30 11 * * 0"` and `--args "-m" "pipeline.prune"`.
8. **Run preflight inside the job's environment** before the first scheduled run:
   start the daily job once manually with the command overridden to
   `python -m pipeline.preflight --live`, and check its log under **Execution history**.
9. **Run the pipeline once by hand** (**Run now** on the daily job), then open the
   dashboard URL and check **Data health**.

## Updating

Build a new image tag (step 3), then point the app and both jobs at it
(`az containerapp update` / `az containerapp job update --image ...`).

## Cost notes

Container Apps bill per use; the jobs run a few minutes a day. Claude runs on the
subscription, so each run counts against its usage limits rather than being billed.
