import asyncio
import json
import time
from jsonschema import validate

class DeviceError(ValueError):
    def __init__(self, phase, code):
        self.phase, self.code = phase, code
        super().__init__(code)

    def detail(self):
        return {'phase': self.phase, 'code': self.code}

def unpack(value):
    if isinstance(value, dict):
        for key in ('Results', 'devices', 'results', 'data'):
            if isinstance(value.get(key), (dict, list)):
                return unpack(value[key])
        if not value:
            return []
        if all(isinstance(v, dict) for v in value.values()):
            return [dict(v, Name=k) for k, v in value.items()]
    if isinstance(value, list):
        return value
    raise ValueError('Unbekanntes Geräteformat')

def payload(response):
    if getattr(response, 'isError', False):
        raise DeviceError('tool_response', 'mcp_tool_error')
    value = getattr(response, 'structuredContent', None)
    texts = [p.text for p in response.content if getattr(p, 'type', '') == 'text']
    combined = json.dumps(value) if value is not None else '\n'.join(texts)
    if 'connection refused' in combined.lower() or 'errno 111' in combined.lower():
        raise DeviceError('fhem_connection', 'connection_refused')
    if 'fhem connection failed' in combined.lower():
        raise DeviceError('fhem_connection', 'connection_failed')
    if value is None:
        value = json.loads('\n'.join(texts))
    for _ in range(4):
        if isinstance(value, dict) and 'result' in value and isinstance(value['result'], (str, dict, list)):
            value = value['result']
        elif isinstance(value, str):
            value = json.loads(value)
        else:
            break
    return value

def normalize(rows):
    result = {}
    for item in rows:
        if not isinstance(item, dict):
            continue
        internals, attrs = item.get('Internals', {}), item.get('Attributes', item.get('attributes', {}))
        if not isinstance(internals, dict) or not isinstance(attrs, dict):
            continue
        name = item.get('Name') or item.get('name') or internals.get('NAME')
        if not isinstance(name, str) or not name or len(name) > 200:
            continue
        result[name] = {'name': name, 'alias': str(attrs.get('alias', item.get('alias', name)))[:200], 'room': str(attrs.get('room', item.get('room', '')))[:300], 'group': str(attrs.get('group', ''))[:300], 'type': str(internals.get('TYPE', item.get('type', '')))[:100]}
    return list(result.values())

def search(rows, query):
    return [d for d in rows if all(w in ' '.join(d.values()).casefold() for w in query.casefold().split())]

class DeviceService:
    def __init__(self, core):
        self.core, self.expires, self.cached = core, 0, []
        self.lock = asyncio.Lock()

    async def devices(self, tools=None, refresh=False):
        async with self.lock:
            if not refresh and self.expires > time.monotonic():
                return self.cached
            c = self.core
            if not c.MCP_URL:
                raise DeviceError('configuration', 'missing_mcp_url')
            if tools is None:
                try:
                    tools = await c.discover()
                except Exception as exc:
                    raise DeviceError('discovery', 'timeout' if isinstance(exc, TimeoutError) else 'failed') from None
            tool = tools.get('fhem_list_devices')
            if not tool or not c.readonly('fhem_list_devices'):
                raise DeviceError('tool_selection', 'missing_readonly_device_list')
            try:
                validate({}, tool['function']['parameters'])
            except Exception:
                raise DeviceError('arguments', 'empty_arguments_invalid') from None
            try:
                async with asyncio.timeout(25):
                    async with c.mcp_session() as session:
                        response = await session.call_tool('fhem_list_devices', {})
            except Exception as exc:
                raise DeviceError('tool_call', 'timeout' if isinstance(exc, TimeoutError) else 'failed') from None
            try:
                rows = unpack(payload(response))
                result = normalize(rows)
                if rows and not result:
                    raise DeviceError('normalization', 'no_valid_device_records')
            except DeviceError:
                raise
            except Exception:
                raise DeviceError('parsing', 'unsupported_device_format') from None
            self.cached, self.expires = result, time.monotonic()+30
            return result
