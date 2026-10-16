"""Native manifest for explicitly supplied local JavaScript text."""
from __future__ import annotations

import json
from typing import Iterable, Iterator

from .. import normalize
from ..js_intelligence import extract, redact_url
from . import _json
from ._urls import EndpointSink
from .base import ParseContext, Parser, RawObservation, Record


class JsManifestParser(Parser):
    tool = "js-manifest"
    format_version = "1"
    known_fields = frozenset({"magpie_js", "url", "content"})

    @classmethod
    def sniff(cls, sample: str, filename: str) -> float:
        del filename
        if not sample.lstrip().startswith("{"):
            return 0.0
        return 1.0 if "magpie_js" in _json.top_level_keys(sample) else 0.0

    def parse(self, lines: Iterable[str], ctx: ParseContext) -> Iterator[Record]:
        text = _json.collect_json(lines, ctx, "JS manifest")
        if text is None:
            return
        try:
            doc = json.loads(text)
        except (ValueError, TypeError):
            ctx.on_bad_line(1, "JS manifest 不是合法 JSON", "")
            return
        if not isinstance(doc, dict) or type(doc.get("magpie_js")) is not int or doc.get("magpie_js") != 1:
            ctx.on_bad_line(1, "需要 magpie_js=1 的 JS manifest", "")
            return
        _json.report_unknown(doc, self.known_fields, ctx, "JS manifest.")
        if not isinstance(doc.get("url"), str) or not isinstance(doc.get("content"), str):
            ctx.on_bad_line(1, "JS manifest 需要 url 与 content 字符串", "")
            return
        yield from _records(doc["url"], doc["content"], ctx, "$.content")


def _records(url: str, content: str, ctx: ParseContext, record_path: str) -> Iterator[Record]:
    if len(content.encode("utf-8")) > 16 * 1024 * 1024:
        ctx.on_bad_line(1, "JavaScript 内容超过 16 MiB 上限", "")
        result = {"status": "limit_exceeded", "reason": "内容超过 16 MiB"}
    else:
        result = extract(content, cancelled=ctx.cancelled)
    if result.get("status") == "cancelled":
        return
    try:
        identity = normalize.endpoint_identity(url)
    except normalize.NormalizeError as exc:
        ctx.on_bad_line(1, f"JS manifest 的资源 URL 无效：{exc}", "")
        return
    key = str(identity["key"])
    yield from EndpointSink().emit(url, ctx, 1)
    yield RawObservation(entity_ref=("endpoint", key), type="js_content_version",
                         value=json.dumps({"sha256": result.get("sha256"),
                                           "status": result.get("status"), "rule_id": "js-literal-call",
                                           "rule_version": "1", "record_path": record_path,
                                           "candidate_count": len(result.get("candidates", [])),
                                           "unsupported_count": result.get("unsupported_count", 0),
                                           "truncated": bool(result.get("truncated")),
                                           "reason": result.get("reason")}, ensure_ascii=False, separators=(",", ":")))
    sink = EndpointSink()
    for candidate in result.get("candidates", []):
        safe = dict(candidate)
        safe["url"] = str(redact_url(str(normalize.redact_secrets(safe["url"]))))
        safe["snippet"] = str(normalize.redact_secrets(safe["snippet"]))[:500]
        safe["content_sha256"] = result.get("sha256")
        safe["record_path"] = record_path
        yield RawObservation(entity_ref=("endpoint", key), type="js_candidate",
                             value=json.dumps(safe, ensure_ascii=False, separators=(",", ":")))
        if candidate["absolute"]:
            target_url = str(redact_url(str(normalize.redact_secrets(candidate["url"]))))
            yield from sink.emit(target_url, ctx, 1)
            try:
                target = normalize.endpoint_identity(target_url)["key"]
            except normalize.NormalizeError:
                continue
            if candidate.get("method"):
                yield RawObservation(entity_ref=("endpoint", str(target)), type="js_static_method",
                                     value=candidate["method"])
            for name in candidate.get("query_names", []):
                yield RawObservation(entity_ref=("endpoint", str(target)), type="js_static_param", value=name)
            yield RawObservation(entity_ref=("endpoint", str(target)), type="js_source_ref",
                                 value=json.dumps({"resource_key": key, "content_sha256": result.get("sha256"),
                                                   "record_path": record_path}, ensure_ascii=False,
                                                   separators=(",", ":")))
