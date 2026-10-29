"""讀 Streamlit secrets。沒有 secrets.toml 時回傳預設值，不印出內容。"""


def read_secret(default="", *path):
    import streamlit as st

    try:
        node = st.secrets
    except Exception as exc:
        if type(exc).__name__ == "StreamlitSecretNotFoundError":
            return default
        raise
    try:
        for key in path:
            if node is None:
                return default
            getter = getattr(node, "get", None)
            if getter is not None:
                node = getter(key, None)
            else:
                node = node[key] if key in node else None
        if node is None or node == "":
            return default
        return node
    except Exception as exc:
        if type(exc).__name__ in {"StreamlitSecretNotFoundError", "KeyError", "FileNotFoundError"}:
            return default
        raise
