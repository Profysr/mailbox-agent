#!/usr/bin/env python3
"""One-time OAuth setup for gmail-inbox-manager.

Creates the authorized-user token file that gmail_inbox_manager.py reads at
run time. Requests ONLY the gmail.modify scope: the manager adds labels,
marks read and archives — it never sends mail, so gmail.send is deliberately
NOT requested.

Usage (opens a browser and asks you to sign in + consent):
    python3 setup_token.py --client-secret ./client_secret.json \
        --token-out ./google_token.json

Headless machine (no browser):
    python3 setup_token.py --client-secret ./client_secret.json \
        --token-out ./google_token.json --headless
    -> prints a URL; open it yourself, sign in, paste the code back here.

Prerequisite: pip install google-auth-oauthlib (only needed for this script,
not for the manager itself).
"""
import argparse
import json
import os
import sys
from pathlib import Path

# gmail.modify is the whole permission surface of this product. Do not add
# gmail.send (or anything broader) "just in case".
SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]


def main():
    ap = argparse.ArgumentParser(
        description="Create the OAuth token for gmail-inbox-manager "
                    "(scope: gmail.modify only — never gmail.send).")
    ap.add_argument("--client-secret", required=True,
                    help="OAuth client JSON downloaded from Google Cloud "
                         "(Desktop app type).")
    ap.add_argument("--token-out", default="./google_token.json",
                    help="where to write the token (default "
                         "./google_token.json).")
    ap.add_argument("--headless", action="store_true",
                    help="no local browser: print the consent URL, then "
                         "paste the authorization code back.")
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
    if args.headless:
        auth_url, _ = flow.authorization_url(access_type="offline",
                                             prompt="consent")
        print("1. Open this URL in any browser and sign in:\n")
        print(f"   {auth_url}\n")
        print("2. Approve the consent screen (offline access is required "
              "so the token can refresh unattended).")
        code = input("3. Paste the authorization code here: ").strip()
        flow.fetch_token(code=code)
        creds = flow.credentials
    else:
        creds = flow.run_local_server(port=0, access_type="offline",
                                      prompt="consent")

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


if __name__ == "__main__":
    main()
