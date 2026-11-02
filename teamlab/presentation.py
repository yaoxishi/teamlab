"""Shared visual hierarchy; no study or widget-state changes."""
import streamlit as st


INTRODUCTION = (
    'TeamLab simulates independent participants in two- or three-person interaction '
    'studies. It records conversations and simulated self-reports before, during and '
    'after the interaction, according to your study settings. Use it to explore team '
    'dynamics and develop hypotheses; its outputs do not replace evidence from human studies.'
)


def apply_style():
    # Use bundled/system fonts and relative sizes: no external font requests.
    # Native help buttons retain their accessible names and keyboard behavior.
    st.html('''<style>
    [data-testid="stMainBlockContainer"] {
        max-width: 76rem; padding-top: 4.5rem; padding-bottom: 4rem;
    }
    [data-testid="stMain"] h1 { font-size: 2.25rem; line-height: 1.2;
        font-weight: 650; letter-spacing: -0.025em; padding-bottom: .5rem; }
    [data-testid="stMain"] h2 { font-size: 1.5rem; line-height: 1.3;
        font-weight: 600; padding-top: 1.25rem; padding-bottom: .4rem; }
    [data-testid="stMain"] h3 { font-size: 1.125rem; line-height: 1.4; font-weight: 600; }
    [data-testid="stMarkdownContainer"] p,
    [data-testid="stMarkdownContainer"] li { line-height: 1.6; }
    [data-testid="stWidgetLabel"] { width: fit-content; max-width: 100%;
        justify-content: flex-start; gap: .35rem; }
    [data-testid="stWidgetLabel"] > div { flex: 0 1 auto; width: auto; }
    [data-testid="stWidgetLabel"] p { font-size: .9375rem; font-weight: 500; }
    [data-testid="stWidgetLabel"] [data-testid="stTooltipIcon"] {
        flex: 0 0 auto; margin-left: 0;
    }
    [data-testid="stCaptionContainer"] p { font-size: .875rem; line-height: 1.5; }
    [data-testid="stExpander"] details summary p { font-size: 1rem; font-weight: 550; }
    [data-testid="stText"] { white-space: pre-wrap; overflow-wrap: anywhere; }
    @media (max-width: 640px) {
        [data-testid="stMainBlockContainer"] { padding: 4.5rem 1rem 3rem; }
        [data-testid="stMain"] h1 { font-size: 1.875rem; }
    }
    </style>''')


def introduction():
    with st.container(key='platform_introduction'):
        st.markdown(INTRODUCTION)


def study_setup_heading():
    st.header('Setup a study')
    st.write('Choose one way to begin: create a new study, load an example for a demonstration, or import a saved YAML study file.')
    st.caption('Please save your current setup before switching to another study. Creating a new study, loading an example or importing a valid study file replaces your previous study settings and clears entered API keys. These actions do not start a study execution.')
