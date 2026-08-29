"""
Turning model output into plain prose, as it streams.

Two jobs, both of them things the system prompt already asked for and the model
did anyway: stripping markdown, and stripping an opening sentence that reports
what the search did not find.

The app renders answers as plain text. It has no markdown renderer, so a model
that reaches for ``**bold**`` and ``- bullets`` puts literal asterisks and
hyphens on a student's screen — which reads as the tutor being broken rather
than as formatting that did not get rendered.

Two defences, and both are needed. The system prompt asks for prose; this is
what happens when the model formats anyway, which it will, because every one of
them is trained to.

The hard part is doing it to a **stream**.

Markdown markers are scoped to a line (``- ``, ``### ``, ``---``) or to a pair
(``**x**``). Neither can be resolved from a fragment: ``**`` routinely arrives
as one asterisk in one chunk and one in the next, and ``---`` is a horizontal
rule only if nothing else is on the line. So text cannot simply be cleaned
chunk by chunk — an earlier attempt at that leaked markers whenever a chunk
boundary fell inside one, and produced different output depending on how the
provider happened to split the response.

What this does instead: hold text back until it is *decidable*, then release it.
A finished line is always decidable. A long unfinished line is released up to
the last point that no pending marker can reach back through, so a paragraph
still streams rather than appearing all at once at its newline.

The invariant, which the tests pin: **chunking must not change the output.** The
same answer delivered one character at a time has to come out identical to the
same answer delivered whole.
"""

from __future__ import annotations

import re

#: Characters that can begin a marker. A trailing run of these is never
#: released, because the next chunk decides what they were.
_PENDING = set("*_#-+~[`>")

#: A partial line is only released once it is longer than this. Below it, the
#: whole line is held — which costs nothing, because a line that short is one
#: chunk away from finishing anyway.
#:
#: Tuned for feel rather than correctness: correctness comes from `_safe_cut`.
#: Too high and a long paragraph appears in one lump; too low and the reader
#: sees text arrive in stutters.
_SOFT_LIMIT = 90

# --- Line-scoped rules --------------------------------------------------------
#
# Applied to the start of a line, once that start is known.

# A horizontal rule is decoration with no words in it, so the whole line goes.
# Only decidable on a *complete* line: "---" may yet turn into "--- and so on".
_HORIZONTAL_RULE = re.compile(r"^\s*([-*_]\s*){3,}$")

_LINE_RULES = (
    # Heading hashes. The words stay; a heading in prose is just a sentence.
    (re.compile(r"^\s{0,3}#{1,6}\s+"), ""),
    # Bullet markers. The item's text stays, the dash or asterisk goes.
    (re.compile(r"^(\s*)[-*+]\s+"), r"\1"),
    # Blockquote markers.
    (re.compile(r"^\s{0,3}>\s?"), ""),
)

# --- Pair-scoped rules --------------------------------------------------------

_INLINE_RULES = (
    # Bold before italic: `**x**` must go first or the outer pair is eaten one
    # asterisk at a time and leaves strays behind.
    (re.compile(r"\*\*\*(.+?)\*\*\*"), r"\1"),
    (re.compile(r"\*\*(.+?)\*\*"), r"\1"),
    (re.compile(r"(?<!\w)\*(?!\s)(.+?)(?<!\s)\*(?!\w)"), r"\1"),
    (re.compile(r"__(.+?)__"), r"\1"),
    (re.compile(r"(?<!\w)_(?!\s)(.+?)(?<!\s)_(?!\w)"), r"\1"),
    (re.compile(r"~~(.+?)~~"), r"\1"),
    # A markdown link keeps its words and loses its plumbing. The URL goes: the
    # app cannot open one from inside a message, so it is noise on the screen.
    (re.compile(r"\[([^\]]+)\]\([^)]*\)"), r"\1"),
    # Any asterisk the pair rules could not match. Left to last so it only ever
    # catches genuine strays.
    (re.compile(r"\*+"), ""),
)

#: Markers whose removal depends on finding a partner, and the closer to count
#: against. An odd count means the last one is still open.
_PAIRED = (("**", "**"), ("~~", "~~"), ("[", "]"))


def _apply_inline(text: str) -> str:
    for pattern, replacement in _INLINE_RULES:
        text = pattern.sub(replacement, text)
    return text


def _apply_line_start(text: str) -> str:
    for pattern, replacement in _LINE_RULES:
        text = pattern.sub(replacement, text)
    return text


def clean_line(line: str) -> str:
    """
    One complete line, as prose.

    Deliberately leaves hyphens inside words and dashes used as punctuation
    alone. "State-of-the-art", a range like 5-10, and a parenthetical dash are
    not formatting, and stripping every hyphen to be thorough would mangle
    ordinary English.
    """
    if _HORIZONTAL_RULE.match(line):
        return ""
    return _apply_inline(_apply_line_start(line))


def _unmatched_opener(text: str) -> int | None:
    """
    Where a pair opened and has not closed yet, or None.

    Approximate on purpose, and approximate in the safe direction: when in
    doubt it reports an opener, the caller holds more text back, and the only
    cost is that a few characters arrive a moment later.
    """
    candidates: list[int] = []

    for opener, closer in _PAIRED:
        if opener == closer:
            if text.count(opener) % 2 == 1:
                candidates.append(text.rfind(opener))
        elif text.count(opener) > text.count(closer):
            candidates.append(text.rfind(opener))

    # A lone asterisk — one that is not part of a `**` — opens italics.
    lone = list(re.finditer(r"(?<!\*)\*(?!\*)", text))
    if len(lone) % 2 == 1:
        candidates.append(lone[-1].start())

    return min(candidates) if candidates else None


def _safe_cut(text: str) -> int:
    """
    How much of an unfinished line can be released now.

    Two things reach backwards through a stream and so must be excluded: a
    trailing run of characters that might be the start of a marker, and
    everything from the last unclosed pair onwards.
    """
    cut = len(text)

    while cut > 0 and text[cut - 1] in _PENDING:
        cut -= 1

    opener = _unmatched_opener(text[:cut])
    if opener is not None:
        cut = min(cut, opener)

    return cut


class StreamCleaner:
    """
    Feed it chunks, get back cleaned text.

    ``feed`` returns everything now decidable; ``flush`` returns the rest once
    the stream ends. Both are safe to call any number of times.
    """

    def __init__(self) -> None:
        #: The current line, up to the last thing released.
        self._buffer = ""
        #: Whether part of the current line has already gone out — after which
        #: its line-start rules no longer apply, having already been applied.
        self._started = False

    def feed(self, chunk: str) -> str:
        if not chunk:
            return ""

        self._buffer += chunk
        released: list[str] = []

        # A finished line is always decidable.
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            released.append(self._release(line, complete=True))
            released.append("\n")
            self._started = False

        # A long unfinished line is released as far as is safe, so a paragraph
        # streams instead of landing in one lump at its newline.
        if len(self._buffer) >= _SOFT_LIMIT:
            cut = _safe_cut(self._buffer)
            if cut > 0:
                released.append(self._release(self._buffer[:cut], complete=False))
                self._buffer = self._buffer[cut:]

        return "".join(released)

    def flush(self) -> str:
        """Whatever is left, cleaned."""
        remainder = self._buffer
        self._buffer = ""
        if not remainder:
            self._started = False
            return ""
        out = self._release(remainder, complete=True)
        self._started = False
        return out

    def _release(self, text: str, *, complete: bool) -> str:
        if self._started:
            # The line's opening already went out, so only pair rules are left.
            return _apply_inline(text)

        self._started = True

        # The horizontal-rule test needs the whole line: "---" alone is a rule,
        # "--- and then" is a bullet. Only a completed line can be judged.
        if complete and _HORIZONTAL_RULE.match(text):
            return ""

        return _apply_inline(_apply_line_start(text))


# --- The opening sentence -----------------------------------------------------
#
# The prompts forbid opening with what could not be found. Models do it anyway,
# because "I could not find that in the provided context" is the most rehearsed
# sentence in every RAG corpus they were trained on. It is also the one sentence
# a student must never read first: it turns an answer the tutor is about to give
# perfectly well into a report on a database miss, and in a unit with nothing
# uploaded it would open every single reply.
#
# So it is removed. Only at the very start, only one sentence, and only when it
# is about their material — "I do not know" is a different sentence and an
# honest one, and it stays.

#: How far in to look for the end of the first sentence. Past this, the opener
#: is not a disclaimer, it is the answer, and holding text back to inspect it
#: only delays the first thing on the screen.
_OPENER_LIMIT = 240

_SOURCE = (
    r"(?:material|materials|note|notes|document|documents|pdf|pdfs|file|files|"
    r"upload|uploads|slide|slides|handout|handouts|passage|passages|excerpt|"
    r"excerpts|context|knowledge\s+base|course\s+content)"
)

_SORRY = r"(?:unfortunately|sorry|apologies|i\s*['\u2019]?m\s+sorry)[,:\s]+"

_NEGATED = (
    r"(?:could\s*not|couldn['\u2019]?t|cannot|can\s*not|can['\u2019]?t|do\s*not|"
    r"don['\u2019]?t|did\s*not|didn['\u2019]?t|was\s+not\s+able\s+to|"
    r"wasn['\u2019]?t\s+able\s+to|am\s+unable\s+to|have\s+no|found\s+no|see\s+no)"
)

#: Every one of these requires the sentence to name the student's material. That
#: is what keeps "I do not know" and "I cannot be certain" — honest sentences,
#: worth reading — out of the net.
_DISCLAIMERS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        # "I could not find anything about this in your notes."
        rf"^\s*(?:{_SORRY})?(?:i|we)\s+{_NEGATED}\b[^.!?\n]*\b{_SOURCE}\b[^.!?\n]*[.!?]+",
        # "There is nothing in your material about deadlock."
        rf"^\s*(?:{_SORRY})?there\s+(?:is|are|was|were)\s+(?:no|nothing|not)\b"
        rf"[^.!?\n]*\b{_SOURCE}\b[^.!?\n]*[.!?]+",
        # "Your CS201 notes do not cover this." / "The provided context does not
        # mention it." The words between the determiner and the noun are the
        # unit code and whatever else the model felt like naming.
        rf"^\s*(?:{_SORRY})?(?:your|the|these|those)\s+(?:[\w'-]+\s+){{0,4}}"
        rf"{_SOURCE}\b[^.!?\n]*\b(?:do|does|did)\s*(?:not|n['\u2019]?t)\b"
        rf"[^.!?\n]*[.!?]+",
        # "Nothing in your uploaded material matched this question."
        rf"^\s*(?:{_SORRY})?(?:nothing|none)\b[^.!?\n]*\b{_SOURCE}\b[^.!?\n]*[.!?]+",
    )
)

_SENTENCE_END = re.compile(r"[.!?]")


class OpenerGuard:
    """
    Drops a disclaimer if the model opens with one, then gets out of the way.

    Holds text back only until the first sentence is decidable — one sentence of
    latency, once, at the very start of an answer — and is a plain pass-through
    for the rest of the stream after that. A disclaimer the model puts *later*
    is left alone: by then it is a caveat inside an answer the student is
    already reading, which is a different and reasonable thing to write.

    The same invariant as `StreamCleaner`: chunking must not change the output.
    That is why the decision is made against a fixed-length head of the buffer
    rather than against however much text happens to have arrived by then.
    """

    def __init__(self) -> None:
        self._buffer = ""
        self._done = False
        #: Set when a disclaimer was dropped and the text following it had not
        #: arrived yet, so the whitespace it left behind is still to be eaten.
        self._trim_next = False

    def feed(self, text: str) -> str:
        if self._done:
            return self._passthrough(text)
        if not text:
            return ""

        self._buffer += text
        head = self._buffer[:_OPENER_LIMIT]

        if _SENTENCE_END.search(head):
            return self._resolve(head)

        if len(self._buffer) >= _OPENER_LIMIT:
            # No sentence ends inside the window, so no disclaimer can match.
            return self._release(self._buffer)

        return ""

    def flush(self) -> str:
        """Whatever is still held, once the stream has ended."""
        if self._done:
            return ""
        head = self._buffer[:_OPENER_LIMIT]
        if _SENTENCE_END.search(head):
            return self._resolve(head)
        return self._release(self._buffer)

    def _resolve(self, head: str) -> str:
        """Decide on a head that contains the end of the first sentence."""
        tail = self._buffer[len(head):]

        for pattern in _DISCLAIMERS:
            match = pattern.match(head)
            if match:
                return self._release(head[match.end():] + tail, trim=True)

        return self._release(head + tail)

    def _release(self, text: str, *, trim: bool = False) -> str:
        self._buffer = ""
        self._done = True
        if trim:
            self._trim_next = True
        return self._passthrough(text)

    def _passthrough(self, text: str) -> str:
        if self._trim_next:
            text = text.lstrip()
            if text:
                self._trim_next = False
        return text
