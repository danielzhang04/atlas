# Constitution amendment: conversation persistence (DD-4)

**Status: DRAFT — awaiting Daniel's explicit sign-off. The feature ships dark until then.**

Rule 10 today reads:

> 10. Persistent logs are bounded and host-shaped: never pairing or shutdown tokens, credentials,
>     private environment values, MCP child environments, prompts, or raw child stdout.

Every persistent file Atlas has written so far obeys that by being **metadata only**. `traces.db`
holds turn timings, token counts, and enum'd tool names. `jobs.sqlite3` holds job ids and titles.
`worker.log` holds host sentences that a formatter truncates at the first path-shaped token.
None of them holds a sentence anybody said.

DD-4 writes one that does. `transcript.db` stores what Daniel said to Atlas and what Atlas said
back, so that closing the window stops being amnesia — the last handful of exchanges come back at
the next start, and anything older is reachable when the model asks for it by name.

That is a real extension of what lives on Daniel's disk, and it is not something a merge should do
quietly. Hence this file, and hence `persistence.enabled: false`.

---

## 1. What is stored

Two rows per exchange, in one SQLite file:

| column    | holds                                                                       |
|-----------|-----------------------------------------------------------------------------|
| `at`      | turn timestamp, unix seconds                                                |
| `role`    | `user` or `assistant` — the column has a CHECK constraint, nothing else fits |
| `text`    | the spoken text, redacted and length-capped (§3, §4)                        |
| `tools`   | the **names** of tools that turn touched, checked against the live registry |
| `tainted` | whether that turn had read outside content — see §6b                        |

`text` is the utterance as the brain already remembers it: what Daniel said, and the concatenation
of what Atlas actually spoke back. Nothing more is derived from it and nothing else is written. The
one place the store deliberately holds **less** than the brain's own history is the reflex-open
lane, whose host-authored framing note stays in memory (§6g); and one shape is not written at all,
the turn that ended with an unanswered readback (§6f).

`tools` is names only. The name in a turn's evidence is the name the *model* asked for — a refused
call still leaves one behind — so each is checked against `ToolRegistry.names()` at write time and
anything unrecognised is stored as `other`. A model cannot get a string of its own choosing into
the file by inventing a tool name.

## 2. What is never stored

Structurally, not by convention — these are not on the path into the store at all:

- **Tool arguments.** `Brain._remember` passes names; `record_exchange` accepts names; the column
  takes nothing else. The path a file tool opened, the query a search tool sent, the text a
  `type_text` typed — none of it reaches here.
- **Prompts.** Not the system prefix, not the persona, not the capability text, not the tool
  schemas. The brain hands the store two strings per exchange and never its message array.
- **Raw child stdout, MCP child environments.** Neither is on this path.
- **Anything at all, on any turn, while `persistence.enabled` is false** (§7).

And two things that are **bounded rather than absent** — separated out because the first draft
listed them as never-stored and that was not exactly true:

- **MCP tool results.** Almost never on this path — but the confirmation lane's failure line quotes
  up to **160 characters** of a tool's own result (`That didn't go through: …`), and that line is
  what the turn persists. It goes through the same redaction and the same per-row cap as anything
  else, and since the rework it also forces that exchange to be recorded **tainted**, so it can
  never return as an untainted boot seed (§6b).
- **Pairing tokens, shutdown tokens, credentials, private environment values.** Rule 1 keeps them
  out of the brain's text in the first place; §3 is a **shape filter** behind it, not a second
  guarantee. §3b says what it catches and §3c says what it does not.

## 3. Redaction before write — what is covered, and what is not

Every string is passed through `sanitize.redact_secrets` before it touches the database.

**This section was rewritten after the adversarial review.** The first draft of this document said
"secrets never reach the file". That was false. The reviewer wrote 19 realistic credential shapes
through `record_exchange` and grepped the raw `.db` bytes: **15 of the 19 survived**, including an
OAuth refresh token, a dictated password, a Gmail app password, a `.env` line, an AWS key pair, a
GitHub token, and the `AQ.Ab8…` format Daniel's own Gemini keys use. The pattern set has been
widened and the claim is now stated as what it is: a **shape filter**, with named limits.

### 3a. The three layers

| layer | catches | example |
|---|---|---|
| **token shapes** | a token that is credential-shaped on its own | `sk-…`, `eyJ…`, `ghp_…`, `xoxb-…`, `AKIA…`, `AIza…`, `AQ.Ab8…`, `1//0g…`, `glpat-`, `npm_`, `SG.`, 16+ hex, UUID, a URL carrying `user:password@` |
| **opaque runs** | 24+ chars of base64/base64url alphabet mixing digits with both cases | an AWS secret key, a raw base64 blob, a long bearer value — none of which carry a prefix |
| **marker context** | a value that is only recognisable from the word in front of it | `password is Tr0ub4dor&3`, `app password qxyz jklm nptv wsdg`, `bearer <value>` |

A private-key header (`-----BEGIN …`) is the one case that cuts to the **end** of the text, the way
the log always does: what follows a PEM header is key material, not a sentence.

The marker layer exists specifically because this store holds what Daniel **speaks**. A dictated
password has no shape a pattern can find; the word "password" in front of it is a signal only a
human produces.

A marker arms a **window** of the following few tokens and redacts the first one in range that
looks like a value — so a noun, a filled pause, or an STT-rendered colon between the two is simply
stepped over ("the password for the **router** is X"). A strong marker also looks *backward*, for
"use X as the password". A sentence-ending token closes the window, so a marker cannot reach into
the next sentence. Markers are split by how much rope they get: `password`/`bearer`/`api key`/
`client secret` are *strong* (six tokens forward, four back); `key`/`secret`/`token`/`pin` are
ordinary English too and get four.

**Nothing is redacted for being in the window** — only for looking like a value while in it. That
single gate is what keeps "the key is to stay calm" and "the secret ingredient" intact, and it is
deliberately conservative: a token must carry a digit, mix case mid-word, or be at least 16
characters if it is spelled like a word. Pinned by
`test_a_marker_in_ordinary_speech_eats_nothing` and
`test_ordinary_conversation_survives_redaction_word_for_word`.

### 3b. Coverage against the reviewer's 19 shapes

All 19 are now caught. Re-run `tests/test_transcript.py::test_realistic_credential_shapes_never_reach_the_file`
(19 parametrized cases, asserted against raw `.db` + `-wal` + `-shm` bytes).

| shape | before | now | by |
|---|---|---|---|
| OpenAI `sk-proj-…` | caught | caught | token shape |
| Anthropic `sk-ant-…` | caught | caught | token shape |
| JWT `eyJ…` | caught | caught | token shape |
| 16+ hex session id | caught | caught | token shape |
| Google OAuth refresh `1//0g…` | **LEAKED** | caught | token shape |
| Google API key `AIzaSy…` | **LEAKED** | caught | token shape |
| Gemini 2026 key `AQ.Ab8…` | **LEAKED** | caught | token shape |
| AWS access key id `AKIA…` | **LEAKED** | caught | token shape |
| GitHub PAT `ghp_…` | **LEAKED** | caught | token shape |
| Slack `xoxb-…` | **LEAKED** | caught | token shape |
| `.env` line `…=postgres://user:hunter2@host/db` | **LEAKED** | caught | token shape (URL userinfo) |
| AWS secret key (40 chars, no prefix) | **LEAKED** | caught | opaque run |
| base64 secret `aGVsbG8…==` | **LEAKED** | caught | opaque run |
| bearer **value** after the marker | **LEAKED** | caught | opaque run + marker |
| dictated password `Tr0ub4dor&3` | **LEAKED** | caught | marker context |
| Gmail app password `qxyz jklm nptv wsdg` | **LEAKED** | caught | marker context (4×4 group) |
| Gmail app password, joined | **LEAKED** | caught | marker context |
| PEM private key body | **LEAKED** | caught | PEM cut to end |
| `ASSISTANT_SIDE_SECRET` (assistant row) | **LEAKED** | caught | SCREAMING_SNAKE credential name |

One shape was added beyond the reviewer's list, found while checking false positives: a **payment
card number**, both run-on and spoken as four groups.

**It is gated behind a card marker** (`card`, `visa`, `mastercard`, `amex`, `credit`, `debit`,
`cvv`, …), and the reason is a correction to what an earlier draft of this section claimed. That
draft said the rule was "validated with Luhn so it cannot fire on an order number, a year, or a
phone number." That is false, and it was stated as an impossibility:

- Luhn accepts roughly **one numeric string in ten**. Measured over 20,000 random samples per
  length: 13-digit **9.7%**, 16-digit **9.6%**, 19-digit **10.1%**. It removed ~90% of order
  numbers, not all of them.
- The four-group spoken form inherits that rate: **about one in ten** permutations of ordinary
  years and clock times (`2026 0800 1430 …`) passes Luhn and was being eaten. (An earlier draft
  quoted "2,091 of 20,736" here. That figure is real but not reproducible from the text — 20,736
  is 12⁴, so it depends entirely on which twelve four-digit values the probe happened to use, and
  a reviewer's own twelve gave 2,212. The rate is the fact; the count was an artefact of a
  candidate list this document never named.)
- And one whole category it removed **none** of: **every IMEI is Luhn-valid by specification**.
  `my imei is 490154203237518` was redacted, always, by design of the checksum rather than by bad
  luck.

So Luhn is now a confirmation, not the filter. The filter is the marker. `my card number is
4111 1111 1111 1111` is redacted; `my imei is 490154203237518`, `order number 4829571234567` and
`the numbers are 2024 1200 2026 0800` are not. **A card number spoken with no card word near it is
therefore not caught** — that is the price of not destroying every device id Daniel reads aloud,
and it is listed in §3c. Pinned by `test_a_card_number_is_only_looked_for_behind_a_card_marker`.

### 3c. What is NOT covered — stated plainly

**The marker layer was widened a second time, after the re-review.** The first version allowed at
most two tokens from a *closed connector set* between a marker and its value. A reviewer ran 17
realistic ways a person dictates a password **to a voice assistant** and **10 leaked** — and none
of them was an exotic input:

> `the password for the router is …` · `my password, uh, is …` · `password colon …` ·
> `use … as the password` · `password for the admin account …` · `the api key for stripe is …`

Every leak had one cause: an ordinary noun ("router"), a filled pause ("uh"), or an STT-rendered
colon sat between the marker and the value, and the marker disarmed. The connector list is gone.
A marker now arms a **window** of the next few tokens, skips whatever is in between, and redacts
the first thing in range that looks like a value; a strong marker also looks *backward*, because
"use X as the password" puts the value first. **All 17 are now caught**
(`test_a_password_dictated_the_way_people_speak_is_redacted`).

What is still not covered:

- **A dictated passphrase with no marker.** "the code word is correct horse battery" is stored
  verbatim. There is no shape to find and no marker to key on; it is ordinary words. Detecting it
  would mean redacting ordinary words, which would empty the store.
- **A password made of ordinary words even WITH a marker.** "my password is correct" keeps
  `correct`. The window's gate is that the value must *look like* a value — carry a digit, mix case
  mid-word, or be at least 16 characters if it is spelled like a word. Loosening that gate is
  exactly what starts eating conversation.
- **A key spelled out letter by letter** — "the key is A Q dot A b 8 R N 6 J m X q" — which is how
  a voice assistant actually receives one. Every token is a single character; none is a value shape
  and none can be made one without redacting the alphabet.
- **Scripts that do not delimit words with whitespace.** Chinese, Japanese and Korean arrive as one
  token, so the window has nothing to step over: `我的密码是Zq7Wm2Lp` is stored verbatim. The
  marker vocabulary is Latin- and Cyrillic-script and is a short list, not a translation table.
- **A payment card with no card word near it** (§3b).
- **A value that falls outside the marker's window.** The window is finite, so a value more than
  **six tokens after** its marker, more than **four tokens before** it, or on the far side of a
  **sentence boundary** is not caught. All four of these leak, and all four are ordinary speech:

  > `the password for the upstairs guest network router is Zq7Wm2Lp` — seven tokens forward
  > `the password that I set up last week for the box is Zq7Wm2Lp` — ten tokens forward
  > `what is the password again? it is Zq7Wm2Lp` — the `?` closes the window
  > `Zq7Wm2Lp okay so that one is the password` — six tokens back

  Widening the window further is not free — §3a's gate is the only thing standing between the
  marker layer and ordinary speech, and the cost paragraph below is what a wider window buys more
  of. These are the accepted edge of that trade, not an oversight.

All of these are pinned as *gaps* — by
`test_an_unmarked_dictated_password_is_not_caught_and_is_not_claimed`,
`test_a_value_outside_the_marker_window_is_not_caught` and
`test_a_card_number_is_only_looked_for_behind_a_card_marker` — so that a later change cannot
quietly start claiming coverage this does not have.

**Cost of the widening — measured on a corpus built to find it.** An earlier draft of this
paragraph said the widening "cost **zero** new false positives". That was measured on corpora that
*cannot detect the cost*: 36 lines of ordinary developer speech and 15 marker-adjacent sentences,
none of which puts a value-shaped token anywhere near a marker — which is the only condition under
which the window can do damage. Both still pass byte-identical, and that fact is worth much less
than it looked.

Widening the window cost zero false positives *on both corpora* — but neither corpus puts a
value-shaped token near a marker, and that is where the cost lands. A CamelCase identifier within
four tokens *before* a strong marker, or a date, time, version or long number within six tokens
*after* one, is now redacted: `AuthController handles the password reset` and `my password expires
2026-09-15` each lose a token. Measured on a corpus built for it, **19 of 25** such sentences lose
one word (an independent corpus built by the reviewer put it at 23 of 25 — call it three in four).
That is the price of catching 17 of 17 dictations, and it is **one word, never the sentence**.

That last clause is the load-bearing one and it is pinned, not asserted: every altered sentence in
that corpus loses **exactly one** token and the remaining words survive verbatim
(`test_the_cost_of_the_marker_window_is_one_word_never_the_sentence`).

Of the 19, **8 are structurally new** — they come from the *backward* pass, which did not exist
before the re-review, and they are all the same shape: a CamelCase product or class name sitting in
front of a credential word (`BitWarden replaced my password manager`, `PostgreSQL stores the
password hash column`). 10 come from the forward window reaching a date, time, version or long
number. 1 is not the marker layer at all — `bearer bonds were a 1980s thing` loses `bearer` to the
token-shape rule, and has since before the widening.

The known false positives **include** these three — the count is not a promise that there are no
others, and two of them are things Daniel says weekly:

1. A **full git SHA** (`git sha 9c1d2e3f…` → redacted) — 40 hex characters is the 16+-hex-run rule
   doing its job; a commit hash and a session token are the same shape.
2. A **UUID** request id (`3f2504e0-4f89-11d3-…` → redacted) — same rule, same reasoning.
3. A **SCREAMING_SNAKE variable name** said aloud (`OPENAI_API_KEY`) is redacted even when no value
   follows it.

All three are derivable from §3a's table and all three are defensible losses of one word — but
they are losses, and a signer should not have to derive them.

### 3d. How this differs from the worker log — **two** differences, not one

The first draft said the two rules differ in exactly one way. They differ in two.

1. **Truncation.** The log truncates at the first offending token and drops the rest of the line;
   a log line's tail is diagnostic detail nobody needs badly enough to risk. Conversation text *is*
   the payload, so here the token — and only the token — becomes `<redacted>` and the sentence
   survives. Punctuation attached to the token goes with it, so no suffix of a key is left behind.
2. **Paths, `@`, and URLs.** The log also redacts on `_UNSAFE_LOG_TOKEN` — any backslash, `@`,
   drive letter, or `/x` path shape. **The store does not**, and this was previously undisclosed.
   Conversation is made of paths, folder names and email addresses; applying that rule here would
   empty the store of the thing it exists to keep. The one credential-bearing URL shape — userinfo
   in the authority, `postgres://user:hunter2@host/db` — is unambiguous and *is* covered, as a
   token shape. So `~/.claude/settings.json` and `daniel.zhang.t1@gmail.com` are stored as spoken.
   Pinned by `test_a_credential_bearing_url_dies_but_an_ordinary_path_does_not`.

`sanitize.secret_shaped` remains one definition shared by both sinks, so a shape added for one is
added for both.

This is defence in depth, and it is not a claim that a secret would otherwise be spoken — rule 1 is
the first wall. It is also not a guarantee. It is a filter with a measured hit rate and named gaps.

## 4. Bounds

Three caps, all enforced at write time *and* on the boot sweep, oldest-row-first:

| bound | value | why |
|---|---|---|
| retention | **30 days** | matches `traces.db`; one number for "how far back Atlas remembers anything" |
| rows | **20,000** (10,000 exchanges) | over a month at a heavy 300 turns/day, so retention normally binds first |
| stored text | **4,194,304 characters** | `SUM(LENGTH(text))` — ~13,000 turns |
| per turn | **4,096 characters** | backstop under `brain.MAX_TRANSCRIPT`; a longer turn is truncated with a marker, never dropped |

The text cap measures stored text, not file size, on purpose: SQLite does not return pages on
`DELETE`, so an eviction loop measured against the file would never converge. Incremental
auto-vacuum reclaims the freed pages after an eviction so the file does not grow without limit.

It is the backstop against a pathological run — a wall of pasted text, a loop — which is exactly
the case a row count alone would not catch.

**The text cap is not a disk budget, and the first draft called it "4 MiB" as though it were.**
`LENGTH()` in SQLite counts *characters*, so 4,194,304 is 4 million characters, and what that costs
on disk depends entirely on the script. Measured at a full cap on this machine:

| content | stored text | main `.db` | peak `-wal` | **peak on disk** |
|---|---|---|---|---|
| ASCII | 4.19M chars | 5.08 MB | 4.13 MB | **9.22 MB** |
| CJK | 4.19M chars | 14.51 MB | — | **~18 MB** |
| emoji | 4.19M chars | 19.42 MB | — | **~23 MB** |

So the honest sentence is: **the store keeps at most ~4.2 million characters of conversation, which
occupies roughly 5 MB on disk for English and up to ~20 MB for CJK or emoji, plus a write-ahead log
of up to ~4 MB while a session is open.** The WAL is checkpointed away on close. If a hard disk
budget is wanted instead, that is a different cap and a different design; this one bounds
conversation, not bytes.

### 4a. Write cost — measured, after the rework

The first draft described the bounds check as "O(1) writes". At the shipped row cap that was false.
Once at `max_rows`, every `record_exchange` evicted exactly the 2-row surplus and then re-derived
the running totals with a full `COUNT(*) + SUM(LENGTH(text)) + MIN(at)` scan of 20,000 rows — on
the asyncio loop carrying Daniel's audio, in an app with a freeze history.

Two changes: evict a **batch** (1% of the cap) so one write in a hundred pays for an eviction, and
**decrement** the running totals from the delete's own `WHERE` clause instead of rescanning. Both
delete clauses are index-served (`id <= ?` is a rowid range, `at < ?` uses `turns_at`), and `MIN`
and `MAX` are single-column index seeks.

A/B in one process, same machine, same load, 600 writes at a full 20,000-row store:

| | evicting writes | median | p95 | p99 | max |
|---|---|---|---|---|---|
| before | 600 / 600 | 89.68 ms | 194.35 ms | 286.90 ms | 313.19 ms |
| after | **6 / 600** | **6.11 ms** | **8.25 ms** | **19.37 ms** | **40.88 ms** |

The store still never sits above `max_rows`; the batch means it sits a little under. Pinned by
`test_the_row_cap_evicts_in_batches_and_stops_rescanning_the_table`, which asserts the shape (one
recount, at connection open, for 400 exchanges through a 200-row store) rather than a millisecond
count.

**End-to-end `record_exchange`** — the whole write, redaction included — against a full store:

| turn | median | p95 | max |
|---|---|---|---|
| a realistic voice exchange (77 chars said, 39 spoken) | **3.25 ms** | 4.58 ms | 14.4 ms |
| 4,096 characters of prose — the per-row backstop | 12.1 ms | 26.8 ms | 51.5 ms |

A deliberately degenerate 4,096 characters (2,048 single-character tokens, which no real utterance
looks like) costs 41.9 ms median. It is the pathological ceiling, not a working figure.

**Redaction alone**, on the same machine and the same inputs: **0.23 ms** median for the realistic
turn, **7.3 ms** for 4,096 characters of prose. An earlier draft put a 21.9 ms end-to-end figure in
the table and a "~10 ms" redaction figure two paragraphs below it and left the reader to notice
they were measuring different things; these are now labelled, and the reviewer's independent run on
other hardware read 5.20 ms / 19.2 ms for the same two rows. Treat the ratios as the finding and
the absolute numbers as machine- and corpus-dependent.

Redaction is per token, so its cost tracks token count, and two things keep it affordable.
`sanitize.secret_shaped` takes an **exact** fast path for short all-letters tokens — no shape in
the set can match one except `bearer` and `eyJ`, so ordinary prose never reaches the 20-branch
alternation; that alone took 4,096 characters from 42 ms to 23 ms, verified behaviour-identical
over 300,000 random tokens. The marker rewrite then roughly halved it again, because a window
computed once per token replaced a state machine that re-ran several alternations on each one.

This whole path runs in `Brain._remember`, from the `finally` **after** every spoken chunk has
already been yielded. It is not in front of Daniel's audio; it is behind it.

### 4b. Retention is wall-clock arithmetic — the guard, and its limit

Retention compares `now` against a stored timestamp, so a machine whose clock jumps **forward** —
dead CMOS battery, a bad NTP step, a hand-typed date — would compute a cutoff past every row and
empty the store on the next write.

There is no trusted clock to check against, so the guard uses the only other evidence available:
the newest row Atlas itself wrote. A gap larger than **twice the retention window, and never less
than 90 days**, is read as a clock fault rather than as elapsed time, and retention is skipped for
that pass.

**What that buys is exactly one write, and the earlier draft of this paragraph framed that as a
feature.** It said the guard "self-corrects immediately … the following pass applies the window
normally", which is true and is also the whole limitation seen from the flattering side. Said
plainly: the first write after the clock breaks keeps history. That write lands stamped with the
faulty clock, so the guard's own reference moves with it, and the **second** write — with no time
passing at all — applies the window and deletes everything older. The guard is a speed bump that
carries history through the turn in which the clock breaks. It is not protection against a clock
that stays broken, and it does not make retention safe on an untrusted clock.

**What it does not catch at all:** a forward jump *smaller* than the threshold. A 40-day jump and a
40-day absence are the same two numbers, and the second is a real thing that should delete a month
of history.

Pinned, and named for the limit rather than the feature, by
`test_a_clock_jump_defers_retention_for_exactly_one_write`.

## 5. Encryption at rest — decided against, on measurement

The honest answer is that the only cipher reachable without a new third-party dependency is
Windows DPAPI through `ctypes` (`CryptProtectData`), and it is the wrong tool here.

**Measured on this machine, against the venv interpreter:**

- ~860 µs to protect and ~592 µs to unprotect a 300-byte row; the cost is dominated by a fixed
  ~1.2 ms per call, not by payload size (a 300-byte and a 4 KiB payload cost about the same).
- ~230 bytes of ciphertext overhead per row — roughly 75% on a typical 300-character turn, which
  fights the size cap directly.

A keyword search has to decrypt every candidate row, because ciphertext is not searchable. Over a
full 30-day store (~18,000 rows) that is **~23 seconds** for a tool that has to answer inside a
voice turn. Capping the scan to keep it fast would silently turn "search everything kept" into
"search the last few days", which is the half of the feature Daniel actually asked for. Batching
many turns into one encrypted segment fixes the speed and buys an append-log with re-encrypted
tails, coarse eviction, and a new corruption mode.

And it would be protecting against the wrong thing. DPAPI is keyed to the Windows logon, so
**any process already running as Daniel decrypts it for free** — and a process running as Daniel is
the threat that matters for a file inside his own profile. There is no AES in the standard library,
and a hand-rolled cipher is worse than none.

So: **plaintext, redacted, under the user profile.** The store lives at
`%LOCALAPPDATA%\Atlas\transcript.db`, beside `traces.db` and `jobs.sqlite3`.

### 5a. One thing to decide, Daniel — the ACLs are not what they should be

The usual sentence here is "it sits under the user profile with default ACLs". On this machine that
is **not true**, and it is worth knowing before signing:

```
> icacls %LOCALAPPDATA%
C:\Users\danie\AppData\Local  S-1-5-21-818925566-...-3169261931:(OI)(CI)(M)
                              MSI\CodexSandboxUsers:(OI)(CI)(M)
                              S-1-5-21-1557731587-...-2966199677:(OI)(CI)(M)
                              S-1-5-21-2647961035-...-710158332:(OI)(CI)(M)
                              S-1-5-21-3262500355-...-1112611458:(OI)(CI)(M)
                              NT AUTHORITY\SYSTEM:(I)(OI)(CI)(F)
                              BUILTIN\Administrators:(I)(OI)(CI)(F)
                              MSI\danie:(I)(OI)(CI)(F)
```

`MSI\CodexSandboxUsers` and four unresolved local SIDs hold **Modify** on `%LOCALAPPDATA%`, and it
inherits onto `%LOCALAPPDATA%\Atlas`. These look like sandbox accounts left behind by the Codex
CLI. They are separate local principals, so DPAPI *would* have stopped them — this is the one case
where §5's reasoning does not fully apply.

The right fix is the ACL, not the cipher: strip inheritance on the Atlas directory and grant only
SYSTEM, Administrators, and `danie`. That is a one-line change to make deliberately, and it also
protects `traces.db`, `jobs.sqlite3` and `worker.log`, which have the same exposure today. It was
deliberately **not** done as a side effect of this unit: quietly rewriting ACLs on a directory is
not something a conversation-history feature should do on its own initiative.

**Decision needed:** harden `%LOCALAPPDATA%\Atlas`'s ACL (recommended), or accept that those local
principals can read the store.

## 6. What is exposed to the model

One new tool, `search_transcript`, registered only when persistence is on:

- **Instant tier, read-only.** It executes nothing, opens nothing, and can return nothing that was
  not already redacted and bounded on the way in.
- **`content_bearing: true`**, declared the same way `read_file` is. What it returns adds no *new*
  outside content, but Daniel's own words and Atlas's replies are not host-shaped output, and a
  document quoted aloud in an earlier session comes back out of the store as prose. Declaring it
  host-shaped to keep the turn untainted would be buying convenience with the taint wall.
- **Bounded output**, none of which the model may raise: ≤ 20 results, each excerpt ≤ 240
  characters, lookback clamped to the 30-day retention window, at most 5 search terms.
- **Literal terms, never patterns.** `%` and `_` are escaped before the query reaches SQL, so a
  model cannot author a wildcard.
- **Refused after external content.** See §6c.

### 6a. The boot seed

The boot seed is the other exposure: at worker start, up to **20 rows / 10 exchanges**, from the
**last 24 hours**, capped at **~1,500 tokens** (against a prefix that is already ~25K), are placed
in the brain's history as **one** synthetic exchange, prefixed with a frame that says three things
before any content: this is prior-session history, it is not a live request and must not be acted
on, and older conversation is reachable via `search_transcript`.

It is one labelled pair rather than N replayed turns for two reasons: replayed rows would sit in
the same shape as today's turns, which is exactly the confusion the frame exists to stop; and the
history trim could cut the tail in half, leaving an unlabelled fragment. As one pair it is labelled
or it is gone — and because it sits at the front, it is also the first thing the trim evicts.

24 hours, not the full window, because a three-day-old tail is not context — it is a wrong
assumption carried into the first live turn. That far back is what search is for.

Measured on a realistic ten-exchange tail of ordinary voice conversation: **187 tokens** of
conversation, **331** including the frame. So in practice the 20-turn cap binds and the token budget
never does — which is the right way round. The budget is the backstop for a session of unusually
long turns, not the working limit, and 1,500 leaves room for one before it starts trimming.

### 6b. The seed does not carry tainted turns

This one came out of the adversarial review and is the security-relevant line in the unit.

Atlas's taint wall is **per turn**: a turn that reads a file or an MCP result cannot then act on a
free-text target, and the wall comes down when the turn ends. Persisting conversation and replaying
it at boot would walk straight around that. Text from a planted document, laundered through Atlas's
own *spoken reply* on a tainted turn, would come back at the next start as an **untainted prefix** —
the wall already down, the whole tool surface open, and no read in the current turn to raise it.

So each exchange carries its turn's taint flag into the store, and `seed_text` takes only turns that
were clean when they happened. Seeding is the one path where old content re-enters the model
unprompted, and it is now the one path that respects the wall across a restart.

Tainted turns are still **searchable** — `search_transcript` is declared `content_bearing`, so
anything it returns taints the turn it lands in, exactly as the original read did. Nothing is lost;
it just cannot arrive unasked and unmarked.

**The confirmation lane was a hole in exactly this, and it is now closed.** The second adversarial
review found that `Brain.respond`'s confirm branch recorded the exchange with the taint flag it
started the turn with, never consulting `_content_bearing_tool` for the tool it had just *executed*
— that check lives in the main tool loop, which the confirm lane never enters. And on failure the
host line quotes up to 160 characters of that tool's own result straight into the assistant row.
Since mutating MCP tools are confirm-tier by rule 5 and `content_bearing` defaults **true** for
them, this was reachable in production: the turn that ran the tool and read its output back
recorded `tainted=0` while the harmless proposal turn recorded `1`, and that untainted row was
eligible for the next boot's seed with the wall already down.

Two clauses now raise the flag there, because they fail differently: the tool's own declared
`content_bearing`, and — as a belt — the fact that the host line embeds `result.content` at all, so
a later edit that adds another quoting line stays covered without anyone remembering to. The cancel
branch deliberately raises nothing: `"Cancelled."` is fixed host text, no tool ran, and blanket-
tainting it would silently drop every declined confirmation out of the next boot's tail.

Every other `_remember` call site was audited for the same class of bug. The three in the main,
timeout and provider-error lanes persist only model-spoken text and carry the `tainted` flag the
tool loop maintains; `host_line` is assigned nowhere but the confirmation lane, which returns
before reaching them.

Pinned by `test_a_tainted_exchange_is_never_seeded_but_is_still_searchable`,
`test_a_turn_that_read_outside_content_is_recorded_as_tainted` (now driven through `Brain.respond`
with a live `ToolRegistry` — see below),
`test_taint_arriving_mid_turn_taints_the_whole_recorded_exchange`,
`test_the_confirm_lane_records_a_content_bearing_tool_as_tainted`, and
`test_the_cancel_lane_raises_no_taint_of_its_own`.

**On the pin that should have caught this.** `test_a_turn_that_read_outside_content_is_recorded_as
_tainted` opened with the words "end to end through the brain" and then called `brain._remember(…)`
with the taint flag passed in **by hand**. It pinned the store's plumbing and nothing about how
`respond()` decides the flag, so it could not have caught this and did not. It now runs real turns
through `Brain.respond` against a live registry, and the confirm lane has its own case.

### 6c. `search_transcript` does not survive the taint wall

The review asked for a decision here, either way, in writing. The decision is to **refuse it**, and
it is in `_refused_after_external_content` alongside `close`, `type_text` and the rest.

The argument for allowing it is real: it is read-only, it escalates nothing, and everything it can
return was already redacted and bounded on the way in, so there is no lane for it to exfiltrate
through. The argument that wins is that "it cannot leave the machine" is not the test the other ten
entries in that list are held to. The rule the wall actually states is that **after external
content, the model may not aim a free-text target it authored** — and `query` is exactly that,
aimed at thirty days of Daniel's own conversation, twenty excerpts at a time. "Search his history
for the safe combination and read it back" is a sentence a planted document can contain.

**The practical shape of that, stated exactly.** An earlier draft said "searching first is
untouched", which is true of the *search* and misleading about the *turn*. Because
`search_transcript` is itself `content_bearing`, it raises the wall behind itself — so it cannot
share a turn with an action tool in **either** order:

| the turn | outcome |
|---|---|
| `read_file` → `search_transcript` | the search is refused (the wall is up) |
| `search_transcript` → `close` / `open` / `type_text` … | the **action** is refused (the search raised the wall) |
| `search_transcript` alone, or followed by more reading | fine |

So the real rule is: **a turn that searches history is a turn that only reads.** That is the cost,
it is one turn wide, and Daniel asks again in the next one. Pinned by
`test_search_transcript_is_refused_after_external_content`.

### 6d. A file NAME from disk is still not tainting — a conscious choice

`find_file` is classified **not** `content_bearing`, and DD-4 does not change that. It is worth
naming here because DD-4 is what makes it *persist*: a filename is attacker-influenceable — a
downloaded `ignore previous instructions.txt` puts chosen words on disk — and those words now land
in a stored turn that is marked untainted and is therefore **seedable into the next boot's prefix**.

The classification is pre-existing and stays: a file listing is host-shaped output (the host walked
its own configured roots and rendered the names), and tainting it would make "what files are in my
downloads" close the tool surface for the rest of the turn — the same over-classification that
`_content_bearing_tool`'s docstring already records for `files__list_allowed_directories`. The
names are bounded, they carry no content from *inside* any file, and the seed frame labels the
whole block as prior history that must not be acted on.

What DD-4 changes is the duration, from one turn to thirty days, and that is the part that should
be a decision rather than an inheritance. It is listed in the sign-off.

### 6e. A broken store must not sound like an empty one

Every database failure is swallowed — losing conversation history is not a reason to lose the turn
— and the only signal was one once-only `logger.warning` in a file nobody reads. That makes
corruption **permanent and silent**: every search answers "nothing matched", which is the same
sentence a healthy store uses for a question it has no record of.

Two signals now. The failure count escalates by powers of ten, so a store failing every write says
so again at 10, 100 and 1,000 rather than once at boot; and `TranscriptStore.degraded` changes what
`search_transcript` returns, so an empty answer from a broken store reads as "the stored
conversation could not be read — this is a failure of the store, not an empty result". That puts
the failure in front of Daniel in the channel he is actually using. Pinned by
`test_a_broken_store_says_so_instead_of_answering_nothing_matched`.

### 6f. An UNCONFIRMED readback is not persisted at all

The seed frame does not merely present prior conversation, it makes a **claim** about it: "every
line was already answered and already acted on". For almost every turn that is true, because a turn
ends when Atlas has finished doing the thing. It is false for exactly one shape — a turn that ended
with the host's single pending action still standing.

The shape, end to end. Daniel says "delete that". A confirm-tier tool returns `needs_confirmation`,
the host mints the pending, Atlas reads the action back and asks for a yes or no. Daniel closes the
window without answering. Rule 5 holds perfectly: the pending is in memory, it dies with the
process, and **nothing was executed**. But the exchange was already filed, and — for the host
confirm-tier tools — filed clean: `_HOST_CONTENT_BEARING` is `{list_windows, read_file,
search_transcript}`, so `press_delete` and `window_action(close)` raise no taint, `context` is
normally `None` so `tainted=bool(context)` is `False`, and §6b's wall lets it through. At the next
boot the model is handed "delete that" and Atlas's readback of it, under a frame asserting both
were already acted on. Ask "did you delete it?" and get a confident yes about a deletion that never
happened — which is worse than an admitted gap, because it is the model's own history telling it so.

**The host already knows.** At `Brain._remember` time, `registry.pending` is either set or it is
not, and set means precisely "this turn ended in a question Daniel has not answered". So the
exchange is **not written to the store**. It stays in this session's in-memory history, where the
pending it refers to is still real, and it is gone at the next boot along with the pending itself —
which is the honest state of affairs, because the two only make sense together.

Not persisted rather than persisted-and-flagged, deliberately. A flag has to be read correctly by
`seed_text`, by `search_transcript`, and by whatever reads the store next; an unanswered readback
has no content that survives the session anyway; and the failure mode of a missed flag is the
confident-false-yes above. This is a small, deliberate hole in the record — a readback Daniel never
answered leaves no trace on disk — and it is listed in the sign-off as such.

Pinned by `test_an_exchange_that_ends_with_a_live_pending_is_not_persisted`.

### 6g. The reflex host note is not persisted

`REFLEX_HOST_NOTE` (§ `Brain.remember_host_exchange`) is a ~60-word block the host appends to the
user side of a reflex-open turn, telling the model that the host answered this one from its own
vocabulary, that the reply is the host's words and not the model's, and that it may never say a
line like it without calling the tool. It is framing for a model about to read the next few turns.

It is **in-memory only**. Persisting it put a paragraph Daniel never said into a row labelled
`user`, and returned it at the next boot nested inside `PRIOR_SESSION_FRAME` — a framing wrapped in
another framing, describing a session that had already ended. The store is handed his words alone.

The same lane also now files the **name of the tool it ran** (`open`, `open_folder`,
`focus_last_opened`). §1 says `tools` holds the names of the tools the turn touched, and a reflex
open touches one; an empty column there made a turn that opened a window look like small talk. It
stays **untainted** — all three are host tools and none is content-bearing.

Pinned by `test_the_reflex_host_note_never_reaches_the_store` and
`test_a_reflex_open_reaches_the_models_history_so_later_pronouns_resolve`.

## 7. The flag

`config/atlas.yaml`:

```yaml
persistence:
  enabled: false
```

While it is false:

- `runtime.build` constructs no store, and `Runtime.transcript` is `None`.
- The brain holds no store reference, so `_remember` writes nothing.
- `search_transcript` is **not registered** — it is absent from the capability text and from the
  tool schemas. The model is not told it exists.
- `transcript.db` is never created. Nothing is created at boot either: the sweep and the seed both
  open an existing file, never create one, so even with the flag *on*, a session that says nothing
  leaves nothing behind.

`enabled is True`, not `is not False`. Traces default on and read their config the other way; this
is the first content-bearing sink in Atlas, so a missing section, a null, `"yes"`, or `1` all read
as off. Only the literal turns it on.

## 8. Proposed rule 10

> 10. Persistent logs are bounded and host-shaped: never pairing or shutdown tokens, credentials,
>     private environment values, MCP child environments, prompts, or raw child stdout.
>     The single exception is the conversation store (`transcript.db`), which persists spoken
>     conversation text and tool names — never tool arguments — only while `persistence.enabled`
>     is true, filtered through the same secret patterns as every other persistent sink, and
>     bounded by 30-day retention, 20,000 rows, and 4 million characters of stored text.
>     Everything rule 10 forbids is forbidden there too.

## 9. What backs each claim

`tests/test_transcript.py`, 121 tests. These are pins, not coverage: if one stops holding, this
document is no longer describing the code.

Each claim below is worded to be **exactly** what the test asserts. The first draft's row here read
"secrets never reach the file"; 15 of 19 realistic shapes did. An overstatement in this document is
worse than a gap in the code, because this is the document that gets signed.

| claim | test |
|---|---|
| names only, on the reply row | `test_an_exchange_lands_as_two_rows_with_tool_names_on_the_reply` |
| no model-authored string in `tools` | `test_a_tool_name_the_registry_does_not_have_is_recorded_as_other` |
| **19 realistic credential shapes** — the reviewer's own list — do not reach the file | `test_realistic_credential_shapes_never_reach_the_file` (19 cases, raw `.db`/`-wal`/`-shm` bytes) |
| the five original shapes still die, token-only, sentence intact | `test_secret_shaped_tokens_never_reach_the_file` |
| a marker takes the VALUE after it, not just itself | `test_a_marker_takes_the_value_after_it_not_just_the_marker` |
| a credential-bearing URL dies; ordinary paths, emails and URLs do not | `test_a_credential_bearing_url_dies_but_an_ordinary_path_does_not` |
| ordinary conversation survives redaction word for word | `test_ordinary_conversation_survives_redaction_word_for_word` (9 lines) |
| **17 realistic ways a password is dictated to a voice assistant** are redacted | `test_a_password_dictated_the_way_people_speak_is_redacted` (17 cases) |
| a marker sitting next to innocent text eats nothing | `test_a_marker_in_ordinary_speech_eats_nothing` (15 lines) |
| the marker window's cost is **one word, never the sentence** — and 19 of 25 is still the measured rate | `test_the_cost_of_the_marker_window_is_one_word_never_the_sentence` (25 lines) |
| a value outside the window — too far after, too far before, or past a sentence boundary — is **not** caught | `test_a_value_outside_the_marker_window_is_not_caught` |
| the marker vocabulary is not English-only | `test_the_marker_layer_is_not_english_only` |
| a card number is looked for **only** behind a card word — an IMEI, order number or clock time is not | `test_a_card_number_is_only_looked_for_behind_a_card_marker` |
| **NOT caught, and pinned as gaps**: an unmarked passphrase, an ordinary-word password behind a marker, a key spelled out letter by letter, and any script without whitespace between words (CJK) | `test_an_unmarked_dictated_password_is_not_caught_and_is_not_claimed` |
| a redacted secret is not findable afterwards | `test_a_redacted_secret_is_not_findable_by_searching_for_it` |
| 30-day retention, at write time | `test_retention_drops_turns_past_the_window_at_write_time` |
| ...and on the boot sweep | `test_the_boot_sweep_applies_retention_without_a_write` |
| a forward clock jump defers retention for **exactly one write** — the second write wipes, with no time passing — and a real absence still expires normally | `test_a_clock_jump_defers_retention_for_exactly_one_write` |
| a row cap too small to hold one exchange is refused, not silently kept empty | `test_a_row_cap_too_small_to_hold_an_exchange_is_refused` |
| row cap, oldest first | `test_the_row_cap_evicts_oldest_first` |
| the row cap evicts in batches and does not rescan the table per write | `test_the_row_cap_evicts_in_batches_and_stops_rescanning_the_table` |
| text cap, oldest first, converges | `test_the_byte_cap_evicts_oldest_first_and_converges` |
| seed framing | `test_the_seed_is_framed_as_prior_history_and_not_as_the_live_turn` |
| seed budget / count / age caps | `test_the_seed_stops_at_the_token_budget_keeping_the_newest`, `test_the_seed_is_capped_by_turn_count_and_by_age` |
| search bounds the model cannot raise | `test_search_is_bounded_in_count_snippet_length_and_lookback` |
| search terms are literals | `test_search_terms_are_literals_never_patterns_the_model_authors` |
| `search_transcript` is refused after external content | `test_search_transcript_is_refused_after_external_content` |
| the seed never carries a tainted turn | `test_a_tainted_exchange_is_never_seeded_but_is_still_searchable` |
| a turn that read outside content records tainted — **through `Brain.respond`, live registry** | `test_a_turn_that_read_outside_content_is_recorded_as_tainted` |
| taint arriving mid-turn taints the whole exchange | `test_taint_arriving_mid_turn_taints_the_whole_recorded_exchange` |
| the confirm lane taints an executed content-bearing tool | `test_the_confirm_lane_records_a_content_bearing_tool_as_tainted` |
| ...and the cancel lane still does not | `test_the_cancel_lane_raises_no_taint_of_its_own` |
| the tool advertises the real window | `test_the_tool_advertises_the_window_the_store_actually_keeps` |
| flag off ⇒ no store, no tool, no file | `test_the_flag_off_means_no_store_no_tool_and_no_file` (6 config shapes) |
| a bad bound raises, never substitutes | `test_a_bad_persistence_bound_raises_instead_of_being_substituted` |
| a database failure never costs the turn | `test_a_database_failure_never_reaches_the_turn` |
| a broken store says so instead of answering "nothing matched" | `test_a_broken_store_says_so_instead_of_answering_nothing_matched` |
| a schema change cannot silently kill persistence | `test_a_store_written_before_a_column_existed_still_records` |

## 10. Rule 11

`worker/transcript.py` adds **no new dependency, third-party or otherwise**. Everything it imports
— `sqlite3`, `datetime`, `re`, `threading`, `time`, `logging`, `os`, `pathlib`, `worker.sanitize` —
is already eagerly imported on the worker startup path by `jobstore`, `tools`, `traces` and `app`.

Measured anyway, since it does land on that path:

- Module body self-time: **3.3–4.3 ms** over 5 runs (`-X importtime`).
- A/B of `import worker.runtime` with the module stubbed out: median **610 ms** with it, **770 ms**
  without, run-to-run spread ±150 ms. The delta is below the noise floor.
- Idle RSS: the per-module effect does not resolve against a ±10 MiB baseline swing.

### 10a. The real startup cost is not the import — it is the sweep

The measurements above are import time only, which is the smaller half and was the only half the
first draft disclosed.

`runtime.build` calls `transcript.sweep()` at boot, **synchronously, on the worker startup path**.
That opens the connection, applies the schema guard, and pays one full
`COUNT(*) + SUM(LENGTH(text)) + MIN(at) + MAX(at)` scan to seed the running totals. Against a
**full 20,000-row store** that measured **48–55 ms** across five runs on an idle machine, and
123–179 ms on a loaded one. `seed_text()` afterwards is ~1 ms — it is a 20-row indexed read.

It is once per boot, not per turn, and it only reaches that size on a store that is actually full;
an empty or absent file costs nothing, because the sweep never creates one. It is disclosed here
because "3.3 ms of import" is not the number Daniel would be agreeing to. Moving the recount off
boot and onto the first write is possible — the first write happens after the turn has finished
speaking — and is deliberately **not** done here: it trades a measured, bounded, once-per-boot cost
for extra lazily-initialised state, which is not a trade worth making inside a rework.

---

## Sign-off

- [ ] Daniel has read §1–§7 and accepts what is stored.
- [ ] Daniel has read **§3c** and accepts every gap listed there: an unmarked passphrase of
      ordinary words, an ordinary-word password even behind a marker, a key spelled out letter by
      letter, CJK/Japanese/Korean text, and a card number with no card word near it.
- [ ] Daniel has read **§3c**'s false-positive list and accepts that a **full git SHA** and a
      **UUID** said aloud are redacted — and that the list says "include", not "are exactly".
- [ ] Daniel has read **§3c**'s cost paragraph and accepts the marker window's price: a CamelCase
      identifier just before a credential word, or a date, time, version or long number just after
      one, loses that word — `AuthController handles the password reset`, `my password expires
      2026-09-15`. Measured at **19 of 25** such sentences, always exactly one word and never the
      sentence. This is the largest of the accepted false positives and it is the price of catching
      17 of 17 realistic dictations.
- [ ] Daniel has read **§6c** and accepts that a turn which searches history is a turn that only
      reads: `search_transcript` cannot share a turn with an action tool in either order.
- [ ] Daniel has read **§6d** and accepts that file NAMES from disk stay untainting, and therefore
      seedable into the next boot, for thirty days rather than one turn.
- [ ] Daniel has read **§6f** and accepts that a turn ending with an unanswered readback is not
      stored at all — nothing was executed, and the seed frame's "already acted on" must not be
      allowed to become true of it — so that exchange is also not searchable later.
- [ ] Daniel has read **§6g** and accepts that a reflex-open turn stores his words and the tool's
      name, and that the host note framing it stays in memory only.
- [ ] Daniel has read **§3d** and accepts that ordinary paths, email addresses and URLs are stored
      as spoken (the worker log redacts them; this store deliberately does not).
- [ ] Daniel has read **§4** and accepts the real disk envelope (~5 MB English, up to ~20 MB
      CJK/emoji, plus up to ~4 MB of WAL while a session is open) rather than "4 MiB".
- [ ] Daniel has read **§4b** and accepts that a forward clock jump smaller than the guard's
      threshold is honoured as elapsed time.
- [ ] Daniel has decided §5a (harden the `%LOCALAPPDATA%\Atlas` ACL, or accept).
- [ ] Rule 10 in `CLAUDE.md` is amended per §8.
- [ ] `persistence.enabled` is set to `true` in `config/atlas.yaml`.

Until every box is ticked, the code is merged and inert.
