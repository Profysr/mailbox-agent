#!/usr/bin/env python3
"""One-time OAuth setup for gmail-inbox-manager.

Creates the authorized-user token file that gmail_inbox_manager.py reads at
run time. Requests ONLY the gmail.modify scope: the manager adds labels,
marks read and archives — it never sends mail, so gmail.send is deliberately
NOT requested.

Usage (opens a browser if one exists, otherwise prints the URL only):
    python3 setup_token.py --client-secret ./client_secret.json \
        --token-out ./google_token.json

How it works (loopback consent server — no local browser needed):
    python3 setup_token.py
    defaults: --client-secret <comms profile>/google_client_secret.json
              --token-out    ./google_token.json
              --port         9090

The script starts a loopback consent server on localhost (default
http://localhost:9090/) and prints the full Google consent URL, redirect_uri
included. If this machine has no display, sign in from your laptop through a
port forward:

    laptop:   ssh -L 9090:localhost:9090 <you>@<this-server>
    browser:  open the printed URL (it contains
              redirect_uri=http://localhost:9090/) and approve consent

The token is written on this server as soon as Google redirects back to the
loopback URL. It is then verified with one real Gmail API call
(users.getProfile): the script prints VERIFIED or FAILED and exits non-zero
on failure.

Prerequisite: pip install google-auth-oauthlib (only needed for this script,
not for the manager itself).
"""
import argparse
import json
import os
import socket
import sys
import webbrowser
from pathlib import Path

# gmail.modify is the whole permission surface of this product. Do not add
# gmail.send (or anything broader) "just in case".
SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]

# This host is headless: the client secret lives with the comms profile, so
# the flag can be omitted. The flag still wins when it is given.
DEFAULT_CLIENT_SECRET = "/home/ubuntu/.hermes/profiles/comms/google_client_secret.json"

# Fixed port so the ssh -L instruction is always the same command.
DEFAULT_PORT = 9090


def is_headless() -> bool:
    """True when no local browser can be opened (headless VM or container)."""
    if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
        return False
    try:
        webbrowser.get()
        return False
    except webbrowser.Error:
        return True


def resolve_port(want: int) -> int:
    """Return a bindable port. 0 means 'pick a free one', never 'random'."""
    if want:
        return want
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def print_onboarding(port: int, auth_url: str, headless: bool) -> None:
    """Print the full consent URL and, when headless, the tunnel command."""
    print("=" * 72)
    print("1. Open this URL in any browser (laptop is fine) and sign in:")
    print()
    print(f"   {auth_url}")
    print()
    print("2. Approve the consent screen. Offline access is required so the")
    print("   token can refresh unattended.")
    if headless:
        print()
        print("   This server has no browser, so open the URL from your laptop")
        print(f"   through a port forward (port {port}):")
        print()
        print(f"       ssh -L {port}:localhost:{port} <you>@<this-server>")
        print()
        print("   then open the URL above in the laptop browser. The consent")
        print("   redirect lands on this server and the token is written here.")
    print("=" * 72)


def verify_token(creds) -> bool:
    """One real Gmail API call (users.getProfile). Never logs the token."""
    try:
        from google.auth.transport.requests import AuthorizedSession

        session = AuthorizedSession(creds)
        resp = session.get(
            "https://gmail.googleapis.com/gmail/v1/users/me/profile")
        if resp.status_code == 200:
            return True
        print(f"   users.getProfile failed: HTTP {resp.status_code} "
              f"{resp.text[:200]}")
    except Exception as exc:  # network, transport, bad creds — all non-fatal
        print(f"   users.getProfile raised: {type(exc).__name__}: {exc}")
    return False


def main():
    ap = argparse.ArgumentParser(
        description="Create the OAuth token for gmail-inbox-manager "
                    "(scope: gmail.modify only — never gmail.send).")
    ap.add_argument("--client-secret", default=DEFAULT_CLIENT_SECRET,
                    help="OAuth client JSON downloaded from Google Cloud "
                         "(Desktop app type). Default: "
                         f"{DEFAULT_CLIENT_SECRET}")
    ap.add_argument("--token-out", default="./google_token.json",
                    help="where to write the token (default "
                         "./google_token.json).")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT,
                    help=f"localhost port for the consent redirect "
                         f"(default {DEFAULT_PORT}; 0 = pick a free port). "
                         f"With a laptop-side `ssh -L {DEFAULT_PORT}:"
                         f"localhost:{DEFAULT_PORT}` the consent screen "
                         f"opens on the laptop and the token lands here. "
                         f"Never requires a local browser.")
    args = ap.parse_args()

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        sys.exit("ERROR: missing dependency.\n"
                 "  python3 -m pip install google-auth-oauthlib")

    secret = Path(args.client_secret).expanduser()
    if not secret.exists():
        sys.exit(f"ERROR: client secret not found: {secret}\n"
                 f"Download it from Google Cloud -> APIs & Services -> "
                 f"Credentials (OAuth client ID, Application type: Desktop app).")

    flow = InstalledAppFlow.from_client_secrets_file(str(secret), SCOPES)

    # Loopback only. The console (paste-a-code) flow is gone: Google rejects
    # a redirect_uri-less authorization URL with invalid_request.
    port = resolve_port(args.port)
    flow.redirect_uri = f"http://localhost:{port}/"
    auth_url, _ = flow.authorization_url(access_type="offline",
                                         prompt="consent")
    if "redirect_uri=" not in auth_url:
        sys.exit("ERROR: consent URL is missing redirect_uri — Google will "
                 "reject it.\n"
                 f"  URL: {auth_url}")

    headless = is_headless()
    print_onboarding(port, auth_url, headless)
    if not headless:
        try:
            webbrowser.open(auth_url)  # convenience only; never required
        except Exception as exc:
            print(f"(no browser opened: {exc} — use the URL above)")

    creds = flow.run_local_server(port=port, open_browser=False,
                                  authorization_prompt_message=None,
                                  access_type="offline", prompt="consent")

    if not creds.valid:
        sys.exit("ERROR: authorization did not produce a valid token.")

    out = Path(args.token_out).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = json.loads(creds.to_json())
    payload["scopes"] = SCOPES
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.chmod(out, 0o600)
    print(f"\nToken written to {out} (mode 600).")
    print(f"Scopes granted: {', '.join(SCOPES)}")
    print("gmail.send was NOT requested — this product never sends mail.")
    print("Keep this file private; it contains a refresh token.")

    # Prove the token works against the live API before reporting success.
    if verify_token(creds):
        print("\nVERIFIED: the new token works against the Gmail API.")
    else:
        print(f"\nFAILED: the token at {out} did not verify.")
        print("The file was still written (you can retry it), but the API")
        print("call failed — check the error above and re-run setup.")
        sys.exit(1)


if __name__ == "__main__":
    main()
