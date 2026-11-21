"""YouTube Data API v3: one-time OAuth login + resumable Shorts uploads."""
from __future__ import annotations
import os
import random
import time

SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/youtube.readonly"]
RETRY_STATUS = {500, 502, 503, 504}


class AuthError(RuntimeError):
    pass


def login(client_secret: str, token: str):
    """Open the browser once, save a refreshable token. Returns the channel title."""
    from google_auth_oauthlib.flow import InstalledAppFlow
    flow = InstalledAppFlow.from_client_secrets_file(client_secret, SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")
    _save(creds, token)
    return channel_title(service(client_secret, token))


def _save(creds, token: str):
    with open(token, "w") as f:
        f.write(creds.to_json())
    os.chmod(token, 0o600)  # the token can upload to your channel: keep it private


def service(client_secret: str, token: str):
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    if not os.path.exists(token):
        raise AuthError("not logged in: run  .venv/bin/python upload.py login")
    creds = Credentials.from_authorized_user_file(token, SCOPES)
    if not creds.valid:
        try:
            creds.refresh(Request())
        except RefreshError as e:
            raise AuthError("login expired (Google expires test-mode logins after 7 days): "
                            "run  .venv/bin/python upload.py login") from e
        _save(creds, token)
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def channel_title(yt) -> str:
    r = yt.channels().list(part="snippet", mine=True).execute()
    items = r.get("items", [])
    return items[0]["snippet"]["title"] if items else "(no channel on this account)"


def upload(yt, path: str, title: str, description: str, tags, privacy: str = "private",
           publish_at: str | None = None, log=print) -> str:
    """Upload one video, resumable with retries. Returns the YouTube video id."""
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload
    status = {"privacyStatus": "private" if publish_at else privacy,
              "selfDeclaredMadeForKids": False}
    if publish_at:
        status["publishAt"] = publish_at  # YouTube flips it public at this time
    body = {"snippet": {"title": title[:100], "description": description[:5000],
                        "tags": list(tags)[:30], "categoryId": "20"},  # 20 = Gaming
            "status": status}
    media = MediaFileUpload(path, mimetype="video/mp4", chunksize=8 * 1024 * 1024, resumable=True)
    req = yt.videos().insert(part="snippet,status", body=body, media_body=media)
    resp, tries = None, 0
    while resp is None:
        try:
            st, resp = req.next_chunk()
            if st:
                log(f"    {int(st.progress() * 100)}%")
        except HttpError as e:
            if e.resp.status not in RETRY_STATUS or tries >= 5:
                raise
            tries += 1
            wait = 2 ** tries + random.random()
            log(f"    server error {e.resp.status}, retry {tries} in {wait:.0f}s")
            time.sleep(wait)
        except (ConnectionError, TimeoutError, OSError) as e:
            if tries >= 5:
                raise
            tries += 1
            wait = 2 ** tries + random.random()
            log(f"    network error ({e}), retry {tries} in {wait:.0f}s")
            time.sleep(wait)
    return resp["id"]
