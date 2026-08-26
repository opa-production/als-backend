"""
Turning model output into plain prose, as it streams.

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
