import json
import urllib.request

url = 'https://basaltaistudio.app.n8n.cloud/mcp-server/http'
headers = {
    'Authorization': 'Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiI2Njc1YzEwMC1lMGM4LTRiZjUtYWJmMC1jMDE0Mjg2OTllOWYiLCJpc3MiOiJuOG4iLCJqdGkiOiIxYzA5YmFhZC0yMGU5LTQ4N2UtODBiMy04YWUyZDg3ZTJlZDIiLCJpYXQiOjE3OTIwNDQwMzksImF1ZCI6Im1jcC1zZXJ2ZXItYXBpIn0.q4P-wF99gmI0UhqgOzb-lHMpYsIs25nRfQqrp-wrw6Q',
    'Content-Type': 'application/json',
    'Accept': 'application/json, text/event-stream',
}
data = json.dumps({
    'jsonrpc': '2.0',
    'id': 3,
    'method': 'tools/call',
    'params': {
        'name': 'get_workflow_details',
        'arguments': {'workflowId': '8LQm502vsdhmX9Sg'}
    }
}).encode()

req = urllib.request.Request(url, data=data, headers=headers, method='POST')
with urllib.request.urlopen(req, timeout=30) as resp:
    raw = resp.read().decode(errors='replace')

# Find the JSON string
start_marker = '"text":"'
start = raw.find(start_marker)
if start >= 0:
    content_start = start + len(start_marker)
    line = raw[content_start:]
    json_str = line.strip()
    if json_str.endswith(','):
        json_str = json_str[:-1]
    # Unescape double-escaped JSON
    json_str = json_str.replace('\\"', '"')
    json_str = json_str.replace('\\\\', '\\')
    print(f'Extracted JSON length: {len(json_str)}')
    print(f'First 300 chars: {json_str[:300]}')
    try:
        workflow = json.loads(json_str)
        wf = workflow.get('workflow', {})
        print('Workflow:', wf.get('name'))
        print('Nodes:', len(wf.get('nodes', [])))
        for node in wf.get('nodes', []):
            print(f'  - {node.get("name")} ({node.get("type")})')
    except Exception as e:
        print(f'JSON parse error: {e}')
else:
    print('Could not find JSON in response')
