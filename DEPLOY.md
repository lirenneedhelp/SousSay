# Deployment guide

## Which hosting option?

| Option | Persistent SQLite | HTTPS | AWS credentials | Effort | Verdict |
|---|---|---|---|---|---|
| **A. One EC2 instance + Docker Compose + Caddy** | Yes (Docker volume on EBS) | Automatic (Caddy, needs a domain) | **IAM instance role: no keys on disk** | ~30 min | **Recommended** |
| B. Your laptop + Cloudflare quick tunnel | Yes (local file) | Yes (random `*.trycloudflare.com` URL) | Your own AWS profile | ~5 min | Best demo-day fallback |
| C. App Runner / Fargate / Lambda | **No** (ephemeral disk; needs EFS or a different DB) | Yes | Role | High | Rejected: breaks SQLite |
| D. Fly.io / Render | Yes (volume) | Yes | Needs long-lived access keys as secrets | ~20 min | Workable, weaker credential story |

Why A: it is the only simple option that keeps SQLite on a real disk, uses a role instead of stored keys, and
serves Streamable HTTP MCP without buffering. Lightsail is not suitable (instances cannot assume IAM roles).
Speech recognition needs HTTPS (or localhost), so a public demo needs a domain name.

## Local development

    python -m venv venv && source venv/bin/activate
    pip install -r requirements.txt
    cp .env.example .env     # set ACCESS_CODE (APP_ENV=development is already set)
    # AWS credentials: `aws configure sso`/a profile, or export AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY in your shell
    uvicorn main:app --port 8000
    # open http://localhost:8000 in Chrome/Edge and enter your ACCESS_CODE

Tests: `pip install -r requirements-dev.txt && pytest`. UI assets: only if you change page classes, run
`cd frontend-build && npm install && npm run build` (output: `static/app.css`).

## AWS: Bedrock configuration and permissions

1. Region: set `AWS_DEFAULT_REGION` (default `us-east-1`). Confirm `amazon.nova-lite-v1:0` is available to your
   account in that region (Bedrock console, Model catalog / Model access). If you change `BEDROCK_MODEL_ID`,
   update the policy below. Using a cross-region inference profile (e.g. `us.amazon...`) also needs that profile's
   ARN and the underlying model ARNs in the policy.
2. Least-privilege policy (`deploy/iam-policy.json`): only `bedrock:InvokeModel` on that one model. The Converse API
   is authorised through this action. Nothing else is needed (no admin, no S3, no list/manage actions).
3. Credentials come from the standard AWS provider chain: an instance role on EC2 (recommended), a profile or
   SSO session locally. Never put keys in the page, the image, or Git.

## Option A: EC2 + Docker Compose + Caddy (commands for you to run; nothing here was run on AWS)

These create billable resources, so review each command first. Replace `<...>`.

    # 1. IAM role and instance profile
    aws iam create-role --role-name talksous-ec2 --assume-role-policy-document file://deploy/ec2-trust-policy.json
    aws iam put-role-policy --role-name talksous-ec2 --policy-name invoke-nova-lite --policy-document file://deploy/iam-policy.json
    aws iam create-instance-profile --instance-profile-name talksous-ec2
    aws iam add-role-to-instance-profile --instance-profile-name talksous-ec2 --role-name talksous-ec2

    # 2. Firewall: web ports open, SSH from your IP only
    aws ec2 create-security-group --group-name talksous --description "talksous demo"
    for p in 80 443; do aws ec2 authorize-security-group-ingress --group-name talksous --protocol tcp --port $p --cidr 0.0.0.0/0; done
    aws ec2 authorize-security-group-ingress --group-name talksous --protocol tcp --port 22 --cidr <YOUR_IP>/32

    # 3. Instance. IMPORTANT: hop limit 2 lets containers reach the instance role (IMDSv2 stays required)
    aws ec2 run-instances --instance-type t3.small --key-name <KEY_PAIR> --security-groups talksous \
      --image-id $(aws ssm get-parameter --name /aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id --query Parameter.Value --output text) \
      --iam-instance-profile Name=talksous-ec2 \
      --metadata-options HttpTokens=required,HttpPutResponseHopLimit=2,HttpEndpoint=enabled \
      --block-device-mappings 'DeviceName=/dev/sda1,Ebs={VolumeSize=20,VolumeType=gp3,Encrypted=true}'

    # 4. Allocate an Elastic IP, attach it, and point an A record for <DOMAIN> at it. Wait for DNS.

    # 5. On the instance (ssh in):
    curl -fsSL https://get.docker.com | sudo sh && sudo usermod -aG docker $USER && newgrp docker
    git clone <YOUR_REPO> talksous && cd talksous        # or scp the project folder
    cp .env.example .env && chmod 600 .env
    # edit .env: set ACCESS_CODE, SESSION_SECRET (see generators in the file), optional MCP_API_KEY; leave AWS keys EMPTY
    cd deploy && DOMAIN=<DOMAIN> docker compose up -d --build

    # 6. Check
    curl -s https://<DOMAIN>/api/health                 # {"status":"ok"}
    curl -s -o /dev/null -w "%{http_code}\n" https://<DOMAIN>/api/state?session_id=abcdefgh1   # 401 (locked)
    # open https://<DOMAIN>, enter the access code, and try voice + a timer

MCP clients connect to `https://<DOMAIN>/mcp/` with header `Authorization: Bearer <MCP_API_KEY>`.

## Option B: laptop + Cloudflare quick tunnel (fastest fallback)

    APP_ENV=production ACCESS_CODE=<12+ chars> SESSION_SECRET=<32+ chars> COOKIE_SECURE=true \
      ALLOWED_HOSTS='localhost,127.0.0.1,*.trycloudflare.com' uvicorn main:app --port 8000 --workers 1 --proxy-headers
    cloudflared tunnel --url http://localhost:8000      # prints the https URL; keep the laptop awake and online

## Operations

- **Start/stop:** `docker compose up -d` / `docker compose stop` (SIGTERM, graceful). Restart: `docker compose restart app`.
- **Logs:** `docker compose logs -f app`. Logs contain error causes, never message text or secrets.
- **Persistence:** the database is `/data/talksous.db` on the `talksous-data` volume. It survives restarts and
  rebuilds (verified locally across a process restart; timers keep their original deadlines).
- **Backup:** `docker compose exec app python -c "import sqlite3;a=sqlite3.connect('/data/talksous.db');b=sqlite3.connect('/data/backup.db');a.backup(b);b.close()"`
  then copy `/data/backup.db` out of the volume. Restore: stop the app, copy it back as `talksous.db`, start.
- **Recovery after a crash:** `restart: unless-stopped` brings it back; nothing else to do. A failed model call
  never changes session state beyond the tool actions that really ran.
- **Rotate secrets:** edit `.env`, then `docker compose up -d` (new `SESSION_SECRET` signs everyone out; new
  `ACCESS_CODE` blocks new sign-ins; new `MCP_API_KEY` revokes old MCP clients).
- **Always one worker** (`--workers 1`). Do not scale out.

## Environment variables

| Variable | Required | Notes |
|---|---|---|
| `ACCESS_CODE` | yes | 12+ chars in production (6+ in development) |
| `SESSION_SECRET` | production | 32+ chars; signs cookies |
| `APP_ENV` | no | `production` (default) or `development` |
| `MCP_API_KEY` | no | 24+ chars; unset = `/mcp/` disabled |
| `ALLOWED_HOSTS` | deploy | comma list; default `localhost,127.0.0.1`; keep `127.0.0.1` for the health check |
| `ALLOWED_ORIGINS` | no | extra trusted browser origins |
| `COOKIE_SECURE` | no | default true in production; false only to test over plain `http://localhost` |
| `AWS_DEFAULT_REGION`, `BEDROCK_MODEL_ID` | no | default `us-east-1`, `amazon.nova-lite-v1:0` |
| `CHAT_PER_MIN`, `GLOBAL_CHAT_PER_HOUR`, `LOGIN_PER_MIN`, `TURN_TIMEOUT_S`, `COOKIE_TTL_HOURS`, `MAX_BODY_BYTES` | no | limits |
| `TALKSOUS_DB` | no | database path (Docker: `/data/talksous.db`) |

## Hackathon checklist

- [ ] `.env` exists only on the server, `chmod 600`, not in Git (`git status` shows nothing sensitive)
- [ ] `APP_ENV=production`, strong `ACCESS_CODE` and `SESSION_SECRET`, `ALLOWED_HOSTS` set to your domain
- [ ] IAM role has only `iam-policy.json`; no AWS keys in `.env`; an AWS budget alert is set
- [ ] `https://<DOMAIN>/api/health` is ok; `/docs` is 404; the API without sign-in is 401
- [ ] Voice and a timer tested on the actual demo device and browser (Chrome/Edge), with the tab in front
- [ ] MCP key set only if you will demo MCP; tested with `python bedrock_agent.py --smoke`
- [ ] After the event: rotate or delete `ACCESS_CODE`, `MCP_API_KEY`; stop or terminate the instance
