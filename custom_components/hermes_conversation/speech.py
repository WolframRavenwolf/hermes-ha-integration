"""Emoji and inline-link cleanup for voice responses."""

from __future__ import annotations

import re

# Unicode 17.0 Emoji property, excluding keycap bases and the meaningful text
# symbols below. Use exact ranges, not all Unicode symbols or pictograph blocks:
# https://www.unicode.org/Public/17.0.0/ucd/emoji/emoji-data.txt
_EMOJI_RE = re.compile(
    "["
    "\u203c\u2049\u2139\u231a-\u231b\u2328\u23cf\u23e9-\u23f3\u23f8-\u23fa\u24c2"
    "\u25aa-\u25ab\u25b6\u25c0\u2600-\u2604\u260e\u2611\u2614-\u2615\u2618\u261d"
    "\u2620\u2622-\u2623\u2626\u262a\u262e-\u262f\u2638-\u263a\u2640\u2642"
    "\u2648-\u2653\u265f-\u2660\u2663\u2665-\u2666\u2668\u267b\u267e-\u267f"
    "\u2692-\u2697\u2699\u269b-\u269c\u26a0-\u26a1\u26a7\u26aa-\u26ab"
    "\u26b0-\u26b1\u26bd-\u26be\u26c4-\u26c5\u26c8\u26ce-\u26cf\u26d1"
    "\u26d3-\u26d4\u26e9-\u26ea\u26f0-\u26f5\u26f7-\u26fa\u26fd\u2702\u2705"
    "\u2708-\u270d\u270f\u2712\u2714\u271d\u2721\u2728\u2733-\u2734\u2744\u2747"
    "\u274c\u274e\u2753-\u2755\u2757\u2763-\u2764\u27a1\u27b0\u27bf\u2b05-\u2b07"
    "\u2b1b-\u2b1c\u2b50\u2b55\u3030\u303d\u3297\u3299\U0001f004\U0001f0cf"
    "\U0001f170-\U0001f171\U0001f17e-\U0001f17f\U0001f18e\U0001f191-\U0001f19a"
    "\U0001f1e6-\U0001f1ff\U0001f201-\U0001f202\U0001f21a\U0001f22f"
    "\U0001f232-\U0001f23a\U0001f250-\U0001f251\U0001f300-\U0001f321"
    "\U0001f324-\U0001f393\U0001f396-\U0001f397\U0001f399-\U0001f39b"
    "\U0001f39e-\U0001f3f0\U0001f3f3-\U0001f3f5\U0001f3f7-\U0001f4fd"
    "\U0001f4ff-\U0001f53d\U0001f549-\U0001f54e\U0001f550-\U0001f567"
    "\U0001f56f-\U0001f570\U0001f573-\U0001f57a\U0001f587\U0001f58a-\U0001f58d"
    "\U0001f590\U0001f595-\U0001f596\U0001f5a4-\U0001f5a5\U0001f5a8"
    "\U0001f5b1-\U0001f5b2\U0001f5bc\U0001f5c2-\U0001f5c4\U0001f5d1-\U0001f5d3"
    "\U0001f5dc-\U0001f5de\U0001f5e1\U0001f5e3\U0001f5e8\U0001f5ef\U0001f5f3"
    "\U0001f5fa-\U0001f64f\U0001f680-\U0001f6c5\U0001f6cb-\U0001f6d2"
    "\U0001f6d5-\U0001f6d8\U0001f6dc-\U0001f6e5\U0001f6e9\U0001f6eb-\U0001f6ec"
    "\U0001f6f0\U0001f6f3-\U0001f6fc\U0001f7e0-\U0001f7eb\U0001f7f0"
    "\U0001f90c-\U0001f93a\U0001f93c-\U0001f945\U0001f947-\U0001f9ff"
    "\U0001fa70-\U0001fa7c\U0001fa80-\U0001fa8a\U0001fa8e-\U0001fac6\U0001fac8"
    "\U0001facd-\U0001fadc\U0001fadf-\U0001faea\U0001faef-\U0001faf8"
    "]"
)
# Keep these as text (copyright, arrows, mathematical operators) unless an
# explicit emoji presentation selector follows them. Digits/#/* need a keycap.
_TEXT_SYMBOLS = frozenset("©®™↔↕↖↗↘↙↩↪◻◼◽◾⤴⤵✖➕➖➗")
_KEYCAP_BASES = frozenset("#*0123456789")


class SpeechMarkdownFilter:
    """Keep split inline links/images out of speech without buffering a turn.

    Retain only a possible label (up to 4096 characters); once a URL starts,
    discard it incrementally. Overlong bracketed text is dropped rather than
    exposing a possible destination. Ordinary text is emitted immediately.
    """

    def __init__(self) -> None:
        self._label = ""
        self._label_depth = 0
        self._label_escaped = False
        self._overlong_label = False
        self._url_depth = 0
        self._url_escaped = False
        self._url_label_prefix = ""

    @staticmethod
    def _last_label_start(label: str) -> int:
        """Find the opening bracket paired with the trailing close bracket."""
        stack: list[int] = []
        escaped = False
        for index, char in enumerate(label):
            if escaped:
                escaped = False
                continue
            if char == "\\":
                escaped = True
            elif char == "[":
                stack.append(index)
            elif char == "]":
                if index == len(label) - 1:
                    return stack[-1] if stack else 0
                if stack:
                    stack.pop()
        # Even an escaped trailing ] may introduce a destination. Fail closed.
        return stack[-1] if stack else 0

    def feed(self, text: str) -> str:
        """Emit visible text and link labels, never inline URL destinations."""
        parts: list[str] = []
        for char in text:
            if self._overlong_label:
                if self._label_escaped:
                    self._label_escaped = False
                elif char == "\\":
                    self._label_escaped = True
                elif char == "[":
                    self._label_depth += 1
                elif char == "]":
                    self._label_depth -= 1
                    if self._label_depth == 0:
                        self._overlong_label = False
                        self._label = "]"  # Suppress a following destination.
                continue
            if self._url_depth:
                if self._url_escaped:
                    self._url_escaped = False
                elif char == "\\":
                    self._url_escaped = True
                elif char == "(":
                    self._url_depth += 1
                elif char == ")":
                    self._url_depth -= 1
                    if self._url_depth == 0:
                        self._label = self._url_label_prefix
                        self._url_label_prefix = ""
                        self._label_depth = self._label.count("[") - self._label.count("]")
                continue

            if self._label.endswith("]"):
                if char == "(":
                    start = self._last_label_start(self._label)
                    prefix = self._label[:start]
                    if prefix.endswith("!"):
                        prefix = prefix[:-1]
                    # Run labels through the same filter, so an image/link
                    # inside a label cannot leak its own destination.
                    nested = SpeechMarkdownFilter()
                    label = nested.feed(self._label[start + 1:-1]) + nested.flush()
                    if not prefix:
                        parts.append(label)
                    elif "[" not in prefix:
                        parts.append(prefix + label)
                        prefix = ""
                    self._url_label_prefix = prefix + (label if prefix else "")
                    self._label = ""
                    self._url_depth = 1
                    continue
                if self._label_depth == 0:
                    if char == "]":
                        self._label += char
                        continue
                    parts.append(self._label)
                    self._label = ""

            if self._label == "!" and char != "[":
                parts.append("!")
                self._label = ""

            if not self._label:
                if char in "[!":
                    self._label = char
                    self._label_depth = int(char == "[")
                    self._label_escaped = False
                elif char == "]":
                    # A stray close bracket can still prefix a destination.
                    self._label = char
                    self._label_depth = 0
                else:
                    parts.append(char)
                continue

            self._label += char
            if self._label == "![":
                self._label_depth = 1
            elif self._label_escaped:
                self._label_escaped = False
            elif char == "\\":
                self._label_escaped = True
            elif char == "[":
                self._label_depth += 1
            elif char == "]" and self._label_depth:
                self._label_depth -= 1
            if len(self._label) >= 4096:
                # Conservatively discard the rest of an overlong bracketed
                # expression; emitting it could expose a future destination.
                self._label = ""
                self._overlong_label = True
        return "".join(parts)

    def flush(self) -> str:
        """Release a non-link label, discarding any unfinished URL."""
        pending = self._label
        self._label = ""
        self._label_depth = 0
        self._label_escaped = False
        self._overlong_label = False
        self._url_depth = 0
        self._url_escaped = False
        self._url_label_prefix = ""
        return pending


class SpeechEmojiFilter:
    """Remove emoji without leaking components split across stream deltas.

    Only an ambiguous text symbol, keycap prefix or possible text-presentation
    emoji is held back. Joiners and selectors are discarded after removed emoji,
    not globally: a ZWJ can be meaningful in ordinary non-Latin text.
    """

    def __init__(self) -> None:
        self._pending = ""
        self._after_emoji = False

    def feed(self, text: str) -> str:
        """Return text safe to emit now, retaining a possible sequence prefix."""
        return self._drain(text, final=False)

    def flush(self) -> str:
        """Emit any retained plain-text symbol at end of stream."""
        return self._drain("", final=True)

    def _drain(self, text: str, *, final: bool) -> str:
        text = self._pending + text
        self._pending = ""
        parts: list[str] = []
        i = 0
        while i < len(text):
            char = text[i]
            if self._after_emoji and (
                char in "\u200d\ufe0e\ufe0f" or "\U000e0020" <= char <= "\U000e007f"
            ):
                i += 1
                continue
            self._after_emoji = False

            if _EMOJI_RE.fullmatch(char):
                if i + 1 == len(text) and not final:
                    self._pending = char
                    break
                if i + 1 < len(text) and text[i + 1] == "\ufe0e":
                    parts.append(char + "\ufe0e")
                    i += 2
                    continue
                self._after_emoji = True
                i += 1
                continue

            if char in _TEXT_SYMBOLS or char in _KEYCAP_BASES:
                end = i + 1
                if end < len(text) and text[end] == "\ufe0f":
                    end += 1
                if char in _TEXT_SYMBOLS and end > i + 1:
                    self._after_emoji = True
                    i = end
                    continue
                if char in _KEYCAP_BASES and end < len(text) and text[end] == "\u20e3":
                    self._after_emoji = True
                    i = end + 1
                    continue
                if not final and end == len(text):
                    self._pending = text[i:]
                    break

            parts.append(char)
            i += 1
        return "".join(parts)


def strip_speech_emoji(text: str) -> str:
    """Apply the same emoji rules to a complete, non-streaming response."""
    emoji_filter = SpeechEmojiFilter()
    return emoji_filter.feed(text) + emoji_filter.flush()
