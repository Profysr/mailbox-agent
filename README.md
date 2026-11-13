# gmail-inbox-manager

A configurable, safety-first Gmail triage tool: classify inbox mail against a
ruleset, apply Gmail labels, mark read, and archive low-priority mail — never
send, reply, trash, or delete. Productized from the Gmail consolidated inbox
manager so a third party can set it up on their own machine with their own
Gmail account.

## Quickstart

The whole path in one block; each step is detailed below.

    git clone <your-repo-url> gmail-inbox-manager
    cd gmail-inbox-manager
    python3 -m venv .venv && . .venv/bin/activate
    python3 -m pip install -r requirements.txt
    cp config.example.json config.json
    cp rules.template.json rules.json        # then edit senders/subjects
    # one-time OAuth setup (Step 2 — Google Cloud project + client secret):
    python3 setup_token.py --client-secret ./client_secret.json --token-out ./google_token.json
    python3 gmail_inbox_manager.py --check           # pre-flight, no API calls
    python3 gmail_inbox_manager.py --dry-run --max 25  # review counts + new labels
    # copy the NEW labels into approved_labels in config.json, then:
    python3 gmail_inbox_manager.py --max 50          # first live run
    0 * * * * CONFIG=$PWD/config.json $PWD/gmail_inbox_manager_cron.sh

Every new user needs their OWN Google Cloud project, OAuth client, token,
`config.json` and `rules.json` — those are per-person and gitignored.

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

- **Linux (or WSL)** with Python 3.10 or newer. On Windows use WSL: the
  script uses POSIX-only timeout handling (SIGALRM) and the launcher is a
  `sh` script, so plain PowerShell / Task Scheduler will NOT work — run
  everything inside a WSL shell (cron there, or `wsl.exe` from Task
  Scheduler).
- A Gmail account
- Python packages from `requirements.txt`: google-api-python-client and
  google-auth for the pipeline, google-auth-oauthlib only for the one-time
  token setup, and rich for the pretty `--check` / `--print-plan` output
  (optional — the script falls back to plain text if rich is absent)

Use a virtualenv so the dependencies stay isolated:

    python3 -m venv .venv
    . .venv/bin/activate
    python3 -m pip install -r requirements.txt

Check:

    python3 -c "import googleapiclient, google.oauth2.credentials; print('deps ok')"

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

5. Create the token. No local browser is required: the script prints the
   full Google consent URL and waits on a loopback callback server.

    python3 setup_token.py --client-secret ./client_secret.json --token-out ./google_token.json

   Defaults are pre-wired for this host, so plain `python3 setup_token.py`
   works: --client-secret falls back to the comms profile copy, --port is a
   fixed 9090 (so the ssh -L instruction is always the same command), and the
   token is written to ./google_token.json.

   Headless box (this VM): the script detects the missing browser, prints the
   consent URL itself, and waits. From your laptop open the tunnel:

    ssh -L 9090:localhost:9090 you@server

   then on the server run setup_token.py (any order works — the tunnel only
   needs to exist before you open the URL), and open the printed URL in the
   laptop's browser. The URL contains `redirect_uri=http://localhost:9090/`;
   after you approve, the token is written on the server and verified with
   one real Gmail API call (users.getProfile): the script prints VERIFIED or
   FAILED and exits non-zero on failure.

   The token is written with mode 600, contains a refresh token (one-time
   setup), and requests scope gmail.modify only — gmail.send is deliberately
   NOT requested because the manager never sends mail.

   **Keep the token private** — it carries a refresh token for your mailbox.
   It is gitignored, written mode 600, and `--check` verifies the
   permissions. Never commit or share it.

   **Testing-status warning:** Google expires refresh tokens issued while
   the consent screen is in *Testing* status after about 7 days — a week
   later every run dies with "Re-run setup_token.py to re-authorize".
   Either re-run `setup_token.py` when that happens, or publish the app
   (OAuth consent screen → Publishing status) so the token keeps refreshing
   unattended.

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
| rules_path | the ruleset (default ./rules.json) |
| state_dir | where state files live (default ./state) |
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

Validate the ruleset without touching the mailbox — this is the same check
the script runs at start-up, so it enforces the catch-all invariant above
(a plain JSON parse does not):

    python3 -c "from gmail_inbox_manager import load_rules; load_rules('rules.json'); print('rules ok')"

Then run the full pre-flight — dependencies, config, ruleset, token file
and token permissions, with NO Gmail API call:

    python3 gmail_inbox_manager.py --check

## Step 5 — Dry-run (review counts + planned labels)

    python3 gmail_inbox_manager.py --dry-run --max 25

This mutates NOTHING and writes no state. The digest prints per-category counts
and a dedicated LABELS section:

- EXISTING labels — already in your mailbox
- NEW labels this run WOULD create — not yet in your mailbox

Review the counts and the planned-new-label list before going live.

## Step 6 — Approve labels (the label-approval gate)

**Why it exists.** `rules.json` names labels ("Receipts", "Banking", …) that
may not exist in your mailbox yet. A live run may create missing labels —
but only after you have explicitly listed each name in the config key
`approved_labels` (default `[]`). Nothing ever creates a label in your
mailbox without your sign-off.

**The workflow is two dry-runs plus one manual JSON edit:**

1. **Dry-run #1 — discover which labels are new.**

       python3 gmail_inbox_manager.py --dry-run --max 25

   The digest ends with a Labels section:

       EXISTING labels:
       - Security
       NEW labels this run WOULD create:
       - Receipts
       - Banking  (NOT APPROVED — would be refused)

   EXISTING labels already live in Gmail and are used as-is. NEW labels
   would have to be created. (Shortcut: `--print-plan` prints just this
   list and exits.)

2. **The manual JSON edit — approve what you want.** Open `config.json`
   and copy the NEW names into the `approved_labels` array: exact
   spelling, double-quoted, comma-separated, e.g.

       "approved_labels": ["Banking", "Receipts", "Security", "Unsorted"]

   Any name you leave out stays unapproved — that is safe by design: on a
   live run those messages are left untouched and a stderr line
   `REFUSED: label 'X' is not approved` tells you exactly what to add.
   Nothing is lost; the mail simply waits until you approve it.

3. **Dry-run #2 — verify the approval took.** Run the same dry-run again:
   approved names no longer carry the `(NOT APPROVED — would be refused)`
   marker.

4. **Run live:**

       python3 gmail_inbox_manager.py --max 300

   Missing + approved labels are created; missing + unapproved labels are
   refused; existing labels are used directly.

No live label is created on your mailbox without approval; dry-run and the
refusal path are the evidence.

## Step 7 — Cron

    0 * * * *   CONFIG=/path/to/config.json /path/to/gmail_inbox_manager_cron.sh

The launcher (`gmail_inbox_manager_cron.sh`) runs the script with `--config`
and passes extra flags through. stdout IS the notification message: empty
stdout = silent run (nothing changed); non-empty stdout = digest; exit 1 =
failure alert. It is no-agent and generalized to config paths.

A single-instance lock (`.run.lock`, via `flock -n`) makes a run that
overlaps a still-running one exit 1 immediately instead of racing it on
the state files — so an overrun shows up as an alert, never as corrupted
state.

The delivery target is configured by your OPERATOR/scheduler — this package
only prints the digest on stdout and never hard-codes a channel or webhook.
There is no `delivery` key in the config: pipe stdout to mail, a webhook
wrapper, or a Discord bot as your setup prefers (see the launcher header).

## Files

- gmail_inbox_manager.py — the pipeline (classify → label → read → archive →
  optional summaries/follow-ups/contacts/attachments → audit → digest)
- requirements.txt — the three Python packages (`pip install -r requirements.txt`)
- test_safety.py — safety self-check (`python3 test_safety.py`): catch-all
  validation, unknown-sender invariant, preflight — no API calls
- config.example.json — every machine-specific knob (copy to config.json)
- rules.template.json — example ruleset (copy to rules.json)
- POLICY.example.md — worked Policy A (archive/keep) with prompts for your own
- gmail_inbox_manager_cron.sh — cron launcher (watchdog pattern)
- setup_token.py — one-time OAuth token setup
- LICENSE — MIT (replaceable)
- state/, logs/, attachments/ — created at run time

## Troubleshooting

| symptom | cause | fix |
|---------|-------|-----|
| `Token file not found` / `Re-run setup_token.py` | token missing or expired (consent screen in *Testing* status expires tokens after ~7 days) | re-run `setup_token.py` (Step 2) |
| `REFUSED: label 'X' is not approved` | X missing from `approved_labels` | add it to config.json (Step 6), re-run |
| `ABORTED (rate limit)` | Gmail quota — repeated 429/403/5xx | wait and re-run; lower `max_messages` |
| `NOT READY` from `--check` | see the `PROBLEM:` lines it prints | fix each, re-run `--check` |

## License

MIT. See LICENSE — it is replaceable if your organization prefers another.
