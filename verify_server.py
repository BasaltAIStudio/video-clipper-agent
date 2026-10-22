import json
import urllib.request

url = 'https://basaltaistudio.app.n8n.cloud/mcp-server/http'
headers = {
    'Authorization': 'Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiI2Njc1YzEwMC1lMGM4LTRiZjUtYWJmMC1jMDE0Mjg2OTllOWYiLCJpc3MiOiJuOG4iLCJqdGkiOiIxYzA5YmFhZC0yMGU5LTQ4N2UtODBiMy04YWUyZDg3ZTJlZDIiLCJpYXQiOjE3OTIwNDQwMzksImF1ZCI6Im1jcC1zZXJ2ZXItYXBpIn0.q4P-wF99gmI0UhqgOzb-lHMpYsIs25nRfQqrp-wrw6Q',
    'Content-Type': 'application/json',
    'Accept': 'application/json, text/event-stream',
}

print("Step 1: Verifying MCP server...")
verify_request = json.dumps({
    'jsonrpc': '2.0',
    'id': 1,
    'method': 'tools/call',
    'params': {
        'name': 'verify_agent_mcp_server',
        'arguments': {
            'projectId': 'default',
            'name': 'n8n-mcp',
            'url': 'https://basaltaistudio.app.n8n.cloud/mcp-server/http'
        }
    }
}).encode()

req = urllib.request.Request(url, data=verify_request, headers=headers, method='POST')
with urllib.request.urlopen(req, timeout=30) as resp:
    raw = resp.read().decode(errors='replace')

start_marker = '"text":"'
start = raw.find(start_marker)
if start >= 0:
    content_start = start + len(start_marker)
    line = raw[content_start:]
    text = line.strip()
    text = text.encode('utf-8').decode('unicode_escape')
    print(f"Server response: {text[:500]}")
