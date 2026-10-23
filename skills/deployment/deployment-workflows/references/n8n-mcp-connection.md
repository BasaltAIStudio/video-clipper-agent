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
