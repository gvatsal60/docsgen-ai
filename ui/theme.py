"""Light styling on top of the Streamlit theme."""

import streamlit as st

CSS = """
<style>
section[data-testid="stSidebar"] .stRadio label { padding: 2px 0; }
div[data-testid="stMetricValue"] { font-size: 1.35rem; }
.dg-hero {
  padding: 1.1rem 1.3rem; border-radius: 12px; color: #fff; margin-bottom: 1rem;
  background: linear-gradient(120deg,#01305C 0%,#124E8C 100%);
}
.dg-hero h1 { color: #fff; font-size: 1.7rem; margin: 0 0 .25rem 0; }
.dg-hero p { color: #D6E2F0; margin: 0; }
.dg-step { font-size: .9rem; color: #5F6773; }
.dg-mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: .85rem; }
</style>
"""


def inject_css() -> None:
    st.markdown(CSS, unsafe_allow_html=True)
