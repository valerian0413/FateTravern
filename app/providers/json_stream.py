"""Pull one top-level JSON string out of a partial object, as tokens arrive."""
from __future__ import annotations

import inspect


def partial_top_level_string(raw: str, field: str) -> str | None:
    """Return the decoded text of a top-level string field, even if the object is unfinished."""
    if not raw or not field:
        return None
    parser = _Parser(raw)
    return parser.field(field)


class TextFieldWatch:
    """Turn raw model chunks into the growing text of one JSON string field."""

    def __init__(self, field: str, on_text):
        self.field = field
        self.on_text = on_text
        self.raw = ''
        self.sent = ''

    async def __call__(self, piece: str | None) -> None:
        if piece is None:
            self.raw = ''
            self.sent = ''
            return
        if piece == '':
            return
        self.raw += piece
        visible = partial_top_level_string(self.raw, self.field)
        if not visible or visible == self.sent:
            return
        self.sent = visible
        result = self.on_text(visible)
        if inspect.isawaitable(result):
            await result


class _Parser:
    def __init__(self, raw: str):
        self.raw = raw
        self.n = len(raw)
        self.i = 0

    def field(self, name: str) -> str | None:
        self._skip_ws()
        if self.i >= self.n or self.raw[self.i] != '{':
            return None
        self.i += 1
        while True:
            self._skip_ws()
            if self.i >= self.n or self.raw[self.i] == '}':
                return None
            if self.raw[self.i] != '"':
                return None
            key, closed = self._string(capture=True)
            if not closed:
                return None
            self._skip_ws()
            if self.i >= self.n or self.raw[self.i] != ':':
                return None
            self.i += 1
            self._skip_ws()
            if self.i >= self.n:
                return None
            if key == name:
                if self.raw[self.i] != '"':
                    return None
                text, _closed = self._string(capture=True)
                return text
            if not self._skip_value():
                return None
            self._skip_ws()
            if self.i >= self.n:
                return None
            if self.raw[self.i] == ',':
                self.i += 1
                continue
            return None

    def _skip_ws(self) -> None:
        while self.i < self.n and self.raw[self.i] in ' \t\r\n':
            self.i += 1

    def _string(self, capture: bool) -> tuple[str | None, bool]:
        self.i += 1
        out: list[str] = []
        while self.i < self.n:
            ch = self.raw[self.i]
            if ch == '"':
                self.i += 1
                return (''.join(out) if capture else None), True
            if ch == '\\':
                if self.i + 1 >= self.n:
                    return (''.join(out) if capture else None), False
                nxt = self.raw[self.i + 1]
                mapping = {'n': '\n', 't': '\t', 'r': '\r', 'b': '\b', 'f': '\f', '"': '"', '\\': '\\', '/': '/'}
                if nxt in mapping:
                    if capture:
                        out.append(mapping[nxt])
                    self.i += 2
                    continue
                if nxt == 'u':
                    if self.i + 6 > self.n:
                        return (''.join(out) if capture else None), False
                    hexpart = self.raw[self.i + 2:self.i + 6]
                    if any(c not in '0123456789abcdefABCDEF' for c in hexpart):
                        return (''.join(out) if capture else None), False
                    if capture:
                        out.append(chr(int(hexpart, 16)))
                    self.i += 6
                    continue
                if capture:
                    out.append(nxt)
                self.i += 2
                continue
            if capture:
                out.append(ch)
            self.i += 1
        return (''.join(out) if capture else None), False

    def _skip_value(self) -> bool:
        self._skip_ws()
        if self.i >= self.n:
            return False
        ch = self.raw[self.i]
        if ch == '"':
            _text, closed = self._string(capture=False)
            return closed
        if ch == '{':
            return self._skip_container('{', '}')
        if ch == '[':
            return self._skip_container('[', ']')
        if ch == '-' or ch.isdigit():
            return self._skip_number()
        for word in ('true', 'false', 'null'):
            if self.raw.startswith(word, self.i):
                self.i += len(word)
                return True
            if word.startswith(self.raw[self.i:]):
                return False
        return False

    def _skip_number(self) -> bool:
        if self.raw[self.i] == '-':
            self.i += 1
        digits = False
        while self.i < self.n and self.raw[self.i].isdigit():
            digits = True
            self.i += 1
        if self.i < self.n and self.raw[self.i] == '.':
            self.i += 1
            while self.i < self.n and self.raw[self.i].isdigit():
                digits = True
                self.i += 1
        if self.i < self.n and self.raw[self.i] in 'eE':
            self.i += 1
            if self.i < self.n and self.raw[self.i] in '+-':
                self.i += 1
            while self.i < self.n and self.raw[self.i].isdigit():
                self.i += 1
        return digits and self.i < self.n

    def _skip_container(self, open_ch: str, close_ch: str) -> bool:
        self.i += 1
        while True:
            self._skip_ws()
            if self.i >= self.n:
                return False
            if self.raw[self.i] == close_ch:
                self.i += 1
                return True
            if open_ch == '{':
                if self.raw[self.i] != '"':
                    return False
                _key, closed = self._string(capture=False)
                if not closed:
                    return False
                self._skip_ws()
                if self.i >= self.n or self.raw[self.i] != ':':
                    return False
                self.i += 1
            if not self._skip_value():
                return False
            self._skip_ws()
            if self.i >= self.n:
                return False
            if self.raw[self.i] == ',':
                self.i += 1
                continue
            if self.raw[self.i] == close_ch:
                self.i += 1
                return True
            return False
