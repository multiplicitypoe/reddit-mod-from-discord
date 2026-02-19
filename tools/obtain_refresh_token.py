from __future__ import annotations

import os
import random
import socket
import sys
from urllib.parse import urlparse

import praw
from dotenv import load_dotenv


def _receive_connection(host: str, port: int) -> socket.socket:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((host, port))
    server.listen(1)
    client = server.accept()[0]
    server.close()
    return client


def _send_message(client: socket.socket, message: str) -> None:
    print(message)
    client.send(f"HTTP/1.1 200 OK\r\n\r\n{message}".encode("utf-8"))
    client.close()


def main() -> int:
    load_dotenv()
    client_id = os.getenv("REDDIT_CLIENT_ID")
    client_secret = os.getenv("REDDIT_CLIENT_SECRET")
    user_agent = os.getenv("REDDIT_USER_AGENT") or "reddit-mod-from-discord/0.1"
    scopes_raw = os.getenv("REDDIT_SCOPES")
    redirect_uri = os.getenv("REDDIT_REDIRECT_URI") or "http://localhost:8080"

    if not client_id or not client_secret:
        print("REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET must be set in env.")
        return 2

    default_scopes = ["modlog", "modposts", "modmail", "modcontributors", "read", "identity"]

    if scopes_raw:
        scopes = [scope.strip() for scope in scopes_raw.split(",") if scope.strip()]
    else:
        scopes_input = input(
            "Enter comma-separated scopes (press Enter or 'y' for default: "
            "modlog,modposts,modmail,modcontributors,read,identity), or '*' for all: "
        ).strip()
        if not scopes_input or scopes_input.lower() in {"y", "yes"}:
            scopes = list(default_scopes)
        else:
            scopes = [scope.strip() for scope in scopes_input.split(",") if scope.strip()]

    parsed = urlparse(redirect_uri)
    host = parsed.hostname or "localhost"
    port = parsed.port or 8080
    if parsed.scheme not in {"http", "https"}:
        print("REDDIT_REDIRECT_URI must be an http(s) URL")
        return 2

    reddit = praw.Reddit(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        user_agent=user_agent,
    )

    state = str(random.randint(0, 65000))
    url = reddit.auth.url(duration="permanent", scopes=scopes, state=state)
    print("Open this URL in your browser and click Allow:")
    print(url)

    client = _receive_connection(host, port)
    data = client.recv(1024).decode("utf-8")
    try:
        param_tokens = data.split(" ", 2)[1].split("?", 1)[1].split("&")
        params = {key: value for (key, value) in [token.split("=") for token in param_tokens]}
    except Exception:
        _send_message(client, "Failed to parse redirect parameters. Try again.")
        return 1

    if state != params.get("state"):
        _send_message(client, f"State mismatch. Expected: {state} Received: {params.get('state')}")
        return 1
    if "error" in params:
        _send_message(client, params["error"])
        return 1

    refresh_token = reddit.auth.authorize(params["code"])
    _send_message(client, f"Refresh token: {refresh_token}")
    print("\nSet this in your .env:")
    print(f"REDDIT_REFRESH_TOKEN={refresh_token}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
