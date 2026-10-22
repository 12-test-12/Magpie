"""Conservative, bounded JavaScript call recognizer (not a JavaScript parser).

Only literal first arguments to fetch and axios HTTP methods are promoted. The
source text is never returned or persisted; snippets are redacted at the edge.
"""
from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import urlsplit, parse_qsl, urlencode, urlunsplit
from . import normalize

MAX_BYTES = 16 * 1024 * 1024
MAX_CANDIDATES = 500
MAX_CHARS = 8192
MAX_TOKENS = 200_000
RULE_ID = "js-literal-call"
RULE_VERSION = "2"


def is_js_resource(url: str, mime: str = "") -> bool:
    try:
        path = urlsplit(url).path
    except ValueError:
        path = ""
    return path.endswith((".js", ".mjs")) or re.match(r"\s*(?:application|text)/(?:javascript|x-javascript|ecmascript)", mime, re.I) is not None


def is_manifest_sample(sample: str) -> bool:
    return re.search(r'^\s*\{\s*"magpie_js"\s*:\s*1(?:\s*[,}])', sample) is not None


def redact_url(value: str) -> str:
    parts = urlsplit(value)
    query = urlencode([(key, "[redacted]") for key, _ in parse_qsl(parts.query, keep_blank_values=True)])
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


def _tokens(text):
    """Yield (kind, value, start, end), skipping comments and preserving strings."""
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c.isspace(): i += 1; continue
        if text.startswith("//", i):
            j = text.find("\n", i + 2); i = n if j < 0 else j + 1; continue
        if text.startswith("/*", i):
            j = text.find("*/", i + 2)
            if j < 0:
                yield "invalid", "unterminated_comment", i, n; return
            i = j + 2; continue
        if c == "/":
            k = i - 1
            while k >= 0 and text[k].isspace(): k -= 1
            if k < 0 or text[k] in "=(:,[!&|?;{}":
                j, escaped, bracket = i + 1, False, False
                while j < n:
                    ch = text[j]
                    if escaped: escaped = False
                    elif ch == "\\": escaped = True
                    elif ch == "[": bracket = True
                    elif ch == "]": bracket = False
                    elif ch == "/" and not bracket: break
                    elif ch == "\n": break
                    j += 1
                if j < n and text[j] == "/":
                    j += 1
                    while j < n and text[j].isalpha(): j += 1
                    yield "regex", "", i, j; i = j; continue
        start = i
        if c in "'\"`":
            quote = c; i += 1; out = []; dynamic = False
            closed = False
            while i < n:
                if text[i] == "\\" and i + 1 < n:
                    nxt = text[i + 1]
                    if nxt in (quote, "\\", "/", "'", '"', '`'):
                        out.append(nxt); i += 2; continue
                    if nxt in ("n", "r", "t", "0", "b", "f", "v"):
                        out.append({"n": "\n", "r": "\r", "t": "\t", "0": "\0", "b": "\b", "f": "\f", "v": "\v"}[nxt])
                        i += 2; continue
                    if nxt == "x" and i + 3 < n and all(ch in "0123456789abcdefABCDEF" for ch in text[i+2:i+4]):
                        out.append(chr(int(text[i+2:i+4], 16)))
                        i += 4; continue
                    if nxt == "u" and i + 5 < n and all(ch in "0123456789abcdefABCDEF" for ch in text[i+2:i+6]):
                        out.append(chr(int(text[i+2:i+6], 16)))
                        i += 6; continue
                    if nxt in "\r\n":
                        i += 2
                        if nxt == "\r" and i < n and text[i] == "\n": i += 1
                        continue
                    yield "invalid", "unsupported_escape", start, i + 2
                    i += 2; continue
                if quote == '`' and text.startswith('${', i): dynamic = True
                if text[i] == quote: i += 1; closed = True; break
                out.append(text[i]); i += 1
            yield ("template" if quote == '`' and dynamic else "string", "".join(out), start, i) if closed else ("invalid", "unterminated_string", start, i)
        elif c.isalpha() or c in "_$":
            i += 1
            while i < n and (text[i].isalnum() or text[i] in "_$"): i += 1
            yield "id", text[start:i], start, i
        else:
            i += 1; yield "punct", c, start, i


def extract(text: str, *, max_candidates: int = MAX_CANDIDATES, cancelled=None):
    encoded = text.encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    if len(encoded) > MAX_BYTES:
        return {"sha256": digest, "status": "limit_exceeded", "candidates": [], "reason": "内容超过 16 MiB"}
    toks = []
    for token in _tokens(text):
        if cancelled is not None and len(toks) % 4096 == 0 and cancelled():
            return {"sha256": digest, "status": "cancelled", "unsupported_count": 0, "candidates": []}
        if len(toks) >= MAX_TOKENS:
            return {"sha256": digest, "status": "partial", "unsupported_count": 0,
                    "candidates": [], "reason": "词法 token 超过 200,000 项上限", "truncated": True}
        toks.append(token)
    found = []; unsupported = sum(1 for t in toks if t[0] == "invalid")
    def string_arg(index):
        if index >= len(toks) or toks[index][0] != 'string':
            return None, index
        start = toks[index]
        value, end = start[1], index
        # Evaluate string literals only. Identifiers, templates with interpolation,
        # operators other than +, and excessive expressions remain unsupported.
        while end + 2 < len(toks) and toks[end+1][1] == '+' and toks[end+2][0] == 'string':
            if end - index >= 78 or len(value) > MAX_CHARS:
                return None, index
            value += toks[end+2][1]; end += 2
        return ('string', value, start[2], toks[end][3]), end
    for i, tok in enumerate(toks):
        if tok[0] != "id": continue
        if i and toks[i-1][1] == ".": continue
        # A slash in expression-start position may begin a regular expression;
        # skip its body rather than interpreting calls embedded in its pattern.
        if i and toks[i-1][1] == "/": continue
        method = None; arg_index = None; call_start = tok[2]
        if tok[1] == "fetch" and i + 2 < len(toks) and toks[i+1][1] == "(":
            method, arg_index = "GET", i + 2
        elif tok[1] == "axios" and i + 4 < len(toks) and toks[i+1][1] == "." and toks[i+3][1] == "(":
            name = toks[i+2][1].upper()
            if toks[i+2][0] == "id" and name in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}:
                method, arg_index = name, i + 4
        if method is None: continue
        arg, arg_index = string_arg(arg_index)
        if arg is None:
            unsupported += 1; continue
        if arg[0] != "string" or arg_index + 1 >= len(toks) or toks[arg_index + 1][1] not in (",", ")"):
            unsupported += 1; continue
        url = arg[1]
        # fetch options are interpreted only when a literal method property is obvious.
        if tok[1] == "fetch" and arg_index + 1 < len(toks) and toks[arg_index+1][1] == ",":
            # Only infer GET when options are absent. An option object is treated
            # as a whole; ambiguous/dynamic method fields remain unknown.
            j = arg_index + 2
            if j >= len(toks) or toks[j][1] != "{":
                method = None
            else:
                depth = 0; close = None; top_methods = []; spread = False; computed = False; accessor = False
                k = j
                while k < len(toks):
                    val = toks[k][1]
                    if val == "{": depth += 1
                    elif val == "}":
                        depth -= 1
                        if depth == 0: close = k; break
                    if depth == 1 and val == "method":
                        top_methods.append(k)
                    if depth == 1 and val == "." and k + 2 < len(toks) and toks[k+1][1] == "." and toks[k+2][1] == ".": spread = True
                    if depth == 1 and val == "[": computed = True
                    if depth == 1 and val in ("get", "set"): accessor = True
                    k += 1
                if close is None or close + 1 >= len(toks) or toks[close+1][1] != ")":
                    method = None
                elif close == j + 1:
                    method = "GET"
                elif top_methods:
                    k = top_methods[0] + 2
                    literal_method = toks[k] if top_methods[0] + 1 < close and toks[top_methods[0]+1][1] == ":" and k < close else None
                    follows = k + 1 == close or (k + 1 < close and toks[k+1][1] == ",")
                    method = literal_method[1].upper() if literal_method and literal_method[0] == "string" and follows and len(top_methods) == 1 and not (spread or computed or accessor) else None
                else:
                    method = None
            if method is None:
                unsupported += 1
        before = text[:call_start]
        line = before.count("\n") + 1
        line_start = before.rfind("\n") + 1
        line_end = text.find("\n", tok[3]); line_end = len(text) if line_end < 0 else line_end
        full_line = str(normalize.redact_secrets(text[line_start:line_end]))
        full_line = re.sub(r"([?&][^=\s&'\"]+)=([^&\s'\"]*)", r"\1=[redacted]", full_line)
        snippet = full_line[:500]
        try:
            parsed = urlsplit(url)
        except ValueError:
            unsupported += 1; continue
        absolute = parsed.scheme.lower() in ("http", "https") and bool(parsed.netloc)
        dynamic = "${" in url or "}" in url or "{" in url or " + " in url
        if dynamic: unsupported += 1; continue
        names = sorted({k for k, _ in parse_qsl(parsed.query, keep_blank_values=True)})[:40]
        too_long = len(url) > 2000
        found.append({"url": url, "method": method if method != "unknown" else None,
                      "absolute": absolute and not too_long, "query_names": names, "start": arg[2], "end": arg[3],
                      "line": line, "snippet": snippet, "rule_id": RULE_ID, "rule_version": RULE_VERSION,
                      "disposition": "unsupported_limit" if too_long else ("confirmed_static" if absolute else "needs_base_confirmation"),
                      "reason": "URL 超过候选长度上限，未创建接口事实" if too_long else (None if absolute else "相对 URL 不使用 JS 资源地址推断运行基址")})
        if too_long:
            unsupported += 1
        if len(found) >= min(MAX_CANDIDATES, max_candidates):
            unsupported += 1; break
    truncated = len(found) >= min(MAX_CANDIDATES, max_candidates)
    return {"sha256": digest, "status": "partial" if unsupported else "bounded_rules_complete",
            "unsupported_count": unsupported, "candidates": found,
            "truncated": truncated,
            "reason": "达到候选上限，仅保留前500个候选，扫描已截断" if truncated and max_candidates >= MAX_CANDIDATES else
                      (f"达到候选上限，仅保留前{min(MAX_CANDIDATES, max_candidates)}个候选，扫描已截断" if truncated else None)}


def indicators(text, *, cancelled=None):
    """Bounded references and existing secret-rule hits, with no secret values."""
    result = []
    if len(text.encode('utf-8')) > MAX_BYTES:
        return {'rows':[],'truncated':True}
    offset = 0
    for line_number, line in enumerate(text.splitlines(keepends=True), 1):
        if cancelled and cancelled():
            return {'rows':result,'truncated':True}
        for hit in normalize.detect_secrets(line):
            value = str(hit['value'])
            pos = line.find(value)
            result.append({'kind':'secret_candidate','rule_id':str(hit['rule']),'rule_version':RULE_VERSION,
                'masked':str(hit['masked']),'value_sha256':hashlib.sha256((str(hit['rule'])+':'+value).encode()).hexdigest(),
                'line':line_number,'start':offset+max(0,pos),'end':offset+max(0,pos)+len(value)})
            if len(result) >= MAX_CANDIDATES:
                return {'rows':result,'truncated':True}
        offset += len(line)
    for index, token in enumerate(_tokens(text)):
        if index >= MAX_TOKENS or cancelled and index % 4096 == 0 and cancelled():
            return {'rows':result,'truncated':True}
        if token[0] != 'string': continue
        try:
            parsed = urlsplit(token[1])
            if parsed.scheme not in ('http','https') or not parsed.hostname: continue
            safe = str(normalize.redact_secrets(redact_url(token[1])))
        except ValueError:
            continue
        result.append({'kind':'url_reference','url':safe[:2000],'host':str(normalize.redact_secrets(parsed.hostname)),
                       'truncated':len(safe)>2000,'rule_id':'js-string-url','rule_version':RULE_VERSION,
                       'line':text.count('\n',0,token[2])+1,'start':token[2],'end':token[3]})
        if len(result) >= MAX_CANDIDATES:
            return {'rows':result,'truncated':True}
    return {'rows':result,'truncated':False}
