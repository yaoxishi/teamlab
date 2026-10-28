"""Private browser fetch without Streamlit media or long-lived bearer URLs."""
from functools import lru_cache
import hashlib
from importlib.resources import files
import json
from .auth import canonical_uuid
from .settings import HostedError

@lru_cache(maxsize=1)
def download_renderer():
    import streamlit as st
    root=files('convolab.hosted').joinpath('assets')
    js=root.joinpath('download.js').read_bytes()
    expected=json.loads(root.joinpath('download-manifest.json').read_text())
    if hashlib.sha256(js).hexdigest()!=expected.get('sha256'):
        raise HostedError('The private download component failed its integrity check.')
    return st.components.v2.component('convolab_private_download_v1',
        html='<button type="button">Download research ZIP</button><p role="status"></p>',js=js.decode())

def browser_download(settings, identity, execution_id):
    canonical_uuid(execution_id)
    download_renderer()(key='convolab_download_'+execution_id, data={
        'endpoint':settings.url+'/functions/v1/private-download','publicKey':settings.publishable_key,
        'token':identity.access_token.get_secret_value(),'execution':execution_id})
