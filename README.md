# gmail-inbox-manager

A configurable, safety-first Gmail triage tool: classify inbox mail against a
ruleset, apply Gmail labels, mark read, and archive low-priority mail — never
send, reply, trash, or delete. Productized from the uc09 consolidated inbox
manager so a third party can set it up on their own machine with their own
Gmail account.

## Safety invariants (never relaxed, not configurable)

- Never sends, replies, trashes, or deletes a message.
- The only mailbox mutations are: `addLabelIds [label]`,
  `removeLabelIds [UNREAD]`, and `removeLabelIds [INBOX]` (only for rules
  explicitly marked `archive: true`).
- An unknown/unmatched sender lands on the trailing "Unsorted" catch-all rule
  (`archive: false`) — it is labelled and KEPT in the inbox. Unknown mail is
  never archived.
- Chunked processing: page size and pass size bounded (<= 25), batch modify
  calls carry at most `modify_chunk` (<= 50) ids.
- At least `api_sleep_seconds` (>= 1.5) between Gmail API calls.
- Exponential backoff with Retry-After on 429/403/5xx; hard stop after
  `max_strikes` consecutive failures.
- Per-message failure isolation: one bad message never kills a pass.
- Checkpoint state written after every pass (crash-safe resume).
- Append-only audit log; one JSON line per action.

## Requirements

- Linux (or WSL) with Python 3.10 or newer
- A Gmail account
- Python packages: google-api-python-client, google-auth, and
  google-auth-oauthlib (the last only for the one-time token setup)

    python3 -m pip install --upgrade google-api-python-client google-auth google-auth-oauthlib

Check:

    python3 -c "import googleapiclient, google.oauth2.credentials; print('deps ok')"

On Windows the same commands work in PowerShell; run the Python script
directly (via Task Scheduler) instead of the sh-based cron launcher.

## Step 1 — Get the code

    git clone <your-repo-url> gmail-inbox-manager
    cd gmail-inbox-manager

Or copy this directory wherever you want it to live. Nothing depends on a
fixed path — every path comes from config.json.

## Step 2 — Google Cloud project, OAuth client, and token

1. Open https://console.cloud.google.com/ and create (or pick) a project.
2. APIs & Services → Library → search *Gmail API* → Enable.
3. APIs & Services → OAuth consent screen:
   - choose External,
   - fill in app name and support email,
   - add your own Gmail address under Test users (an unverified External app
     can only authorize test users),
   - leave the app in Testing status.
4. APIs & Services → Credentials → Create Credentials → OAuth client ID →
   Application type Desktop app → Create. Click Download JSON and save it next
   to the package as client_secret.json:

    mv ~/Downloads/client_secret*.json ./client_secret.json

5. Create the token (opens a browser; asks you to sign in and consent):

    python3 setup_token.py --client-secret ./client_secret.json --token-out ./google_token.json

   Headless box: use --headless, open the printed URL yourself, and paste the
   code back:

    python3 setup_token.py --client-secret ./client_secret.json --token-out ./google_token.json --headless

   The token is written with mode 600, contains a refresh token (one-time
   setup), and requests scope gmail.modify only — gmail.send is deliberately
   NOT requested because the manager never sends mail.

## Step 3 — Config

    cp config.example.json config.json

Edit config.json. The only values you must change are the paths and the labels
you approve; everything else has a working default. Relative paths resolve
against the directory holding config.json, so the package stays movable.

The script reads config from the $CONFIG environment variable, falling back to
./config.json:

    CONFIG=/etc/gmail-inbox-manager/config.json python3 gmail_inbox_manager.py --dry-run

| key | what it is |
|-----|------------|
| token_path | the token from Step 2 (default ./google_token.json) |
| client_secret_path | the OAuth client secret (setup_token.py only) |
| rules_path | the ruleset (default ./rules.json) |
| state_dir | where state files live (default ./state) |
| label_map_path | label name -> id cache (default ./state/labels.json) |
| processed_ids_path | processed-message ids for idempotency |
| contacts_path / followups_path / attachments_state_path | optional state |
| log_dir / run_log / audit_log | logs; audit_log is append-only JSON lines |
| attachment_dest | where extracted attachments are written |
| max_messages | inbox scan cap per run (default 300; use 5000+ for backfill) |
| page_size / pass_size | must stay <= 25 |
| batch_size / modify_chunk | modify_chunk <= 50 ids per batchModify call |
| api_sleep_seconds | pause between API calls — must stay >= 1.5 |
| backoff_max_retries / backoff_max_seconds / max_strikes | backoff behaviour |
| digest_max_items / digest_max_chars / summary_max / summary_chars | digest limits |
| enable_followups / enable_contacts / enable_attachments | optional stages on/off |
| fallback_label | label for unmatched senders (default Unsorted) |
| approved_labels | THE approval gate — label names the script may create (default []) |
| delivery.webhook_url | leave empty if your cron delivers stdout directly |

## Step 4 — Rules and policy

    cp rules.template.json rules.json

Edit rules.json: replace the EXAMPLE-* placeholder senders/subjects with your
own. Matching semantics (documented inside rules.template.json):
case-insensitive substring on the full From header and on the Subject; within
one rule ANY from_contains OR ANY subject_contains entry matches; rules are
evaluated top to bottom and the FIRST match wins; each rule carries an
`archive` flag (true = label + remove from inbox, false = keep in inbox).

Two rules you must not break:

- The LAST rule is the catch-all (empty match lists) with `archive: false` —
  the script refuses to start otherwise. Unknown senders land here and stay in
  the inbox.
- Unknown senders are never archived.

Read POLICY.example.md and fill in the prompts ("write your own" sections)
before your first live run — it explains which categories may leave the inbox
and why the catch-all rule must never archive.

Validate the ruleset without touching the mailbox:

    python3 -c "import json; d=json.load(open('rules.json')); print(len(d['rules']), 'rules')"

## Step 5 — Dry-run (review counts + planned labels)

    python3 gmail_inbox_manager.py --dry-run --max 25

This mutates NOTHING and writes no state. The digest prints per-category counts
and a dedicated LABELS section:

- EXISTING labels — already in your mailbox
- NEW labels this run WOULD create — not yet in your mailbox

Review the counts and the planned-new-label list before going live.

## Step 6 — Approve labels (the label-approval gate)

A label that does not exist yet is created in LIVE mode ONLY if its exact name
is listed in config `approved_labels` (default []). Creating an unapproved
label is refused: a clear stderr line names the label and explains how to
approve it, and the messages that would have used it are left untouched.

Workflow:

1. Run the dry-run and copy the NEW labels you want into `approved_labels`
   in config.json.
2. Run the dry-run again — those labels now show as approved (would-be
   created) and no refusal is printed.
3. Run live:

    python3 gmail_inbox_manager.py --max 300

No live label is created on your mailbox without approval; dry-run and the
refusal path are the evidence.

## Step 7 — Cron

    0 * * * *   CONFIG=/path/to/config.json /path/to/gmail_inbox_manager_cron.sh

The launcher (`gmail_inbox_manager_cron.sh`) runs the script with `--config`
and passes extra flags through. stdout IS the notification message: empty
stdout = silent run (nothing changed); non-empty stdout = digest; exit 1 =
failure alert. It is no-agent and generalized to config paths.

The delivery target (a Discord channel via cron `deliver=discord:<channel>`,
or a webhook) is configured by your OPERATOR/scheduler — this package never
hard-codes a channel or webhook. See config.example.json `delivery` and the
launcher header.

## Files

- gmail_inbox_manager.py — the pipeline (classify → label → read → archive →
  optional summaries/follow-ups/contacts/attachments → audit → digest)
- config.example.json — every machine-specific knob (copy to config.json)
- rules.template.json — example ruleset (copy to rules.json)
- POLICY.example.md — worked Policy A (archive/keep) with prompts for your own
- gmail_inbox_manager_cron.sh — cron launcher (watchdog pattern)
- setup_token.py — one-time OAuth token setup
- LICENSE — MIT (replaceable)
- state/, logs/, attachments/ — created at run time

## License

MIT. See LICENSE — it is replaceable if your organization prefers another.
