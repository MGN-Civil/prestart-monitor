"""Departure alerts and the daily report for Prestart Monitor.

Truck movements come from the Geraldton opsboard (PACCAR / e.things geofence events), forwarded to
POST /movements. When a truck *exits* one of the configured base geofences, a "departure" is recorded;
the 5-minute tick then checks (after a grace period) whether that truck had a completed prestart that
WA day before it left. If not, the supervisors are emailed (at most once per truck per day).

The tick also sends the daily report at the configured Brisbane time.

All settings live in the DynamoDB item  id = "settings#notify"  and are edited from the app.
"""
import datetime as dt
import html
import json
import os
import re

import boto3

BNE = dt.timezone(dt.timedelta(hours=10), 'AEST')   # Brisbane, no DST
AWST = dt.timezone(dt.timedelta(hours=8), 'AWST')   # Geraldton, no DST
DOW = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']

MAIL_FROM = os.environ.get('MAIL_FROM', 'Prestart Monitor <noreply@mgncivil.com.au>')
APP_URL = os.environ.get('APP_URL', 'https://prestart.mgncivil.com.au/')
MAX_KM_PER_DAY = float(os.environ.get('MAX_KM_PER_DAY', '1200'))
MAX_HOURS_PER_DAY = float(os.environ.get('MAX_HOURS_PER_DAY', '24'))
MIN_KM = float(os.environ.get('MIN_KM', '10'))
MIN_HOURS = float(os.environ.get('MIN_HOURS', '10'))

DEFAULTS = {
    'alertsEnabled': True,
    'supervisorEmails': [],
    'baseGeofences': [],
    'graceMinutes': 15,
    'reportEnabled': True,
    'reportEmails': [],
    'reportTime': '10:00',               # Brisbane time
    'reportDays': [1, 1, 1, 1, 1, 1, 0],  # Mon..Sun
    'updatedBy': '',
    'updatedAt': '',
}
EMAIL_RE = re.compile(r'^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$')

_ses = None


def ses():
    global _ses
    if _ses is None:
        _ses = boto3.client('sesv2')
    return _ses


# ---------------------------------------------------------------- small helpers
def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def parse_iso(s):
    if not s:
        return None
    try:
        d = dt.datetime.fromisoformat(str(s).replace('Z', '+00:00'))
        return d if d.tzinfo else d.replace(tzinfo=BNE)
    except Exception:
        return None


def num(v):
    try:
        return float(v)
    except Exception:
        return None


def norm_fleet(s):
    return re.sub(r'\s+', '', str(s or '')).lower()


def wa_date(rec):
    """WA (Geraldton) date of a prestart: from completedAt when known, else the stored date."""
    d = parse_iso(rec.get('completedAt'))
    return d.astimezone(AWST).date().isoformat() if d else str(rec.get('date') or '')


def wa_time(d):
    if not d:
        return ''
    w = d.astimezone(AWST)
    return w.strftime('%I:%M %p').lstrip('0').lower()


def fmt_date(iso):
    try:
        d = dt.date.fromisoformat(iso)
        return f"{DOW[d.weekday()]} {d.day} {d.strftime('%b')}"
    except Exception:
        return iso


def esc(s):
    return html.escape(str(s if s is not None else ''))


def clean_emails(v):
    if isinstance(v, str):
        v = re.split(r'[,;\s]+', v)
    out = []
    for e in v or []:
        e = str(e).strip().lower()
        if e and EMAIL_RE.match(e) and e not in out:
            out.append(e)
    return out[:50]


# ---------------------------------------------------------------- settings
def get_settings(table):
    item = table.get_item(Key={'id': 'settings#notify'}).get('Item') or {}
    s = {k: item.get(k, v) for k, v in DEFAULTS.items()}
    s['graceMinutes'] = int(num(s['graceMinutes']) or 0)
    s['reportDays'] = [1 if int(num(x) or 0) else 0 for x in (list(s['reportDays']) + [0] * 7)[:7]]
    return s


def save_settings(table, body, email):
    cur = get_settings(table)
    s = dict(cur)
    if 'alertsEnabled' in body:
        s['alertsEnabled'] = bool(body['alertsEnabled'])
    if 'reportEnabled' in body:
        s['reportEnabled'] = bool(body['reportEnabled'])
    if 'supervisorEmails' in body:
        s['supervisorEmails'] = clean_emails(body['supervisorEmails'])
    if 'reportEmails' in body:
        s['reportEmails'] = clean_emails(body['reportEmails'])
    if 'baseGeofences' in body:
        g = body['baseGeofences']
        if isinstance(g, str):
            g = g.split(',')
        s['baseGeofences'] = [str(x).strip()[:120] for x in g if str(x).strip()][:20]
    if 'graceMinutes' in body:
        s['graceMinutes'] = max(0, min(240, int(num(body['graceMinutes']) or 0)))
    if 'reportTime' in body:
        t = str(body['reportTime'])
        if not re.fullmatch(r'([01]\d|2[0-3]):[0-5]\d', t):
            raise ValueError('reportTime must be HH:MM (24 h)')
        s['reportTime'] = t
    if 'reportDays' in body:
        d = list(body['reportDays'])
        if len(d) != 7:
            raise ValueError('reportDays must have 7 entries (Mon..Sun)')
        s['reportDays'] = [1 if x else 0 for x in d]
    s['updatedBy'] = email
    s['updatedAt'] = now_utc().isoformat(timespec='seconds')
    table.put_item(Item={'id': 'settings#notify', 'kind': 'settings', **s})
    print(json.dumps({'msg': 'notify settings saved', 'by': email}))
    return s


# ---------------------------------------------------------------- movements / departures
def is_base(geofence, settings):
    g = (geofence or '').strip().lower()
    return bool(g) and any(g == b.strip().lower() for b in settings['baseGeofences'])


def ingest_movement(table, ev):
    """Store a forwarded geofence event. Returns a short description of what was done."""
    fleet = str(ev.get('label') or ev.get('fleet') or ev.get('thingId') or '').strip()
    direction = str(ev.get('direction') or '').lower()
    geofence = str(ev.get('geofence') or '').strip()
    at = None
    if ev.get('epoch') is not None:
        try:
            at = dt.datetime.fromtimestamp(float(ev['epoch']), dt.timezone.utc)
        except Exception:
            at = None
    at = at or parse_iso(ev.get('whenIso')) or parse_iso(ev.get('receivedAt')) or now_utc()
    if not fleet or direction not in ('enter', 'exit'):
        return {'stored': False, 'reason': 'need label/fleet and direction enter|exit'}
    at_iso = at.astimezone(dt.timezone.utc).isoformat(timespec='seconds')
    ttl90 = int(now_utc().timestamp()) + 90 * 86400
    table.put_item(Item={'id': f'movement#{at_iso}#{norm_fleet(fleet)}#{direction}', 'kind': 'movement',
                         'fleet': fleet, 'direction': direction, 'geofence': geofence,
                         'group': str(ev.get('group') or '')[:120], 'at': at_iso,
                         'where': str(ev.get('where') or '')[:200], 'ttl': ttl90})
    settings = get_settings(table)
    out = {'stored': True, 'fleet': fleet, 'direction': direction, 'geofence': geofence, 'at': at_iso}
    if direction == 'exit' and is_base(geofence, settings):
        check_after = at + dt.timedelta(minutes=settings['graceMinutes'])
        table.put_item(Item={'id': f'departure#{norm_fleet(fleet)}#{at_iso}', 'kind': 'departure',
                             'fleet': fleet, 'geofence': geofence, 'at': at_iso,
                             'waDate': at.astimezone(AWST).date().isoformat(),
                             'checkAfter': check_after.isoformat(timespec='seconds'),
                             'status': 'pending', 'ttl': int(now_utc().timestamp()) + 180 * 86400})
        out['departure'] = True
    return out


def prestart_before(prestarts, fleet, wa_day, deadline):
    """A completed prestart for this fleet on this WA day, completed no later than deadline
    (imported prestarts have no time; they count as done for that day)."""
    best = None
    for r in prestarts:
        if norm_fleet(r.get('fleet')) != norm_fleet(fleet) or r.get('completed') is False:
            continue
        if wa_date(r) != wa_day:
            continue
        t = parse_iso(r.get('completedAt'))
        if t is None or t <= deadline:
            if best is None or (t and parse_iso(best.get('completedAt')) and t > parse_iso(best.get('completedAt'))):
                best = r
    return best


def process_departures(table, items, settings, now=None):
    now = now or now_utc()
    prestarts = [i for i in items if i.get('kind') == 'prestart']
    deps = sorted([i for i in items if i.get('kind') == 'departure'], key=lambda d: d['at'])
    done = []
    for d in deps:
        if d.get('demo') or d.get('status') != 'pending' or parse_iso(d.get('checkAfter')) > now:
            continue
        at = parse_iso(d['at'])
        p = prestart_before(prestarts, d['fleet'], d['waDate'], at + dt.timedelta(minutes=settings['graceMinutes']))
        upd = {'checkedAt': now.isoformat(timespec='seconds')}
        if p:
            upd.update(status='ok', prestartId=str(p.get('id')))
        elif any(x is not d and not x.get('demo') and norm_fleet(x['fleet']) == norm_fleet(d['fleet']) and x.get('waDate') == d['waDate']
                 and x.get('status') in ('alerted', 'no-recipients', 'disabled') for x in deps):
            upd.update(status='repeat')                       # already alerted for this truck today
        elif not settings['alertsEnabled']:
            upd.update(status='disabled')
        elif not settings['supervisorEmails']:
            upd.update(status='no-recipients')
        else:
            last = latest_prestart(prestarts, d['fleet'])
            send_alert(settings['supervisorEmails'], d, last)
            upd.update(status='alerted', alertedTo=', '.join(settings['supervisorEmails']))
        d.update(upd)
        names = {f'#k{i}': k for i, k in enumerate(upd)}
        vals = {f':v{i}': v for i, v in enumerate(upd.values())}
        table.update_item(Key={'id': d['id']}, UpdateExpression='SET ' + ', '.join(f'#k{i} = :v{i}' for i in range(len(upd))),
                          ExpressionAttributeNames=names, ExpressionAttributeValues=vals)
        print(json.dumps({'msg': 'departure checked', 'fleet': d['fleet'], 'at': d['at'], 'status': upd['status']}))
        done.append(d)
    return done


def latest_prestart(prestarts, fleet):
    rs = [r for r in prestarts if norm_fleet(r.get('fleet')) == norm_fleet(fleet) and r.get('completed') is not False]
    rs.sort(key=lambda r: (wa_date(r), str(r.get('completedAt') or '')))
    return rs[-1] if rs else None


# ---------------------------------------------------------------- email
def send_email(to, subject, html_body, text_body):
    to = clean_emails(to)
    if not to:
        return None
    r = ses().send_email(FromEmailAddress=MAIL_FROM, Destination={'ToAddresses': to},
                         Content={'Simple': {'Subject': {'Data': subject, 'Charset': 'UTF-8'},
                                             'Body': {'Html': {'Data': html_body, 'Charset': 'UTF-8'},
                                                      'Text': {'Data': text_body, 'Charset': 'UTF-8'}}}})
    print(json.dumps({'msg': 'email sent', 'to': to, 'subject': subject, 'id': r.get('MessageId')}))
    return r.get('MessageId')


STYLE_TD = 'padding:6px 10px;border-bottom:1px solid #e4e3df;font-size:13px;vertical-align:top'
STYLE_TH = 'padding:6px 10px;border-bottom:2px solid #c4c3bd;font-size:12px;text-align:left;color:#52514e'


def wrap_html(title, inner):
    return f"""<!doctype html><html><body style="margin:0;background:#f4f4f2;font-family:Segoe UI,Arial,sans-serif;color:#0b0b0b">
<div style="max-width:760px;margin:0 auto;padding:20px">
<div style="background:#fff;border:1px solid #dddcd7;border-radius:10px;padding:20px 22px">
<div style="font-size:12px;color:#7a7974;margin-bottom:4px">Prestart Monitor</div>
<h1 style="font-size:19px;margin:0 0 14px">{esc(title)}</h1>
{inner}
<p style="margin:18px 0 0"><a href="{esc(APP_URL)}" style="background:#2a78d6;color:#fff;text-decoration:none;padding:8px 14px;border-radius:7px;font-size:13px;display:inline-block">Open Prestart Monitor</a></p>
</div>
<p style="font-size:11px;color:#7a7974;margin:10px 4px">Times are Geraldton (WA) time. Change recipients and schedule in Prestart Monitor → Alerts &amp; reports.</p>
</div></body></html>"""


def send_alert(to, dep, last):
    at = parse_iso(dep['at'])
    when = f"{wa_time(at)} WA, {fmt_date(dep['waDate'])}"
    subject = f"⚠ {dep['fleet']} left {dep['geofence']} without a prestart ({wa_time(at)} WA)"
    last_txt = 'none on record'
    if last:
        lt = parse_iso(last.get('completedAt'))
        last_txt = f"{fmt_date(wa_date(last))}{' ' + wa_time(lt) if lt else ''} by {last.get('employee') or 'unknown'} (#{last.get('id')})"
    rows = [('Truck', dep['fleet']), ('Left', dep['geofence']), ('Time', when), ('Last prestart', last_txt)]
    inner = ('<p style="margin:0 0 12px;font-size:14px">This truck left base and no completed prestart was recorded for it today.</p>'
             '<table style="border-collapse:collapse;width:100%">' +
             ''.join(f'<tr><td style="{STYLE_TD};color:#52514e;width:130px">{esc(k)}</td><td style="{STYLE_TD}"><b>{esc(v)}</b></td></tr>' for k, v in rows) +
             '</table>')
    text = (f"{dep['fleet']} left {dep['geofence']} at {when} and no completed prestart was recorded for it today.\n"
            f"Last prestart: {last_txt}\n\n{APP_URL}")
    return send_email(to, subject, wrap_html(f"{dep['fleet']} left base without a prestart", inner), text)


# ---------------------------------------------------------------- daily report
def check_sequence(valid, field, max_per_day, label, unit):
    """Python port of the page's sequence check (same rules, default limits)."""
    flags = {}

    def ok(a, b):
        d = max(1, (dt.date.fromisoformat(b['_d']) - dt.date.fromisoformat(a['_d'])).days)
        return b[field] >= a[field] and b[field] - a[field] <= max_per_day * d

    ref = None
    for i, r in enumerate(valid):
        nx = valid[i + 1] if i + 1 < len(valid) else None
        nx2 = valid[i + 2] if i + 2 < len(valid) else None
        if ref is None:
            if nx and not ok(r, nx) and nx2 and ok(nx, nx2):
                flags[r['id']] = f"{label} {r[field]:,.0f} doesn't fit the following readings"
                continue
            ref = r
            continue
        if ok(ref, r):
            ref = r
            continue
        diff = r[field] - ref[field]
        msg = (f"{label} lower than previous ({ref[field]:,.0f} → {r[field]:,.0f} {unit})" if diff < 0
               else f"{label} jump too large (+{diff:,.0f} {unit} since {fmt_date(ref['_d'])})")
        if nx and not ok(ref, nx) and ok(r, nx):
            ref = r
        flags[r['id']] = msg
    return flags


def build_report(items, allowed_fleets, now=None, work_days=None):
    now = now or now_utc()
    today = now.astimezone(AWST).date()
    # previous working day (per the report-day setting), e.g. Saturday on a Monday
    work_days = work_days or DEFAULTS['reportDays']
    yday = today - dt.timedelta(days=1)
    for _ in range(6):
        if work_days[yday.weekday()]:
            break
        yday -= dt.timedelta(days=1)
    prev_label = 'Yesterday' if yday == today - dt.timedelta(days=1) else 'Last working day'
    t_iso, y_iso = today.isoformat(), yday.isoformat()
    prestarts = []
    for r in items:
        if r.get('kind') != 'prestart':
            continue
        if allowed_fleets is not None and norm_fleet(r.get('fleet')) not in allowed_fleets:
            continue
        r = dict(r)
        r['_d'] = wa_date(r)
        r['_t'] = parse_iso(r.get('completedAt'))
        r['km'] = num(r.get('km'))
        r['hours'] = num(r.get('hours'))
        prestarts.append(r)
    # trucks in scope: the filtered vehicle list when there is one, else every fleet with prestarts
    names = {}
    for r in prestarts:
        names.setdefault(norm_fleet(r.get('fleet')), r.get('fleet'))
    keys = allowed_fleets if allowed_fleets is not None else set(names)
    fleets = sorted((names.get(k) or k.upper() for k in keys if k), key=lambda s: (len(s), s.lower()))

    # km / hours issues (full history for context, report only yesterday + today)
    issues = []
    for fl in fleets:
        rs = sorted([r for r in prestarts if norm_fleet(r['fleet']) == norm_fleet(fl) and r.get('completed') is not False],
                    key=lambda r: (r['_d'], r['_t'].isoformat() if r['_t'] else '', str(r['id'])))
        kmv = [r for r in rs if r['km'] is not None and r['km'] > MIN_KM]
        hv = [r for r in rs if r['hours'] is not None and r['hours'] > MIN_HOURS]
        kf = check_sequence(kmv, 'km', MAX_KM_PER_DAY, 'Km', 'km')
        hf = check_sequence(hv, 'hours', MAX_HOURS_PER_DAY, 'Hours', 'h')
        for r in rs:
            if r['_d'] not in (y_iso, t_iso):
                continue
            msgs = []
            if r['km'] is None or r['km'] <= MIN_KM:
                msgs.append('No km entered')
            elif r['id'] in kf:
                msgs.append(kf[r['id']])
            if r['hours'] is None or r['hours'] <= MIN_HOURS:
                msgs.append('No hours entered')
            elif r['id'] in hf:
                msgs.append(hf[r['id']])
            if msgs:
                issues.append((r, msgs))

    def day_status(day):
        rows = []
        for fl in fleets:
            rs = [r for r in prestarts if norm_fleet(r['fleet']) == norm_fleet(fl) and r['_d'] == day]
            done = [r for r in rs if r.get('completed') is not False]
            done.sort(key=lambda r: r['_t'].isoformat() if r['_t'] else '')
            rows.append((fl, done, [r for r in rs if r.get('completed') is False]))
        return rows

    today_rows, y_rows = day_status(t_iso), day_status(y_iso)
    deps = [i for i in items if i.get('kind') == 'departure' and not i.get('demo') and i.get('waDate') in (y_iso, t_iso)
            and i.get('status') in ('alerted', 'no-recipients', 'disabled')]   # first departure per truck per day
    deps.sort(key=lambda d: d['at'])
    since = (now - dt.timedelta(hours=24)).isoformat()
    by_id = {str(r['id']): r for r in prestarts}
    comments = sorted([i for i in items if i.get('kind') == 'comment' and str(i.get('at', '')) >= since],
                      key=lambda c: c.get('at', ''))

    def who(r):
        t = wa_time(r['_t']) if r['_t'] else ''
        return f"{t + ' · ' if t else ''}{r.get('employee') or '—'}"

    def status_table(rows):
        h = f'<table style="border-collapse:collapse;width:100%"><tr><th style="{STYLE_TH}">Truck</th><th style="{STYLE_TH}">Prestart</th></tr>'
        for fl, done, inc in rows:
            if done:
                cell = '<span style="color:#087a08;font-weight:600">✓ Done</span> ' + esc('; '.join(who(r) for r in done))
            elif inc:
                cell = '<span style="color:#9a6a00;font-weight:600">◐ Started, not completed</span> ' + esc('; '.join(who(r) for r in inc))
            else:
                cell = '<span style="color:#d03b3b;font-weight:600">✗ No prestart</span>'
            h += f'<tr><td style="{STYLE_TD}"><b>{esc(fl)}</b></td><td style="{STYLE_TD}">{cell}</td></tr>'
        return h + '</table>'

    n_today = sum(1 for _, d, _ in today_rows if d)
    n_y = sum(1 for _, d, _ in y_rows if d)
    inner = f'<h2 style="font-size:15px;margin:6px 0 8px">Today so far · {esc(fmt_date(t_iso))} <span style="color:#7a7974;font-weight:400">({n_today} of {len(fleets)} trucks)</span></h2>' + status_table(today_rows)
    inner += f'<h2 style="font-size:15px;margin:18px 0 8px">{prev_label} · {esc(fmt_date(y_iso))} <span style="color:#7a7974;font-weight:400">({n_y} of {len(fleets)} trucks)</span></h2>' + status_table(y_rows)
    inner += '<h2 style="font-size:15px;margin:18px 0 8px">Left base without a prestart</h2>'
    if deps:
        inner += f'<table style="border-collapse:collapse;width:100%"><tr><th style="{STYLE_TH}">When</th><th style="{STYLE_TH}">Truck</th><th style="{STYLE_TH}">Geofence</th></tr>'
        for d in deps:
            at = parse_iso(d['at'])
            inner += f'<tr><td style="{STYLE_TD}">{esc(fmt_date(d["waDate"]))} {esc(wa_time(at))}</td><td style="{STYLE_TD}"><b>{esc(d["fleet"])}</b></td><td style="{STYLE_TD}">{esc(d.get("geofence"))}</td></tr>'
        inner += '</table>'
    else:
        inner += '<p style="margin:0;font-size:13px;color:#52514e">None.</p>'
    inner += f'<h2 style="font-size:15px;margin:18px 0 8px">Km / hours issues ({prev_label.lower()} and today)</h2>'
    if issues:
        inner += f'<table style="border-collapse:collapse;width:100%"><tr><th style="{STYLE_TH}">Date</th><th style="{STYLE_TH}">Truck</th><th style="{STYLE_TH}">Driver</th><th style="{STYLE_TH}">Km</th><th style="{STYLE_TH}">Hours</th><th style="{STYLE_TH}">Issue</th></tr>'
        for r, msgs in issues:
            km_s = '' if r['km'] is None else f"{r['km']:,.0f}"
            hr_s = '' if r['hours'] is None else f"{r['hours']:,.0f}"
            inner += (f'<tr><td style="{STYLE_TD}">{esc(fmt_date(r["_d"]))}</td><td style="{STYLE_TD}"><b>{esc(r["fleet"])}</b></td>'
                      f'<td style="{STYLE_TD}">{esc(r.get("employee"))}</td><td style="{STYLE_TD}">{km_s}</td><td style="{STYLE_TD}">{hr_s}</td>'
                      f'<td style="{STYLE_TD};color:#b02a2a">{esc("; ".join(msgs))}</td></tr>')
        inner += '</table>'
    else:
        inner += '<p style="margin:0;font-size:13px;color:#52514e">None.</p>'
    if comments:
        inner += '<h2 style="font-size:15px;margin:18px 0 8px">New comments (last 24 h)</h2><ul style="margin:0;padding-left:18px;font-size:13px">'
        for c in comments:
            inner += f'<li style="margin:3px 0">{esc(c.get("text"))} <span style="color:#7a7974">— {esc(str(c.get("by", "")).split("@")[0])}, {esc(comment_where(c.get("key", ""), by_id))}</span></li>'
        inner += '</ul>'

    subject = f"Prestart report {fmt_date(t_iso)}: {n_today}/{len(fleets)} done today · {len(deps)} left without prestart · {len(issues)} km/hours issues"
    text = [f"Prestart report — {fmt_date(t_iso)} (WA)", '', f"Today so far ({n_today}/{len(fleets)}):"]
    for fl, done, inc in today_rows:
        text.append(f"  {fl}: " + ('done — ' + '; '.join(who(r) for r in done) if done else 'started, not completed' if inc else 'NO PRESTART'))
    text += ['', f"{prev_label} ({n_y}/{len(fleets)}):"]
    for fl, done, inc in y_rows:
        text.append(f"  {fl}: " + ('done' if done else 'started, not completed' if inc else 'NO PRESTART'))
    text += ['', 'Left base without a prestart:'] + ([f"  {fmt_date(d['waDate'])} {wa_time(parse_iso(d['at']))} {d['fleet']} ({d.get('geofence')})" for d in deps] or ['  none'])
    text += ['', 'Km / hours issues:'] + ([f"  {fmt_date(r['_d'])} {r['fleet']} {r.get('employee')}: {'; '.join(m)}" for r, m in issues] or ['  none'])
    text += ['', APP_URL]
    return subject, wrap_html(f"Daily prestart report · {fmt_date(t_iso)}", inner), '\n'.join(text)


def comment_where(key, by_id):
    """'36160' -> 'MGN52 Thu 22 Oct (#36160)'; 'missing#MGN53#2026-10-21' -> 'MGN53 Wed 21 Oct (missing day)'."""
    m = re.match(r'missing#(.+)#(\d{4}-\d{2}-\d{2})$', key or '')
    if m:
        return f"{m.group(1)} {fmt_date(m.group(2))} (missing day)"
    r = by_id.get(str(key))
    return f"{r['fleet']} {fmt_date(r['_d'])} (#{key})" if r else f"#{key}"


def report_due(settings, last_sent_date, now=None):
    """Due when enabled, today (Brisbane) is a report day, the time has passed (within 2 h) and not yet sent."""
    now = (now or now_utc()).astimezone(BNE)
    if not settings['reportEnabled'] or not settings['reportEmails']:
        return False
    if not settings['reportDays'][now.weekday()]:
        return False
    hh, mm = map(int, settings['reportTime'].split(':'))
    sched = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return sched <= now < sched + dt.timedelta(hours=2) and last_sent_date != now.date().isoformat()


def tick(table, items, allowed_fleets, now=None):
    """Runs every 5 minutes: check pending departures, then send the daily report when due."""
    now = now or now_utc()
    settings = get_settings(table)
    checked = process_departures(table, items, settings, now)
    last = (table.get_item(Key={'id': 'report#last'}).get('Item') or {}).get('date')
    sent = None
    if report_due(settings, last, now):
        subject, html_body, text = build_report(items, allowed_fleets, now, settings['reportDays'])
        sent = send_email(settings['reportEmails'], subject, html_body, text)
        table.put_item(Item={'id': 'report#last', 'kind': 'state', 'date': now.astimezone(BNE).date().isoformat(),
                             'at': now.isoformat(timespec='seconds'), 'to': ', '.join(settings['reportEmails']),
                             'subject': subject})
    return {'departuresChecked': len(checked), 'reportSent': bool(sent)}
