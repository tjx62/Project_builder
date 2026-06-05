import streamlit as st

st.set_page_config(page_title="Adaptive Code Builder", page_icon="🏗️")
st.markdown('<style>[data-testid="stSidebarNav"]{display:none}</style>', unsafe_allow_html=True)
st.switch_page("app.py")
