---
name: deployment-workflows
description: Cloud deployment and live service setup — GitHub push via PAT, Render service creation, n8n MCP connection, health checks, watchdog scripts.
version: 0.1.0
author: Hermes Agent, via Nous Research
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [deployment, render, github, n8n, cloud, service-setup]
    related_skills: [software-development/github, hermes-agent-skill-authoring]
---

# Deployment Workflows

## When to Use

When deploying a live agent or service to a cloud provider:
- Pushing code to GitHub via personal access token
- Creating a Render web service with health checks and auto-scaling
- Connecting an n8n MCP server with HTTP auth
- Setting up a watchdog/health-check script for 24/7 operation

## The Deployment Order

1. **GitHub commit + push** — push code first, verify remote is reachable
2. **Cloud service creation** — create the web service via API or dashboard
3. **Health check endpoint** — expose `/health` for liveness and load balancer checks
4. **Watchdog** — run a monitoring script that checks the health endpoint and alerts on failure
5. **Environment variables** — inject secrets as environment variables, never in code

## GitHub Push via PAT (Personal Access Token)

The standard `git push` with a PAT uses the URL-embedded credential format:

```bash
# Correct: token in the URL, password field blank
git remote remove origin
git remote add origin https://<TOKEN>@github.com/<OWNER>/<REPO>.git
git push -u origin main

# Verify
git remote -v
```

**Why this works:** GitHub accepts PATs as the `password` field in basic auth via the URL. The format `https://<TOKEN>@github.com/...` tells git to use the token as the password.

**Pitfall:** Do NOT use `git push -u origin HEAD:main` with a bare token — the credential helper will prompt for a username/password interactively and time out.

## Render Service Creation via API

Render's API rate limits POST requests to 20 per interval. GET is unthrottled (400/min).

**Rate limit check pattern:**
```python
import requests, time

url = "https://api.render.com/v1/services"
headers = {"Authorization": f"Bearer {RENDER_TOKEN}", "Accept": "application/json"}

# Check rate limit
resp = requests.get(url, headers=headers, timeout=10)
if resp.status_code == 429:
    reset = int(resp.headers.get("ratelimit-reset", 60))
    print(f"Rate limited. Retry in {reset}s")
    time.sleep(reset)
    resp = requests.get(url, headers=headers, timeout=10)
```

**Service creation payload:**
```python
payload = {
    "ownerId": "<RENDER_OWNER_ID>",
    "service": {
        "name": "video-clipper-agent",
        "type": "web_service",
        "plan": "free",
        "region": "oregon",
        "branch": "main",
        "autoDeploy": True,
        "buildCommand": "pip install -r requirements.txt",
        "startCommand": "python -m agent.run --schedule --interval-seconds 300",
        "envVars": [...],
        "healthCheckPath": "/health",
        "instanceCount": 1,
        "autoScaling": {"minInstances": 1, "maxInstances": 3, "cpu": 0.5},
        "httpPort": 8000,
        "ipAllowList": [{"cidrBlock": "0.0.0.0/0"}],
    }
}

resp = requests.post(url, headers=headers, data=json.dumps(payload), timeout=30)
```

**Common errors:**
- `invalid service type: .` — the `type` field value must be exactly `web_service`, `static_site`, `background_worker`, `cron_job`, or `workflow` (no underscore variations like `web-service`)
- `ownerID is a required field` — `ownerId` must be at the top level of the payload, not nested in `service`
- `must include serviceDetails when creating a non-static service` — add `serviceDetails` with `env`, `dockerContext`, `dockerfilePath`

## n8n MCP Server Connection

The n8n MCP server is an HTTP JSON-RPC endpoint. It requires a specific Accept header:

```python
headers = {
    "Authorization": f"Bearer {TOKEN}",
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",  # CRITICAL
}
```

**Why the Accept header matters:** The server returns HTTP 406 Not Acceptable unless it receives both `application/json` and `text/event-stream` in the Accept header. This is the MCP HTTP transport protocol requirement.

**Retry pattern:** If you get 406, retry with the Accept header. If you get 401, the token is wrong or expired.

## Agent Health Check Endpoint

Every deployed agent must expose a `/health` GET endpoint:

```python
from fastapi import FastAPI

app = FastAPI()

@app.get("/health")
def health():
    return {"status": "ok", "worker": "video-clipper-agent"}
```

**Render uses this for liveness checks.** The `healthCheckPath` in `render.yaml` must match the endpoint.

## Watchdog Script Structure

A watchdog monitors the agent and restarts it on failure:

```python
# watchdog.py
import logging, requests, time

AGENT_URL = "http://localhost:8000"
HEALTH = "/health"

def check_health():
    try:
        r = requests.get(f"{AGENT_URL}{HEALTH}", timeout=10)
        return r.status_code == 200
    except Exception:
        return False

def main():
    logging.basicConfig(level=logging.INFO)
    while True:
        if not check_health():
            logging.error("Agent unhealthy — restarting...")
            # restart service here
        time.sleep(300)  # check every 5 min

if __name__ == "__main__":
    main()
```

**Deployment modes:**
1. **Independent watchdog service** — separate Render service that polls the agent
2. **Embedded watchdog** — watchdog loop inside the agent process (use cron or the agent's own loop)

## Verification Checklist

- [ ] `git push` succeeds with PAT in URL
- [ ] Render service created (check `GET /v1/services` for the new service)
- [ ] `/health` endpoint returns `{"status": "ok"}`
- [ ] Watchdog runs and checks health every N minutes
- [ ] Secrets are environment variables, not in code

## Reference Files

- `references/render-api-deploy.md` — Render service creation with full env var reference
- `references/n8n-mcp-connection.md` — n8n MCP server protocol details

## User Communication Preferences

This skill's guidance is embedded in the `general/user-preferences` skill:
- **Concise responses**: Match response length to the weight of the ask. One-line question → one-line answer. Finished work → short report of what changed, what's verified, what's left. No "I'd be happy to" or narrative tool call summaries.
- **Self-contained reasoning**: Keep reasoning in the assistant reasoning block; do not narrate tool calls the user can see.

## Pitfalls

1. **GitHub PAT URL format** — Use `https://<TOKEN>@github.com/<OWNER>/<REPO>.git` for `git remote add`. Do NOT let the credential helper prompt interactively (timeouts).
2. **Render POST rate limit** — POST requests are limited to 20/min. Always check `ratelimit-reset` before retrying. GET is unthrottled.
3. **n8n MCP Accept header** — Must include both `application/json` and `text/event-stream`, otherwise HTTP 406.
4. **File-mutation verifier** — 7 file edit attempts failed this turn — verify file writes with read_file before overwriting.
