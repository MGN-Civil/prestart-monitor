"""Prestart Monitor backend (AWS Lambda, Python 3.12, no external dependencies).

Routes (API Gateway HTTP API, payload v2):
  POST /webhook/{token}      Gearbox webhook receiver (no JWT; secret path token + HMAC signature)
  GET  /prestarts            all stored prestarts + vehicles            (JWT: Microsoft via Cognito)
  POST /prestarts/import     upsert records from a Gearbox Excel export (JWT)
  POST /vehicles/sync        pull the vehicle list from the Gearbox API (JWT)
Scheduled (EventBridge): {"task": "sync_vehicles"} once a day.

Secrets Manager secret (JSON), name in SECRET_ID:
  client_id, client_secret, business_token   Gearbox API (Settings -> Integrations)
  webhook_signing_key                        Gearbox webhook signing key(s), comma separated
  webhook_path_token                         random token that forms part of the webhook URL
"""
import base64, datetime as dt, hashlib, hmac, json, os, re, time, urllib.parse, urllib.request
from decimal import Decimal

import boto3

TABLE = os.environ.get('TABLE_NAME', 'PrestartMonitor')
SECRET_ID = os.environ.get('SECRET_ID', 'prestart-monitor/gearbox')
GEARBOX = os.environ.get('GEARBOX_BASE', 'https://api.gearbox.com.au')
ALLOWED_DOMAIN = os.environ.get('ALLOWED_EMAIL_DOMAIN', '').lower()

ddb = boto3.resource('dynamodb')
table = ddb.Table(TABLE)
secrets = boto3.client('secretsmanager')
_secret_cache = {'t': 0, 'v': None}

# Fields a record may carry (same shape the web page uses)
FIELDS = {'id', 'gid', 'group', 'subgroup', 'fleet', 'rego', 'desc', 'date', 'completedAt', 'employee', 'empNo',
          'km', 'hours', 'type', 'completed', 'timeTaken', 'gearbox', 'notes', 'fails', 'source'}


# ---------------------------------------------------------------- helpers
def secret():
    if _secret_cache['v'] is None or time.time() - _secret_cache['t'] > 300:
        _secret_cache['v'] = json.loads(secrets.get_secret_value(SecretId=SECRET_ID)['SecretString'])
        _secret_cache['t'] = time.time()
    return _secret_cache['v']


def resp(status, body=None, headers=None):
    h = {'content-type': 'application/json', 'cache-control': 'no-store'}
    h.update(headers or {})
    return {'statusCode': status, 'headers': h, 'body': json.dumps(body, default=_json_default) if body is not None else ''}


def _json_default(o):
    if isinstance(o, Decimal):
        return int(o) if o == o.to_integral_value() else float(o)
    if isinstance(o, set):
        return list(o)
    raise TypeError(type(o))


def to_ddb(v):
    if isinstance(v, float):
        return Decimal(str(v))
    if isinstance(v, dict):
        return {k: to_ddb(x) for k, x in v.items()}
    if isinstance(v, list):
        return [to_ddb(x) for x in v]
    return v


def body_of(event):
    b = event.get('body') or ''
    if event.get('isBase64Encoded'):
        b = base64.b64decode(b).decode('utf-8')
    return b


def upsert(rec):
    """Merge fields into the item with this id (fields not supplied are kept)."""
    rec = {k: v for k, v in rec.items() if k in FIELDS and v is not None and v != ''}
    rid = str(rec.pop('id'))
    rec['updatedAt'] = dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')
    names, values, sets = {}, {':kind': 'prestart'}, ['#kind = :kind']
    names['#kind'] = 'kind'
    for i, (k, v) in enumerate(rec.items()):
        names[f'#f{i}'] = k
        values[f':v{i}'] = to_ddb(v)
        sets.append(f'#f{i} = :v{i}')
    table.update_item(Key={'id': rid}, UpdateExpression='SET ' + ', '.join(sets),
                      ExpressionAttributeNames=names, ExpressionAttributeValues=values)


def scan_all():
    items, kw = [], {}
    while True:
        r = table.scan(**kw)
        items += r.get('Items', [])
        if 'LastEvaluatedKey' not in r:
            return items
        kw['ExclusiveStartKey'] = r['LastEvaluatedKey']


# ---------------------------------------------------------------- webhook
def ruby_inspect(o, arrow):
    """Ruby's Hash#inspect for JSON-parsed data. Gearbox signs "timestamp:#{JSON.parse(body)}"."""
    if isinstance(o, dict):
        return '{' + ', '.join(f'{ruby_inspect(k, arrow)}{arrow}{ruby_inspect(v, arrow)}' for k, v in o.items()) + '}'
    if isinstance(o, list):
        return '[' + ', '.join(ruby_inspect(x, arrow) for x in o) + ']'
    if o is None:
        return 'nil'
    if o is True:
        return 'true'
    if o is False:
        return 'false'
    if isinstance(o, str):
        s = o.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n').replace('\t', '\\t').replace('\r', '\\r').replace('#{', '\\#{')
        return '"' + s + '"'
    if isinstance(o, float):
        return repr(o)
    return str(o)


def signature_ok(headers, raw, keys):
    sigs = [s.strip() for s in (headers.get('x-gearbox-signature') or '').split(',') if s.strip()]
    ts = headers.get('x-gearbox-request-timestamp') or ''
    if not sigs or not keys:
        return False
    try:
        parsed = json.loads(raw)
    except Exception:
        parsed = None
    payloads = [raw]
    if parsed is not None:
        payloads += [ruby_inspect(parsed, '=>'), ruby_inspect(parsed, ' => '),
                     json.dumps(parsed, separators=(',', ':'), ensure_ascii=False)]
    for key in keys:
        for p in payloads:
            for base in (f'{ts}:{p}', p):
                calc = 'sha256=' + hmac.new(key.encode(), base.encode(), hashlib.sha256).hexdigest()
                if any(hmac.compare_digest(calc, s) for s in sigs):
                    return True
    return False


def timestamp_fresh(headers, max_age=900):
    ts = headers.get('x-gearbox-request-timestamp')
    if not ts:
        return False
    try:
        if re.fullmatch(r'\d{9,11}', ts):
            t = int(ts)
        else:
            t = dt.datetime.fromisoformat(ts.replace('Z', '+00:00').replace(' UTC', '+00:00')).timestamp()
        return abs(time.time() - t) <= max_age
    except Exception:
        return True  # unknown format: rely on signature + secret path


def prestart_from_webhook(p):
    qs = p.get('questions') or []
    fails = [f"{q.get('question', '')}: {q.get('fail_reason') or q.get('notes') or ''}".strip(': ')
             for q in qs if str(q.get('status', '')).lower() not in ('passed', 'pass', 'n/a', 'na', '')]
    completed_at = p.get('completed_at') or p.get('created_at') or ''
    return {
        'id': str(p.get('prestart_number') or p.get('id')),
        'gid': p.get('id'),
        'fleet': p.get('fleet_number'),
        'rego': p.get('registration'),
        'employee': p.get('completed_by'),
        'completedAt': completed_at,
        'date': completed_at[:10] if re.match(r'\d{4}-\d{2}-\d{2}', completed_at) else None,
        'km': p.get('mileage', p.get('odometer')),
        'hours': p.get('hours'),
        'completed': True,
        'type': 'Daily',
        'fails': fails,
        'notes': ' | '.join(x for x in (p.get('spare1'), p.get('spare2'), p.get('spare3')) if x),
        'source': 'webhook',
    }


def log_webhook(headers, raw, outcome, name):
    """Keep the last deliveries (7 days) for troubleshooting: DynamoDB items kind=webhooklog."""
    print(json.dumps({'msg': 'webhook', 'event': name, 'outcome': outcome, 'bytes': len(raw or ''),
                      'headers': sorted(h for h in headers if h.startswith('x-') or h in ('user-agent', 'content-type'))}))
    try:
        now = dt.datetime.now(dt.timezone.utc)
        table.put_item(Item={'id': f"webhooklog#{now.isoformat(timespec='milliseconds')}#{time.time_ns() % 1000000}", 'kind': 'webhooklog',
                             'event': name or '', 'outcome': outcome, 'body': (raw or '')[:20000],
                             'headers': {k: str(v)[:300] for k, v in headers.items()
                                         if k.startswith('x-gearbox') or k in ('user-agent', 'content-type')},
                             'ttl': int(now.timestamp()) + 7 * 86400})
    except Exception as e:
        print(json.dumps({'msg': 'webhook log failed', 'error': str(e)[:200]}))


def find_prestart(payload):
    """Find the prestart object. Gearbox actually sends {"event_name", "model", "attributes": {...prestart...}};
    also accept it at the top level or under any other wrapper key."""
    if not isinstance(payload, dict):
        return {}
    if 'fleet_number' in payload or 'prestart_number' in payload:
        return payload
    for k in ('attributes', 'data', 'prestart', 'payload', 'object', 'resource'):
        if isinstance(payload.get(k), dict):
            found = find_prestart(payload[k])
            if found:
                return found
    for v in payload.values():
        if isinstance(v, dict):
            found = find_prestart(v)
            if found:
                return found
    return {}


def handle_webhook(event):
    headers = {k.lower(): v for k, v in (event.get('headers') or {}).items()}
    sec = secret()
    token = (event.get('pathParameters') or {}).get('token', '')
    raw = body_of(event)
    if not sec.get('webhook_path_token') or not hmac.compare_digest(token, sec['webhook_path_token']):
        print(json.dumps({'msg': 'webhook wrong path token', 'token_len': len(token), 'ua': headers.get('user-agent', '')[:60]}))
        return resp(404, {'error': 'not found'})
    try:
        payload = json.loads(raw or '{}')
    except Exception:
        log_webhook(headers, raw, 'invalid json', '')
        return resp(400, {'error': 'invalid json'})
    name = str(headers.get('x-gearbox-event') or payload.get('event_name') or payload.get('event') or payload.get('type') or '')
    if name == 'url_verification':
        log_webhook(headers, raw, 'verification answered', name)
        sig = (headers.get('x-gearbox-signature') or '').split(',')[0].strip()
        return resp(200, {'challenge': sig})
    strict = os.environ.get('REQUIRE_SIGNATURE', 'false') == 'true'
    keys = [k.strip() for k in str(sec.get('webhook_signing_key', '')).split(',') if k.strip()]
    verified = signature_ok(headers, raw, keys)
    if strict and not timestamp_fresh(headers):
        log_webhook(headers, raw, 'rejected: stale timestamp', name)
        return resp(401, {'error': 'stale'})
    if strict and keys and not verified:
        log_webhook(headers, raw, 'rejected: bad signature', name)
        return resp(401, {'error': 'bad signature'})
    data = find_prestart(payload)
    looks_like_prestart = 'prestart' in name.lower() or 'prestart_number' in data or ('fleet_number' in data and 'questions' in data)
    if not looks_like_prestart:
        log_webhook(headers, raw, 'ignored (not a prestart event)', name)
        return resp(200, {'ignored': name})
    rec = prestart_from_webhook(data)
    if not rec['id'] or rec['id'] == 'None' or not rec['fleet']:
        log_webhook(headers, raw, 'rejected: no prestart id/fleet', name)
        return resp(400, {'error': 'missing id or fleet_number'})
    upsert(rec)
    log_webhook(headers, raw, f"stored {rec['id']} ({'signature ok' if verified else 'signature NOT verified'})", name)
    print(json.dumps({'msg': 'stored', 'event': name, 'id': rec['id'], 'fleet': rec['fleet'], 'verified': verified}))
    return resp(200, {'ok': True, 'verified': verified})


# ---------------------------------------------------------------- Gearbox API (vehicles)
def gearbox_token():
    s = secret()
    data = urllib.parse.urlencode({'grant_type': 'client_credentials', 'client_id': s['client_id'],
                                   'client_secret': s['client_secret'], 'business_token': s.get('business_token', '')}).encode()
    req = urllib.request.Request(GEARBOX + '/oauth/token', data=data, method='POST',
                                 headers={'Content-Type': 'application/x-www-form-urlencoded', 'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())['access_token']


def gearbox_get_all(path, key, token):
    url, out = GEARBOX + path, []
    for _ in range(200):
        req = urllib.request.Request(url, headers={'Authorization': f'Bearer {token}', 'Accept': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                body = json.loads(r.read())
                link = r.headers.get('Link') or ''
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(int(e.headers.get('Retry-After') or 15))
                continue
            raise
        out += body.get(key, []) if isinstance(body, dict) else body
        m = re.search(r'<([^>]+)>;\s*rel="next"', link)
        if not m:
            break
        url = m.group(1)
        time.sleep(0.8)  # stay well under 20 requests / 15 s
    return out


def sync_vehicles():
    token = gearbox_token()
    vehicles = gearbox_get_all('/public/v1/vehicles', 'vehicles', token)
    n = 0
    with table.batch_writer(overwrite_by_pkeys=['id']) as bw:
        for v in vehicles:
            fl = v.get('fleet_number')
            if not fl:
                continue
            desc = ' '.join(x for x in (v.get('make'), v.get('model'), v.get('type')) if x)
            bw.put_item(Item=to_ddb({k: x for k, x in {
                'id': 'vehicle#' + fl, 'kind': 'vehicle', 'fleet': fl, 'rego': v.get('registration'), 'desc': desc,
                'group': v.get('group'), 'subgroup': v.get('sub_group'), 'vtype': v.get('type'),
                'make': v.get('make'), 'model': v.get('model'), 'sold': bool(v.get('sold')),
                'updatedAt': dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')}.items() if x not in (None, '')}))
            n += 1
    print(json.dumps({'msg': 'vehicles synced', 'count': n}))
    return n


# ---------------------------------------------------------------- vehicle filter
# Only prestarts for vehicles matching these Gearbox vehicle attributes are returned by GET /prestarts.
# Env vars (comma-separated, case-insensitive; empty = no restriction):
#   FILTER_SUBGROUPS  e.g. "Geraldton"      (Gearbox vehicle sub_group)
#   FILTER_TYPES      e.g. "Prime Mover"    (Gearbox vehicle type)
#   FILTER_GROUPS     e.g. "MGN Civil"      (Gearbox vehicle group)
def _norm(s):
    return re.sub(r'\s+', ' ', str(s or '')).strip().lower()


def vehicle_filter():
    f = {}
    for env, field in (('FILTER_SUBGROUPS', 'subgroup'), ('FILTER_TYPES', 'vtype'), ('FILTER_GROUPS', 'group')):
        vals = {_norm(x) for x in os.environ.get(env, '').split(',') if x.strip()}
        if vals:
            f[field] = vals
    return f


def vehicle_matches(v, flt):
    return all(_norm(v.get(field)) in vals for field, vals in flt.items())


# ---------------------------------------------------------------- authenticated API
def claims(event):
    return (((event.get('requestContext') or {}).get('authorizer') or {}).get('jwt') or {}).get('claims') or {}


def handle_api(event, route):
    c = claims(event)
    email = (c.get('email') or c.get('username') or '').lower()
    if ALLOWED_DOMAIN and not email.endswith('@' + ALLOWED_DOMAIN):
        return resp(403, {'error': 'forbidden'})
    if route == 'GET /prestarts':
        items = scan_all()
        vehicles = [{k: v for k, v in i.items() if k in ('fleet', 'rego', 'desc', 'group', 'subgroup', 'vtype', 'sold')}
                    for i in items if i.get('kind') == 'vehicle']
        recs = [{k: v for k, v in i.items() if k not in ('kind', 'updatedAt')} for i in items if i.get('kind') == 'prestart']
        flt = vehicle_filter()
        if flt:
            vehicles = [v for v in vehicles if vehicle_matches(v, flt)]
            allowed = {v['fleet'] for v in vehicles}
            recs = [r for r in recs if r.get('fleet') in allowed]
        latest = max((i.get('updatedAt', '') for i in items if i.get('kind') == 'prestart'), default='')
        vsync = max((i.get('updatedAt', '') for i in items if i.get('kind') == 'vehicle'), default='')
        return resp(200, {'records': recs, 'vehicles': vehicles, 'updatedAt': latest, 'vehiclesUpdatedAt': vsync, 'user': email,
                          'filter': {k: sorted(v) for k, v in flt.items()}})
    if route == 'POST /prestarts/import':
        try:
            recs = json.loads(body_of(event) or '{}').get('records', [])
        except Exception:
            return resp(400, {'error': 'invalid json'})
        if len(recs) > 1000:
            return resp(413, {'error': 'max 1000 records per request'})
        n = 0
        for r in recs:
            if isinstance(r, dict) and r.get('id') and r.get('fleet') and re.fullmatch(r'\d{4}-\d{2}-\d{2}', str(r.get('date', ''))):
                r = {**r, 'source': 'import'}
                upsert(r)
                n += 1
        print(json.dumps({'msg': 'imported', 'count': n, 'by': email}))
        return resp(200, {'imported': n})
    if route == 'POST /vehicles/sync':
        try:
            return resp(200, {'vehicles': sync_vehicles()})
        except Exception as e:
            print(json.dumps({'msg': 'vehicle sync failed', 'error': str(e)}))
            return resp(502, {'error': 'Gearbox vehicle sync failed: ' + str(e)[:200]})
    return resp(404, {'error': 'not found'})


def lambda_handler(event, context):
    if event.get('task') == 'sync_vehicles':
        return {'vehicles': sync_vehicles()}
    route = event.get('routeKey', '')
    if route.startswith('POST /webhook/'):
        return handle_webhook(event)
    return handle_api(event, route)
