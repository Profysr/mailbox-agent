#!/usr/bin/env python3
"""gmail-inbox-manager — configurable, safety-first Gmail triage.

A generalized, shareable build of the "uc09 consolidated inbox manager"
pipeline. Every machine-specific and policy-specific knob lives in a JSON
config file (see config.example.json); nothing about a particular mailbox
is baked into this file.

Pipeline per run:
  a. classify every INBOX message against a ruleset (first-match-wins,
     case-insensitive substring on From + Subject)
  b. resolve Gmail labels — a label that does not exist yet is created
     ONLY if its name is listed in the config key "approved_labels"
     (label-approval gate)
  c. apply label + mark read (+ archive per the rule's archive flag)
  d. extractive summaries of new KEEP mail (optional)
  e. follow-up detection: needs-my-reply / awaiting-their-reply (optional)
  f. new-contact tracking (optional)
  g. attachment extraction to a local directory (optional)
  h. append-only audit log, one JSON line per action
  i. ONE plain-text digest on stdout; NOTHING printed when nothing changed

SAFETY INVARIANTS (never relaxed, not configurable):
  * never sends, replies, trashes, or deletes a message
  * the only mailbox mutations are: addLabelIds, removeLabelIds [INBOX],
    removeLabelIds [UNREAD]
  * an unknown/unmatched sender is labelled "Unsorted" and KEPT in the inbox
    (unknown mail is never archived)
  * chunked processing: page size and pass size are bounded, batch modify
    calls carry at most `modify_chunk` ids
  * at least `api_sleep_seconds` between Gmail API calls
  * exponential backoff with Retry-After support on 429/403/5xx, and a
    hard stop after `max_strikes` consecutive failures
  * per-message failure isolation: one bad message never kills a pass
  * checkpoint state is written after every pass (crash-safe resume)
  * audit log is append-only

Usage:
  python gmail_inbox_manager.py --dry-run
  python gmail_inbox_manager.py
  CONFIG=/path/to/config.json python gmail_inbox_manager.py --dry-run
"""
import argparse
import base64
import json
import os
import re
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr
from pathlib import Path

# --------------------------------------------------------------------------
# Defaults — every one of these can be overridden in the config file.
# --------------------------------------------------------------------------
DEFAULTS = {
    "token_path": "~/.config/gmail-inbox-manager/token.json",
    # !! Unused — resolved from config but never consumed by the pipeline;
    # setup_token.py takes its own --client-secret flag instead.
    "client_secret_path": "",
    "scopes": ["https://www.googleapis.com/auth/gmail.modify"],
    "rules_path": "./rules.json",
    "state_dir": "./state",
    "label_map_path": "./state/labels.json",
    "processed_ids_path": "./state/processed_ids.json",
    "contacts_path": "./state/contacts.json",
    "followups_path": "./state/followups.json",
    "attachments_state_path": "./state/attachments.json",
    "log_dir": "./logs",
    "run_log": "./logs/gmail_inbox_manager.log",
    "audit_log": "./logs/actions.log",
    "attachment_dest": "./attachments",
    "max_messages": 300,
    "page_size": 25,
    "pass_size": 25,
    "batch_size": 25,
    "modify_chunk": 50,
    "api_sleep_seconds": 1.5,
    "batch_retries": 5,
    "backoff_base_seconds": 1,
    "backoff_max_seconds": 8,
    "max_strikes": 3,
    "state_keep": 20000,
    "followup_days": 3,
    "attachment_days": 2,
    "summary_max": 5,
    "summary_chars": 300,
    "digest_max_items": 8,
    "digest_max_chars": 1500,
    "approved_labels": [],
    "enable_followups": True,
    "enable_contacts": True,
    "enable_attachments": True,
    "write_refreshed_token": False,
    "contact_skip_domains": ["google.com"],
    "fallback_label": "Unsorted",
}


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

def load_config(path):
    """Load the JSON config. Relative paths inside the config are resolved
    against the config file's own directory, so the package is portable."""
    cfg = dict(DEFAULTS)
    cfg_path = Path(path).expanduser().resolve()
    if cfg_path.exists():
        try:
            user = json.loads(cfg_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise SystemExit(f"ERROR: cannot read config {cfg_path}: {exc}")
        if not isinstance(user, dict):
            raise SystemExit(f"ERROR: config {cfg_path} must be a JSON object")
        cfg.update(user)
    else:
        raise SystemExit(
            f"ERROR: config file not found: {cfg_path}\n"
            f"       Copy config.example.json to config.json and edit it.")
    cfg["_config_dir"] = str(cfg_path.parent)
    cfg["_config_path"] = str(cfg_path)
    base = cfg_path.parent

    def resolve(value):
        if not isinstance(value, str) or not value:
            return value
        v = os.path.expanduser(value)
        p = Path(v)
        return str((base / p).resolve()) if not p.is_absolute() else str(p)

    for key in ("token_path", "client_secret_path", "rules_path", "state_dir",
                "label_map_path", "processed_ids_path", "contacts_path",
                "followups_path", "attachments_state_path", "log_dir",
                "run_log", "audit_log", "attachment_dest"):
        if key in cfg:
            cfg[key] = resolve(cfg[key])
    if not cfg.get("approved_labels"):
        cfg["approved_labels"] = []
    return cfg


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_logger(run_log_path):
    """stderr + file logger. Never raises on IO problems."""
    def log(msg):
        line = f"{now_iso()} {msg}"
        print(line, file=sys.stderr, flush=True)
        try:
            Path(run_log_path).parent.mkdir(parents=True, exist_ok=True)
            with open(run_log_path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass
    return log


def load_json(path, default):
    path = Path(path)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                merged = dict(default)
                merged.update(data)
                return merged
        except (json.JSONDecodeError, OSError):
            pass
    return json.loads(json.dumps(default))


def save_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                   encoding="utf-8")
    tmp.replace(path)


def audit(audit_path, line):
    """Append-only audit trail — one JSON object per line, never rewritten."""
    Path(audit_path).parent.mkdir(parents=True, exist_ok=True)
    with open(audit_path, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


# --------------------------------------------------------------------------
# Gmail service
# --------------------------------------------------------------------------

class AuthError(Exception):
    pass


def build_service(cfg, log):
    """Build an authenticated Gmail API service from the configured token.

    Standalone: needs only google-api-python-client + google-auth (no Hermes
    internals). Refreshes an expired token in place when
    "write_refreshed_token" is true."""
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
    except ImportError as exc:  # pragma: no cover - env problem
        raise AuthError(
            "Missing dependencies. Install them with:\n"
            "  python3 -m pip install google-api-python-client google-auth") \
            from exc

    token_path = Path(cfg["token_path"])
    if not token_path.exists():
        raise AuthError(
            f"Token file not found: {token_path}\n"
            f"Run setup_token.py first (see README, step 2).")
    try:
        payload = json.loads(token_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise AuthError(f"Cannot read token file {token_path}: {exc}") from exc

    scopes = payload.get("scopes") or cfg.get("scopes") or []
    creds = Credentials.from_authorized_user_file(str(token_path), scopes)
    if creds.expired and creds.refresh_token:
        log("token expired — refreshing")
        try:
            creds.refresh(Request())
        except Exception as exc:  # noqa: BLE001
            raise AuthError(
                f"Token refresh failed: {exc}\n"
                f"Re-run setup_token.py to re-authorize.") from exc
        if cfg.get("write_refreshed_token"):
            token_path.write_text(
                json.dumps(json.loads(creds.to_json()), indent=2),
                encoding="utf-8")
            try:
                # never let a refreshed token land group/world-readable
                os.chmod(token_path, 0o600)
            except OSError:
                pass
    if not creds.valid:
        raise AuthError(
            f"Token in {token_path} is not valid.\n"
            f"Re-run setup_token.py to re-authorize.")
    return build("gmail", "v1", credentials=creds,
                 cache_discovery=False)


class _AlarmTimeout(Exception):
    pass


def _alarm_handler(signum, frame):
    raise _AlarmTimeout()


def with_timeout(seconds, fn):
    """Run fn() under a SIGALRM deadline — an API call may hang forever."""
    old = signal.signal(signal.SIGALRM, _alarm_handler)
    signal.alarm(seconds)
    try:
        return fn()
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


class FatalRateLimit(Exception):
    """Raised after max_strikes consecutive 429/403/5xx failures."""


_STRIKES = {"n": 0}


def _retry_after_seconds(exc):
    try:
        resp = getattr(exc, "resp", None)
        headers = getattr(resp, "headers", None) or {}
        raw = headers.get("retry-after") or headers.get("Retry-After")
        if raw:
            return max(0, int(float(raw)))
    except Exception:  # noqa: BLE001 - best effort
        pass
    return None


def _is_rate_limited(exc):
    try:
        code = getattr(getattr(exc, "resp", None), "status", None)
        if code in (403, 429, 500, 502, 503, 504):
            return True
    except Exception:  # noqa: BLE001
        pass
    txt = str(exc).lower()
    return any(t in txt for t in ("429", "403", "rate limit", "ratelimit",
                                  "quota", "too many requests", "backenderror",
                                  "service unavailable", "internal error"))


def api_call(fn, log, what, cfg):
    """Call a Gmail API thunk with exponential backoff, honouring Retry-After.

    Consecutive 429/403/5xx failures are counted; after `max_strikes` the
    run aborts (FatalRateLimit) instead of hammering the API. Non rate-limit
    errors are re-raised immediately so the caller can isolate them."""
    delay = cfg["backoff_base_seconds"]
    last = None
    for attempt in range(cfg["batch_retries"]):
        try:
            out = fn()
            _STRIKES["n"] = 0
            return out
        except Exception as exc:  # noqa: BLE001 - classified below
            last = exc
            if not _is_rate_limited(exc):
                raise
            _STRIKES["n"] += 1
            ra = _retry_after_seconds(exc)
            sleep_for = ra if ra is not None else delay
            log(f"WARN {what} attempt {attempt + 1}/{cfg['batch_retries']}: "
                f"{str(exc)[:150]} (strike {_STRIKES['n']}/"
                f"{cfg['max_strikes']}, sleep {sleep_for}s)")
            if _STRIKES["n"] >= cfg["max_strikes"]:
                raise FatalRateLimit(
                    f"{cfg['max_strikes']} consecutive rate-limit failures on "
                    f"{what}: {str(exc)[:300]}")
            time.sleep(sleep_for)
            delay = min(delay * 2, cfg["backoff_max_seconds"])
    raise last


# --------------------------------------------------------------------------
# Rules
# --------------------------------------------------------------------------

def load_rules(path):
    """Load + validate the ruleset.

    Format (see rules.template.json):
      {"version": N, "rules": [{"name", "label", "archive", "match": {...}}]}
    Matching semantics: case-insensitive substring on the full From header
    and on the Subject. Within one rule ANY from_contains OR ANY
    subject_contains entry matches. Rules are evaluated in order and the
    FIRST match wins. `archive: true` also removes INBOX (message stays in
    All Mail — reversible). `archive: false` keeps the message in the inbox.

    The LAST rule must be the catch-all (empty match lists) with
    archive:false — unknown senders are never archived."""
    path = Path(path)
    if not path.exists():
        raise SystemExit(f"ERROR: rules file not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise SystemExit(f"ERROR: cannot read rules {path}: {exc}")
    rules = data.get("rules")
    if not isinstance(rules, list) or not rules:
        raise SystemExit(f"ERROR: {path} has no 'rules' list")
    for i, r in enumerate(rules):
        for key in ("name", "label", "match", "archive"):
            if key not in r:
                raise SystemExit(f"ERROR: rule #{i} in {path} missing '{key}'")
        if not isinstance(r["match"], dict):
            raise SystemExit(f"ERROR: rule #{i} in {path} has a non-object match")
    catch_all = rules[-1]
    if catch_all["match"].get("from_contains") or \
            catch_all["match"].get("subject_contains"):
        raise SystemExit(
            f"ERROR: the LAST rule in {path} must be the catch-all with empty "
            f"from_contains and subject_contains — classify() falls through "
            f"to the last rule, so a matching last rule would label and "
            f"archive unknown senders")
    if catch_all.get("archive"):
        raise SystemExit(
            f"ERROR: the catch-all rule '{catch_all['name']}' in {path} "
            f"must have archive=false — unknown senders are never archived")
    return rules


def rule_matches(rule, from_header, subject):
    m = rule.get("match", {})
    hay_from = (from_header or "").lower()
    hay_subj = (subject or "").lower()
    if any(s.lower() in hay_from for s in m.get("from_contains", [])):
        return True
    return any(s.lower() in hay_subj for s in m.get("subject_contains", []))


def classify(rules, from_header, subject):
    """First-match-wins. The trailing catch-all rule catches everything."""
    for r in rules:
        if rule_matches(r, from_header, subject):
            return r
    return rules[-1]


# --------------------------------------------------------------------------
# Labels + the approval gate
# --------------------------------------------------------------------------

class LabelApprovalError(Exception):
    """Raised when a live run needs a label that is not approved."""


def resolve_labels(service, cfg, rules, labelmap, dry_run, log):
    """Resolve label name -> id, creating missing labels ONLY when approved.

    Returns (label_ids, plan) where plan is a dict:
      existing     : [names]      labels already in the mailbox
      approved_new : [names]      missing AND in config approved_labels
      unapproved   : [names]      missing AND NOT in approved_labels
      refused      : [names]      unapproved labels a live run refused

    Dry run: nothing is created. Live run: approved missing labels are
    created; unapproved missing labels are REFUSED with a clear stderr line
    naming the label and how to approve it. Refused labels are reported so
    the caller can leave those messages untouched."""
    resp = api_call(
        lambda: with_timeout(120, lambda: service.users().labels()
                             .list(userId="me").execute()),
        log, "labels.list", cfg)
    existing = {}
    for lab in resp.get("labels", []):
        existing[lab["name"].lower()] = lab["id"]

    approved = {str(a) for a in cfg.get("approved_labels", [])}
    needed = sorted({str(r["label"]) for r in rules})
    plan = {"existing": [], "approved_new": [], "unapproved": [], "refused": []}
    label_ids = {}
    for name in needed:
        key = name.lower()
        if key in existing:
            label_ids[name] = existing[key]
            plan["existing"].append(name)
            labelmap.setdefault("labels", {})[key] = existing[key]
            continue
        if name in approved:
            plan["approved_new"].append(name)
            if dry_run:
                label_ids[name] = f"DRYRUN:{name}"
                continue
            created = api_call(
                lambda: with_timeout(120, lambda: service.users().labels()
                                     .create(userId="me", body={
                                         "name": name,
                                         "labelListVisibility": "labelShow",
                                         "messageListVisibility": "show"})
                                     .execute()),
                log, f"labels.create {name}", cfg)
            label_ids[name] = created["id"]
            labelmap.setdefault("labels", {})[key] = created["id"]
            log(f"created label '{name}' -> {created['id']}")
            time.sleep(cfg["api_sleep_seconds"])
            continue
        # NOT approved -> refuse to create it.
        plan["unapproved"].append(name)
        plan["refused"].append(name)
        print(
            f"REFUSED: label '{name}' is not approved — it will NOT be "
            f"created and messages for it are left untouched. "
            f"To approve it, add \"{name}\" to the \"approved_labels\" list "
            f"in {cfg['_config_path']} and re-run.",
            file=sys.stderr, flush=True)
        log(f"REFUSED label '{name}' (not in approved_labels)")
    return label_ids, plan


# --------------------------------------------------------------------------
# Fetch: paginate the inbox, then batch metadata gets
# --------------------------------------------------------------------------

# !! Duplicate — byte-for-byte the same pagination loop as list_filtered_ids();
# this is list_filtered_ids(service, cfg, "in:inbox", ...) with a copied loop.
def list_inbox_ids(service, cfg, max_msgs, log):
    ids, token = [], None
    while len(ids) < max_msgs:
        kwargs = {"userId": "me", "q": "in:inbox",
                  "maxResults": min(cfg["page_size"], max_msgs - len(ids))}
        if token:
            kwargs["pageToken"] = token
        resp = api_call(
            lambda: with_timeout(120, lambda: service.users().messages()
                                 .list(**kwargs).execute()),
            log, "messages.list", cfg)
        ids.extend(m["id"] for m in resp.get("messages", []))
        token = resp.get("nextPageToken")
        if not token or not resp.get("messages"):
            break
        time.sleep(cfg["api_sleep_seconds"])
    return ids


def list_filtered_ids(service, cfg, query, max_msgs, log, what):
    """List message ids matching a Gmail query (used by --scope all and
    --recheck)."""
    ids, token = [], None
    while len(ids) < max_msgs:
        kwargs = {"userId": "me", "q": query,
                  "maxResults": min(cfg["page_size"], max_msgs - len(ids))}
        if token:
            kwargs["pageToken"] = token
        resp = api_call(
            lambda: with_timeout(120, lambda: service.users().messages()
                                 .list(**kwargs).execute()),
            log, what, cfg)
        ids.extend(m["id"] for m in resp.get("messages", []))
        token = resp.get("nextPageToken")
        if not token or not resp.get("messages"):
            break
        time.sleep(cfg["api_sleep_seconds"])
    return ids


def list_unlabeled_ids(service, cfg, label_names, max_msgs, log):
    neg = " ".join(f'-label:"{n}"' for n in sorted(label_names))
    return list_filtered_ids(service, cfg, f"in:anywhere {neg}", max_msgs, log,
                             "messages.list(unlabeled)")


def list_labeled_ids(service, cfg, label_names, max_msgs, log):
    pos = " OR ".join(f'label:"{n}"' for n in sorted(label_names))
    return list_filtered_ids(service, cfg, f"in:anywhere ({pos})", max_msgs,
                             log, "messages.list(labeled)")


def _headers_of(msg):
    headers = {h["name"].lower(): h.get("value", "")
               for h in msg.get("payload", {}).get("headers", [])}
    return (headers.get("from", ""), headers.get("subject", ""),
            msg.get("threadId", ""), msg.get("labelIds", []))


def fetch_metadata(service, cfg, ids, log):
    """Batch metadata fetch with per-batch retry + per-message fallback.

    Returns ({id: msgdict}, [error_ids]). A single bad message is isolated:
    it ends up in errors and is left untouched."""
    out, errors = {}, []
    sleep = cfg["api_sleep_seconds"]

    def make_cb(store):
        def cb(rid, resp, exc):
            if exc is not None:
                return
            from_hdr, subject, thread_id, labels = _headers_of(resp)
            store[rid] = {"id": rid, "from": from_hdr, "subject": subject,
                          "thread": thread_id, "labels": labels}
        return cb

    for i in range(0, len(ids), cfg["batch_size"]):
        chunk = ids[i:i + cfg["batch_size"]]
        store = {}
        batch = service.new_batch_http_request()
        for mid in chunk:
            batch.add(service.users().messages().get(
                userId="me", id=mid, format="metadata",
                metadataHeaders=["From", "Subject"]), callback=make_cb(store))
        done = False
        try:
            api_call(lambda: with_timeout(120, batch.execute),
                     log, f"batch fetch {i // cfg['batch_size']}", cfg)
            done = True
        except FatalRateLimit:
            raise
        except Exception as exc:  # noqa: BLE001 - isolated per batch
            log(f"WARN batch {i // cfg['batch_size']}: {str(exc)[:150]}")
        for mid in chunk:
            if mid in store:
                out[mid] = store[mid]
                continue
            if not done:
                errors.append(mid)
                log(f"ERROR fetch id={mid} (batch failed)")
                continue
            try:
                msg = api_call(
                    lambda mid=mid: with_timeout(
                        120, lambda: service.users().messages().get(
                            userId="me", id=mid, format="metadata",
                            metadataHeaders=["From", "Subject"]).execute()),
                    log, f"fetch {mid}", cfg)
                from_hdr, subject, thread_id, labels = _headers_of(msg)
                out[mid] = {"id": mid, "from": from_hdr, "subject": subject,
                            "thread": thread_id, "labels": labels}
            except FatalRateLimit:
                raise
            except Exception as exc:  # noqa: BLE001 - one bad msg never kills
                errors.append(mid)
                log(f"WARN fetch id={mid} failed: {str(exc)[:150]}")
            time.sleep(sleep)
        log(f"fetch progress: batch {i // cfg['batch_size']} done "
            f"(total fetched {len(out)}, errors {len(errors)})")
        time.sleep(sleep)
    return out, errors


# --------------------------------------------------------------------------
# Summaries (extractive)
# --------------------------------------------------------------------------

def clean_text(text):
    return re.sub(r"\s+", " ", (text or "")).strip()


def split_sentences(text):
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])", text)
    return [p.strip() for p in parts if len(p.strip()) > 0]


def summarize_text(body, subject, max_chars, max_sentences=2):
    sentences = split_sentences(body)
    if len(sentences) <= max_sentences:
        return body[:max_chars] + ("…" if len(body) > max_chars else "")
    subj_words = {w.lower() for w in re.findall(r"[A-Za-z0-9']{4,}", subject or "")}
    n = len(sentences)
    scored = []
    for i, s in enumerate(sentences):
        words = {w.lower() for w in re.findall(r"[A-Za-z0-9']{4,}", s)}
        overlap = len(subj_words & words) / (len(subj_words) or 1)
        position = 1.0 if i == 0 else (0.5 if i < n * 0.25 else 0.0)
        length_fit = 1.0 if 40 <= len(s) <= 220 else 0.3
        scored.append((overlap * 2 + position + length_fit, i, s))
    top = sorted(sorted(scored, reverse=True)[:max_sentences], key=lambda t: t[1])
    out = " ".join(s for _, _, s in top)
    if len(out) > max_chars:
        cut = out[:max_chars]
        if " " in cut:
            cut = cut[: cut.rfind(" ")]
        out = cut + "…"
    return out


def body_of(msg_full):
    return clean_text(msg_full.get("body", "")) or \
        clean_text(msg_full.get("snippet", ""))


# --------------------------------------------------------------------------
# Follow-up detection (needs-my-reply / awaiting-their-reply)
# --------------------------------------------------------------------------

BULK_MARKERS = ("noreply", "no-reply", "donotreply", "notifications",
                "newsletter", "promo", "deals", "mailer", "digest",
                "unsubscribe", "mailer-daemon", "postmaster", "billing@",
                "invoice", "statement", "notification@", "jobs@",
                "recommendedjobs", "jobalerts", "info@", "marketing@",
                "updates@", "statements")
BULK_SUBJECT_PATTERNS = ("alert", "logged[- ]in", "session", "lucky draw",
                         "reward", "job pick", "latest job", "unsubscribe",
                         "invoice", "statement", "receipt",
                         "payment confirmation", "otp", "verification code")


def sender_is_bulk(from_hdr, subject):
    f = (from_hdr or "").lower()
    if any(m in f for m in BULK_MARKERS):
        return True
    s = (subject or "").lower()
    return any(re.search(p, s) for p in BULK_SUBJECT_PATTERNS)


def classify_thread(msgs):
    if not msgs:
        return None
    mine = [i for i, m in enumerate(msgs) if "SENT" in m["labels"]]
    last = msgs[-1]
    if "SENT" in last["labels"]:
        return "awaiting_their_reply"
    if sender_is_bulk(last["from"], last["subject"]):
        return None
    if not mine:
        return "needs_my_reply"
    return None


def followups(service, cfg, state, log):
    """Scan recent threads; return (new_items, updated_state)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    state["threads"] = {tid: rec for tid, rec in state.get("threads", {}).items()
                        if isinstance(rec, dict) and rec.get("at", "") >= cutoff}
    ids, token = [], None
    limit = 150
    # !! Duplicate — same page-loop as list_filtered_ids (threads vs messages)
    while len(ids) < limit:
        kwargs = {"userId": "me", "q": f"newer_than:{cfg['followup_days']}d",
                  "maxResults": min(cfg["page_size"], limit - len(ids))}
        if token:
            kwargs["pageToken"] = token
        resp = api_call(
            lambda: with_timeout(120, lambda: service.users().threads()
                                 .list(**kwargs).execute()),
            log, "threads.list", cfg)
        ids.extend(t["id"] for t in resp.get("threads", []))
        token = resp.get("nextPageToken")
        if not token or not resp.get("threads"):
            break
        time.sleep(cfg["api_sleep_seconds"])
    new_items = []
    for tid in ids:
        try:
            th = api_call(
                lambda tid=tid: with_timeout(
                    120, lambda: service.users().threads().get(
                        userId="me", id=tid, format="metadata",
                        metadataHeaders=["From", "Subject", "Date"]).execute()),
                log, f"thread {tid}", cfg)
        except FatalRateLimit:
            raise
        except Exception as exc:  # noqa: BLE001
            log(f"WARN thread {tid}: {str(exc)[:120]}")
            time.sleep(cfg["api_sleep_seconds"])
            continue
        msgs = []
        for m in th.get("messages", []):
            # !! Duplicate — same header extraction as _headers_of() (plus Date)
            hdrs = {h["name"].lower(): h.get("value", "")
                    for h in m.get("payload", {}).get("headers", [])}
            msgs.append({"labels": m.get("labelIds", []),
                         "from": hdrs.get("from", ""),
                         "subject": hdrs.get("subject", ""),
                         "date": hdrs.get("date", "")})
        kind = classify_thread(msgs)
        time.sleep(cfg["api_sleep_seconds"])
        if not kind:
            continue
        prev = state["threads"].get(tid)
        if prev and prev.get("kind") == kind:
            continue
        last = msgs[-1]
        rec = {"kind": kind, "from": last["from"][:120],
               "subject": (last["subject"] or "")[:160],
               "date": last["date"][:22]}
        new_items.append(rec)
        state["threads"][tid] = dict(rec, at=now_iso())
    return new_items, state


# --------------------------------------------------------------------------
# New-contact tracking
# --------------------------------------------------------------------------

def track_contacts(contacts, msgs, me, cfg, log):
    skip_domains = {str(d).lower() for d in cfg.get("contact_skip_domains", [])}
    new = []
    for m in msgs:
        _name, addr = parseaddr(m.get("from", "") or "")
        addr = (addr or "").lower().strip()
        if not addr or "@" not in addr or addr == me:
            continue
        domain = addr.rsplit("@", 1)[1]
        if domain in skip_domains:
            continue
        if addr in contacts.get("contacts", {}):
            continue
        rec = {"name": (parseaddr(m.get("from", "") or "")[0] or "")[:120],
               "first_seen": now_iso(),
               "first_subject": (m.get("subject") or "")[:200],
               "first_message": m["id"]}
        contacts.setdefault("contacts", {})[addr] = rec
        new.append(dict(rec, address=addr))
        log(f"NEW contact {addr}")
    return new


# --------------------------------------------------------------------------
# Attachment extraction (local destination, configurable)
# --------------------------------------------------------------------------

def sanitize_filename(name, used):
    name = (name or "attachment.bin").strip()
    name = name.replace("\\", "_").replace("/", "_").replace("\x00", "")
    name = re.sub(r"\s+", "_", name)
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    stem = re.sub(r"[^A-Za-z0-9._-]", "_", stem)[:64] or "file"
    ext = re.sub(r"[^A-Za-z0-9._-]", "_", ext)[:16]
    candidate = f"{stem}.{ext}" if dot else stem
    n = 1
    while candidate.lower() in used:
        candidate = f"{stem}_{n}.{ext}" if dot else f"{stem}_{n}"
        n += 1
    return candidate


def iter_attachment_parts(payload):
    stack = [payload]
    while stack:
        part = stack.pop()
        stack.extend(part.get("parts", []) or [])
        if part.get("filename") and part.get("body", {}).get("attachmentId"):
            yield part


def extract_attachments(service, cfg, att_state, dest, days, me, log, dry):
    """Extract attachments from recent messages. Own outgoing mail is
    skipped; size caps apply. Returns (extracted_metas, skipped)."""
    query = f"newer_than:{days}d has:attachment"
    ids, token = [], None
    limit = 100
    # !! Duplicate — same page-loop as list_filtered_ids (attachments vs messages)
    while len(ids) < limit:
        kwargs = {"userId": "me", "q": query,
                  "maxResults": min(cfg["page_size"], limit - len(ids))}
        if token:
            kwargs["pageToken"] = token
        resp = api_call(
            lambda: with_timeout(120, lambda: service.users().messages()
                                 .list(**kwargs).execute()),
            log, "attachments list", cfg)
        ids.extend(m["id"] for m in resp.get("messages", []))
        token = resp.get("nextPageToken")
        if not token or not resp.get("messages"):
            break
        time.sleep(cfg["api_sleep_seconds"])
    max_file = cfg.get("max_file_bytes", 25 * 1024 * 1024)
    max_msg = cfg.get("max_msg_bytes", 30 * 1024 * 1024)
    extracted, skipped = [], []
    for mid in ids:
        if mid in att_state.get("processed", {}):
            continue
        try:
            msg = api_call(
                lambda mid=mid: with_timeout(
                    120, lambda: service.users().messages().get(
                        userId="me", id=mid, format="full").execute()),
                log, f"att fetch {mid}", cfg)
        except FatalRateLimit:
            raise
        except Exception as exc:  # noqa: BLE001
            log(f"WARN att fetch id={mid}: {str(exc)[:120]}")
            time.sleep(cfg["api_sleep_seconds"])
            continue
        time.sleep(cfg["api_sleep_seconds"])
        parts = list(iter_attachment_parts(msg.get("payload", {})))
        if not parts:
            continue
        headers = {h["name"].lower(): h.get("value", "")
                   for h in msg.get("payload", {}).get("headers", [])}
        sender = parseaddr(headers.get("from", "") or "")[1].lower()
        if sender == me:
            log(f"skip id={mid} (own outgoing attachment)")
            continue
        day_dir = Path(dest) / datetime.now(timezone.utc).strftime("%Y-%m-%d") / mid
        if not dry:
            day_dir.mkdir(parents=True, exist_ok=True)
        meta = {"message_id": mid, "from": headers.get("from", ""),
                "subject": headers.get("subject", ""),
                "extracted_at": now_iso(), "files": []}
        used, total = set(), 0
        for part in parts:
            size = int(part.get("body", {}).get("size") or 0)
            if size > max_file:
                skipped.append({"id": mid, "file": part.get("filename"),
                                "reason": "size cap"})
                continue
            try:
                att = api_call(
                    lambda mid=mid, aid=part["body"]["attachmentId"]:
                    with_timeout(120, lambda: service.users().messages()
                                 .attachments().get(
                                     userId="me", messageId=mid,
                                     id=aid).execute()),
                    log, f"att dl {mid}", cfg)
            except FatalRateLimit:
                raise
            except Exception as exc:  # noqa: BLE001
                log(f"WARN att dl id={mid}: {str(exc)[:120]}")
                time.sleep(cfg["api_sleep_seconds"])
                continue
            time.sleep(cfg["api_sleep_seconds"])
            blob = base64.urlsafe_b64decode(att.get("data", ""))
            if total + len(blob) > max_msg:
                skipped.append({"id": mid, "file": part.get("filename"),
                                "reason": "message size cap"})
                continue
            fname = sanitize_filename(part.get("filename"), used)
            used.add(fname.lower())
            if not dry:
                (day_dir / fname).write_bytes(blob)
            total += len(blob)
            meta["files"].append({"name": fname, "bytes": len(blob)})
        if not meta["files"]:
            continue
        if not dry:
            (day_dir / "_meta.json").write_text(
                json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
            att_state.setdefault("processed", {})[mid] = {
                "at": now_iso(), "files": len(meta["files"])}
        extracted.append(meta)
        log(f"{'(dry-run) would extract' if dry else 'extracted'} "
            f"{len(meta['files'])} file(s) from id={mid}")
    return extracted, skipped


# --------------------------------------------------------------------------
# Actions: batch modify (label / mark read / archive) — LABELS ONLY
# --------------------------------------------------------------------------

def apply_actions(service, cfg, plans, label_ids, dry_run, log):
    """plans: list of {id, label, archive}.

    Label-only mutations: addLabelIds [label], removeLabelIds [UNREAD] plus
    [INBOX] when the rule archives. Ids are grouped by (label, remove-set)
    so each group is ONE messages.batchModify call, capped at
    `modify_chunk` ids per call. Batch calls are sequential. If a batch
    fails we fall back to per-message modify so one bad id cannot kill the
    pass. State claims are written by the CALLER after success."""
    if not plans:
        return [], []
    if dry_run:
        return [p["id"] for p in plans], []

    groups = {}
    for p in plans:
        remove = ["UNREAD"] + (["INBOX"] if p["archive"] else [])
        remove.extend(p.get("stale") or [])
        add_inbox = bool(p.get("add_inbox"))
        groups.setdefault((p["label"], tuple(sorted(set(remove))), add_inbox),
                          []).append(p["id"])

    applied, errors = [], []
    for (label, remove, add_inbox), gids in sorted(groups.items()):
        for i in range(0, len(gids), cfg["modify_chunk"]):
            chunk = gids[i:i + cfg["modify_chunk"]]
            add = [label_ids[label]] + (["INBOX"] if add_inbox else [])
            body = {"addLabelIds": add, "removeLabelIds": list(remove)}
            ok_ids = []
            try:
                resp = api_call(
                    lambda chunk=chunk, body=body: with_timeout(
                        120, lambda: service.users().messages()
                        .batchModify(userId="me", body={
                            "ids": chunk, **body}).execute()),
                    log, f"batchModify {label} x{len(chunk)}", cfg)
                # Gmail answers 200 with an EMPTY body on success, so an
                # absent "ids" key means the whole chunk succeeded.
                if isinstance(resp, dict) and "ids" in resp:
                    ok_ids = list(resp.get("ids", []))
                else:
                    ok_ids = list(chunk)
            except FatalRateLimit:
                raise
            except Exception as exc:  # noqa: BLE001 - fall back per message
                log(f"WARN batchModify {label} x{len(chunk)} failed: "
                    f"{str(exc)[:150]} -> per-message fallback")
                for mid in chunk:
                    try:
                        api_call(
                            lambda mid=mid: with_timeout(
                                120, lambda: service.users().messages().modify(
                                    userId="me", id=mid, body=body).execute()),
                            log, f"modify {mid}", cfg)
                        ok_ids.append(mid)
                    except FatalRateLimit:
                        raise
                    except Exception as exc2:  # noqa: BLE001
                        errors.append(mid)
                        log(f"ERROR modify id={mid}: {str(exc2)[:150]}")
                    time.sleep(cfg["api_sleep_seconds"])
            applied.extend(ok_ids)
            bad = [m for m in chunk if m not in ok_ids]
            errors.extend(bad)
            for mid in bad:
                log(f"ERROR modify id={mid} (batchModify did not confirm)")
            log(f"modify progress: label={label} ok={len(ok_ids)} "
                f"err={len(bad)} total_applied={len(applied)}")
            time.sleep(cfg["api_sleep_seconds"])
    return applied, errors


# --------------------------------------------------------------------------
# Digest
# --------------------------------------------------------------------------

def render_digest(cfg, now, scanned, cat_counts, archived, kept, errors,
                  summaries, followups_new, new_contacts, att_extracted,
                  label_plan, refused_labels):
    cats = "\n".join(f"- {k}: {v}" for k, v in
                     sorted(cat_counts.items(), key=lambda kv: -kv[1]))
    lines = [
        f"📥 **Gmail Inbox Manager** ({now.strftime('%Y-%m-%d %H:%M')} "
        f"{now.strftime('%Z') or 'local'})",
        f"Scanned: {scanned} | archived: {archived} | kept+labeled: {kept} "
        f"| errors: {len(errors)}",
        "",
        "**Categories**",
        cats,
    ]
    if label_plan is not None:
        lines += ["", "**Labels**"]
        existing = label_plan.get("existing", [])
        new_labels = label_plan.get("approved_new", []) + \
            label_plan.get("unapproved", [])
        lines.append("EXISTING labels:")
        if existing:
            lines += [f"- {n}" for n in sorted(existing)]
        else:
            lines.append("- (none)")
        lines.append("NEW labels this run WOULD create:")
        if new_labels:
            for n in sorted(new_labels):
                mark = "" if n in label_plan.get("approved_new", []) \
                    else "  (NOT APPROVED — would be refused)"
                lines.append(f"- {n}{mark}")
        else:
            lines.append("- (none)")
        if refused_labels:
            lines.append("REFUSED labels (not in approved_labels):")
            lines += [f"- {n}" for n in sorted(refused_labels)]
    if followups_new:
        lines += ["", f"🔁 **Follow-ups ({len(followups_new)})**"]
        for it in followups_new[:cfg["digest_max_items"]]:
            lines.append(f"- [{it['kind']}] {it['from']} — "
                         f"“{it['subject'][:90]}”")
        if len(followups_new) > cfg["digest_max_items"]:
            lines.append(f"- …{len(followups_new) - cfg['digest_max_items']} more")
    if new_contacts:
        lines += ["", f"👥 **New contacts ({len(new_contacts)})**"]
        for c in new_contacts[:cfg["digest_max_items"]]:
            lines.append(f"- {c['name'] or c['address']} <{c['address']}>")
        if len(new_contacts) > cfg["digest_max_items"]:
            lines.append(f"- …{len(new_contacts) - cfg['digest_max_items']} more")
    if summaries:
        lines += ["", "💬 **Summaries (new important mail)**"]
        for s in summaries[:cfg["digest_max_items"]]:
            lines.append(f"- {s['subject'][:80]}: {s['summary'][:160]}")
    if att_extracted:
        n_files = sum(len(m["files"]) for m in att_extracted)
        lines += ["", f"📎 **Attachments: {n_files} file(s) from "
                      f"{len(att_extracted)} message(s)**"]
    out = "\n".join(lines)
    if len(out) > cfg["digest_max_chars"]:
        out = out[:cfg["digest_max_chars"] - 10] + "…"
    return out


# --------------------------------------------------------------------------
# Main pipeline
# --------------------------------------------------------------------------

def make_console():
    """rich Console for interactive output, or None when rich is absent.

    Preflight must work precisely when deps are missing, so rich is probed
    lazily and every caller falls back to plain print(). The cron digest
    never goes through this — stdout must stay a clean notification."""
    try:
        from rich.console import Console
        return Console()
    except ImportError:
        return None


def emit(con, text, style=None):
    """Print one line: styled via rich when available, plain otherwise."""
    if con is None:
        print(text)
    else:
        con.print(text, style=style, markup=False, highlight=False,
                  soft_wrap=True)


def preflight(cfg, rules):
    """--check: validate dependencies, config, ruleset and token WITHOUT
    any Gmail API call. Prints one line per check and returns 0 when the
    install is ready to run (1 otherwise), so scripts can gate on it."""
    problems = []

    # 1. runtime dependencies
    try:
        import googleapiclient  # noqa: F401
        from google.oauth2 import credentials  # noqa: F401
    except ImportError:
        problems.append("missing Python deps — run: "
                        "python3 -m pip install -r requirements.txt")

    # 2. token: present, parseable, refreshable, not group/world-readable
    tp = Path(cfg["token_path"])
    if not tp.exists():
        problems.append(f"token not found: {tp} — run setup_token.py first "
                        f"(README step 2)")
    else:
        try:
            payload = json.loads(tp.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            payload = None
            problems.append(f"token unreadable ({tp}): {exc}")
        if payload is not None:
            if not payload.get("refresh_token"):
                problems.append("token has no refresh_token — re-run "
                                "setup_token.py")
            if os.name == "posix" and (tp.stat().st_mode & 0o077):
                problems.append(f"token permissions too open: {tp} — chmod 600")

    # 3. documented safety bounds (README "Safety invariants")
    if cfg["page_size"] > 25 or cfg["pass_size"] > 25:
        problems.append("page_size/pass_size must stay <= 25 (Gmail quota)")
    if cfg["modify_chunk"] > 50:
        problems.append("modify_chunk must stay <= 50 ids per batchModify")
    if cfg["api_sleep_seconds"] < 1.5:
        problems.append("api_sleep_seconds must be >= 1.5")

    # config + ruleset were already validated by load_config()/load_rules()
    con = make_console()
    catch = rules[-1]
    emit(con, f"config : {cfg['_config_path']}")
    emit(con, f"rules  : {cfg['rules_path']} ({len(rules)} rules; catch-all "
              f"'{catch['name']}' archive=false)")
    emit(con, f"token  : {tp}")
    if problems:
        for p in problems:
            emit(con, f"PROBLEM: {p}", style="red")
        emit(con, f"NOT READY — {len(problems)} problem(s) above.",
             style="red")
        return 1
    emit(con, "READY — next: python3 gmail_inbox_manager.py --dry-run --max 25",
         style="green")
    return 0


def parse_args(argv):
    ap = argparse.ArgumentParser(
        description="Gmail Inbox Manager — classify, label and archive mail "
                    "safely (never sends, never deletes).")
    ap.add_argument("--config", default=os.environ.get("CONFIG", ""),
                    help="path to config.json (default: $CONFIG or ./config.json)")
    ap.add_argument("--rules", default="",
                    help="override the rules file from the config")
    ap.add_argument("--dry-run", action="store_true",
                    help="classify + report only; mutates NOTHING, writes no state")
    ap.add_argument("--max", type=int, default=0,
                    help="max messages scanned per run (0 = config value)")
    ap.add_argument("--scope", choices=("inbox", "all"), default="inbox",
                    help="'inbox' = inbox mail only; 'all' = also label mail "
                         "already archived by other tools but carrying no "
                         "category label")
    ap.add_argument("--recheck", action="store_true",
                    help="re-evaluate already-labeled mail against the current "
                         "rules (use after editing rules)")
    ap.add_argument("--only-label", default="",
                    help="with --recheck: restrict to one category label")
    ap.add_argument("--max-passes", type=int, default=0,
                    help="max pipeline passes this run (0 = all)")
    ap.add_argument("--sum-max", type=int, default=0,
                    help="max body summaries per run (0 = config value)")
    ap.add_argument("--att-days", type=int, default=0,
                    help="attachment-extraction window in days (0 = config value)")
    ap.add_argument("--skip-followups", action="store_true",
                    help="skip the thread follow-up scan")
    ap.add_argument("--skip-attachments", action="store_true",
                    help="skip the attachment scan")
    ap.add_argument("--skip-contacts", action="store_true",
                    help="skip new-contact tracking")
    ap.add_argument("--check", action="store_true",
                    help="preflight: validate deps, config, rules and token "
                         "without any Gmail API call; exit 1 if not ready")
    ap.add_argument("--print-plan", action="store_true",
                    help="print the label plan (existing / would-create) and exit")
    return ap.parse_args(argv)


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])

    config_path = args.config or "./config.json"
    cfg = load_config(config_path)
    if args.rules:
        base = Path(cfg["_config_dir"])
        p = Path(os.path.expanduser(args.rules))
        cfg["rules_path"] = str(p if p.is_absolute() else (base / p).resolve())
    if args.max:
        cfg["max_messages"] = args.max
    if args.sum_max:
        cfg["summary_max"] = args.sum_max
    if args.att_days:
        cfg["attachment_days"] = args.att_days

    dry = args.dry_run or bool(os.environ.get("DRY_RUN"))
    if dry:
        os.environ["DRY_RUN"] = "1"  # !! Unused — nothing reads DRY_RUN again this run

    log = make_logger(cfg["run_log"])
    log(f"=== gmail-inbox-manager start mode={'DRY-RUN' if dry else 'LIVE'} "
        f"config={cfg['_config_path']} max={cfg['max_messages']}")

    rules = load_rules(cfg["rules_path"])
    log(f"rules={cfg['rules_path']} rules={len(rules)}")

    if args.check:
        return preflight(cfg, rules)

    try:
        service = build_service(cfg, log)
    except AuthError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    try:
        service._http.timeout = 120
    except Exception:  # noqa: BLE001
        pass

    me = api_call(
        lambda: with_timeout(120, lambda: service.users().getProfile(
            userId="me").execute()),
        log, "getProfile", cfg).get("emailAddress", "").lower()
    log(f"account={me}")

    # !! Unused — write-only cache: state/labels.json is saved every run but
    # never read back for lookups; resolve_labels() re-queries the API anyway.
    labelmap = load_json(cfg["label_map_path"], {"version": 1, "labels": {}})
    label_ids, plan = resolve_labels(service, cfg, rules, labelmap, dry, log)
    log(f"label plan: existing={len(plan['existing'])} "
        f"approved_new={len(plan['approved_new'])} "
        f"unapproved={len(plan['unapproved'])}")

    if args.print_plan:
        # !! Duplicate — same label-plan printing as render_digest()'s Labels
        # section; kept because --print-plan must work before the digest exists.
        con = make_console()
        emit(con, "EXISTING labels:")
        for n in sorted(plan["existing"]):
            emit(con, f"- {n}")
        emit(con, "NEW labels this run WOULD create:")
        new_all = sorted(plan["approved_new"] + plan["unapproved"])
        if new_all:
            for n in new_all:
                if n in plan["approved_new"]:
                    emit(con, f"- {n}", style="green")
                else:
                    emit(con, f"- {n}  (NOT APPROVED — would be refused)",
                         style="red")
        else:
            emit(con, "- (none)")
        return 0

    if not dry:
        save_json(cfg["label_map_path"], labelmap)

    state = load_json(cfg["processed_ids_path"], {"version": 1, "processed": []})
    processed = set(state.get("processed", []))
    follow_state = load_json(cfg["followups_path"], {"version": 1, "threads": {}})
    contacts = load_json(cfg["contacts_path"], {"version": 1, "contacts": {}})
    att_state = load_json(cfg["attachments_state_path"],
                          {"version": 1, "processed": {}})

    label_names = {r["label"] for r in rules}
    if args.recheck:
        names = label_names
        if args.only_label:
            if args.only_label not in label_names:
                raise SystemExit(f"ERROR: --only-label '{args.only_label}' is "
                                 f"not a category label. Known: "
                                 f"{', '.join(sorted(label_names))}")
            names = {args.only_label}
        ids = list_labeled_ids(service, cfg, names, cfg["max_messages"], log)
    elif args.scope == "all":
        ids = list_unlabeled_ids(service, cfg, label_names, cfg["max_messages"],
                                  log)
    else:
        ids = list_inbox_ids(service, cfg, cfg["max_messages"], log)

    if args.recheck:
        pending = list(ids)
    else:
        pending = [i for i in ids if i not in processed]
    total_passes = (len(pending) + cfg["pass_size"] - 1) // cfg["pass_size"]
    if args.max_passes:
        total_passes = min(total_passes, args.max_passes)
    log(f"ids={len(ids)} pending={len(pending)} passes={total_passes} "
        f"(pass size {cfg['pass_size']})")

    cat_counts = {}
    all_plans, all_applied, all_errors = [], [], []
    all_kept_msgs = []
    skipped_own = []
    pass_no = 0
    for start in range(0, len(pending), cfg["pass_size"]):
        if pass_no >= total_passes:
            break
        pass_no += 1
        chunk = pending[start:start + cfg["pass_size"]]
        log(f"--- pass {pass_no}/{total_passes} ids={len(chunk)} ---")
        try:
            msgs, fetch_errors = fetch_metadata(service, cfg, chunk, log)
        except FatalRateLimit:
            raise
        all_errors.extend(fetch_errors)

        plans, kept_msgs = [], []
        for mid in chunk:
            m = msgs.get(mid)
            if m is None:
                continue
            if any(lb in ("SENT", "DRAFT") for lb in (m.get("labels") or [])):
                skipped_own.append(mid)
                continue
            rule = classify(rules, m["from"], m["subject"])
            cat_counts[rule["name"]] = cat_counts.get(rule["name"], 0) + 1
            if rule["label"] not in label_ids:
                # Label refused (not approved) -> leave the message untouched.
                continue
            in_inbox = "INBOX" in (m.get("labels") or [])
            if rule["archive"]:
                add_inbox = False
            else:
                add_inbox = (args.scope == "all" or args.recheck) \
                    and not in_inbox and rule["name"] != cfg["fallback_label"]
            stale_labels = []
            if args.recheck:
                cur_id = label_ids.get(rule["label"])
                for lname, lid in label_ids.items():
                    if lid == cur_id:
                        continue
                    if lid in (m.get("labels") or []):
                        stale_labels.append(lid)
            plans.append({"id": mid, "label": rule["label"],
                          "archive": bool(rule["archive"]), "rule": rule["name"],
                          "add_inbox": add_inbox, "stale": stale_labels})
            if not rule["archive"]:
                kept_msgs.append(m)

        applied, mod_errors = apply_actions(service, cfg, plans, label_ids,
                                            dry, log)
        all_plans.extend(plans)
        all_applied.extend(applied)
        all_errors.extend(mod_errors)
        all_kept_msgs.extend(kept_msgs)

        if not dry:
            for p in plans:
                if p["id"] not in applied:
                    continue
                m = msgs.get(p["id"]) or {}
                action = "archive" if p["archive"] else "label_keep"
                if p.get("add_inbox"):
                    action = "restore_inbox"
                audit(cfg["audit_log"], json.dumps({
                    "ts": now_iso(), "id": p["id"], "action": action,
                    "category": p["rule"], "label": p["label"],
                    "from": (m.get("from") or "")[:160],
                    "subject": (m.get("subject") or "")[:160]},
                    ensure_ascii=False))
            processed.update(applied)
            processed.update(skipped_own)
            state["processed"] = sorted(processed)[-cfg["state_keep"]:]
            state["updated_at"] = now_iso()
            save_json(cfg["processed_ids_path"], state)
            log(f"pass {pass_no}/{total_passes} applied={len(applied)} "
                f"errors={len(mod_errors) + len(fetch_errors)} "
                f"skipped_own={len(skipped_own)} "
                f"state={len(state['processed'])} processed id(s)")
        else:
            log(f"pass {pass_no}/{total_passes} (dry-run) planned={len(plans)}")

    plans, applied = all_plans, all_applied
    kept_msgs = all_kept_msgs
    now = datetime.now().astimezone()
    archived_count = sum(1 for p in plans
                         if p["archive"] and p["id"] in applied) if not dry \
        else sum(1 for p in plans if p["archive"])
    kept_count = (len(applied) - sum(1 for p in plans
                                     if p["archive"] and p["id"] in applied)) \
        if not dry else sum(1 for p in plans if not p["archive"])

    summaries = []
    for m in kept_msgs[:cfg["summary_max"]]:
        try:
            full = api_call(
                lambda mid=m["id"]: with_timeout(
                    120, lambda: service.users().messages().get(
                        userId="me", id=mid, format="full").execute()),
                log, f"summary {m['id']}", cfg)
            body = body_of(full)
        except FatalRateLimit:
            raise
        except Exception as exc:  # noqa: BLE001
            log(f"WARN summary fetch id={m['id']}: {str(exc)[:120]}")
            continue
        time.sleep(cfg["api_sleep_seconds"])
        summaries.append({"subject": m["subject"] or "(no subject)",
                          "summary": summarize_text(body, m["subject"],
                                                    cfg["summary_chars"])
                          or "(empty body)"})

    fu_new = []
    if cfg["enable_followups"] and not args.skip_followups:
        try:
            fu_new, follow_state = followups(service, cfg, follow_state, log)
            if not dry:
                save_json(cfg["followups_path"], follow_state)
        except Exception as exc:  # noqa: BLE001
            log(f"WARN follow-up scan failed: {str(exc)[:200]}")

    new_contacts = []
    if cfg["enable_contacts"] and not args.skip_contacts:
        new_contacts = track_contacts(contacts, kept_msgs, me, cfg, log)
        if not dry and new_contacts:
            save_json(cfg["contacts_path"], contacts)

    att_extracted = []
    if cfg["enable_attachments"] and not args.skip_attachments:
        try:
            # !! Unused — the per-file skip reasons are computed but never
            # reported anywhere (digest and log only see att_extracted).
            att_extracted, _skipped = extract_attachments(
                service, cfg, att_state, cfg["attachment_dest"],
                cfg["attachment_days"], me, log, dry)
            if not dry and att_extracted:
                save_json(cfg["attachments_state_path"], att_state)
        except Exception as exc:  # noqa: BLE001
            log(f"WARN attachment scan failed: {str(exc)[:200]}")

    total_changed = (len(applied) if not dry else len(plans)) + len(fu_new) \
        + len(new_contacts) + len(att_extracted)
    if total_changed == 0 and not dry:
        log("nothing changed — stdout stays empty (silent run)")
        return 0
    digest = render_digest(cfg, now, len(plans), cat_counts,
                           archived_count, kept_count, all_errors,
                           summaries, fu_new, new_contacts, att_extracted,
                           plan, plan.get("refused", []))
    if dry:
        digest = "(DRY-RUN — nothing was modified)\n" + digest
    print(digest)
    log(f"done: plans={len(plans)} applied={len(applied)} "
        f"errors={len(all_errors)} followups={len(fu_new)} "
        f"contacts={len(new_contacts)} att_msgs={len(att_extracted)}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except FatalRateLimit as exc:
        print(f"ABORTED (rate limit): {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001 - cron watchdog alert
        print(f"FATAL: {exc}", file=sys.stderr)
        sys.exit(1)
