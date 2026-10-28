"""
Fix pre-existing hardcoded credentials on Composio-based nodes:
Upload to YouTube, Poll Analytics, Instagram Analytics.
Convert hardcoded x-api-key headers to env var references.
"""
import json
import ssl
import uuid as uuid_lib
import urllib.request

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

MCP_BASE = 'https://njengatiffyy.app.n8n.cloud/mcp-server/http'
TOKEN_FILE = '.n8n_mcp_token.json'
COMPOSIO_API_KEY = 'ck_lZ_c-AUbOW9-0byww15T'
WF_ID = 'GKqRgtAbBe66W2B3'

def load_token():
    import os
    token = os.getenv('N8N_MCP_TOKEN')
    if token:
        return token.strip()
    try:
        with open(TOKEN_FILE) as f:
            return json.load(f).get('access_token')
    except FileNotFoundError:
        return None

def rpc_call(tool_name, arguments=None):
    token = load_token()
    payload = {
        'jsonrpc': '2.0',
        'id': str(uuid_lib.uuid4()),
        'method': 'tools/call',
        'params': {'name': tool_name, 'arguments': arguments or {}},
    }
    req = urllib.request.Request(
        MCP_BASE,
        data=json.dumps(payload).encode(),
        headers={
            'Authorization': 'Bearer ' + token,
            'Content-Type': 'application/json',
            'Accept': 'application/json, text/event-stream',
        },
        method='POST',
    )
    resp = urllib.request.urlopen(req, timeout=60, context=ctx)
    raw = resp.read().decode('utf-8', errors='replace')
    for line in raw.split('\n'):
        line = line.strip()
        if line.startswith('data:'):
            data = line[5:].strip()
            try:
                sse_json = json.loads(data)
                result = sse_json.get('result', {})
                content = result.get('content', [])
                for item in content:
                    if item.get('type') == 'text':
                        return json.loads(item['text'])
                return result
            except (json.JSONDecodeError, KeyError):
                continue
    return {}

def main():
    token = load_token()
    if not token:
        print('No token found. Run refresh_token.py first.')
        import sys
        sys.exit(1)

    history = rpc_call('get_workflow_history', {'workflowId': WF_ID})
    versions = history.get('versions', [])
    latest_version_id = versions[0]['versionId'] if versions else None
    print(f'Latest version: {latest_version_id}')

    details = rpc_call('get_workflow_version', {'workflowId': WF_ID, 'versionId': latest_version_id})
    nodes = details.get('nodes', [])
    node_map = {n.get('name', ''): n for n in nodes}

    operations = []

    # Fix Upload to YouTube - replace hardcoded x-api-key with env var
    yt_node = node_map.get('Upload to YouTube', {})
    yt_params = yt_node.get('parameters', {})
    yt_header_params = yt_params.get('headerParameters', {}).get('parameters', [])
    yt_new_headers = []
    for h in yt_header_params:
        if h.get('name') == 'x-api-key':
            yt_new_headers.append({'name': 'x-api-key', 'value': '= {{ $env.COMPOSIO_API_KEY }}'})
        else:
            yt_new_headers.append(h)

    operations.append({
        'type': 'updateNodeParameters',
        'nodeName': 'Upload to YouTube',
        'parameters': {
            'headerParameters': {'parameters': yt_new_headers},
        },
    })

    # Fix Poll Analytics - replace hardcoded x-api-key with env var
    pa_node = node_map.get('Poll Analytics', {})
    pa_params = pa_node.get('parameters', {})
    pa_header_params = pa_params.get('headerParameters', {}).get('parameters', [])
    pa_new_headers = []
    for h in pa_header_params:
        if h.get('name') == 'x-api-key':
            pa_new_headers.append({'name': 'x-api-key', 'value': '= {{ $env.COMPOSIO_API_KEY }}'})
        else:
            pa_new_headers.append(h)

    operations.append({
        'type': 'updateNodeParameters',
        'nodeName': 'Poll Analytics',
        'parameters': {
            'headerParameters': {'parameters': pa_new_headers},
        },
    })

    # Fix Instagram Analytics - replace hardcoded x-api-key with env var
    ia_node = node_map.get('Instagram Analytics', {})
    ia_params = ia_node.get('parameters', {})
    ia_header_params = ia_params.get('headerParameters', {}).get('parameters', [])
    ia_new_headers = []
    for h in ia_header_params:
        if h.get('name') == 'x-api-key':
            ia_new_headers.append({'name': 'x-api-key', 'value': '= {{ $env.COMPOSIO_API_KEY }}'})
        else:
            ia_new_headers.append(h)

    operations.append({
        'type': 'updateNodeParameters',
        'nodeName': 'Instagram Analytics',
        'parameters': {
            'headerParameters': {'parameters': ia_new_headers},
        },
    })

    print(f'\n=== Applying {len(operations)} operations ===')
    result = rpc_call('update_workflow', {
        'workflowId': WF_ID,
        'operations': operations,
    })
    print(f'Result: {json.dumps(result, indent=2)[:3000]}')

    print('\n=== Publishing workflow ===')
    publish = rpc_call('publish_workflow', {'workflowId': WF_ID})
    print(f'Publish: {json.dumps(publish, indent=2)[:500]}')
    print('\n=== Done ===')

if __name__ == '__main__':
    main()
