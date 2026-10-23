---

## Render API Service Creation

### Service Types (from API docs)
- `static_site` — static HTML/CSS/JS
- `web_service` — containerized web app (FastAPI, Node, etc.)
- `private_service` — internal service with IP allowlist
- `background_worker` — long-running job without HTTP
- `cron_job` — scheduled task
- `workflow` — multi-step workflow

### Required Fields for web_service
```json
{
  "ownerId": "<RENDER_OWNER_ID>",
  "service": {
    "name": "service-name",
    "type": "web_service",
    "plan": "free|starter|standard",
    "region": "oregon|frankfurt|us-east|us-west",
    "branch": "main",
    "autoDeploy": true,
    "buildCommand": "pip install -r requirements.txt",
    "startCommand": "python -m app",
    "httpPort": 8000,
    "ipAllowList": [{"cidrBlock": "0.0.0.0/0"}],
    "healthCheckPath": "/health"
  }
}
```

### ServiceDetails (required for non-static services)
```json
"serviceDetails": {
  "env": "docker",
  "dockerContext": ".",
  "dockerfilePath": "Dockerfile"
}
```

### Environment Variables
```json
"envVars": [
  {"key": "AGENT_PORT", "value": "8000"},
  {"key": "LLM_PROVIDER", "value": "nvidia_nim"},
  {"key": "NVIDIA_NIM_API_KEY", "value": "REDACTED"}
]
```

### Rate Limit Headers
```
ratelimit-limit: 20
ratelimit-remaining: 0
ratelimit-reset: 602
retry-after: 602
```

### Getting the Owner ID
```bash
curl -s https://api.render.com/v1/services \
  -H "Authorization: Bearer $RENDER_TOKEN" \
  -H "Accept: application/json" | python3 -c "import sys,json; print([s['ownerId'] for s in json.load(sys.stdin) if 'service' in s][0])"
```

---

## n8n MCP Server Connection

The n8n MCP server is an HTTP JSON-RPC endpoint. The protocol requires:

### Request Format
```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/list",
  "params": {}
}
```

### Headers (CRITICAL)
```python
headers = {
    "Authorization": f"Bearer {TOKEN}",
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}
```

**The Accept header must include both `application/json` and `text/event-stream`.** Without it, the server returns HTTP 406 Not Acceptable.

### Response Format (SSE)
MCP HTTP transport uses Server-Sent Events:
```
event: message
data: {"jsonrpc":"2.0","id":1,"result":{"tools":[...]}}

event: message
data: {"jsonrpc":"2.0","id":1,"method":"notifications/tools/list"}
```

### Common Errors
- **406 Not Acceptable** — Missing `Accept: application/json, text/event-stream` header
- **401 Unauthorized** — Invalid or expired bearer token
- **Connection refused** — Server is down or wrong port

---

## Render Service Validation Checklist

After creating a service:
1. Get service ID: `curl /v1/services` and find by name
2. Check service status: `GET /v1/services/{id}` → verify `suspended: false`
3. Get deployment URL: `service.url`
4. Test health endpoint: `curl $SERVICE_URL/health` → expect `{"status": "ok"}`

## Common Render API Errors
- `rate limit exceeded (429)` — POST requests limited to 20/interval. Wait for `ratelimit-reset` header.
- `invalid service type: .` — type value is not in the valid enum list
- `ownerID is a required field` — ownerId must be top-level, not in service
- `must include serviceDetails when creating a non-static service` — add serviceDetails for non-static types
