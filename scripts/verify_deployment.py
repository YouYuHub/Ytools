"""Read-only deployment checks: current assets, capabilities and anonymous denials.

No login, credentials, account changes, task changes or service restarts.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

ROOT=Path(__file__).resolve().parents[1]


def validate_origin(value):
    parts=urllib.parse.urlsplit(value)
    if (parts.scheme not in {"http","https"} or not parts.hostname or parts.username or parts.password
            or parts.path not in {"","/"} or parts.query or parts.fragment):
        raise ValueError("Use an origin only, without credentials, path, query or fragment")
    if parts.scheme!="https" and parts.hostname not in {"127.0.0.1","localhost","::1"}:
        raise ValueError("Remote deployment verification requires HTTPS")
    return value.rstrip("/")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):
        return None


def request(origin,path,method="GET"):
    req=urllib.request.Request(origin+path,method=method,data=b"{}" if method=="POST" else None,
        headers={"Origin":origin,"Content-Type":"application/json"})
    try:
        response=urllib.request.build_opener(NoRedirect).open(req,timeout=10)
    except urllib.error.HTTPError as error:
        response=error
    with response:
        data=response.read(8*1024*1024+1)
        if len(data)>8*1024*1024:
            raise ValueError("Unexpectedly large public check response")
        return response.code,data,response.headers


def assets(root=ROOT):
    sys.path.insert(0,str(root))
    from util.ui_pages import PAGES
    found={}
    for page in PAGES:
        html=(root/"H5"/page.file).read_text(encoding="utf-8")
        for relative,digest in re.findall(r'(?:src|href)="((?:js/[^"?]+\.js|(?:style/|js/)[^"?]+\.css|login\.(?:js|css)))\?v=([a-f0-9]{12})"',html):
            found[relative]=digest
    for owner,relative in (("js/history_parse_worker.js","history_parser.js"),("js/history_parse_async.js","js/history_parse_worker.js")):
        source=(root/"H5"/owner).read_text(encoding="utf-8")
        match=re.search(re.escape(relative)+r'\?v=([a-f0-9]{12})',source)
        if not match:
            raise ValueError("Unversioned worker dependency: "+owner)
        found["js/"+relative if "/" not in relative else relative]=match.group(1)
    for relative,digest in found.items():
        if hashlib.sha256((root/"H5"/relative).read_bytes()).hexdigest()[:12]!=digest:
            raise ValueError("Local asset hash mismatch: "+relative)
    return found


def verify(origin,*,root=ROOT,fetch=request):
    origin=validate_origin(origin)
    status,data,_=fetch(origin,"/auth/status")
    if status!=200:
        raise ValueError("Health status: "+str(status))
    health=json.loads(data)
    if not all(health.get(name) is True for name in ("initialized","account_panel","account_sharing","history_paging")):
        raise ValueError("Expected release capabilities are not enabled")
    checks=[]
    for path,method in (("/chat_history/summaries","GET"),("/chat_history/page?session_id=anonymous-probe","GET"),
                        ("/chat_history/event?session_id=anonymous-probe&round_no=1&event_index=0&revision="+"0"*64,"GET"),
                        ("/chat_history/file?session_id=anonymous-probe","GET"),("/chat_history/export_zip?session_ids=anonymous-probe","GET"),
                        ("/chat_history/import_preview","POST"),("/chat_history/import_package","POST"),
                        ("/auth/mfa/devices","GET"),("/user/preferences","GET")):
        code,_,headers=fetch(origin,path,method)
        if code!=401 or "no-store" not in headers.get("Cache-Control",""):
            raise ValueError("Anonymous boundary check failed: "+path.split("?")[0]+" ("+str(code)+")")
        checks.append(path.split("?")[0])
    for page in ("/ui/","/ui/account.html","/ui/editor.html"):
        code,_,headers=fetch(origin,page)
        if code!=303 or headers.get("Location")!="/ui/login.html":
            raise ValueError("Anonymous page check failed: "+page)
    versions=assets(root)
    for relative,digest in versions.items():
        code,data,_=fetch(origin,"/ui/"+relative+"?v="+digest)
        if code!=200 or data!=(root/"H5"/relative).read_bytes():
            raise ValueError("Served asset mismatch: "+relative)
    return {"passed":True,"origin":origin,"anonymous_checks":len(checks),"private_page_checks":3,"assets_checked":len(versions)}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin",required=True)
    parser.add_argument("--release-report",type=Path)
    parser.add_argument("--output",type=Path)
    args=parser.parse_args()
    try:
        if args.release_report:
            from release_check import source_digest
            saved=json.loads(args.release_report.read_text(encoding="utf-8"))
            if not saved.get("passed") or not saved.get("full_validation") or saved.get("source_digest")!=source_digest():
                raise ValueError("Release validation report is failed, focused, or stale")
        result=verify(args.origin)
    except (OSError,ValueError) as error:
        result={"passed":False,"error":str(error)}
    if args.output:
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(result,ensure_ascii=True,indent=2),encoding="utf-8")
    print(json.dumps(result,ensure_ascii=True))
    return 0 if result["passed"] else 1


if __name__=="__main__":
    raise SystemExit(main())
