import json
import urllib.request

url = 'https://basaltaistudio.app.n8n.cloud/mcp-server/http'
headers = {
    'Authorization': 'Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiI2Njc1YzEwMC1lMGM4LTRiZjUtYWJmMC1jMDE0Mjg2OTllOWYiLCJpc3MiOiJuOG4iLCJqdGkiOiIxYzA5YmFhZC0yMGU5LTQ4N2UtODBiMy04YWUyZDg3ZTJlZDIiLCJpYXQiOjE3OTIwNDQwMzksImF1ZCI6Im1jcC1zZXJ2ZXItYXBpIn0.q4P-wF99gmI0UhqgOzb-lHMpYsIs25nRfQqrp-wrw6Q',
    'Content-Type': 'application/json',
    'Accept': 'application/json, text/event-stream',
}

workflow_code = '''import { workflow, node, trigger, sticky, placeholder, newCredential, ifElse, switchCase, merge, splitInBatches, nextBatch, languageModel, memory, tool, outputParser, embedding, embeddings, vectorStore, retriever, documentLoader, textSplitter, reranker, fromAi, expr } from '@n8n/workflow-sdk';

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
      url: '{{ $json.submit_url || \"https://contentrewards.com/api/webhooks/submit\" }}',
      sendBody: true,
      specifyBody: true,
      contentType: 'json',
      body: '{{ JSON.stringify($json) }}',
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
      videoUrl: '{{ $json.video_url }}',
      title: '{{ $json.clip_id }} - {{ $json.campaign_name }}',
      description: '{{ $json.description || \"Content Rewards campaign clip\" }}'
    },
    credentials: { youTube: newCredential('YouTube') }
  },
  output: [{ videoId: 'VIDEO_ID', videoUrl: 'https://youtu.be/VIDEO_ID', success: true }]
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
      assignments: [
        { id: 'post_url', name: 'post_url', value: expr('{{ $json.videoUrl || \"\" }}') }
      ]
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
      url: '{{ $json.analytics.submit_url || \"https://contentrewards.com/api/webhooks/analytics\" }}',
      sendBody: true,
      specifyBody: true,
      contentType: 'json',
      body: '{{ JSON.stringify($json) }}',
      responseMode: 'all'
    },
    credentials: { openAiApi: newCredential('Content Rewards') }
  },
  output: [{ success: true, analytics: 'tracked' }]
});

// 6. Step 5: Log the submission
const logSubmission = node({
  type: 'n8n-nodes-base.code',
  version: 2,
  config: {
    name: 'Log Submission',
    parameters: {
      mode: 'runOnceForAllItems',
      language: 'javascript',
      jsCode: \"\"\"const now = new Date().toISOString();
items[0].json.submitted_at = now;
items[0].json.status = 'submitted';"""
    }
  },
  output: [{ submitted_at: '2026-10-14T00:00:00Z', status: 'submitted' }]
});

// Compose workflow
export default workflow('content-rewards-posting-analytics', 'Content Rewards: Posting & Analytics')
  .add(startTrigger)
  .to(submitToContentRewards)
  .to(postToYouTube)
  .to(extractYouTubeUrl)
  .to(submitAnalytics)
  .to(logSubmission);
'''
