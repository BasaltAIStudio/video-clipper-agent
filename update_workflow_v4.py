"""
Fix n8n workflow: Clean up hardcoded credentials from Upload to Instagram,
Upload to TikTok, and TikTok Analytics (Zernio) headers.
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

ZERONIO_API_KEY = 'sk_550972737d0840065057698dadeb952b78c96d8d7c66b77f4955271543348f07'
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

    # Get latest workflow version
    history = rpc_call('get_workflow_history', {'workflowId': WF_ID})
    versions = history.get('versions', [])
    latest_version_id = versions[0]['versionId'] if versions else None
    print(f'Latest version: {latest_version_id}')

    details = rpc_call('get_workflow_version', {'workflowId': WF_ID, 'versionId': latest_version_id})
    nodes = details.get('nodes', [])
    existing_names = [n.get('name', '') for n in nodes]
    print(f'Existing nodes ({len(nodes)}): {existing_names}')

    operations = []

    # 1. Update Upload to Instagram - remove hardcoded headerParameters
    instagram_jsonBody = '= {{ JSON.stringify({ platform: "instagram", videoUrl: $json.public_video_url, caption: $json.caption, title: $json.hook_title, hashtags: $json.tags, apiKey: "' + ZERONIO_API_KEY + '" }) }}'
    operations.append({
        'type': 'updateNodeParameters',
        'nodeName': 'Upload to Instagram',
        'parameters': {
            'method': 'POST',
            'url': 'https://zernio.com/api/v1/posts',
            'sendBody': True,
            'specifyBody': 'json',
            'jsonBody': instagram_jsonBody,
            'sendHeaders': False,
            'specifyHeaders': 'keypair',
            'headerParameters': {'parameters': []},
            'options': {'timeout': 300000},
        },
    })

    # 2. Update Upload to TikTok - remove hardcoded headerParameters
    tiktok_jsonBody = '= {{ JSON.stringify({ platform: "tiktok", videoUrl: $json.public_video_url, caption: $json.caption, title: $json.hook_title, hashtags: $json.tags, apiKey: "' + ZERONIO_API_KEY + '" }) }}'
    operations.append({
        'type': 'updateNodeParameters',
        'nodeName': 'Upload to TikTok',
        'parameters': {
            'method': 'POST',
            'url': 'https://zernio.com/api/v1/posts',
            'sendBody': True,
            'specifyBody': 'json',
            'jsonBody': tiktok_jsonBody,
            'sendHeaders': False,
            'specifyHeaders': 'keypair',
            'headerParameters': {'parameters': []},
            'options': {'timeout': 300000},
        },
    })

    # 3. Update TikTok Analytics (Zernio) - use env var for Authorization header
    operations.append({
        'type': 'updateNodeParameters',
        'nodeName': 'TikTok Analytics (Zernio)',
        'parameters': {
            'method': 'GET',
            'url': 'https://zernio.com/api/v1/analytics/tiktok',
            'sendHeaders': True,
            'specifyHeaders': 'keypair',
            'headerParameters': {
                'parameters': [
                    {'name': 'Authorization', 'value': '= {{ "Bearer " + $env.ZERONIO_API_KEY }}'},
                    {'name': 'Content-Type', 'value': 'application/json'},
                ],
            },
            'queryParameters': {
                'parameters': [
                    {'name': 'videoId', 'value': '= {{ $json.tiktok_video_id || $json.video_id || $json.tiktok_video_url }}'},
                ],
            },
            'options': {'timeout': 60000},
        },
    })

    print(f'\n=== Applying {len(operations)} operations ===')
    result = rpc_call('update_workflow', {
        'workflowId': WF_ID,
        'operations': operations,
    })
    print(f'Result: {json.dumps(result, indent=2)[:3000]}')

    # Publish
    print('\n=== Publishing workflow ===')
    publish = rpc_call('publish_workflow', {'workflowId': WF_ID})
    print(f'Publish: {json.dumps(publish, indent=2)[:500]}')
    print('\n=== Done ===')

if __name__ == '__main__':
    main()
