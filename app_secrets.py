"""讀 Streamlit secrets。沒有 secrets.toml 時回傳預設值，不印出內容。"""


def read_secret(default="", *path):
    try:
        import streamlit as st
    except ModuleNotFoundError:
        # API 映像沒有 Streamlit。沒有 secrets.toml 就用環境變數，不要把缺套件變成抓取失敗。
        return default

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
