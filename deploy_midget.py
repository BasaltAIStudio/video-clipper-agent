import urllib.request, json, ssl, time

base = 'https://app.miget.com/api/v1'
key = 'miget_live_6bfd938522c4a507d5090deae1eb7c32e9483392934af34a2c12eb7d66c4a1ea'
app_uuid = '01a17d44-8636-7d9c-86d5-7a72d85db5e6'
headers = {'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'}

def api(path, data=None, method='GET'):
    url = f'{base}/{path}'
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        raw = resp.read()
        if raw:
            return resp.getcode(), json.loads(raw)
        return resp.getcode(), {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode()[:500]
        return e.code, raw
    except Exception as e:
        return 0, str(e)

# Check deployment status
print('=== Deployment Status ===')
code, deps = api(f'apps/{app_uuid}/deployments')
if isinstance(deps, list):
    for d in deps:
        print(f'  UUID: {d.get("uuid")}')
        print(f'  State: {d.get("state")}')
        print(f'  Commit: {d.get("commit_sha", "")[:12]}')
        print(f'  Message: {d.get("commit_message", "")}')
        print(f'  Created: {d.get("created_at")}')
        print(f'  Updated: {d.get("updated_at")}')
        print()

# Get latest deployment details
if isinstance(deps, list) and len(deps) > 0:
    dep_id = deps[0].get('uuid')
    print(f'\n=== Deployment Logs (uuid: {dep_id}) ===')
    code, logs = api(f'apps/{app_uuid}/deployments/{dep_id}/logs')
    if isinstance(logs, dict):
        print(json.dumps(logs, indent=2)[:3000])
    elif isinstance(logs, str):
        print(logs[:2000])
    else:
        print(f'Code: {code}, Type: {type(logs)}')

# Check app state
print(f'\n=== App State ===')
code, app = api(f'apps/{app_uuid}')
if isinstance(app, dict):
    print(f'Name: {app.get("name")}')
    print(f'State: {app.get("state")}')
    print(f'Public URL: {app.get("public_url")}')
    print(f'Maintenance: {app.get("maintenance")}')
