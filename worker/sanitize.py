"""Voice-clean text (conversation-rules design §2). Streaming-safe by construction for
SINGLE-character markers: they are removed per-chunk at character level (a split "**" still dies);
line-anchored headers, list markers, and links are best-effort within a chunk. Chunks are never
.strip()ped — a single leading/trailing space is the word boundary between streamed segments.

COUPLING NOTE (review 2026-07-22): the multi-char [quiet] marker is NOT independently
streaming-safe — a chunk split mid-marker ("[", "quiet]") would leak audible fragments. Today
livekit's default `filter_markdown` transform runs UPSTREAM of tts_node and buffers any unclosed
"[" until the bracket closes, so the marker always arrives contiguous (verified against installed
livekit-agents 1.6.6: generation.py:355 + filters.py has_incomplete_pattern). If
`tts_text_transforms` is ever customized/emptied, or that upstream bracket-buffering changes on
upgrade, add a bracket-span buffer to our tts_node override — test_split_quiet_marker_limitation
documents the dependency."""
import re

# Silent-turn marker (Gate finding #1, 2026-07-21): the LLM's ONLY way to say nothing is to reply
# exactly [quiet] — an LLM turn always produces text, so silence needs a token. Stripped here so
# it can never be spoken; callers also avoid mirroring marker-only turns.
_QUIET = re.compile(r"\[quiet\]", re.IGNORECASE)
_QUIET_TURN = re.compile(r"\s*\[quiet\][.!\s]*$", re.IGNORECASE)

_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_HEADER = re.compile(r"(?m)^[ \t]*#{1,6}[ \t]+")
_LIST_MARKER = re.compile(r"(?m)^[ \t]*(?:[-•*]|\d+\.)[ \t]+")
_MULTISPACE = re.compile(r"[ \t]{2,}")

# --- secret shapes, shared by every persistent sink (rule 10) ------------------
#
# One definition, two callers, deliberately: worker/app.py's
# _HostShapedFormatter (the worker log) and worker/transcript.py (the
# conversation store). It lives HERE rather than in either of them because
# sanitize is the only module both can import without dragging livekit or
# sqlite3 along, and because a second copy of this pattern is a second thing
# to forget to update.
#
# WHAT THIS IS AND IS NOT. It is a shape filter over whitespace-delimited
# tokens, widened after the DD-4 adversarial review measured the original
# four-shape pattern catching 4 of 19 realistic credential canaries. It is
# defence in depth: rule 1 keeps real credentials out of reach in the first
# place, and nothing in the atlas.* tree is supposed to hand one to a
# persistent sink. It is NOT a promise that a secret cannot reach the file --
# a shape filter cannot be one. The conversation store holds things Daniel
# SPEAKS, and a spoken passphrase with no marker in front of it ("correct
# horse battery") is indistinguishable from ordinary words. The amendment's
# §3 says exactly that, and names each shape that is and is not covered.
#
# THREE LAYERS, in the order redact_secrets applies them:
#   1. TOKEN SHAPES (_SECRET_TOKEN_SHAPES) -- a token that is credential-shaped
#      on its own, wherever it appears. This is also the whole of what
#      secret_shaped() reports, because the worker log checks one token at a
#      time and truncates the rest of the line anyway.
#   2. MARKER CONTEXT (_STRONG_MARKER/_WEAK_MARKER) -- a token that is only
#      recognisable as a secret because of the word in front of it. This is
#      the layer that exists for dictation: "my password is Tr0ub4dor&3" has
#      no shape a pattern could find, but "password" in front of it is a
#      signal only a human would produce.
#   3. PEM CUT (_PEM_HEADER) -- a private-key header redacts the rest of the
#      text, because a key body is not a sentence anyone needs kept.
#
# Every pattern below is anchored with \b or ^ so it cannot fire on a
# substring of an ordinary word.
_SECRET_TOKEN_SHAPES = (
    # OpenAI / Anthropic style prefixes, and a JWT's header. Kept as bare
    # substrings (not \b-anchored) because they show up glued to an assignment
    # -- OPENAI_API_KEY=sk-proj-... is one token.
    r"sk-",
    r"eyJ",
    # "bearer" is a marker, not a value, but the log lane can only act on a
    # token, so it stays a shape too: matching it makes the log truncate the
    # line, and makes the store redact the marker AND (layer 2) the token
    # after it.
    r"bearer",
    # Google OAuth refresh token ("1//0g..."), Google API key, and the 2026
    # "AQ.Ab8..." auth-key format Daniel's own Gemini keys use.
    r"\b1//[A-Za-z0-9_\-]{16,}",
    r"\bAIza[0-9A-Za-z_\-]{16,}",
    r"\bAQ\.[A-Za-z0-9_\-]{16,}",
    # AWS access key id. The 40-character secret key has no prefix and is
    # caught by the opaque-run rule below instead.
    r"\b(?:AKIA|ASIA|ABIA|ACCA|AGPA|AIDA|AROA|AIPA|ANPA|ANVA)[0-9A-Z]{12,}",
    # Forge and SaaS tokens with a self-identifying prefix.
    r"\bgh[pousr]_[A-Za-z0-9]{16,}",
    r"\bgithub_pat_[A-Za-z0-9_]{16,}",
    r"\bglpat-[A-Za-z0-9_\-]{16,}",
    r"\bxox[abposr]-[A-Za-z0-9\-]{10,}",
    r"\bnpm_[A-Za-z0-9]{16,}",
    r"\bshp(?:at|ss|ca|pa)_[A-Za-z0-9]{16,}",
    r"\bdop_v1_[A-Za-z0-9]{16,}",
    r"\bSG\.[A-Za-z0-9_\-]{16,}",
    # A URL carrying userinfo. This is the one path-and-URL shape the store
    # checks (see redact_secrets' docstring): "postgres://user:hunter2@host/db"
    # is unambiguous -- a colon-separated password inside a URL authority is
    # never anything else -- where a bare path is ordinary conversation.
    r"\b[A-Za-z][A-Za-z0-9+.\-]*://[^\s:/@]+:[^\s/@]*@",
    # Hex runs: session ids, key material, pairing tokens. Original shape.
    r"\b[0-9a-fA-F]{16,}\b",
    # A UUID -- session and device ids wear this shape and the hex rule above
    # cannot see across the dashes.
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b",
)
SECRET_TOKEN = re.compile("|".join(_SECRET_TOKEN_SHAPES), re.IGNORECASE)

# An environment-variable NAME whose own name says what it holds. Case
# SENSITIVE and separate from SECRET_TOKEN for that reason: SCREAMING_SNAKE is
# the signal, and folding case would make it fire on "the_key" and "my_token".
# It catches both halves of "FOO_SECRET=bar" (one token) and arms the marker
# layer for "FOO_SECRET is bar" (two). It costs one word when a variable name
# is merely mentioned aloud; §3 of the amendment names that trade.
_SCREAMING_CREDENTIAL_NAME = re.compile(
    r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*_(?:KEY|SECRET|TOKEN|PASSWORD|PASSWD|PWD|"
    r"CREDENTIAL|CREDENTIALS|APIKEY)\b",
)

# A long opaque run: base64/base64url/hex-ish alphabet only, no punctuation
# that ordinary prose or a path would carry (a Windows path has "\" and ":",
# a URL has ":", an address has "@" -- all excluded), long enough that nothing
# in English reaches it, and required to mix digits with BOTH cases so that a
# long hyphenated phrase or a shouted word cannot match. This is what catches
# an AWS secret key, a raw base64 blob, and any bearer value long enough to
# matter, none of which announce themselves with a prefix.
_OPAQUE_RUN = re.compile(r"^[A-Za-z0-9+/=_\-]{24,}$")
_PEM_HEADER = re.compile(r"-{3,}\s*BEGIN", re.IGNORECASE)

# The marker layer, rebuilt after the DD-4 re-review (REQUIRED-2).
#
# It used to be a state machine that allowed at most two tokens from a CLOSED
# connector set between a marker and its value. That covered the textbook
# phrasing and almost nothing a person actually says to a voice assistant: a
# reviewer's 17 realistic dictations leaked 10, and every leak had the same
# cause -- one ordinary noun ("the password for the ROUTER is X"), one filled
# pause ("my password, UH, is X"), or an STT-rendered colon ("password COLON
# X") between the two, and the marker disarmed.
#
# It is now a WINDOW. A marker arms the next few tokens; whatever sits in
# between -- noun, filler, punctuation -- is simply skipped, and the first
# token in the window that looks like a VALUE is redacted. The gate that
# protects ordinary speech is therefore _value_shaped alone, which is where it
# belongs: nothing is redacted for being in the window, only for looking like
# a secret while in it. A sentence-ending token closes the window early, so a
# marker cannot reach into the next sentence.
#
# STRONG markers are words already about credentials. WEAK markers ("key",
# "secret", "token", "pin") are ordinary English too -- "the key is to stay
# calm" -- so they get a shorter window.
_STRONG_MARKER = re.compile(
    r"\b(?:password|passwords|passwd|pwd|passphrase|passcode|"
    r"bearer|authorization|credential|credentials|"
    r"api[_\-]?key|apikey|access[_\-]?token|refresh[_\-]?token|"
    r"client[_\-]?secret|secret[_\-]?key|auth[_\-]?token|session[_\-]?token|"
    r"private[_\-]?key|"
    # Latin- and Cyrillic-script equivalents, so the layer is not English-only.
    # This is a short list, not a translation table: 3c declares the rest of
    # the world's vocabulary -- and CJK entirely -- as uncovered.
    r"contraseña|contrasena|clave|senha|passwort|kennwort|wachtwoord|"
    r"motdepasse|lösenord|losenord|salasana|adgangskode|hasło|haslo|"
    r"пароль)\b",
    re.IGNORECASE,
)
_WEAK_MARKER = re.compile(r"\b(?:key|keys|secret|token|pin|otp|passkey)\b", re.IGNORECASE)
# The password family specifically. Only these arm the four-letter-group rule
# -- "bearer keep it safe" must not lose "keep" to it.
_PASSWORD_MARKER = re.compile(
    r"\b(?:password|passwords|passwd|pwd|passphrase|"
    r"contraseña|contrasena|clave|senha|passwort|kennwort|wachtwoord|"
    r"пароль)\b",
    re.IGNORECASE,
)
# The things that are spoken as a short run of digits. Separate from the
# password family because "pin" must NOT arm the four-letter-group rule:
# "remind me to pin the note about tuesday" loses "note" if it does.
_PIN_MARKER = re.compile(
    r"\b(?:pin|otp|passcode|password|passwd|pwd|code)\b", re.IGNORECASE,
)
# A payment card is only looked for behind one of THESE (re-review
# REQUIRED-1). See _CARD_RUN below for why it is no longer a free-standing
# shape.
_CARD_MARKER = re.compile(
    r"\b(?:card|cards|visa|mastercard|amex|maestro|discover|"
    r"credit|debit|cvv|cvc)\b",
    re.IGNORECASE,
)

# Window sizes, in tokens, tuned against both corpora in tests/test_transcript.py
# (17 dictation phrasings; 36 lines of ordinary developer speech).
#   STRONG 6 -- "the password, and dont forget it, is X" needs six.
#   WEAK   4 -- "the api key for stripe is X" needs four; more starts reaching
#               ordinary numbers after "the key is ...".
#   BACK   4 -- "use X as the password" puts the value BEFORE the marker.
#               Strong markers only, and only when nothing was found forward.
#   TIGHT  3 -- for the two rules whose follower is not self-evidently a secret
#               (four lowercase letters; a bare 4+ digit run). A long window on
#               those eats "my password again this morning at 1430".
_WINDOW_STRONG = 6
_WINDOW_WEAK = 4
_WINDOW_BACK = 4
_WINDOW_TIGHT = 3
_SENTENCE_END = ".!?"

# The Gmail app-password shape: four groups of four lowercase letters. On its
# own that is four ordinary short words, so it only ever fires inside a
# password-family marker's tight window.
_FOUR_LOWER = re.compile(r"^[a-z]{4}$")
_ALL_DIGITS = re.compile(r"^\d{4,}$")
_FOUR_DIGITS = re.compile(r"^\d{4}$")
# A payment card, which people do read aloud at an assistant. MARKER-GATED
# since the re-review, and the reason is worth keeping: Luhn is not the filter
# it looks like. Measured over 20,000 random samples per length it accepts
# ~10% of ALL numeric strings (13-digit 9.7%, 16-digit 9.6%, 19-digit 10.1%),
# and 2,091 of 20,736 four-group permutations of ordinary years and clock
# times. Worse, EVERY IMEI is Luhn-valid by specification, so an unconditional
# rule destroyed every device id anyone read aloud. Behind a card marker the
# rule keeps the case it was for ("my card number is 4111 ...") and gives back
# the whole category it was breaking.
_CARD_RUN = re.compile(r"^\d{13,19}$")
_STRIP_EDGES = ".,;:!?)('\"“”‘’"
# Below this length, an all-letters token cannot reach any shape above except
# "bearer" and "eyJ" -- every other one needs a digit, punctuation, or 16+
# characters. See secret_shaped's fast path; 16 is the shortest such shape
# (a 16-character hex run, and AKIA + 12).
_ALPHA_FAST_PATH = 16
# A token spelled like a word: letters, possibly hyphenated or apostrophed.
_WORDLIKE = re.compile(r"^[A-Za-z]+(?:[-'’][A-Za-z]+)*$")
# How long a WORD-shaped token must be before it counts as a value anyway.
# 16 is the Gmail app-password length, and it clears the long English words
# that turn up around credentials talk ("unauthorized" 12, "administrator" 13,
# "responsibility" 14). A dictated all-lowercase passphrase shorter than this
# is not caught -- 3c declares it.
_WORD_VALUE_LENGTH = 16
# The same idea for tokens that are not word-shaped and carry no digit.
_OPAQUE_VALUE_LENGTH = 12

REDACTED = "<redacted>"
_WHITESPACE_SPLIT = re.compile(r"(\s+)")


def _core(token: str) -> str:
    """The token without the punctuation a sentence wrapped around it."""
    return token.strip(_STRIP_EDGES)


def _luhn(digits: str) -> bool:
    total, double = 0, False
    for character in reversed(digits):
        value = ord(character) - 48
        if double:
            value *= 2
            if value > 9:
                value -= 9
        total += value
        double = not double
    return total % 10 == 0


def _opaque_run(core: str) -> bool:
    return bool(
        _OPAQUE_RUN.match(core)
        and any(character.isdigit() for character in core)
        and any(character.isupper() for character in core)
        and any(character.islower() for character in core)
    )


def secret_shaped(token: str) -> bool:
    """True when one whitespace-delimited token looks like a secret on its own.

    Shape only -- marker context needs the neighbours and lives in
    redact_secrets. The worker log's formatter calls this per token and then
    truncates the rest of the line, so the marker case is already covered
    there by that truncation.
    """
    if not isinstance(token, str) or not token:
        return False
    if token.isalpha() and len(token) < _ALPHA_FAST_PATH:
        # An EXACT shortcut, not an approximation, and it is what keeps this
        # affordable: redact_secrets runs per token over every stored turn,
        # and ordinary prose is almost entirely short alphabetic words. Every
        # other shape above needs a digit, a hyphen, an underscore, a dot, a
        # slash, or at least 16 characters -- run through them and check. What
        # is left that a short all-letters token could still match is "bearer"
        # and a JWT's "eyJ", so those two are tested directly and the 20-branch
        # alternation is skipped. Measured: 46us -> 8us per token.
        lowered = token.lower()
        return "bearer" in lowered or "eyj" in lowered
    core = _core(token)
    return bool(
        SECRET_TOKEN.search(token)
        or _SCREAMING_CREDENTIAL_NAME.search(token)
        or _opaque_run(core)
    )


def _value_shaped(token: str) -> bool:
    """Does this token look like a VALUE rather than the next English word?

    Deliberately conservative, because this is what stops a marker from eating
    the rest of an ordinary sentence: six characters or more, and either
    carrying a digit, or carrying punctuation a word would not, or mixing case
    mid-token, or simply longer than any word a person dictates as prose.
    "my password is correct" keeps "correct"; that limitation is real and §3
    says so rather than pretending otherwise.
    """
    core = _core(token)
    if len(core) < 6:
        return False
    if any(character.isdigit() for character in core):
        return True
    # An initial capital is a sentence starting, not a value; a capital
    # anywhere after it is not something prose does.
    mixed = (
        any(character.isupper() for character in core[1:])
        and any(character.islower() for character in core)
    )
    if _WORDLIKE.match(core):
        # It is spelled like a word: letters, possibly hyphenated. Only
        # internal capitals or a length no English word reaches make it a
        # value. Without this the six-token window ate "re-enrol" after
        # "credentials" and "unauthorized" after "token" -- both of which are
        # just how people talk about credentials without naming one.
        return mixed or len(core) >= _WORD_VALUE_LENGTH
    if any(not character.isalnum() for character in core):
        return True
    return mixed or len(core) >= _OPAQUE_VALUE_LENGTH


def redact_secrets(text: str) -> str:
    """Replace every secret-shaped token with <redacted>, keeping the rest.

    Differs from the worker log's rule in two deliberate ways, both named in
    the amendment's §3:

      1. The log truncates at the first offending token and drops the rest of
         the line, because a log line's tail is diagnostic detail nobody needs
         badly enough to risk. Conversation text IS the payload, so here the
         token -- and only the token -- is replaced and the sentence survives.
         The one exception is a PEM header, which cuts to the end: what
         follows it is key material, not a sentence.
      2. The log also redacts on _UNSAFE_LOG_TOKEN (any path, "@", or URL
         shape). The store does NOT, because conversation is full of paths,
         folder names and addresses and redacting them would empty it. The
         one credential-bearing URL shape -- userinfo in the authority,
         "postgres://user:hunter2@host/db" -- is unambiguous and IS covered,
         as a token shape above.

    Whitespace runs are preserved verbatim so line structure survives.
    """
    if not isinstance(text, str) or not text:
        return text if isinstance(text, str) else ""
    parts = _WHITESPACE_SPLIT.split(text)
    # (position in `parts`, token). Odd entries of `parts` are the whitespace
    # runs and are never touched.
    tokens = [(index, parts[index]) for index in range(0, len(parts), 2) if parts[index]]
    for index, token in tokens:
        if _PEM_HEADER.search(token):
            # Everything from a private-key header on is key material, not a
            # sentence: this is the one place the store cuts to the end the
            # way the log always does.
            return "".join(parts[:index]) + REDACTED

    doomed: set[int] = set()
    for position, (_index, token) in enumerate(tokens):
        if secret_shaped(token):
            doomed.add(position)
    for position, (_index, token) in enumerate(tokens):
        _apply_marker(tokens, position, token, doomed)

    out = list(parts)
    for position in doomed:
        out[tokens[position][0]] = REDACTED
    return "".join(out)


def _ends_sentence(token: str) -> bool:
    return token.rstrip("\"')]”’").endswith(tuple(_SENTENCE_END))


def _apply_marker(
    tokens: list[tuple[int, str]],
    position: int,
    token: str,
    doomed: set[int],
) -> None:
    """If `token` is a marker, find the value it points at and doom it.

    Forward first, over a bounded window; then -- for strong markers that
    found nothing forward -- backward, because "use X as the password" puts
    the value first and is a thing people say.
    """
    strong = bool(_STRONG_MARKER.search(token) or _SCREAMING_CREDENTIAL_NAME.search(token))
    weak = not strong and bool(_WEAK_MARKER.search(token))
    card = bool(_CARD_MARKER.search(token))
    if not (strong or weak or card):
        return
    password = bool(_PASSWORD_MARKER.search(token))
    pin = bool(_PIN_MARKER.search(token))
    window = _WINDOW_STRONG if (strong or card) else _WINDOW_WEAK
    total = len(tokens)

    for offset in range(1, window + 1):
        ahead = position + offset
        if ahead >= total:
            break                       # ...and still try backward, below
        candidate = tokens[ahead][1]
        if ahead in doomed:
            return                      # already dying; the marker is answered
        core = _core(candidate)
        if card and _card_at(tokens, ahead, doomed):
            return
        tight = offset <= _WINDOW_TIGHT
        if _value_shaped(candidate):
            doomed.add(ahead)
            return
        if tight and password and _app_password_at(tokens, ahead, doomed):
            return
        if tight and pin and _ALL_DIGITS.match(core):
            doomed.add(ahead)
            return
        if _ends_sentence(candidate):
            break                       # a marker may not reach into the next sentence

    if not strong:
        return
    for offset in range(1, _WINDOW_BACK + 1):
        behind = position - offset
        if behind < 0 or behind in doomed:
            return
        candidate = tokens[behind][1]
        if _value_shaped(candidate):
            doomed.add(behind)
            return
        if _ends_sentence(candidate):
            return


def _app_password_at(
    tokens: list[tuple[int, str]], position: int, doomed: set[int],
) -> bool:
    """Four consecutive groups of four lowercase letters -- a Gmail app password.

    ALL FOUR are required before any is redacted. One group on its own is just
    a four-letter English word, and inside a password marker's window there are
    plenty: "I forgot my password again THIS morning" and "remind me to pin the
    NOTE" both lost a word to the single-group version of this rule.
    """
    indices = []
    for offset in range(4):
        ahead = position + offset
        if ahead >= len(tokens) or not _FOUR_LOWER.match(_core(tokens[ahead][1])):
            return False
        indices.append(ahead)
    doomed.update(indices)
    return True


def _card_at(tokens: list[tuple[int, str]], position: int, doomed: set[int]) -> bool:
    """A Luhn-valid card at `position`, run-on or in four groups of four."""
    core = _core(tokens[position][1])
    if _CARD_RUN.match(core) and _luhn(core):
        doomed.add(position)
        return True
    if not _FOUR_DIGITS.match(core):
        return False
    groups, indices = [], []
    for offset in range(4):
        ahead = position + offset
        if ahead >= len(tokens):
            return False
        group = _core(tokens[ahead][1])
        if not _FOUR_DIGITS.match(group):
            return False
        groups.append(group)
        indices.append(ahead)
    if not _luhn("".join(groups)):
        return False
    doomed.update(indices)
    return True


def is_quiet_turn(text: str) -> bool:
    """True when an assistant turn is the silent-turn marker (allowing trailing punctuation) —
    the caller must then avoid mirroring the turn."""
    return bool(_QUIET_TURN.fullmatch(text))


def sanitize_for_tts(text: str) -> str:
    # Header regex runs BEFORE the char-level strip: stripping "#" alone would leave its trailing
    # space ("# Status" -> " Status"). The char pass still catches "#" split from its space (PR #44).
    # [quiet] first: it must never reach the speaker, and _LINK would otherwise leave it intact
    # (no parenthesized target). A marker-only turn (trailing punctuation included) sanitizes to
    # nothing at all -> no synthesis; a rule-violating mixed turn just loses the marker.
    if _QUIET_TURN.fullmatch(text):
        return ""
    t = _QUIET.sub("", text)
    t = _LINK.sub(r"\1", t)
    t = _HEADER.sub("", t)
    t = _LIST_MARKER.sub("", t)
    t = t.replace("`", "").replace("*", "").replace("#", "")
    t = t.replace("_", " ")
    return _MULTISPACE.sub(" ", t)
