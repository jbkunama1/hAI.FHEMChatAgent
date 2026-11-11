import asyncio
import json
from jsonschema import validate
from openai import AsyncOpenAI

class AgentService:
    def __init__(self, core):
        self.core = core

    def reply(self, owner):
        c = self.core
        pending = c.store.pending(owner)
        return {'messages': c.store.load(owner), 'pending': pending, 'recovery': c.store.blocked(owner)}

    async def run(self, owner, messages, allow_writes=True):
        c = self.core
        allow_writes = allow_writes and not c.store.blocked(owner)
        discovery_error = False
        try:
            tools = await c.discover()
        except Exception:
            tools, discovery_error = {}, True
        if c.FHEM_URL:
            tools['fhem_web_command'] = c.WEBTOOL
        if not allow_writes:
            tools = {n: t for n, t in tools.items() if n != 'fhem_web_command' and c.readonly(n)}
        if not c.E('OPENAI_API_KEY', ''):
            messages.append({'role': 'assistant', 'content': 'OPENAI_API_KEY fehlt in der Stack-Konfiguration.'})
            c.store.save(owner, messages)
            return self.reply(owner)
        try:
            async with AsyncOpenAI(base_url=c.E('OPENAI_BASE_URL', 'https://9router.arbeitermili.eu/v1').rstrip('/'), api_key=c.E('OPENAI_API_KEY'), max_retries=0, timeout=90) as client:
                for _ in range(12):
                    kwargs = {'tools': list(tools.values()), 'parallel_tool_calls': False} if tools else {}
                    response = await client.chat.completions.create(model=c.MODEL, messages=[c.SYSTEM]+messages, **kwargs)
                    msg = response.choices[0].message
                    calls = msg.tool_calls or []
                    record = {'role': 'assistant', 'content': msg.content}
                    if calls:
                        record['tool_calls'] = [x.model_dump(exclude_none=True) for x in calls]
                    messages.append(record)
                    if not calls:
                        if discovery_error:
                            messages.append({'role': 'assistant', 'content': 'MCP-Werkzeugliste nicht erreichbar. Header, API-Key und Transport prüfen.'})
                        c.store.save(owner, messages)
                        return self.reply(owner)
                    if len(calls) != 1:
                        for call in calls:
                            messages.append({'role': 'tool', 'tool_call_id': call.id, 'content': json.dumps({'error': 'Gesamter Batch abgewiesen: genau ein Werkzeug pro Schritt. Nichts ausgeführt.'})})
                        c.store.save(owner, messages)
                        continue
                    call = calls[0]
                    try:
                        name = call.function.name
                        if name not in tools:
                            raise ValueError('Werkzeug nicht zugelassen')
                        args = json.loads(call.function.arguments)
                        validate(args, tools[name]['function']['parameters'])
                    except Exception:
                        messages.append({'role': 'tool', 'tool_call_id': call.id, 'content': json.dumps({'error': 'Ungültiger oder nicht zugelassener Aufruf. Keine Ausführung.'})})
                        c.store.save(owner, messages)
                        continue
                    route = 'web' if name == 'fhem_web_command' else 'mcp'
                    action = {'name': name, 'args': args, 'route': route, 'destination': c.FHEM_URL if route == 'web' else c.MCP_URL, 'call_id': call.id}
                    if route == 'mcp' and c.readonly(name):
                        result = await c.safe_execute(action)
                        messages.append({'role': 'tool', 'tool_call_id': call.id, 'content': json.dumps(result, ensure_ascii=False)})
                        c.store.save(owner, messages)
                        if result.get('_uncertain'):
                            messages.append({'role': 'assistant', 'content': 'Werkzeugaufruf fehlgeschlagen. Kein automatischer Wiederholungsversuch; bitte Konfiguration oder Gerätestatus prüfen.'})
                            break
                    elif allow_writes:
                        c.store.propose(owner, action, c.TTL, messages=messages)
                        return self.reply(owner)
                    else:
                        raise ValueError('Schreibzugriff gesperrt')
                else:
                    messages.append({'role': 'assistant', 'content': 'Werkzeuglimit erreicht. Bitte Auftrag eingrenzen.'})
        except Exception as exc:
            messages.append({'role': 'assistant', 'content': 'KI-Aufruf fehlgeschlagen ('+type(exc).__name__+'). Endpunkt, Modell und API-Key prüfen.'})
        c.store.save(owner, messages)
        return self.reply(owner)

    async def chat(self, owner, text):
        c = self.core
        async with c.locks[owner]:
            if c.store.pending(owner):
                return self.reply(owner)
            if text.startswith('/weiter '):
                instruction = text[len('/weiter '):].strip()
                if not instruction:
                    raise ValueError('Neuen Auftrag nach /weiter angeben.')
                c.store.acknowledge(owner)
                text = instruction
            messages = c.store.load(owner)
            if len(messages) > 400:
                raise ValueError('Verlauf zu lang. Neuen Chat starten.')
            messages.append({'role': 'user', 'content': text})
            c.store.save(owner, messages)
            return await self.run(owner, messages)

    async def decide(self, owner, ident, approve):
        c = self.core
        async with c.locks[owner]:
            action, state = c.store.claim(owner, ident, approve)
            if state != 'consumed':
                messages = c.store.load(owner)
                messages.append({'role': 'assistant', 'content': 'Auftrag nicht ausgeführt ('+state+').'})
                c.store.save(owner, messages)
                return self.reply(owner)
            try:
                result = await c.safe_execute(action)
            except BaseException:
                c.store.finish(owner, ident, {'status': 'uncertain', 'note': 'Ausführung unterbrochen. Nicht wiederholen; Gerätestatus prüfen.'}, uncertain=True)
                raise
            uncertain = bool(result.get('_uncertain') or result.get('isError'))
            c.store.finish(owner, ident, result, uncertain=uncertain)
            if uncertain:
                messages = c.store.load(owner)
                messages.append({'role': 'assistant', 'content': 'Schreibausgang unklar. Keine automatische Neuplanung. Bitte Status abfragen. Neuer Schreibauftrag erst mit /weiter <Auftrag> und neuer Einzelfreigabe.'})
                c.store.save(owner, messages)
                return self.reply(owner)
            return await self.run(owner, c.store.load(owner), allow_writes=False)
