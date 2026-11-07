"""Personal numeric-only MCP transport with Windows DPAPI token storage.

Setup performs standard OAuth/PKCE in the user's browser. Scheduled runs only
refresh that client and import numeric usage. No Codex/ChatGPT token is copied.
"""
import argparse
import base64
import ctypes
from ctypes import wintypes
from contextlib import contextmanager
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import secrets
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

SCOPES={"skills:read","access:request","telemetry:write"}


class Blob(ctypes.Structure):
    _fields_=[("cbData",wintypes.DWORD),("pbData",ctypes.POINTER(ctypes.c_ubyte))]


def protect(data, decrypt=False):
    if os.name!="nt":
        raise RuntimeError("Windows DPAPI required for this deployment")
    buf=ctypes.create_string_buffer(data)
    source=Blob(len(data),ctypes.cast(buf,ctypes.POINTER(ctypes.c_ubyte)))
    dest=Blob()
    crypt=ctypes.WinDLL("crypt32",use_last_error=True)
    kernel=ctypes.WinDLL("kernel32",use_last_error=True)
    kernel.LocalFree.argtypes=[wintypes.HLOCAL]
    kernel.LocalFree.restype=wintypes.HLOCAL
    if decrypt:
        result=crypt.CryptUnprotectData(ctypes.byref(source),None,None,None,None,1,ctypes.byref(dest))
    else:
        result=crypt.CryptProtectData(ctypes.byref(source),"GatewayUsage",None,None,None,1,ctypes.byref(dest))
    if not result:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(dest.pbData,dest.cbData)
    finally:
        kernel.LocalFree(ctypes.cast(dest.pbData,wintypes.HLOCAL))


def same_origin(cfg,url):
    base=urllib.parse.urlsplit(cfg["gateway"])
    parsed=urllib.parse.urlsplit(url)
    if base.scheme!="https" or (parsed.scheme,parsed.netloc)!=(base.scheme,base.netloc):
        raise ValueError("OAuth endpoint origin mismatch")
    if parsed.username or parsed.password or parsed.fragment:
        raise ValueError("Invalid OAuth endpoint")
    return url


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):
        raise ValueError("Authenticated redirects are disabled")


def request(cfg,url,data=None,form=False,headers=None):
    same_origin(cfg,url)
    payload=None
    hdr=dict(headers or {})
    if data is not None:
        payload=(urllib.parse.urlencode(data).encode() if form else json.dumps(data).encode())
        hdr["Content-Type"]="application/x-www-form-urlencoded" if form else "application/json"
    with urllib.request.build_opener(NoRedirect()).open(
            urllib.request.Request(url,data=payload,headers=hdr),timeout=30) as response:
        return response.read().decode(),dict(response.headers)


def json_request(cfg,url,data=None,form=False):
    raw,_=request(cfg,url,data,form)
    return json.loads(raw)


def mcp(cfg,token,name,args):
    from portable_export import rpc_decode
    headers={"Authorization":"Bearer "+token,"Accept":"application/json, text/event-stream"}
    def post(body):
        raw,received=request(cfg,cfg["gateway"].rstrip("/")+"/mcp",body,headers=headers)
        # Header names are case insensitive.
        received={k.lower():v for k,v in received.items()}
        if received.get("mcp-session-id"):
            headers["Mcp-Session-Id"]=received["mcp-session-id"]
        return rpc_decode(raw,received.get("content-type","")) if raw.strip() else {}
    init=post({"jsonrpc":"2.0","id":1,"method":"initialize","params":{
        "protocolVersion":"2025-03-26","capabilities":{},
        "clientInfo":{"name":"platforma-numeric-usage","version":"0.2.0"}}})
    headers["MCP-Protocol-Version"]=init["result"]["protocolVersion"]
    post({"jsonrpc":"2.0","method":"notifications/initialized"})
    response=post({"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":name,"arguments":args}})
    if response.get("error") or response.get("result",{}).get("isError"):
        raise PermissionError("Gateway rejected numeric client")
    result=json.loads(next(x["text"] for x in response["result"]["content"] if x["type"]=="text"))
    if not result.get("ok"):
        raise RuntimeError("Gateway did not confirm operation")
    return result


def token_payload(cfg,response,previous=None):
    if set(response.get("scope","").split())!=SCOPES:
        raise PermissionError("Unexpected OAuth scope")
    if response.get("token_type","").lower()!="bearer":
        raise ValueError("Bearer token required")
    access=response.get("access_token")
    refresh=response.get("refresh_token") or (previous or {}).get("refresh_token")
    expires=response.get("expires_in")
    if not isinstance(access,str) or not access or not isinstance(refresh,str) or not refresh:
        raise ValueError("Missing OAuth credentials")
    if isinstance(expires,bool) or not isinstance(expires,(int,float)) or not 0<expires<=86400:
        raise ValueError("Invalid token expiry")
    return {**(previous or {}),"access_token":access,"refresh_token":refresh,
            "expires_at":time.time()+expires,"scope":" ".join(sorted(SCOPES)),
            "gateway":cfg["gateway"],"source_id":cfg["source_id"]}


def save(cfg,payload):
    target=Path(cfg["oauth_store"])
    target.parent.mkdir(parents=True,exist_ok=True)
    fd,name=tempfile.mkstemp(prefix=".oauth-",dir=target.parent)
    try:
        with os.fdopen(fd,"wb") as f:
            f.write(protect(json.dumps(payload).encode()));f.flush();os.fsync(f.fileno())
        os.replace(name,target)
    finally:
        Path(name).unlink(missing_ok=True)


def load(cfg):
    payload=json.loads(protect(Path(cfg["oauth_store"]).read_bytes(),decrypt=True))
    if payload.get("gateway")!=cfg["gateway"] or payload.get("source_id")!=cfg["source_id"]:
        raise PermissionError("OAuth store binding mismatch")
    return payload


@contextmanager
def lock(cfg):
    import msvcrt
    path=Path(cfg["oauth_store"]).with_suffix(".lock")
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open("a+b") as handle:
        if handle.tell()==0:handle.write(b"\0");handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(),msvcrt.LK_LOCK,1)
        try:yield
        finally:
            handle.seek(0);msvcrt.locking(handle.fileno(),msvcrt.LK_UNLCK,1)


def setup(cfg):
    # The metadata endpoints must belong to this exact HTTPS Gateway.
    metadata=json_request(cfg,cfg["gateway"].rstrip("/")+"/.well-known/oauth-authorization-server")
    if metadata.get("issuer")!=cfg["gateway"].rstrip("/"):
        raise ValueError("OAuth issuer mismatch")
    for name in ("authorization_endpoint","registration_endpoint","token_endpoint"):
        same_origin(cfg,metadata[name])
    done=threading.Event();callback={};state=secrets.token_urlsafe(32)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_GET(self):
            parsed=urllib.parse.urlsplit(self.path);query=urllib.parse.parse_qs(parsed.query)
            valid=parsed.path=="/callback" and query.get("state")==[state]
            if valid:
                if query.get("iss") and query["iss"]!=[metadata["issuer"]]:valid=False
                if metadata.get("authorization_response_iss_parameter_supported") and query.get("iss")!=[metadata["issuer"]]:valid=False
            if not valid:
                self.send_response(400);self.end_headers();return
            callback["code"]=query.get("code",[""])[0]
            self.send_response(200);self.send_header("Content-Type","text/plain; charset=utf-8");self.end_headers()
            self.wfile.write("Вход завершён. Можно закрыть страницу.".encode())
            done.set()
    server=HTTPServer(("127.0.0.1",0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        redirect="http://127.0.0.1:"+str(server.server_port)+"/callback"
        client=json_request(cfg,metadata["registration_endpoint"],{
            "client_name":"Platforma personal numeric usage","redirect_uris":[redirect],
            "grant_types":["authorization_code","refresh_token"],"response_types":["code"],
            "token_endpoint_auth_method":"none"})
        verifier=secrets.token_urlsafe(48)
        challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        url=metadata["authorization_endpoint"]+"?"+urllib.parse.urlencode({
            "response_type":"code","client_id":client["client_id"],"redirect_uri":redirect,
            "scope":" ".join(sorted(SCOPES)),"resource":cfg["gateway"].rstrip("/")+"/mcp",
            "state":state,"code_challenge":challenge,"code_challenge_method":"S256"})
        opened=webbrowser.open(url)
        if not opened:print(json.dumps({"authorize_url":url}),flush=True)
        if not done.wait(240) or not callback.get("code"):
            raise PermissionError("Personal OAuth was not completed")
        response=json_request(cfg,metadata["token_endpoint"],{
            "grant_type":"authorization_code","code":callback.pop("code"),
            "client_id":client["client_id"],"redirect_uri":redirect,"code_verifier":verifier,
            "resource":cfg["gateway"].rstrip("/")+"/mcp"},form=True)
        payload=token_payload(cfg,response)
        payload.update(client_id=client["client_id"],token_endpoint=metadata["token_endpoint"])
        actor=mcp(cfg,payload["access_token"],"gateway_access_profile",{})["actor"]
        if actor.get("subject")!=cfg["expected_subject"] or actor.get("groups") or set(actor.get("scopes",[]))!=SCOPES:
            raise PermissionError("Collector identity or scope mismatch")
        with lock(cfg):save(cfg,payload)
        return {"ok":True,"personal_oauth":True,"scope":sorted(SCOPES),"store":"Windows DPAPI current user"}
    finally:
        server.shutdown();thread.join();server.server_close()


def refresh(cfg,payload):
    response=json_request(cfg,payload["token_endpoint"],{
        "grant_type":"refresh_token","refresh_token":payload["refresh_token"],
        "client_id":payload["client_id"],"resource":cfg["gateway"].rstrip("/")+"/mcp"},form=True)
    result=token_payload(cfg,response,payload)
    save(cfg,result)
    return result


def call(cfg,command):
    from portable_export import normalize
    args=command.get("arguments")
    if command.get("name")!="gateway_usage_import" or not isinstance(args,dict):
        raise ValueError("Numeric import only")
    if set(args)!={"event_id","project","model","usage_json"} or args["project"]!=cfg["project"]:
        raise ValueError("Unexpected export payload")
    # Rebuild the numeric object; arbitrary fields and conversation text do not pass.
    args={**args,"usage_json":json.dumps(normalize(json.loads(args["usage_json"])))}
    with lock(cfg):
        payload=load(cfg)
        if payload["expires_at"]<=time.time()+60:payload=refresh(cfg,payload)
        try:
            return mcp(cfg,payload["access_token"],"gateway_usage_import",args)
        except urllib.error.HTTPError as exc:
            if exc.code!=401:raise
            payload=refresh(cfg,payload)
            return mcp(cfg,payload["access_token"],"gateway_usage_import",args)


if __name__=="__main__":
    parser=argparse.ArgumentParser();parser.add_argument("--config",required=True)
    parser.add_argument("--setup",action="store_true");args=parser.parse_args()
    try:
        cfg=json.loads(Path(args.config).read_text(encoding="utf-8"))
        result=setup(cfg) if args.setup else call(cfg,json.load(sys.stdin))
        print(json.dumps(result))
    except Exception as exc:
        print(json.dumps({"ok":False,"error_type":type(exc).__name__}))
        raise SystemExit(1)

