import json
import urllib.request

url = 'https://basaltaistudio.app.n8n.cloud/mcp-server/http'
headers = {
    'Authorization': 'Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiI2Njc1YzEwMC1lMGM4LTRiZjUtYWJmMC1jMDE0Mjg2OTllOWYiLCJpc3MiOiJuOG4iLCJqdGkiOiIxYzA5YmFhZC0yMGU5LTQ4N2UtODBiMy04YWUyZDg3ZTJlZDIiLCJpYXQiOjE3OTIwNDQwMzksImF1ZCI6Im1jcC1zZXJ2ZXItYXBpIn0.q4P-wF99gmI0UhqgOzb-lHMpYsIs25nRfQqrp-wrw6Q',
    'Content-Type': 'application/json',
    'Accept': 'application/json, text/event-stream',
}

# Workflow code for Content Rewards posting and analytics
workflow_code = '''
import { workflow, node, trigger, sticky, placeholder, newCredential, ifElse, switchCase, merge, splitInBatches, nextBatch, languageModel, memory, tool, outputParser, embedding, embeddings, vectorStore, retriever, documentLoader, textSplitter, reranker, fromAi, expr } from '@n8n/workflow-sdk';

// 1. Manual trigger
const startTrigger = trigger({
  type: 'n8n-nodes-base.manualTrigger',
  version: 1,
  config: { name: 'Start - Campaign Submission' },
  output: [{}]
});

// 2. Step 1: Submit to Content Rewards via webhook
const submitToContentRewards = node({
  type: 'n8n-nodes-base.httpRequest',
  version: 4.3,
  config: {
    name: 'Submit to Content Rewards Webhook',
    parameters: {
      method: 'POST',
      url: '={{ $json.submit_url || "https://contentrewards.com/api/webhooks/submit" }}',
      sendBody: true,
      specifyBody: 'json',
      contentType: 'json',
      body: '={{ JSON.stringify($json) }}',
      responseMode: 'all'
    },
    credentials: { openAiApi: newCredential('Content Rewards') }
  },
  output: [{ success: false, error: 'No response' }]
});

// 3. Step 2: Post video to YouTube via YouTube API
const postToYouTube = node({
  type: 'n8n-nodes-base.youTube',
  version: 1,
  config: {
    name: 'YouTube Upload',
    parameters: {
      resource: 'video',
      operation: 'upload',
      videoId: '={{ $json.videoId }}',
      title: '={{ $json.clip_id }} - {{ $json.campaign_name }}',
      description: '={{ $json.description || "Content Rewards campaign clip" }}'
    },
    credentials: { youTube: newCredential('YouTube') }
  },
  output: [{ videoId: '{{ $json.videoId }}', videoUrl: '={{ "https://youtu.be/" + $json.videoId }}', success: true }]
});

// 4. Step 3: Extract YouTube URL from n8n response
const extractYouTubeUrl = node({
  type: 'n8n-nodes-base.set',
  version: 3.4,
  config: {
    name: 'Extract YouTube URL',
    parameters: {
      mode: 'manual',
      includeOtherFields: true,
      assignments: {
        assignments: [
          { id: 'post_url', name: 'post_url', value: '={{ "https://youtu.be/" + $json.videoId }}', type: 'string' }
        ],
        type: 'set'
      }
    }
  },
  output: [{ post_url: 'https://youtu.be/VIDEO_ID', success: true }]
});

// 5. Step 4: Submit analytics to Content Rewards
const submitAnalytics = node({
  type: 'n8n-nodes-base.httpRequest',
  version: 4.3,
  config: {
    name: 'Submit Analytics to Content Rewards',
    parameters: {
      method: 'POST',
      url: '={{ $json.analytics.submit_url || "https://contentrewards.com/api/webhooks/analytics" }}',
      sendBody: true,
      specifyBody: 'json',
      contentType: 'json',
      body: '={{ JSON.stringify($json) }}',
      responseMode: 'all'
    },
    credentials: { openAiApi: newCredential('Content Rewards') }
  },
  output: [{ success: true, analytics: 'tracked' }]
});

// Remove the logSubmission node (jsCode with newlines caused parse errors)
// The workflow now has 5 steps: trigger -> submit -> YouTube -> extract URL -> submit analytics
export default workflow('content-rewards-posting-analytics', 'Content Rewards: Posting & Analytics')
  .add(startTrigger)
  .to(submitToContentRewards)
  .to(postToYouTube)
  .to(extractYouTubeUrl)
  .to(submitAnalytics);
'''

# Create the workflow via MCP
create_request = json.dumps({
    'jsonrpc': '2.0',
    'id': 1,
    'method': 'tools/call',
    'params': {
        'name': 'create_workflow_from_code',
        'arguments': {
            'name': 'Content Rewards: Posting & Analytics',
            'description': 'Full posting and analytics pipeline for Content Rewards campaigns. Submits clips via webhook, posts to YouTube via n8n YouTube API, extracts YouTube URL, and submits analytics back to Content Rewards.',
            'code': workflow_code
        }
    }
}).encode()

req = urllib.request.Request(url, data=create_request, headers=headers, method='POST')
with urllib.request.urlopen(req, timeout=60) as resp:
    raw = resp.read().decode(errors='replace')
    print(raw)
