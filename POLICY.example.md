# POLICY — worked example (Policy A)

This file is the POLICY half of the setup: the ruleset says **which label**
a message gets, the policy says **whether the message may leave the inbox**.

Everything below is the worked example from the original installation. It is
an example, not a recommendation for your mailbox — write your own before you
go live. Use the prompts at the bottom.

---

## Policy A (the example)

Two lists decide what happens to a classified message:

**ARCHIVE** — label it, then remove it from the inbox. The message is NOT
deleted: it stays in All Mail, fully searchable and one click from the inbox.
Reversible at any time.

- social / network notifications and digests
- bank and payment alerts
- receipts, invoices and order confirmations
- newsletters and promotions
- job-board digests
- software / subscription notifications

**KEEP** — label it, mark it read, and leave it in the inbox.

- mail from real people (human correspondence)
- direct recruiter emails
- tax / government correspondence
- security alerts that need an action from you
- **anything UNCLASSIFIED**

### The one hard rule

> Never archive an unknown sender.

An unmatched message is labelled with the fallback label (`Unsorted` by
default) and stays in the inbox. The script enforces this: the last rule in
`rules.json` must be a catch-all with `archive: false`, and the script
refuses to start if it is not.

---

## How the policy maps onto the ruleset

Every rule carries an `archive` boolean:

| rule `archive` | effect                                                        |
|----------------|---------------------------------------------------------------|
| `true`         | add the label, mark read, remove `INBOX` (stays in All Mail)  |
| `false`        | add the label, mark read, keep `INBOX`                        |

So the archive/keep split above is expressed directly in the ruleset — there
is no second list to keep in sync. When you edit the policy, edit the
`archive` flag on the matching rules.

---

## Write your own

Answer these before your first live run. Keep the answers next to your
`rules.json` so future-you knows why a rule exists.

**1. Which categories may leave the inbox?**
List the categories you want archived:

```
-
-
```

**2. Which categories must stay in the inbox?**
List the categories that always stay:

```
-
```

**3. What is the one-sentence rule for unknown mail?**
The default is "label it and keep it". If you want something else, say so
here — and remember the script refuses to start if the catch-all rule tries
to archive:

```
-
```

**4. Which senders are "real people" for you?**
These must never be archived by a broad rule. Put them in a high-priority
`Personal` rule with `archive: false` so it is matched first:

```
-
```

**5. Which security subjects need you to act?**
Put them in the `Security-Action` rule (keep, not archive) and above the
routine `Security` rule (archive):

```
-
```

**6. Which subjects are receipts you never need to see again?**
Archive them, but be aware archiving only removes `INBOX` — the mail stays in
All Mail forever:

```
-
```

**7. Which label name should unknown mail get?**
This is `fallback_label` in `config.json` (default `Unsorted`). The catch-all
rule in `rules.json` should use the same label name.

```
-
```

**8. Which label names are you happy for the script to create?**
These go into `approved_labels` in `config.json` — the approval gate refuses
to create anything else:

```
-
```

---

## Changing the policy later

1. Edit `rules.json` (and this file, so the reasoning is recorded).
2. Run the script with `--dry-run` and check the category counts and the
   planned-new-label list in the digest.
3. Run it with `--recheck` to re-evaluate already-labelled mail against the
   new rules. `--recheck` can move a message between archive and keep and
   removes stale category labels.

```sh
CONFIG=./config.json python3 gmail_inbox_manager.py --dry-run --max 500
CONFIG=./config.json python3 gmail_inbox_manager.py --recheck --max 500
```

`--recheck` is the only way to change what happened to mail that was already
processed; the normal run skips anything already in the processed set.
