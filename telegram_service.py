import asyncio, contextlib, json, secrets, time
from device_service import search
from menu import make_prompt

class TelegramService:
    def __init__(self, core, devices):
        self.core, self.devices, self.choices = core, devices, {}

    async def send_state(self, uid, state, start):
        c = self.core
        for message in state['messages'][start:]:
            if message['role'] == 'assistant' and message.get('content'):
                text = message['content']
                for i in range(0, len(text), 3500):
                    await c.tg('sendMessage', {'chat_id': uid, 'text': text[i:i+3500]})
        if state.get('recovery'):
            await c.tg('sendMessage', {'chat_id': uid, 'text': state['recovery']['reason']})
        pending = state['pending']
        if pending:
            text = json.dumps(pending['action'], ensure_ascii=False, indent=2)
            for i in range(0, len(text), 3500):
                await c.tg('sendMessage', {'chat_id': uid, 'text': text[i:i+3500]})
            await c.tg('sendMessage', {'chat_id': uid, 'text': 'Genau diesen Auftrag einmalig ausführen?', 'reply_markup': {'inline_keyboard': [[{'text': 'Ausführen', 'callback_data': 'y:'+pending['id']}, {'text': 'Abbrechen', 'callback_data': 'n:'+pending['id']}]]}})

    async def handle(self, update):
        c = self.core
        cb = update.get('callback_query')
        msg = cb.get('message', {}) if cb else update.get('message', {})
        uid = (cb or msg).get('from', {}).get('id')
        if uid not in c.ALLOWED or msg.get('chat', {}).get('type') != 'private' or msg.get('chat', {}).get('id') != uid:
            return
        owner = 'tg:'+str(uid)
        start = len(c.store.load(owner))
        if cb:
            await c.tg('answerCallbackQuery', {'callback_query_id': cb['id']})
            data = cb.get('data', '')
            if data.startswith(('y:', 'n:')):
                await self.send_state(uid, await c.decide(owner, data[2:], data.startswith('y:')), start)
            elif data.startswith('d:'):
                token = data[2:]
                selection = self.choices.get(token)
                if not selection or selection[0] != uid or selection[2] < time.monotonic():
                    await c.tg('sendMessage', {'chat_id': uid, 'text': 'Auswahl ungültig oder abgelaufen. /devices erneut senden.'})
                    return
                self.choices.pop(token)
                await self.send_state(uid, await c.chat(owner, make_prompt('status', selection[1])), start)
            return
        text = msg.get('text', '').strip()
        if not text or len(text) > 8000:
            return
        if text in ('/start', '/help', '/menu'):
            await c.tg('sendMessage', {'chat_id': uid, 'text': 'FHEM-Chat: /devices Wohnzimmer sucht echte Geräte. Schreibzugriffe benötigen eine Einzelfreigabe. Nach unklarem Ausgang erst Status prüfen; /weiter <neuer Auftrag> hebt die Recovery-Sperre bewusst auf.'})
            return
        if text == '/devices' or text.startswith('/devices '):
            found = search(await self.devices.devices(), text.partition(' ')[2][:120])
            now = time.monotonic()
            self.choices = {k: v for k, v in self.choices.items() if v[2] >= now and v[0] != uid}
            if len(self.choices) > 2000:
                raise ValueError('Auswahllimit erreicht')
            buttons = []
            for device in found[:20]:
                token = secrets.token_hex(12)
                self.choices[token] = (uid, device['name'], now+300)
                buttons.append([{'text': (device['alias']+' · '+device['name']+' · '+device['room'])[:100], 'callback_data': 'd:'+token}])
            await c.tg('sendMessage', {'chat_id': uid, 'text': str(len(found))+' Treffer; maximal 20 angezeigt. Auswahl fragt den Status ab.', 'reply_markup': {'inline_keyboard': buttons}})
            return
        await self.send_state(uid, await c.chat(owner, text), start)

    async def run(self):
        c, offset = self.core, 0
        while True:
            try:
                updates = await c.tg('getUpdates', {'offset': offset, 'timeout': 25, 'allowed_updates': ['message', 'callback_query']})
                for update in updates:
                    offset = update['update_id']+1
                    try:
                        await self.handle(update)
                    except Exception:
                        cb = update.get('callback_query')
                        uid = (cb or update.get('message', {})).get('from', {}).get('id')
                        if uid in c.ALLOWED:
                            with contextlib.suppress(Exception):
                                await c.tg('sendMessage', {'chat_id': uid, 'text': 'Anfrage fehlgeschlagen. Keine automatische Wiederholung. Konfiguration oder Freigabe prüfen.'})
            except Exception:
                await asyncio.sleep(5)
