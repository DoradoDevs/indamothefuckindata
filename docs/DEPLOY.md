# Deploying to AWS (DRAFT)

Proposed setup, pending INDATEL IT's approval. The steps follow current AWS console and CLI
conventions, but none of them has been run in INDATEL's account yet. Check each step against
AWS's documentation as you go, and use INDATEL's names, region, VPC, and tagging rules.
(An Azure version is kept in [DEPLOY-azure.md](DEPLOY-azure.md).)

Two ways to run it. **Option A** (managed containers) is recommended. **Option B** (one
virtual machine) is simpler and fits if INDATEL prefers a VM it already manages, such as
the INDAData instance.

## What gets deployed (Option A: managed containers)

| Piece | AWS service | What it does |
|---|---|---|
| Container image | Amazon ECR | One image for everything (see `Dockerfile`) |
| Dashboard + API | Amazon ECS on Fargate (service, 1 task) | Always on; serves `/` and the JSON API on port 8000 |
| Daily pipeline run | ECS task started by **EventBridge Scheduler** | `python -m pipeline.run --source hubspot` |
| Weekly cleanup | ECS task started by **EventBridge Scheduler** | `python -m pipeline.prune` |
| Run history | Amazon EFS, mounted at `/data` | Shared by the dashboard and the scheduled tasks |
| Secrets | AWS Secrets Manager | Tokens and keys, injected as environment variables |
| HTTPS + sign-in | Application Load Balancer with an ACM certificate and an **authenticate (OIDC or Cognito)** rule | Only INDATEL staff, signing in with their company login, can reach the dashboard |
| Logs | Amazon CloudWatch Logs | Output from the app and each run |

The pipeline writes each run to a hidden folder and renames it when finished, and the API
opens finished runs read-only. That keeps the SQLite files safe on EFS (a network file system).

**Network:** the tasks need outbound HTTPS to HubSpot (`api.hubapi.com`), Salesforce
(INDATEL's `*.my.salesforce.com`), and Anthropic (`api.anthropic.com`, for the Claude passes).
Run them in private subnets with a NAT gateway, or in public subnets with a public IP. Only
the load balancer accepts inbound traffic; the dashboard task accepts traffic only from the
load balancer's security group.

## Secrets (Secrets Manager)

| Secret name | Value | Environment variable | Used by |
|---|---|---|---|
| `insights/hubspot-token` | HubSpot read-only private app token | `HUBSPOT_TOKEN` | pipeline task |
| `insights/hash-salt` | 64 random hex characters (`python -c "import secrets; print(secrets.token_hex(32))"`). **Never change it** once runs exist, or HubSpot-to-Salesforce matching breaks across runs. | `HASH_SALT` | pipeline task |
| `insights/sf-client-id`, `insights/sf-client-secret` | Salesforce connected app credentials | `SF_CLIENT_ID`, `SF_CLIENT_SECRET` | pipeline task |
| `insights/claude-oauth-token` | From `claude setup-token` on a machine logged in to the Claude subscription | `CLAUDE_CODE_OAUTH_TOKEN` | pipeline task |

Plain settings (not secret), set on the task definitions: `DATA_DIR=/data`, `SF_LOGIN_URL`,
`HUBSPOT_PORTAL_ID`, `CONVERTED_STAGES`, `INTERNAL_DOMAINS`, `KEY_SEGMENTS`,
`EXCLUDE_CONTACT_IDS`, `INCLUDE_CONTACT_DETAILS=true`, and `ANALYSIS_CONTEXT` if used (see
`.env.example`). Do **not** set `ANTHROPIC_API_KEY`, or the Claude CLI bills the API
instead of the subscription.

## Steps (Option A)

1. **Create the image repository and push the image** (from the repo root):
   ```bash
   REGION=us-east-1   # INDATEL's region
   ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
   REPO=$ACCOUNT.dkr.ecr.$REGION.amazonaws.com/insights
   TAG=$(git rev-parse --short HEAD)
   aws ecr create-repository --repository-name insights --region $REGION
   aws ecr get-login-password --region $REGION | docker login --username AWS --password-stdin $ACCOUNT.dkr.ecr.$REGION.amazonaws.com
   docker build --platform linux/amd64 -t $REPO:$TAG .
   docker push $REPO:$TAG
   ```
2. **Add the secrets** (console: **Secrets Manager** → **Store a new secret** → **Other type
   of secret** → **Plaintext**), one per row in the table above. Or:
   ```bash
   aws secretsmanager create-secret --name insights/hubspot-token --secret-string '<paste token>'
   ```
   (Typing a secret on the command line saves it in your shell history; the console avoids that.)
3. **Create the file system:** **EFS** → **Create file system** in the same VPC. Add an
   **access point** with POSIX user and group `10001` (the container's user), root directory
   `/insights`, and permissions `750`. Allow NFS (port 2049) from the tasks' security group.
4. **Create the log group:**
   ```bash
   aws logs create-log-group --log-group-name /ecs/insights
   ```
5. **Create two IAM roles:**
   1. **Task execution role** with the managed policy `AmazonECSTaskExecutionRolePolicy`, plus
      `secretsmanager:GetSecretValue` on `insights/*` (so ECS can inject the secrets).
   2. **Task role** with `elasticfilesystem:ClientMount` and `ClientWrite` on the file system.
6. **Create the ECS cluster:** **ECS** → **Clusters** → **Create cluster** → **AWS Fargate**.
7. **Create two task definitions** (Fargate, Linux/X86_64, the image from step 1, the EFS
   volume from step 3 mounted at `/data`, logs to `/ecs/insights`):
   1. `insights-web`: 0.5 vCPU, 1 GB, port 8000, default command, plain settings only (no
      HubSpot, Salesforce, or Claude secrets).
   2. `insights-job`: 1 vCPU, 2 GB, command `python -m pipeline.run --source hubspot`,
      plain settings plus the secrets from step 2 (**Environment variables** → **ValueFrom** →
      the secret's ARN).
8. **Create the load balancer:** **EC2** → **Load balancers** → **Application Load Balancer**,
   internet-facing (or internal if staff connect over VPN). Listener **HTTPS:443** with an
   **ACM** certificate for the chosen name (for example `insights.<indatel domain>`), and a
   target group on port 8000 with health check path `/health`. Redirect HTTP:80 to HTTPS.
9. **Turn on sign-in at the load balancer:** edit the HTTPS listener rule and add an
   **Authenticate** action before **Forward**:
   * **OIDC** with INDATEL's identity provider (for example Microsoft Entra ID, Okta, or
     Google Workspace): issuer, authorization, token, and user info endpoints, plus a client
     ID and secret from an app registration INDATEL IT creates, with the redirect URL
     `https://<dashboard name>/oauth2/idpresponse`.
   * Or **Amazon Cognito** if INDATEL prefers a user pool (federated to their identity provider).
   * Restrict the app registration to INDATEL staff accounts. Unauthenticated requests are redirected to sign in.
10. **Create the dashboard service:** **ECS** → the cluster → **Create service** → task
    definition `insights-web`, 1 task, the load balancer and target group from step 8.
11. **Check the setup before scheduling:** in the cluster, **Run new task** with `insights-job`
    and override the command to `python,-m,pipeline.preflight,--live`. Read the output in
    CloudWatch Logs. Fix anything it flags.
12. **Run the pipeline once by hand** (**Run new task** with `insights-job`, no override), then
    open the dashboard and check **Data health**.
13. **Schedule the runs:** **EventBridge** → **Scheduler** → **Create schedule**:
    1. Daily pipeline, for example `cron(0 11 * * ? *)` (6:00 a.m. Central; Scheduler can also
       take a time zone), target **ECS RunTask** with `insights-job`, Fargate, the same subnets
       and security group, and a role that lets Scheduler run the task and pass its roles.
    2. Weekly cleanup, for example `cron(30 11 ? * SUN *)`, same target with the command
       overridden to `python -m pipeline.prune`.

## Option B: one virtual machine

For a single EC2 instance (or an INDATEL-managed VM):

1. Launch a small instance (2 vCPU, 4 GB, for example `t3.medium`) with an encrypted EBS
   volume. Install Docker. Give the instance an IAM role that can read `insights/*` secrets.
2. Build the image on the instance, or pull it from ECR.
3. Keep runs on the instance: `docker volume create insights-data`.
4. Run the dashboard: `docker run -d --restart unless-stopped -p 8000:8000 -v insights-data:/data --env-file /etc/insights/settings.env insights:<tag>`.
   Keep plain settings in `/etc/insights/settings.env` (readable by root only). Fetch secrets
   at run time with `aws secretsmanager get-secret-value` in a small wrapper script rather
   than writing them to disk.
5. Schedule the daily run and the weekly cleanup with `cron` or a systemd timer, using the
   same image with the command overridden.
6. Put an **Application Load Balancer** with HTTPS and the **Authenticate** rule (steps 8–9
   above) in front of the instance. Don't expose port 8000 to the internet. Allow it only from
   the load balancer's security group.
7. Turn on EBS snapshots (for example with AWS Backup) so run history survives instance loss.

## Updating

Build and push a new image tag (step 1), create a new revision of both task definitions with
that tag, then **Update service** for `insights-web`. The schedules use the newest revision
if they point to the task definition without a revision number; otherwise update them too.

## Cost notes

The scheduled tasks run a few minutes a day. The always-on dashboard task, the load
balancer, and (if used) a NAT gateway are the main standing costs. EFS holds a few hundred
megabytes. Claude runs on the subscription, so each run counts against its usage limits
rather than being billed per call. Check current prices for INDATEL's region before approval.
