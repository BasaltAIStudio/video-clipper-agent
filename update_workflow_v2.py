"""
Update the n8n 'video-agent-campaign-pipeline' workflow with:
1. Fixed clipper URL (Midget async endpoint)
2. max_download_seconds: 600
3. Async polling chain (Wait -> Poll -> Check)
4. Composio API key for TikTok + YouTube posting + analytics
5. Zernio API key for TikTok + YouTube posting + analytics
6. Payment notification via Composio Gmail
7. Content Rewards submission with posted video links
8. Record performance for learning loop
"""
import json
import time
import uuid as uuid_lib
import urllib.request
import ssl
import sys

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

MCP_BASE = 'https://njengatiffyy.app.n8n.cloud/mcp-server/http'
TOKEN_FILE = '.n8n_mcp_token.json'

COMPOSIO_API_KEY = 'ck_lZ_c-AUbOW9-0byww15T'
ZERONIO_API_KEY = 'sk_550972737d0840065057698dadeb952b78c96d8d7c66b77f4955271543348f07'
MIGET_URL = 'https://video-clipper-agent-wx2yn.us-east-1.migetapp.com'
WF_ID = 'GKqRgtAbBe66W2B3'

def load_token():
    import os
    token = os.getenv('N8N_MCP_TOKEN')
    if token:
        return token.strip()
    try:
        with open(TOKEN_FILE) as f:
            data = json.load(f)
            return data.get('access_token')
    except FileNotFoundError:
        return None

def rpc_call(tool_name, arguments=None):
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
            'Authorization': f'Bearer {load_token()}',
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

# JS code strings
ENHANCE_CLIPS_JS = r"""const NL = String.fromCharCode(10);
const out = [];
for (const item of $input.all()) {
  const r = item.json;
  const result = r.result || r;
  const clips = Array.isArray(result.clips) ? result.clips : (Array.isArray(r.clips) ? r.clips : []);
  const campaignName = String(result.campaign_name || r.campaign_name || '');
  for (const c of clips) {
    const text = String(c.text || '').trim();
    const hook = (text.split(/[.!?]/)[0] || campaignName || 'Watch this').trim().slice(0, 80);
    const dur = Number(c.duration || 0);
    const tags = ['#fyp', '#shorts', '#viral'];
    const campaignTag = campaignName.replace(/[^a-zA-Z0-9]/g, '').toLowerCase().slice(0, 20);
    if (campaignTag) tags.push('#' + campaignTag);
    out.push({ json: {
      campaign_id: result.campaign_id || r.campaign_id,
      campaign_name: campaignName,
      clip_id: c.clip_id || c.id || '',
      source_url: result.youtube_url || r.youtube_url || '',
      video_url: c.video_url || c.url || '',
      ass_url: c.ass_url || '',
      duration: dur,
      score: Number(c.score || 0),
      text: text,
      hook_title: hook,
      tags: tags,
      caption: hook + NL + NL + tags.join(' '),
      enhance: {
        captions: { style: 'word-by-word highlight', max_words_per_line: 4, font: 'bold sans-serif', highlight_color: '#FFD400', position: 'lower third inside platform safe zone', subtitle_file: c.ass_url || '' },
        audio: { background_music: 'upbeat royalty-free track only', music_volume_db: -22, duck_under_speech: true, sfx: ['whoosh on hook', 'pop on key word'], target_loudness_lufs: -14 },
        thumbnail: { frame_at_seconds: Math.min(Math.max(dur * 0.15, 1), 3), overlay_text: hook.toUpperCase().slice(0, 40), style: 'high contrast, face close-up, yellow outlined text' },
        framing: { aspect: '9:16', resolution: '1080x1920', crop: 'face-centered smart crop', safe_margin_pct: 8, zoom: 'slow punch-in 1.00 to 1.08 every 4 seconds' },
        hook: { first_seconds: 3, text_overlay: hook, pattern_interrupt_at_seconds: 3 }
      }
    } });
  }
}
return out;"""

PLATFORM_POLICY_CHECK_JS = r"""const CFG = { clipperBase: 'https://video-clipper-agent-wx2yn.us-east-1.migetapp.com', minDur: 5, maxDur: 60, maxHashtags: 5, maxPostsPerDay: 3, staggerHours: 3 };
const BANNED = ['sub4sub', 'follow for follow', 'f4f', 'like for like', 'free money', 'guaranteed income', 'click the link in bio', 'giveaway', 'dm me to earn'];
const seen = new Set();
const passed = [];
let slot = 0;
for (const item of $input.all()) {
  const j = item.json;
  const reasons = [];
  const caption = String(j.caption || '');
  const lower = (caption + ' ' + String(j.text || '')).toLowerCase();
  if (!j.video_url) reasons.push('missing video url');
  if (j.duration < CFG.minDur || j.duration > CFG.maxDur) reasons.push('duration outside 5-60s');
  if ((j.tags || []).length > CFG.maxHashtags) reasons.push('too many hashtags');
  if (caption.length > 2200) reasons.push('caption too long');
  if (String(j.hook_title || '').length > 100) reasons.push('title over 100 chars');
  for (const b of BANNED) { if (lower.indexOf(b) !== -1) reasons.push('spam phrase: ' + b); }
  const letters = caption.replace(/[^a-zA-Z]/g, '');
  if (letters.length > 10 && letters.replace(/[^A-Z]/g, '').length / letters.length > 0.6) reasons.push('mostly capital letters');
  const key = String(j.text || '').toLowerCase().replace(/[^a-z0-9]/g, '').slice(0, 120);
  if (key && seen.has(key)) reasons.push('duplicate of another clip in this batch');
  if (key) seen.add(key);
  if (!(j.enhance && j.enhance.captions)) reasons.push('no captions, low originality');
  if (slot >= CFG.maxPostsPerDay) reasons.push('daily post cap reached');
  if (reasons.length) continue;
  slot += 1;
  const scheduled = new Date(Date.now() + slot * CFG.staggerHours * 3600 * 1000).toISOString();
  passed.push({ json: Object.assign({}, j, {
    scheduled_for: scheduled,
    public_video_url: CFG.clipperBase + '/clips/' + String(j.clip_id || j.video_url || '').split('/').pop()
  }) });
}
return passed;"""

COLLECT_LINKS_JS = r"""const ytItems = $('Upload to YouTube').all();
const ttItems = $('Upload to TikTok').all();
const igItems = $('Upload to Instagram').all();
const ytStats = $('Poll Analytics').all();
const ttStats = $('TikTok Analytics').all();
const ytStatsZ = $('YouTube Analytics (Zernio)').all();

const links = [];
let i = 0;
for (const base of $input.all()) {
  const yt = ytItems[i]?.json?.data?.data?.id || ytItems[i]?.json?.data?.id || ytItems[i]?.json?.id || base.json.youtube_url || '';
  const tt = ttItems[i]?.json?.data?.id || ttItems[i]?.json?.id || '';
  const ig = igItems[i]?.json?.data?.id || igItems[i]?.json?.id || '';

  const ytViews = ytStats[i]?.json?.data?.items?.[0]?.statistics?.viewCount || ytStatsZ[i]?.json?.viewCount || 0;
  const ttViews = ttStats[i]?.json?.data?.view_count || ttStats[i]?.json?.viewCount || 0;

  links.push({ json: Object.assign({}, base.json, {
    youtube_post_url: yt ? 'https://youtube.com/watch?v=' + yt : '',
    tiktok_post_url: tt ? 'https://vm.tiktok.com/' + tt : '',
    instagram_post_url: ig ? 'https://instagram.com/p/' + ig : '',
    youtube_views: Number(ytViews),
    tiktok_views: Number(ttViews),
  }) });
  i++;
}
return links;"""

PAYMENT_NOTIF_JS = r"""const items = $input.all();
const notif = [];
for (const item of items) {
  const j = item.json;
  const earnings = Number(j.earnings || j.cpm || 0);
  const ytViews = Number(j.youtube_views || 0);
  const ttViews = Number(j.tiktok_views || 0);
  const totalViews = ytViews + ttViews;

  if (earnings > 0 || totalViews > 0) {
    notif.push({ json: Object.assign({}, j, {
      payment_notification: true,
      notify_email: 'tiffowambui@gmail.com',
      notification_type: earnings > 0 ? 'payment' : 'analytics_update',
      subject: earnings > 0
        ? 'Payment Received: $' + earnings.toFixed(2) + ' for ' + (j.campaign_name || 'campaign')
        : 'Analytics Update: ' + totalViews + ' views for ' + (j.campaign_name || 'campaign'),
      body: 'Campaign: ' + (j.campaign_name || 'N/A') + '\nEarned: $' + earnings.toFixed(2) + '\nViews: ' + totalViews + ' (YT: ' + ytViews + ', TT: ' + ttViews + ')\nTime: ' + new Date().toISOString() + '\n',
    })});
  }
}
return notif;"""

def main():
    token = load_token()
    if not token:
        print('No token found. Run refresh_token.py first.')
        sys.exit(1)

    print('=== Getting workflow details ===')
    # Use get_workflow_version for full node list
    history = rpc_call('get_workflow_history', {'workflowId': WF_ID})
    versions = history.get('versions', [])
    latest_version_id = versions[0]['versionId'] if versions else None
    print(f'Latest version: {latest_version_id}')

    details = rpc_call('get_workflow_version', {'workflowId': WF_ID, 'versionId': latest_version_id})
    nodes = details.get('nodes', [])
    existing_names = [n.get('name', '') for n in nodes]
    print(f'Existing nodes ({len(nodes)}): {existing_names}')

    operations = []
    print('\n=== Building update operations ===')

    # 1. Update Run Clipper Agent: fix URL and add max_download_seconds
    operations.append({
        'type': 'updateNodeParameters',
        'nodeName': 'Run Clipper Agent',
        'parameters': {
            'method': 'POST',
            'url': f'{MIGET_URL}/api/clip',
            'sendBody': True,
            'specifyBody': 'json',
            'jsonBody': '={{ JSON.stringify({ youtube_url: $json.sourceVideoUrl, campaign_id: $json.campaignId, campaign_name: $json.campaignName, num_clips: Number($json.numClips || 3), aspect: "9:16", submit: false, max_download_seconds: 600 }) }}',
            'options': {
                'timeout': 300000,
                'maxTries': 3,
            },
        },
    })

    # 2. Add Wait for Clipper node (if not exists)
    if 'Wait for Clipper' not in existing_names:
        operations.append({
            'type': 'addNode',
            'node': {
                'name': 'Wait for Clipper',
                'type': 'n8n-nodes-base.wait',
                'typeVersion': 2.1,
                'parameters': {
                    'amount': 120,
                    'unit': 'seconds',
                },
                'position': [1896, 416],
            },
        })

    # 3. Add Poll Clipper Status node (if not exists)
    if 'Poll Clipper Status' not in existing_names:
        operations.append({
            'type': 'addNode',
            'node': {
                'name': 'Poll Clipper Status',
                'type': 'n8n-nodes-base.httpRequest',
                'typeVersion': 4.3,
                'parameters': {
                    'method': 'GET',
                    'url': f'={{ $json.job_id ? "{MIGET_URL}/api/status/" + $json.job_id : "" }}',
                    'options': {
                        'timeout': 30000,
                    },
                },
                'position': [2120, 416],
            },
        })

    # 4. Add Check Job Status (If) node (if not exists)
    if 'Check Job Status' not in existing_names:
        operations.append({
            'type': 'addNode',
            'node': {
                'name': 'Check Job Status',
                'type': 'n8n-nodes-base.if',
                'typeVersion': 2.2,
                'parameters': {
                    'conditions': {
                        'options': {
                            'caseSensitive': True,
                            'typeValidation': 'loose',
                        },
                        'conditions': [
                            {
                                'leftValue': '={{ $json.status }}',
                                'operator': {'type': 'string', 'operation': 'equals'},
                                'rightValue': 'completed',
                            }
                        ],
                        'combinator': 'and',
                    },
                },
                'position': [2344, 416],
            },
        })

    # 5. Fix Enhance Clips code for async result format
    operations.append({
        'type': 'updateNodeParameters',
        'nodeName': 'Enhance Clips',
        'parameters': {
            'mode': 'runOnceForAllItems',
            'language': 'javaScript',
            'jsCode': ENHANCE_CLIPS_JS,
        },
    })

    # 6. Update Platform Policy Check URL to Midget
    operations.append({
        'type': 'updateNodeParameters',
        'nodeName': 'Platform Policy Check',
        'parameters': {
            'mode': 'runOnceForAllItems',
            'language': 'javaScript',
            'jsCode': PLATFORM_POLICY_CHECK_JS,
        },
    })

    # 7. Add Upload to TikTok node (via Zernio) if not exists
    if 'Upload to TikTok' not in existing_names:
        operations.append({
            'type': 'addNode',
            'node': {
                'name': 'Upload to TikTok',
                'type': 'n8n-nodes-base.httpRequest',
                'typeVersion': 4.3,
                'parameters': {
                    'method': 'POST',
                    'url': 'https://zernio.com/api/v1/posts',
                    'sendBody': True,
                    'specifyBody': 'json',
                    'jsonBody': '={{ JSON.stringify({ platform: "tiktok", videoUrl: $json.public_video_url, caption: $json.caption, title: $json.hook_title, hashtags: $json.tags, apiKey: "' + ZERONIO_API_KEY + '" }) }}',
                    'options': {
                        'timeout': 300000,
                    },
                },
                'position': [3168, 416],
            },
        })

    # 8. Add Upload to Instagram node (via Zernio) if not exists
    if 'Upload to Instagram' not in existing_names:
        operations.append({
            'type': 'addNode',
            'node': {
                'name': 'Upload to Instagram',
                'type': 'n8n-nodes-base.httpRequest',
                'typeVersion': 4.3,
                'parameters': {
                    'method': 'POST',
                    'url': 'https://zernio.com/api/v1/posts',
                    'sendBody': True,
                    'specifyBody': 'json',
                    'jsonBody': '={{ JSON.stringify({ platform: "instagram", videoUrl: $json.public_video_url, caption: $json.caption, title: $json.hook_title, hashtags: $json.tags, apiKey: "' + ZERONIO_API_KEY + '" }) }}',
                    'options': {
                        'timeout': 300000,
                    },
                },
                'position': [3168, 640],
            },
        })

    # 9. Add TikTok Analytics node (via Composio) if not exists
    if 'TikTok Analytics' not in existing_names:
        operations.append({
            'type': 'addNode',
            'node': {
                'name': 'TikTok Analytics',
                'type': 'n8n-nodes-base.httpRequest',
                'typeVersion': 4.3,
                'parameters': {
                    'method': 'POST',
                    'url': 'https://backend.composio.dev/api/v3.1/tools/execute/TIKTOK_GET_VIDEO_DETAILS',
                    'authentication': 'genericCredentialType',
                    'genericAuthType': 'httpTemplatedCustomAuth',
                    'sendBody': True,
                    'specifyBody': 'json',
                    'jsonBody': '={{ JSON.stringify({ user_id: "default", arguments: { videoId: $json.tiktok_video_id || $json.video_id } }) }}',
                    'options': {
                        'timeout': 60000,
                    },
                },
                'credentials': {
                    'httpTemplatedCustomAuth': {
                        'id': 'vAcBgRthrdgbRaZP',
                        'name': 'Composio API Key (TIFFANY N)',
                    },
                },
                'position': [3392, 416],
            },
        })

    # 10. Add YouTube Analytics via Zernio if not exists
    if 'YouTube Analytics (Zernio)' not in existing_names:
        operations.append({
            'type': 'addNode',
            'node': {
                'name': 'YouTube Analytics (Zernio)',
                'type': 'n8n-nodes-base.httpRequest',
                'typeVersion': 4.3,
                'parameters': {
                    'method': 'GET',
                    'url': 'https://zernio.com/api/v1/analytics/youtube',
                    'sendHeaders': True,
                    'specifyHeaders': 'keypair',
                    'headerParameters': {
                        'parameters': [
                            {'name': 'Authorization', 'value': 'Bearer ' + ZERONIO_API_KEY},
                            {'name': 'Content-Type', 'value': 'application/json'},
                        ],
                    },
                    'queryParameters': {
                        'parameters': [
                            {'name': 'videoId', 'value': '={{ $json.youtube_video_id || $json.data?.id }}'},
                        ],
                    },
                    'options': {
                        'timeout': 60000,
                    },
                },
                'position': [3392, 640],
            },
        })

    # 11. Add Collect Posted Links node if not exists
    if 'Collect Posted Links' not in existing_names:
        operations.append({
            'type': 'addNode',
            'node': {
                'name': 'Collect Posted Links',
                'type': 'n8n-nodes-base.code',
                'typeVersion': 2,
                'parameters': {
                    'mode': 'runOnceForAllItems',
                    'language': 'javaScript',
                    'jsCode': COLLECT_LINKS_JS,
                },
                'position': [2944, 1024],
            },
        })

    # 12. Update Submit to Content Rewards to include posted links
    operations.append({
        'type': 'updateNodeParameters',
        'nodeName': 'Submit to Content Rewards',
        'parameters': {
            'method': 'POST',
            'url': '={{ $json.rewardsUrl || "https://contentrewards.com/api/submit" }}',
            'sendBody': True,
            'specifyBody': 'json',
            'body': {
                'videoLink': '={{ $json[0].youtube_post_url || $json[0].videoLink }}',
                'tiktokLink': '={{ $json[0].tiktok_post_url || "" }}',
                'instagramLink': '={{ $json[0].instagram_post_url || "" }}',
                'accounts': ['youtube', 'tiktok', 'instagram'],
                'campaignId': '={{ $json[0].campaignId }}',
            },
        },
    })

    # 13. Add Payment Notification node if not exists
    if 'Payment Notification' not in existing_names:
        operations.append({
            'type': 'addNode',
            'node': {
                'name': 'Payment Notification',
                'type': 'n8n-nodes-base.code',
                'typeVersion': 2,
                'parameters': {
                    'mode': 'runOnceForAllItems',
                    'language': 'javaScript',
                    'jsCode': PAYMENT_NOTIF_JS,
                },
                'position': [2720, 1280],
            },
        })

    # 14. Add Send Email via Composio node if not exists
    if 'Send Email Notification' not in existing_names:
        operations.append({
            'type': 'addNode',
            'node': {
                'name': 'Send Email Notification',
                'type': 'n8n-nodes-base.httpRequest',
                'typeVersion': 4.3,
                'parameters': {
                    'method': 'POST',
                    'url': 'https://backend.composio.dev/api/v3.1/tools/execute/GMAIL_SEND_EMAIL',
                    'authentication': 'genericCredentialType',
                    'genericAuthType': 'httpTemplatedCustomAuth',
                    'sendBody': True,
                    'specifyBody': 'json',
                    'jsonBody': '={{ JSON.stringify({ user_id: "default", arguments: { to: $json.notify_email, subject: $json.subject, body: $json.body } }) }}',
                    'options': {
                        'timeout': 30000,
                    },
                },
                'credentials': {
                    'httpTemplatedCustomAuth': {
                        'id': 'vAcBgRthrdgbRaZP',
                        'name': 'Composio API Key (TIFFANY N)',
                    },
                },
                'position': [2944, 1280],
            },
        })

    # 15. Add Record Performance node if not exists
    if 'Record Performance' not in existing_names:
        operations.append({
            'type': 'addNode',
            'node': {
                'name': 'Record Performance',
                'type': 'n8n-nodes-base.httpRequest',
                'typeVersion': 4.3,
                'parameters': {
                    'method': 'POST',
                    'url': f'{MIGET_URL}/api/performance',
                    'sendBody': True,
                    'specifyBody': 'json',
                    'jsonBody': '={{ JSON.stringify({ campaign_id: $json.campaign_id, campaign_name: $json.campaign_name, clip_id: $json.clip_id, youtube_views: $json.youtube_views, tiktok_views: $json.tiktok_views, earnings: $json.earnings, cpm: $json.cpm || 0 }) }}',
                    'options': {
                        'timeout': 30000,
                    },
                },
                'position': [2496, 1024],
            },
        })

    # 16. Update Check Threshold to account for TikTok views
    operations.append({
        'type': 'updateNodeParameters',
        'nodeName': 'Check Threshold',
        'parameters': {
            'conditions': {
                'options': {
                    'caseSensitive': True,
                    'typeValidation': 'loose',
                },
                'conditions': [
                    {
                        'leftValue': '={{ Number($json.data?.items?.[0]?.statistics?.viewCount || $json.data?.data?.items?.[0]?.statistics?.viewCount || 0) + Number($json.tiktok_views || 0) }}',
                        'operator': {'type': 'number', 'operation': 'greaterThanOrEqual'},
                        'rightValue': 1000,
                    }
                ],
                'combinator': 'and',
            },
        },
    })

    # Build connection operations
    connections = []

    # 17. Run Clipper Agent -> Wait for Clipper
    connections.append({'type': 'addConnection', 'source': 'Run Clipper Agent', 'target': 'Wait for Clipper'})

    # 18. Wait for Clipper -> Poll Clipper Status
    connections.append({'type': 'addConnection', 'source': 'Wait for Clipper', 'target': 'Poll Clipper Status'})

    # 19. Poll Clipper Status -> Check Job Status
    connections.append({'type': 'addConnection', 'source': 'Poll Clipper Status', 'target': 'Check Job Status'})

    # 20. Check Job Status -> Enhance Clips
    connections.append({'type': 'addConnection', 'source': 'Check Job Status', 'target': 'Enhance Clips'})

    # 21. Upload to YouTube -> Upload to Instagram
    connections.append({'type': 'addConnection', 'source': 'Upload to YouTube', 'target': 'Upload to Instagram'})

    # 22. Upload to Instagram -> Upload to TikTok
    connections.append({'type': 'addConnection', 'source': 'Upload to Instagram', 'target': 'Upload to TikTok'})

    # 23. Upload to TikTok -> TikTok Analytics
    connections.append({'type': 'addConnection', 'source': 'Upload to TikTok', 'target': 'TikTok Analytics'})

    # 24. TikTok Analytics -> YouTube Analytics (Zernio)
    connections.append({'type': 'addConnection', 'source': 'TikTok Analytics', 'target': 'YouTube Analytics (Zernio)'})

    # 25. YouTube Analytics (Zernio) -> Collect Posted Links
    connections.append({'type': 'addConnection', 'source': 'YouTube Analytics (Zernio)', 'target': 'Collect Posted Links'})

    # 26. Collect Posted Links -> Submit to Content Rewards
    connections.append({'type': 'addConnection', 'source': 'Collect Posted Links', 'target': 'Submit to Content Rewards'})

    # 27. Submit to Content Rewards -> Record Performance
    connections.append({'type': 'addConnection', 'source': 'Submit to Content Rewards', 'target': 'Record Performance'})

    # 28. Record Performance -> Payment Notification
    connections.append({'type': 'addConnection', 'source': 'Record Performance', 'target': 'Payment Notification'})

    # 29. Payment Notification -> Send Email Notification
    connections.append({'type': 'addConnection', 'source': 'Payment Notification', 'target': 'Send Email Notification'})

    all_operations = operations + connections

    print(f'\n=== Applying {len(all_operations)} operations to workflow ===')
    result = rpc_call('update_workflow', {
        'workflowId': WF_ID,
        'operations': all_operations,
    })
    print(f'Result: {json.dumps(result, indent=2)[:2000]}')

    print('\n=== Publishing workflow ===')
    publish_result = rpc_call('publish_workflow', {'workflowId': WF_ID})
    print(f'Publish: {json.dumps(publish_result, indent=2)[:500]}')

    print('\n=== Done ===')

if __name__ == '__main__':
    main()
