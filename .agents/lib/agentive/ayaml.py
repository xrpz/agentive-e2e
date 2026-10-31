# agentive-unit: lib.agentive
# design: docs/IMPLEMENTATION.md §4 (runtime: python3 stdlib only) [design judgment]
"""ayaml: the YAML subset agentive reads and writes at run time (python3 stdlib only).

Why: agentive files in user repos are YAML (.agents/manifest.yaml, guard.yaml, rubric.yaml,
decisions.yaml, .agents/custom/** overrides, workflow overrides) but the run time may use
python3 stdlib only (no PyYAML). This module parses the subset below and fails loudly with
`ayaml: line N: <what>` on anything else, so a caller can report the file instead of guessing.

Supported: one document (optional leading `---`), block mappings, block sequences (also
`key:` followed by `- item` at the same indent), plain / 'single' / "double" scalars,
block scalars | |- |+ > >- (optional indent digit), flow [..] and {..} (nested, may span
lines), comments. Scalars resolve by the YAML 1.2 core schema: null ~ true false ints
floats; everything else is a string (so `on:` stays the string "on").
Not supported (error): anchors &, aliases *, tags !, complex keys `? `, multi-line plain
scalars, several documents, tabs in indentation, duplicate keys.

API: loads(text) -> object; load(path); dumps(obj) -> text; dump(obj, path).
dumps() writes block style, keeps dict order, and quotes only when needed; dumps(loads(x))
round-trips every value this module produces.
"""
import math
import re

__all__ = ["loads", "load", "dumps", "dump", "YAMLError"]


class YAMLError(ValueError):
    pass


# ---------------------------------------------------------------- scalars
_INT = re.compile(r"^[-+]?[0-9]+$")
_OCT = re.compile(r"^0o[0-7]+$")
_HEX = re.compile(r"^0x[0-9a-fA-F]+$")
_FLOAT = re.compile(r"^[-+]?(\.[0-9]+|[0-9]+(\.[0-9]*)?)([eE][-+]?[0-9]+)?$")


def _resolve(s):
    if s in ("", "~", "null", "Null", "NULL"):
        return None
    if s in ("true", "True", "TRUE"):
        return True
    if s in ("false", "False", "FALSE"):
        return False
    if _INT.match(s):
        return int(s, 10)
    if _OCT.match(s):
        return int(s[2:], 8)
    if _HEX.match(s):
        return int(s[2:], 16)
    if _FLOAT.match(s):
        return float(s)
    if s in (".inf", ".Inf", ".INF", "+.inf", "+.Inf", "+.INF"):
        return math.inf
    if s in ("-.inf", "-.Inf", "-.INF"):
        return -math.inf
    if s in (".nan", ".NaN", ".NAN"):
        return math.nan
    return s


_ESC = {"0": "\0", "a": "\a", "b": "\b", "t": "\t", "\t": "\t", "n": "\n", "v": "\v", "f": "\f",
        "r": "\r", "e": "\x1b", " ": " ", '"': '"', "/": "/", "\\": "\\", "N": "\x85",
        "_": "\xa0", "L": " ", "P": " "}


class _Src:
    """Character cursor over the whole text, used for quoted scalars and flow collections."""

    def __init__(self, text, pos, line_of):
        self.t, self.p, self.line_of = text, pos, line_of

    def err(self, msg):
        raise YAMLError(f"ayaml: line {self.line_of(self.p)}: {msg}")

    def peek(self):
        return self.t[self.p] if self.p < len(self.t) else ""

    def skip_ws(self, newlines=True):
        while self.p < len(self.t):
            c = self.t[self.p]
            if c in " \t" or (newlines and c in "\r\n"):
                self.p += 1
            elif c == "#" and (self.p == 0 or self.t[self.p - 1] in " \t\r\n"):
                while self.p < len(self.t) and self.t[self.p] not in "\r\n":
                    self.p += 1
            else:
                break

    def double(self):
        assert self.t[self.p] == '"'
        self.p += 1
        out = []
        while True:
            if self.p >= len(self.t):
                self.err("unterminated double-quoted scalar")
            c = self.t[self.p]
            if c == '"':
                self.p += 1
                return "".join(out)
            if c == "\\":
                n = self.t[self.p + 1:self.p + 2]
                if n in _ESC:
                    out.append(_ESC[n]); self.p += 2
                elif n in ("x", "u", "U"):
                    k = {"x": 2, "u": 4, "U": 8}[n]
                    h = self.t[self.p + 2:self.p + 2 + k]
                    if not re.fullmatch(r"[0-9a-fA-F]{%d}" % k, h):
                        self.err("bad escape \\" + n + h)
                    out.append(chr(int(h, 16))); self.p += 2 + k
                elif n in ("\n", "\r"):
                    self.p += 1
                    while self.p < len(self.t) and self.t[self.p] in " \t\r\n":
                        self.p += 1
                else:
                    self.err("bad escape \\" + n)
            elif c in "\r\n":
                # line folding inside a quoted scalar: newline -> space, blank line -> newline
                j = self.p; nl = 0
                while j < len(self.t) and self.t[j] in " \t\r\n":
                    if self.t[j] == "\n":
                        nl += 1
                    j += 1
                while out and out[-1] in " \t":
                    out.pop()
                out.append("\n" * (nl - 1) if nl > 1 else " ")
                self.p = j
            else:
                out.append(c); self.p += 1

    def single(self):
        assert self.t[self.p] == "'"
        self.p += 1
        out = []
        while True:
            if self.p >= len(self.t):
                self.err("unterminated single-quoted scalar")
            c = self.t[self.p]
            if c == "'":
                if self.t[self.p + 1:self.p + 2] == "'":
                    out.append("'"); self.p += 2; continue
                self.p += 1
                return "".join(out)
            if c in "\r\n":
                j = self.p; nl = 0
                while j < len(self.t) and self.t[j] in " \t\r\n":
                    if self.t[j] == "\n":
                        nl += 1
                    j += 1
                while out and out[-1] in " \t":
                    out.pop()
                out.append("\n" * (nl - 1) if nl > 1 else " ")
                self.p = j
            else:
                out.append(c); self.p += 1

    def flow_plain(self):
        start = self.p
        while self.p < len(self.t):
            c = self.t[self.p]
            if c in ",[]{}\r\n":
                break
            if c == ":" and self.t[self.p + 1:self.p + 2] in (" ", ",", "]", "}", "\n", "\r", ""):
                break
            if c == "#" and self.t[self.p - 1] in " \t":
                break
            self.p += 1
        return self.t[start:self.p].strip()

    def flow_node(self):
        self.skip_ws()
        c = self.peek()
        if c == "[":
            return self.flow_seq()
        if c == "{":
            return self.flow_map()
        if c == '"':
            return self.double()
        if c == "'":
            return self.single()
        if c in "&*!?|>%@`":
            self.err(f"unsupported flow node starting with {c!r}")
        return _resolve(self.flow_plain())

    def flow_seq(self):
        self.p += 1
        out = []
        while True:
            self.skip_ws()
            if self.peek() == "]":
                self.p += 1
                return out
            if self.peek() == "":
                self.err("unterminated flow sequence")
            item = self.flow_node()
            self.skip_ws()
            if self.peek() == ":":  # single-pair mapping inside a flow sequence
                self.p += 1
                item = {self._key(item): self.flow_node()}
                self.skip_ws()
            out.append(item)
            if self.peek() == ",":
                self.p += 1
            elif self.peek() != "]":
                self.err("expected , or ] in flow sequence")

    def flow_map(self):
        self.p += 1
        out = {}
        while True:
            self.skip_ws()
            if self.peek() == "}":
                self.p += 1
                return out
            if self.peek() == "":
                self.err("unterminated flow mapping")
            k = self._key(self.flow_node())
            self.skip_ws()
            if self.peek() == ":":
                self.p += 1
                v = self.flow_node()
            else:
                v = None
            if k in out:
                self.err(f"duplicate key {k!r}")
            out[k] = v
            self.skip_ws()
            if self.peek() == ",":
                self.p += 1
            elif self.peek() != "}":
                self.err("expected , or } in flow mapping")

    def _key(self, k):
        if isinstance(k, (dict, list)):
            self.err("complex (collection) keys are not supported")
        return k


# ---------------------------------------------------------------- block parser
class _Parser:
    def __init__(self, text):
        if text.startswith("﻿"):
            text = text[1:]
        self.text = text.replace("\r\n", "\n")
        self.lines = self.text.split("\n")
        self.starts = []
        pos = 0
        for ln in self.lines:
            self.starts.append(pos)
            pos += len(ln) + 1
        self.i = 0

    def line_of(self, pos):
        lo, hi = 0, len(self.starts) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self.starts[mid] <= pos:
                lo = mid
            else:
                hi = mid - 1
        return lo + 1

    def err(self, msg, i=None):
        raise YAMLError(f"ayaml: line {(self.i if i is None else i) + 1}: {msg}")

    @staticmethod
    def _blank(ln):
        s = ln.strip()
        return s == "" or s.startswith("#")

    def _skip_blank(self):
        while self.i < len(self.lines) and self._blank(self.lines[self.i]):
            self.i += 1

    def _indent(self, ln):
        n = len(ln) - len(ln.lstrip(" "))
        if ln[n:n + 1] == "\t":
            self.err("tab in indentation")
        return n

    def parse(self):
        self._skip_blank()
        if self.i < len(self.lines) and self.lines[self.i].rstrip() == "---":
            self.i += 1
            self._skip_blank()
        if self.i >= len(self.lines):
            return None
        val = self._node(self._indent(self.lines[self.i]))
        self._skip_blank()
        if self.i < len(self.lines):
            s = self.lines[self.i].strip()
            if s in ("---", "..."):
                self.err("several documents are not supported")
            self.err("unexpected content (bad indentation?)")
        return val

    def _node(self, indent):
        """Parse the block node whose first line is self.i at column `indent`."""
        ln = self.lines[self.i]
        body = ln[indent:]
        if body.startswith("- ") or body.rstrip() == "-":
            return self._seq(indent)
        if self._split_key(body, self.i) is not None:
            return self._map(indent)
        # a lone scalar / flow node as the whole document or item
        return self._inline_value(body, self.i, indent, None)

    def _split_key(self, body, i):
        """Return (key, rest) when `body` starts a mapping entry, else None."""
        if body[:1] in ('"', "'"):
            src = _Src(self.text, self.starts[i] + (len(self.lines[i]) - len(body)), self.line_of)
            k = src.double() if body[0] == '"' else src.single()
            rest = self.text[src.p:self.starts[i] + len(self.lines[i])]
            m = re.match(r"^\s*:(\s|$)", rest)
            if not m:
                return None
            return k, rest[m.end():] if m.group(1) else ""
        if body[:2] == "? ":
            self.err("complex keys `? ` are not supported", i)
        if body[:1] in "[{&*!|>%@`#":
            return None
        m = re.search(r":(\s|$)", body)
        if not m:
            return None
        k = body[:m.start()].rstrip()
        if " #" in k or "\t#" in k:
            return None
        if k == "":
            return None
        return _resolve(k), body[m.end():]

    def _map(self, indent):
        out = {}
        while True:
            self._skip_blank()
            if self.i >= len(self.lines):
                return out
            ln = self.lines[self.i]
            ind = self._indent(ln)
            if ind < indent:
                return out
            if ind > indent:
                self.err("bad indentation in mapping")
            body = ln[indent:]
            if body.startswith("- ") or body.rstrip() == "-":
                return out  # a sequence at the parent's indent ends this mapping
            if indent == 0 and body.rstrip() in ("---", "..."):
                self.err("several documents are not supported")
            kv = self._split_key(body, self.i)
            if kv is None:
                self.err("expected `key: value`")
            k, rest = kv
            if isinstance(k, str) and k.startswith(("&", "*", "!")):
                self.err("anchors, aliases and tags are not supported")
            if k in out:
                self.err(f"duplicate key {k!r}")
            key_line = self.i
            out[k] = self._value_after(rest, key_line, indent, allow_same_indent_seq=True)

    def _seq(self, indent):
        out = []
        while True:
            self._skip_blank()
            if self.i >= len(self.lines):
                return out
            ln = self.lines[self.i]
            ind = self._indent(ln)
            if ind < indent:
                return out
            if ind > indent:
                self.err("bad indentation in sequence")
            body = ln[indent:]
            if not (body.startswith("- ") or body.rstrip() == "-"):
                return out
            if body.rstrip() == "-":
                out.append(self._value_after("", self.i, indent, allow_same_indent_seq=False))
                continue
            # item content starts after "- " (and any extra spaces)
            j = 2
            while body[j:j + 1] == " ":
                j += 1
            content = body[j:]
            col = indent + j
            if content.startswith("- "):
                self.err("nested `- - ` sequences on one line are not supported")
            kv = self._split_key(content, self.i) if content[:1] not in "[{" else None
            if kv is not None and not content.startswith("#"):
                # mapping item: re-read it as a mapping at column `col`
                self.lines[self.i] = " " * col + content
                out.append(self._map(col))
            else:
                out.append(self._value_after(content, self.i, indent, allow_same_indent_seq=False, inline=True))

    def _value_after(self, rest, key_line, indent, allow_same_indent_seq, inline=False):
        """Value of a `key:` or `- ` whose remaining text on the line is `rest`."""
        r = rest.strip()
        if r.startswith("#"):
            r = ""
        if r == "":
            self.i = key_line + 1
            self._skip_blank()
            if self.i >= len(self.lines):
                return None
            ln = self.lines[self.i]
            ind = self._indent(ln)
            body = ln[ind:]
            if ind > indent:
                return self._node(ind)
            if ind == indent and allow_same_indent_seq and (body.startswith("- ") or body.rstrip() == "-"):
                return self._seq(ind)
            return None
        col = len(self.lines[key_line]) - len(rest)
        return self._inline_value(rest, key_line, indent, col)

    def _inline_value(self, rest, line, indent, col):
        r = rest.lstrip()
        if col is None:
            col = len(self.lines[line]) - len(rest)
        col += len(rest) - len(r)
        c = r[:1]
        if c in ("|", ">"):
            return self._block_scalar(r, line, indent)
        if c in ("&", "*", "!"):
            self.err("anchors, aliases and tags are not supported", line)
        if c in ('"', "'", "[", "{"):
            src = _Src(self.text, self.starts[line] + col, self.line_of)
            if c == '"':
                v = src.double()
            elif c == "'":
                v = src.single()
            else:
                v = src.flow_node()
            # the rest of the last line must be empty or a comment
            end_line = self.line_of(src.p) - 1
            tail = self.text[src.p:self.starts[end_line] + len(self.lines[end_line])].strip()
            if tail and not tail.startswith("#"):
                self.err(f"unexpected text after value: {tail!r}", end_line)
            self.i = end_line + 1
            return v
        # plain scalar: strip a trailing comment
        m = re.search(r"\s#", r)
        s = (r[:m.start()] if m else r).strip()
        if ": " in s or s.endswith(":"):
            self.err("a plain scalar may not contain ': ' (quote it)", line)
        self.i = line + 1
        # a multi-line plain scalar continues on a more-indented line
        j = self.i
        while j < len(self.lines) and self._blank(self.lines[j]):
            j += 1
        if j < len(self.lines) and self._indent(self.lines[j]) > indent and not self.lines[j].lstrip().startswith(("- ", "#")):
            nxt = self.lines[j].strip()
            if self._split_key(nxt, j) is None:
                self.err("multi-line plain scalars are not supported (quote it or use |)", j)
        return _resolve(s)

    def _block_scalar(self, head, line, indent):
        m = re.match(r"^([|>])([1-9])?([+-])?([1-9])?\s*(#.*)?$", head)
        if not m:
            self.err("bad block scalar header", line)
        style = m.group(1)
        ind_digit = m.group(2) or m.group(4)
        chomp = m.group(3) or ""
        self.i = line + 1
        raw = []
        while self.i < len(self.lines):
            ln = self.lines[self.i]
            if ln.strip() == "":
                raw.append(ln); self.i += 1; continue
            if self._indent(ln) <= indent:
                break
            raw.append(ln); self.i += 1
        content_ind = None
        if ind_digit:
            content_ind = indent + int(ind_digit)
        else:
            for ln in raw:
                if ln.strip():
                    content_ind = len(ln) - len(ln.lstrip(" "))
                    break
        if content_ind is None:
            lines = []
        else:
            lines = [ln[content_ind:] if len(ln) >= content_ind else "" for ln in raw]
        n_trail = 0
        while n_trail < len(lines) and lines[len(lines) - 1 - n_trail].strip() == "":
            n_trail += 1
        core = lines[:len(lines) - n_trail]
        if not core:
            return "\n" * n_trail if chomp == "+" else ""
        if style == "|":
            body = "\n".join(core)
        else:
            # folding: lines of one paragraph join with a space, a blank line is a newline,
            # more-indented lines are kept as they are
            parts, prev = [], None
            for ln in core:
                if ln == "":
                    parts.append("\n"); prev = "blank"
                elif ln[:1] in (" ", "\t"):
                    if prev == "text":
                        parts.append("\n")
                    parts.append(ln + "\n"); prev = "more"
                else:
                    if prev == "text":
                        parts.append(" ")
                    parts.append(ln); prev = "text"
            body = "".join(parts).rstrip("\n") if prev != "more" else "".join(parts)[:-1]
        if chomp == "-":
            return body
        if chomp == "+":
            return body + "\n" + "\n" * n_trail
        return body + "\n"


def loads(text):
    """Parse agentive-subset YAML text. Raises YAMLError('ayaml: line N: ...')."""
    return _Parser(text).parse()


def load(path):
    with open(path, encoding="utf-8") as f:
        return loads(f.read())


# ---------------------------------------------------------------- emitter
_PLAIN_OK = re.compile(r"^[A-Za-z0-9_./$()@%=+~^\-][^\n]*$")


def _plain_safe(s):
    if s == "" or s != s.strip() or not _PLAIN_OK.match(s):
        return False
    if s[0] in "-?:,[]{}#&*!|>'\"%@`" and not (s[0] == "-" and len(s) > 1 and s[1] not in " "):
        return False
    if s.startswith("- ") or ": " in s or s.endswith(":") or " #" in s or "\t" in s:
        return False
    if any(c in s for c in "[]{},") and s[0] not in "$":
        # flow indicators are fine in block context but keep them quoted for readability by agents
        return False
    if s.lower() in _YAML11_WORDS or _YAML11_NUMBERISH.match(s):
        return False  # YAML 1.1 readers (PyYAML, older tools) would read these as bool / number
    return isinstance(_resolve(s), str)


_YAML11_WORDS = {"on", "off", "yes", "no", "y", "n", "true", "false", "null", "~"}
_YAML11_NUMBERISH = re.compile(r"^[-+.]?[0-9]")


def _quote(s):
    out = ['"']
    for c in s:
        if c == '"':
            out.append('\\"')
        elif c == "\\":
            out.append("\\\\")
        elif c == "\n":
            out.append("\\n")
        elif c == "\t":
            out.append("\\t")
        elif ord(c) < 0x20 or c == "\x7f":
            out.append("\\x%02x" % ord(c))
        else:
            out.append(c)
    out.append('"')
    return "".join(out)


def _scalar(v):
    if v is None:
        return "null"
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if math.isnan(v):
            return ".nan"
        if math.isinf(v):
            return ".inf" if v > 0 else "-.inf"
        r = repr(v)
        return r if _FLOAT.match(r) else _quote(r)
    if isinstance(v, str):
        return v if _plain_safe(v) else _quote(v)
    raise YAMLError(f"ayaml: cannot dump {type(v).__name__}")


def _key(k):
    if isinstance(k, (dict, list)):
        raise YAMLError("ayaml: collection keys cannot be dumped")
    return _scalar(k)


def _block_str(v, ind):
    if "\n" not in v or v.endswith(" ") or any(ln != ln.rstrip(" \t") for ln in v.split("\n")) or v.startswith(" "):
        return None
    if v.endswith("\n\n") or "\r" in v or any(ord(c) < 0x20 and c not in "\n" for c in v):
        return None
    head = "|" if v.endswith("\n") else "|-"
    body = v[:-1] if v.endswith("\n") else v
    pad = " " * ind
    return head + "\n" + "\n".join((pad + ln) if ln else "" for ln in body.split("\n"))


def _emit(v, ind, out):
    pad = " " * ind
    if isinstance(v, dict):
        for k, x in v.items():
            if isinstance(x, dict) and x:
                out.append(f"{pad}{_key(k)}:")
                _emit(x, ind + 2, out)
            elif isinstance(x, list) and x:
                out.append(f"{pad}{_key(k)}:")
                _emit(x, ind + 2, out)
            else:
                out.append(f"{pad}{_key(k)}: {_inline(x, ind + 2)}")
    elif isinstance(v, list):
        for x in v:
            if isinstance(x, dict) and x:
                sub = []
                _emit(x, ind + 2, sub)
                sub[0] = pad + "- " + sub[0][ind + 2:]
                out.extend(sub)
            elif isinstance(x, list) and x:
                out.append(f"{pad}-")
                _emit(x, ind + 2, out)
            else:
                out.append(f"{pad}- {_inline(x, ind + 2)}")
    else:
        out.append(pad + _inline(v, ind))


def _inline(x, ind):
    if isinstance(x, dict):
        return "{}"
    if isinstance(x, list):
        return "[]"
    if isinstance(x, str):
        b = _block_str(x, ind)
        if b is not None:
            return b
    return _scalar(x)


def dumps(obj):
    """Block-style YAML text ending with one newline; dict order is kept."""
    out = []
    if isinstance(obj, (dict, list)) and obj:
        _emit(obj, 0, out)
    elif isinstance(obj, (dict, list)):
        out.append(_inline(obj, 0))
    else:
        out.append(_scalar(obj))
    return "\n".join(out) + "\n"


def dump(obj, path):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(dumps(obj))
