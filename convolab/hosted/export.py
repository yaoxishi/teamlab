"""Authorized browser-local exports, with no shared Streamlit media URL."""
import base64
import hashlib
from importlib.resources import files
import json

from convolab.llm import Redactor
from .auth import VerifiedIdentity
from .settings import HostedError
from .limits import MAX_BUNDLE_BYTES

def export_renderer():
    import streamlit as st
    root=files('convolab.hosted').joinpath('assets')
    js=root.joinpath('export.js').read_bytes()
    manifest=json.loads(root.joinpath('export-manifest.json').read_text())
    if manifest != {'version':1,'sha256':hashlib.sha256(js).hexdigest()}:
        raise HostedError('The browser export component failed its integrity check.')
    return st.components.v2.component('convolab_browser_export_v1',
        html='<a role="button"></a><p role="status"></p>',js=js.decode())

def browser_export(identity, data, filename, label, *, keys=(), disabled=False, key):
    if not isinstance(identity, VerifiedIdentity):
        raise HostedError('Connect your private workspace before exporting.')
    if filename not in {'study.yaml','items.csv','personas_template.csv','anes2016_personas.csv'}:
        raise HostedError('The requested export is unavailable.')
    if isinstance(data,str):
        data=Redactor(keys).text(data).encode('utf-8')
    elif keys:
        data=Redactor(keys).text(data.decode('utf-8')).encode('utf-8')
    if not isinstance(data,bytes) or len(data)>MAX_BUNDLE_BYTES:
        raise HostedError('The export exceeds the website size limit.')
    export_renderer()(key=key,data={'content':base64.b64encode(data).decode('ascii'),
        'filename':filename,'label':label,'disabled':bool(disabled)})
