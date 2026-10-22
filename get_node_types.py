import json
import urllib.request

url = 'https://basaltaistudio.app.n8n.cloud/mcp-server/http'
headers = {
    'Authorization': 'Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiI2Njc1YzEwMC1lMGM4LTRiZjUtYWJmMC1jMDE0Mjg2OTllOWYiLCJpc3MiOiJuOG4iLCJqdGkiOiIxYzA5YmFhZC0yMGU5LTQ4N2UtODBiMy04YWUyZDg3ZTJlZDIiLCJpYXQiOjE3OTIwNDQwMzksImF1ZCI6Im1jcC1zZXJ2ZXItYXBpIn0.q4P-wF99gmI0UhqgOzb-lHMpYsIs25nRfQqrp-wrw6Q',
    'Content-Type': 'application/json',
    'Accept': 'application/json, text/event-stream',
}

# Step 1: Get node types to know what's available
print("Step 1: Getting node types...")
node_request = json.dumps({
    'jsonrpc': '2.0',
    'id': 1,
    'method': 'tools/call',
    'params': {
        'name': 'get_node_types',
        'arguments': {}
    }
}).encode()

req = urllib.request.Request(url, data=node_request, headers=headers, method='POST')
with urllib.request.urlopen(req, timeout=30) as resp:
    raw = resp.read().decode(errors='replace')

start_marker = '"text":"'
start = raw.find(start_marker)
if start >= 0:
    content_start = start + len(start_marker)
    line = raw[content_start:]
    node_types_json = line.strip()
    if node_types_json.endswith(','):
        node_types_json = node_types_json[:-1]
    decoded_types = node_types_json.encode('utf-8').decode('unicode_escape')
    node_types = json.loads(decoded_types)
    print(f"Got {len(node_types.get('nodeTypes', []))} node types")
    # Print key node types
    types_of_interest = ['httpRequest', 'code', 'executeWorkflow', 'dataTable', 'webhook', 'manualTrigger', 'scheduleTrigger']
    for nt in node_types.get('nodeTypes', []):
        if any(t in nt.get('name', '') for t in types_of_interest):
            print(f"  - {nt.get('name')}")
PYEOF
