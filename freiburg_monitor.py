import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlencode, urlparse, parse_qs
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

EVENT = 413679
PAGE = f'https://my.raceresult.com/{EVENT}/boerse'
STATE = Path('.monitor/state.json')

def api(url, payload=None):
    body = json.dumps(payload).encode() if payload is not None else None
    req = Request(url, data=body, headers={'Content-Type': 'application/json', 'User-Agent': 'FreiburgSeatMonitor/1.0'})
    try:
        with urlopen(req, timeout=30) as response:
            return json.load(response)
    except Exception:
        # Telegram request URLs contain a secret: never print exceptions or URLs.
        raise RuntimeError('Abruf oder Versand fehlgeschlagen; keine vertraulichen Details im Log.') from None

def offers(data):
    fields = data.get('DataFields')
    groups = data.get('data')
    if not isinstance(fields, list) or not isinstance(groups, (dict, list)):
        raise RuntimeError('Unerwartetes RACE-RESULT-Format.')
    if isinstance(groups, list):
        if groups:
            raise RuntimeError('Wettbewerbsgruppen fehlen; Prüfung abgebrochen.')
        return {}, []
    links = [f.get('Link') for f in data.get('list', {}).get('Fields', []) if f.get('Link')]
    if len(links) != 1 or links[0] not in fields:
        raise RuntimeError('Kauf-Link-Spalte nicht eindeutig.')
    col = fields.index(links[0])
    found, incomplete = {}, []
    for group, rows in groups.items():
        label = re.sub(r'^#[^_]+_', '', group)
        if label.strip().casefold() != 'halbmarathon':
            continue
        if not isinstance(rows, list):
            raise RuntimeError('Ungültige Angebotsgruppe.')
        for row in rows:
            if not isinstance(row, list) or not row:
                raise RuntimeError('Ungültige Angebotszeile.')
            if len(row) == 1:
                incomplete.append(group)
                continue
            if len(row) != len(fields):
                raise RuntimeError('Angebotsspalten haben sich geändert.')
            url = row[col]
            p = urlparse(url)
            q = parse_qs(p.query)
            if p.scheme != 'https' or p.netloc != 'events.raceresult.com' or p.path != '/registrations/' or q.get('event') != [str(EVENT)] or not q.get('URLID'):
                raise RuntimeError('Ungültiger Kauf-Link; Prüfung abgebrochen.')
            found[hashlib.sha256(url.encode()).hexdigest()] = url
    return found, incomplete

def fetch_offers():
    cfg = api(PAGE + '/config?lang=de&sanitize=true')
    if '2027' not in cfg.get('eventname', '') or cfg.get('Tab', {}).get('Enabled') is not True:
        raise RuntimeError('Börse ist nicht für 2027 aktiv.')
    lists = [x for x in cfg['TabConfig']['Lists'] if x.get('ID') == 'DF85F0']
    server = cfg.get('server', 'my.raceresult.com')
    if len(lists) != 1 or not re.fullmatch(r'[a-z0-9-]+\.raceresult\.com', server):
        raise RuntimeError('Börsen-Konfiguration hat sich geändert.')
    base = f'https://{server}/{EVENT}/boerse/list?'
    query = dict(key=cfg['key'], listname=lists[0]['Name'], page='boerse', contest='0', r='all', l='0', openedGroups='{}', term='')
    found, incomplete = offers(api(base + urlencode(query)))
    if incomplete:
        query['openedGroups'] = json.dumps({name: 99999999 for name in incomplete})
        found, incomplete = offers(api(base + urlencode(query)))
        if incomplete:
            raise RuntimeError('Nicht alle Angebote konnten geladen werden.')
    return found

def main():
    today = dt.datetime.now(ZoneInfo('Europe/Berlin')).date()
    if today > dt.date(2027, 3, 21):
        print('Börsenfrist vorbei. Workflow kann deaktiviert werden.')
        return
    dry = '--dry-run' in sys.argv
    token, chat = os.getenv('TELEGRAM_BOT_TOKEN'), os.getenv('TELEGRAM_CHAT_ID')
    if not dry and (not token or not chat):
        raise RuntimeError('Bitte TELEGRAM_BOT_TOKEN und TELEGRAM_CHAT_ID als Actions Secrets anlegen.')
    current = fetch_offers()
    print(f'Halbmarathon-Angebote aktuell: {len(current)}')
    if dry:
        return
    state = json.loads(STATE.read_text()) if STATE.exists() else {'event': EVENT, 'seen': [], 'heartbeat': str(today), 'started': False}
    if state.get('event') != EVENT or not isinstance(state.get('seen'), list):
        raise RuntimeError('Gespeicherter Zustand ungültig.')
    seen = set(state['seen'])
    def save():
        STATE.parent.mkdir(exist_ok=True)
        STATE.write_text(json.dumps(state, indent=2) + '\n')
    def send(text):
        result = api(f'https://api.telegram.org/bot{token}/sendMessage', {'chat_id': chat, 'text': text, 'disable_web_page_preview': True})
        if result.get('ok') is not True:
            raise RuntimeError('Telegram hat die Nachricht nicht bestätigt.')
    if not state.get('started'):
        send(f'Freiburg-Halbmarathon 2027: Überwachung gestartet. Aktuell {len(current)} passende Angebote. Neue Angebote folgen als separate Nachricht.\n{PAGE}')
        state['started'] = True
        save()
    for key, url in current.items():
        if key not in seen:
            send('Neuer Halbmarathon-Startplatz für Freiburg 2027!\nJetzt prüfen und bei Interesse übernehmen:\n' + url)
            seen.add(key)
            state['seen'] = sorted(seen)
            save()
    if (today - dt.date.fromisoformat(state['heartbeat'])).days >= 30:
        state['heartbeat'] = str(today)
        save()

if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # Only our fixed diagnostic messages are safe to publish.
        print(str(exc) if isinstance(exc, RuntimeError) else 'Prüfung fehlgeschlagen; gespeicherte Benachrichtigungen bleiben erhalten.', file=sys.stderr)
        sys.exit(1)
