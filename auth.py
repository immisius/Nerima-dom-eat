import secrets
from typing import Optional

# token → {"user_key": str, "company_code": str, "user_id_str": str, "password": str}
# 認証情報はメモリのみ。サーバ再起動で消える。ファイル・DBには一切保存しない。
_sessions: dict[str, dict] = {}


def create_session(user_key: str, company_code: str, user_id_str: str, password: str, token: str = None) -> str:
    if token is None:
        token = secrets.token_urlsafe(32)
    _sessions[token] = {
        "user_key": user_key,
        "company_code": company_code,
        "user_id_str": user_id_str,
        "password": password,
    }
    return token


def get_session(token: str) -> Optional[dict]:
    return _sessions.get(token)


def delete_session(token: str):
    _sessions.pop(token, None)
