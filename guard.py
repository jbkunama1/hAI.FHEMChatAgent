import json, secrets, sqlite3, time

READS = {'fhem_get', 'fhem_get_readings', 'fhem_list_attrs', 'fhem_list_devices', 'fhem_device_search', 'fhem_reading_history'}

def readonly(name):
    return name in READS

class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        with self.db:
            self.db.execute('CREATE TABLE IF NOT EXISTS chats (owner TEXT PRIMARY KEY, messages TEXT NOT NULL)')
            self.db.execute('CREATE TABLE IF NOT EXISTS actions (id TEXT PRIMARY KEY, owner TEXT, payload TEXT, expires REAL, state TEXT)')
            self.db.execute('CREATE TABLE IF NOT EXISTS recovery (owner TEXT PRIMARY KEY, action_id TEXT, reason TEXT)')
            for ident, owner, raw, state in self.db.execute("SELECT id,owner,payload,state FROM actions WHERE state IN ('pending','consumed')").fetchall():
                action = json.loads(raw)
                outcome = 'cancelled' if state == 'pending' else 'uncertain'
                self.db.execute('UPDATE actions SET state=? WHERE id=?', (outcome, ident))
                self._resolve(owner, action, {'status': outcome, 'note': 'Neustart: keine Wiederholung.'})
                if state == 'consumed':
                    self._block(owner, ident, 'Neustart mit unklarem Schreibausgang. Gerätestatus prüfen.')
            for owner, raw in self.db.execute('SELECT owner,messages FROM chats').fetchall():
                messages = json.loads(raw)
                resolved = {m.get('tool_call_id') for m in messages if m.get('role') == 'tool'}
                missing = [c['id'] for m in messages for c in m.get('tool_calls', []) if c['id'] not in resolved]
                for ident in dict.fromkeys(missing):
                    messages.append({'role': 'tool', 'tool_call_id': ident, 'content': json.dumps({'status': 'interrupted', 'note': 'Neustart: nicht automatisch wiederholen.'})})
                if missing:
                    self._save(owner, messages)
                    self._block(owner, '', 'Unterbrochener Werkzeugaufruf. Status vor neuem Schreibauftrag prüfen.')

    def load(self, owner):
        row = self.db.execute('SELECT messages FROM chats WHERE owner=?', (owner,)).fetchone()
        return json.loads(row[0]) if row else []

    def _save(self, owner, messages):
        self.db.execute('INSERT OR REPLACE INTO chats VALUES (?,?)', (owner, json.dumps(messages, ensure_ascii=False)))

    def save(self, owner, messages):
        with self.db:
            self._save(owner, messages)

    def _block(self, owner, ident, reason):
        self.db.execute('INSERT OR REPLACE INTO recovery VALUES (?,?,?)', (owner, ident, reason))

    def blocked(self, owner):
        row = self.db.execute('SELECT action_id,reason FROM recovery WHERE owner=?', (owner,)).fetchone()
        return {'action_id': row[0], 'reason': row[1]} if row else None

    def acknowledge(self, owner):
        with self.db:
            if self.db.execute("SELECT 1 FROM actions WHERE owner=? AND state='consumed'", (owner,)).fetchone():
                raise ValueError('Ausführung läuft noch.')
            self.db.execute('DELETE FROM recovery WHERE owner=?', (owner,))

    def _resolve(self, owner, action, result):
        ident = action.get('call_id')
        if not ident:
            return
        messages = self.load(owner)
        present = any(c.get('id') == ident for m in messages for c in m.get('tool_calls', []))
        resolved = any(m.get('role') == 'tool' and m.get('tool_call_id') == ident for m in messages)
        if present and not resolved:
            messages.append({'role': 'tool', 'tool_call_id': ident, 'content': json.dumps(result, ensure_ascii=False)})
            self._save(owner, messages)

    def pending(self, owner):
        with self.db:
            for ident, raw in self.db.execute("SELECT id,payload FROM actions WHERE owner=? AND state='pending' AND expires<=?", (owner, time.time())).fetchall():
                self.db.execute("UPDATE actions SET state='expired' WHERE id=?", (ident,))
                self._resolve(owner, json.loads(raw), {'status': 'expired', 'note': 'Freigabe abgelaufen. Nichts ausgeführt.'})
        row = self.db.execute("SELECT id,payload,expires FROM actions WHERE owner=? AND state='pending' ORDER BY rowid DESC LIMIT 1", (owner,)).fetchone()
        return {'id': row[0], 'action': json.loads(row[1]), 'expires': row[2]} if row else None

    def propose(self, owner, payload, ttl, messages=None):
        self.pending(owner)
        ident, expires = secrets.token_hex(16), time.time()+ttl
        raw = json.dumps(payload, sort_keys=True)
        with self.db:
            if self.blocked(owner) or self.db.execute("SELECT 1 FROM actions WHERE owner=? AND state IN ('pending','consumed')", (owner,)).fetchone():
                raise ValueError('Offener Auftrag oder Recovery-Sperre.')
            if messages is not None:
                self._save(owner, messages)
            self.db.execute('INSERT INTO actions VALUES (?,?,?,?,?)', (ident, owner, raw, expires, 'pending'))
        return {'id': ident, 'action': json.loads(raw), 'expires': expires}

    def claim(self, owner, ident, approve):
        with self.db:
            row = self.db.execute("SELECT payload,expires FROM actions WHERE id=? AND owner=? AND state='pending'", (ident, owner)).fetchone()
            if not row:
                raise ValueError('Freigabe fehlt, fremd, abgelaufen oder bereits verwendet.')
            state = 'expired' if row[1] <= time.time() else ('consumed' if approve else 'rejected')
            if state == 'consumed' and self.blocked(owner):
                raise ValueError('Recovery-Sperre: zuerst Status prüfen und neuen Auftrag erteilen.')
            cur = self.db.execute("UPDATE actions SET state=? WHERE id=? AND owner=? AND state='pending'", (state, ident, owner))
            if cur.rowcount != 1:
                raise ValueError('Freigabe bereits verwendet.')
            action = json.loads(row[0])
            if state == 'consumed':
                self._block(owner, ident, 'Ausführung läuft oder Ausgang noch nicht gespeichert.')
            else:
                self._resolve(owner, action, {'status': state, 'note': 'Nicht ausgeführt.'})
        return action, state

    def finish(self, owner, ident, result, uncertain=False):
        with self.db:
            row = self.db.execute("SELECT payload FROM actions WHERE id=? AND owner=? AND state='consumed'", (ident, owner)).fetchone()
            if not row:
                raise ValueError('Keine laufende Ausführung für diesen Benutzer.')
            self._resolve(owner, json.loads(row[0]), result)
            self.db.execute('UPDATE actions SET state=? WHERE id=?', ('uncertain' if uncertain else 'completed', ident))
            if uncertain:
                self._block(owner, ident, 'Schreibausgang unklar. Status prüfen; neuer Auftrag mit /weiter <Auftrag>.')
            else:
                self.db.execute('DELETE FROM recovery WHERE owner=? AND action_id=?', (owner, ident))
