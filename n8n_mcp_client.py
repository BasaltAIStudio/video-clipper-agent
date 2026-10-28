"""
n8n MCP Client - Connects to the n8n MCP server via SSE and JSON-RPC.

Usage:
  1. First, obtain an OAuth token by running: python get_oauth_token.py
  2. Then: python n8n_mcp_client.py

This script searches for, validates, and updates the 'video-agent-campaign-pipeline'
workflow on the n8n server, pointing the Run Clipper Agent node to the Midget URL.
"""
import json
import ssl
import sys
import time
import uuid as uuid_lib
import urllib.request
from typing import Any

MCP_BASE = 'https://njengatiffyy.app.n8n.cloud/mcp-server/http'
MIGET_URL = 'https://video-clipper-agent-wx2yn.us-east-1.migetapp.com'
TOKEN_FILE = '.n8n_mcp_token.json'

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE


def load_token() -> str | None:
    try:
        with open(TOKEN_FILE) as f:
            data = json.load(f)
            return data.get('access_token')
    except FileNotFoundError:
        return None


def sse_connect(token: str) -> str:
    """Connect to MCP SSE endpoint and return the message endpoint URL."""
    req = urllib.request.Request(
        MCP_BASE,
        headers={
            'Accept': 'text/event-stream',
            'Authorization': f'Bearer {token}',
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
        },
    )
    resp = urllib.request.urlopen(req, timeout=10, context=ctx)
    print(f'SSE connected: {resp.status}')
    
    message_endpoint = None
    session_id = None
    
    buffer = b''
    while True:
        chunk = resp.read(1)
        if not chunk:
            time.sleep(0.1)
            continue
        buffer += chunk
        if b'\n\n' in buffer[-200:]:
            text = buffer.decode('utf-8', errors='replace')
            lines = text.split('\n')
            for line in lines:
                line = line.strip()
                if line.startswith('data:'):
                    data = line[5:].strip()
                    try:
                        obj = json.loads(data)
                        if 'endpoint' in obj:
                            message_endpoint = obj['endpoint']
                        if 'sessionId' in obj:
                            session_id = obj['sessionId']
                    except:
                        pass
                elif line.startswith('event:'):
                    pass
            if message_endpoint or session_id:
                break
    
    resp.close()
    
    if not message_endpoint:
        # Default to the MCP base + /message
        message_endpoint = f'{MCP_BASE}/message'
    
    if session_id:
        message_endpoint = message_endpoint.rstrip('/')
        message_endpoint += f'?sessionId={session_id}'
    
    print(f'Message endpoint: {message_endpoint}')
    print(f'Session ID: {session_id}')
    return message_endpoint


def rpc_call(endpoint: str, token: str, method: str, params: dict | None = None) -> dict:
    """Send a JSON-RPC request to the MCP message endpoint."""
    payload = {
        'jsonrpc': '2.0',
        'id': str(uuid_lib.uuid4()),
        'method': method,
    }
    if params:
        payload['params'] = params
    
    req = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode(),
        headers={
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json',
            'Accept': 'application/json',
        },
        method='POST',
    )
    resp = urllib.request.urlopen(req, timeout=30, context=ctx)
    result = json.loads(resp.read())
    
    if 'error' in result:
        print(f'RPC Error: {result["error"]}')
        return {}
    
    return result.get('result', {})


def main():
    token = load_token()
    if not token:
        print('No token found. Run get_oauth_token.py first to get an OAuth token.')
        print('Alternatively, set the N8N_MCP_TOKEN environment variable.')
        sys.exit(1)
    
    print('=== Connecting to n8n MCP server ===')
    endpoint = sse_connect(token)
    
    # Initialize
    print('\n=== Initializing MCP ===')
    init_result = rpc_call(endpoint, token, 'initialize', {
        'protocolVersion': '2024-11-05',
        'capabilities': {'tools': {}},
        'clientInfo': {'name': 'video-deploy', 'version': '1.0'},
    })
    print(json.dumps(init_result, indent=2)[:500])
    
    # Search for existing workflow
    print('\n=== Searching for workflow ===')
    search_result = rpc_call(endpoint, token, 'search_workflows', {
        'query': 'video-agent-campaign-pipeline'
    })
    workflows = search_result.get('workflows', [])
    print(f'Found {len(workflows)} matching workflows:')
    for wf in workflows:
        print(f'  - {wf.get("name")} (ID: {wf.get("id")})')
    
    if not workflows:
        print('\nWorkflow not found. Creating a new one...')
        workflow_code = open('video_agent_workflow.nodejs', 'r').read()
        create_result = rpc_call(endpoint, token, 'create_workflow_from_code', {
            'code': workflow_code
        })
        print(json.dumps(create_result, indent=2)[:500])
    else:
        wf = workflows[0]
        wf_id = wf.get('id')
        
        # Get full workflow details
        print(f'\n=== Getting workflow details (ID: {wf_id}) ===')
        details = rpc_call(endpoint, token, 'get_workflow_details', {'workflowId': wf_id})
        print(json.dumps(details, indent=2)[:1000])
        
        # Validate the corrected workflow before updating
        print('\n=== Validating corrected workflow ===')
        workflow_code = open('video_agent_workflow.nodejs', 'r').read()
        validation = rpc_call(endpoint, token, 'validate_workflow', {
            'code': workflow_code
        })
        print(f'Validation: {json.dumps(validation, indent=2)[:500]}')
        
        # Update the workflow
        print('\n=== Updating workflow ===')
        update_result = rpc_call(endpoint, token, 'update_workflow', {
            'workflowId': wf_id,
            'code': workflow_code
        })
        print(f'Update result: {json.dumps(update_result, indent=2)[:500]}')
        
        # Publish (activate) the workflow
        print('\n=== Publishing workflow ===')
        publish_result = rpc_call(endpoint, token, 'publish_workflow', {'workflowId': wf_id})
        print(f'Publish result: {json.dumps(publish_result, indent=2)[:500]}')


if __name__ == '__main__':
    main()
