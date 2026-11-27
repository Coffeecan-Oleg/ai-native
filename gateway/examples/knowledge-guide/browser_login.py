"""Interactive OAuth for a manual run. Credentials live in process memory."""
import asyncio
import webbrowser
from contextlib import asynccontextmanager
from hmac import compare_digest
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit

import httpx
from mcp.client.auth import OAuthClientProvider
from mcp.shared.auth import OAuthClientMetadata


class SessionStorage:
    def __init__(self):
        self.tokens = self.client = None

    async def get_tokens(self):
        return self.tokens

    async def set_tokens(self, tokens):
        self.tokens = tokens

    async def get_client_info(self):
        return self.client

    async def set_client_info(self, client):
        self.client = client


@asynccontextmanager
async def authorized_client(url, scopes):
    result = asyncio.get_running_loop().create_future()
    expected_state = ''
    origin = urlsplit(url)
    storage = SessionStorage()

    async def receive(reader, writer):
        status = b'400 Bad Request'
        try:
            header = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 4)
            method, target, _ = header.split(b'\r\n', 1)[0].decode('ascii').split(' ')
            target = urlsplit(target)
            fields = parse_qs(target.query)
            state = fields.get('state', [])
            if (method == 'GET' and target.path == '/callback' and not target.netloc
                    and expected_state and len(state) == 1 and compare_digest(state[0], expected_state)
                    and not result.done()):
                if len(fields.get('code', [])) == 1 and 'error' not in fields:
                    result.set_result((fields['code'][0], state[0]))
                    status = b'200 OK'
                elif 'error' in fields:
                    result.set_exception(ValueError('authorization declined'))
        except (ValueError, asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, TypeError):
            pass
        finally:
            writer.write(b'HTTP/1.1 ' + status + b'\r\nCache-Control: no-store\r\n'
                         b'Referrer-Policy: no-referrer\r\nConnection: close\r\n\r\nReturn to the guide.')
            writer.close()
            await writer.wait_closed()

    async def open_sign_in(location):
        nonlocal expected_state
        target = urlsplit(location)
        state = parse_qs(target.query).get('state', [])
        if (target.scheme != 'https' or target.netloc != origin.netloc or len(state) != 1
                or not state[0]):
            raise ValueError('unexpected authorization server')
        expected_state = state[0]
        # Discovery can advertise scopes for every client of this Gateway.
        # The guide only asks the user to authorize its two read scopes.
        params = [(k, v) for k, v in parse_qsl(target.query) if k != 'scope']
        params.append(('scope', ' '.join(sorted(scopes))))
        consent_url = target._replace(query=urlencode(params)).geturl()
        if not await asyncio.to_thread(webbrowser.open, consent_url):
            raise RuntimeError('open the guide on a computer with a browser')

    async def callback():
        return await asyncio.wait_for(result, 180)

    server = await asyncio.start_server(receive, '127.0.0.1', 0, limit=8192)
    async with server:
        port = server.sockets[0].getsockname()[1]
        metadata = OAuthClientMetadata(client_name='Справочник Платформы',
            redirect_uris=[f'http://127.0.0.1:{port}/callback'], scope=' '.join(sorted(scopes)),
            token_endpoint_auth_method='none', grant_types=['authorization_code', 'refresh_token'])
        provider = OAuthClientProvider(url, metadata, storage, open_sign_in, callback)
        async with httpx.AsyncClient(auth=provider, timeout=30) as http:
            yield http
