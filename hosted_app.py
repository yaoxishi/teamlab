"""TeamLab prototype UI with private browser workspaces."""
import os
import streamlit as st
from convolab.presentation import apply_style
from convolab.hosted import auth,component,runtime
from convolab.hosted.interface import welcome,render_interface
from convolab.hosted.settings import HostedError,HostedSettings
from convolab.hosted.workspace import (WIDGET_PREFIX as P,bind_workspace,
                                        clear_key_widgets,clear_private_state)

st.set_page_config(page_title='TeamLab prototype',layout='wide')
apply_style()
st.title('TeamLab prototype')
welcome()
try:
    settings=HostedSettings.from_environment(os.environ)
    message=component.browser_auth(settings)
    identity=auth.AuthVerifier(settings).verify(message)
    workspace=bind_workspace(st.session_state,identity)
except HostedError as error:
    clear_private_state(st.session_state)
    st.warning(str(error));st.stop()
if st.session_state.pop(P+'clear_key_widgets',False):clear_key_widgets(st.session_state)
if st.session_state.get(P+'key_revision')!=workspace.revision:
    clear_key_widgets(st.session_state);st.session_state[P+'key_revision']=workspace.revision
runner=None
try:
    runner=runtime.execution_runtime(settings)
    if runner:runner.attach(identity,workspace);runner.admission.reconcile()
except HostedError as error:st.warning(str(error))
st.success('Your private workspace is connected.')
render_interface(settings,message,identity,workspace,runner)
