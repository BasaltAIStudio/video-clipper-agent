import json
import urllib.request

url = 'https://basaltaistudio.app.n8n.cloud/mcp-server/http'
headers = {
    'Authorization': 'Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiI2Njc1YzEwMC1lMGM4LTRiZjUtYWJmMC1jMDE0Mjg2OTllOWYiLCJpc3MiOiJuOG4iLCJqdGkiOiIxYzA5YmFhZC0yMGU5LTQ4N2UtODBiMy04YWUyZDg3ZTJlZDIiLCJpYXQiOjE3OTIwNDQwMzksImF1ZCI6Im1jcC1zZXJ2ZXItYXBpIn0.q4P-wF99gmI0UhqgOzb-lHMpYsIs25nRfQqrp-wrw6Q',
    'Content-Type': 'application/json',
    'Accept': 'application/json, text/event-stream',
}

# Simple 4-node workflow
workflow_code = """
import { workflow, node, trigger, placeholder, newCredential, expr } from '@n8n/workflow-sdk';

// 1. Manual trigger
const start = trigger({
  type: 'n8n-nodes-base.manualTrigger',
  version: 1,
  config: { name: 'Start' },
  output: [{}]
});

// 2. POST to Content Rewards webhook
const submitCR = node({
  type: 'n8n-nodes-base.httpRequest',
  version: 4.3,
  config: {
    name: 'Submit to Content Rewards',
    parameters: {
      method: 'POST',
      url: '={{ $json.submit_url || "https://contentrewards.com/webhook" }}',
      sendBody: true,
      contentType: 'json',
      body: '={{ JSON.stringify($json) }}',
      responseMode: 'all'
    },
    credentials: { openAiApi: newCredential('Content Rewards') }
  },
  output: [{ success: true }]
});

// 3. YouTube upload
const yt = node({
  type: 'n8n-nodes-base.youTube',
  version: 1,
  config: {
    name: 'Upload to YouTube',
    parameters: {
      resource: 'video',
      operation: 'upload',
      videoId: '={{ $json.video_id }}',
      title: '={{ $json.title }}',
      description: '={{ $json.description }}'
    },
    credentials: { youTube: newCredential('YouTube') }
  },
  output: [{ videoId: '{{ $json.video_id }}', videoUrl: '={{ "https://youtu.be/" + $json.video_id }}', success: true }]
});

// 4. POST analytics
const submitAnalytics = node({
  type: 'n8n-nodes-base.httpRequest',
  version: 4.3,
  config: {
    name: 'Submit Analytics',
    parameters: {
      method: 'POST',
      url: '={{ $json.analytics.submit_url || "https://contentrewards.com/webhook/analytics" }}',
      sendBody: true,
      contentType: 'json',
      body: '={{ JSON.stringify($json) }}',
      responseMode: 'all'
    },
    credentials: { openAiApi: newCredential('Content Rewards') }
  },
  output: [{ success: true }]
});

export default workflow('content-rewards-pipeline', 'Content Rewards Pipeline')
  .add(start)
  .to(submitCR)
  .to(yt)
  .to(submitAnalytics);
"""

create_request = json.dumps({
    'jsonrpc': '2.0',
    'id': 1,
    'method': 'tools/call',
    'params': {
        'name': 'create_workflow_from_code',
        'arguments': {
            'name': 'Content Rewards Pipeline',
            'description': 'Simple 4-node pipeline: submit clip to Content Rewards, upload to YouTube, submit analytics.',
            'code': workflow_code
        }
    }
}).encode()

req = urllib.request.Request(url, data=create_request, headers=headers, method='POST')
with urllib.request.urlopen(req, timeout=60) as resp:
    raw = resp.read().decode(errors='replace')
    print(raw)
