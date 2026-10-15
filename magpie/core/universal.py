"""Bounded, offline format decoding and explainable import mapping.

No database writes: the configured parser emits the existing Raw* contract.
Candidate details are bounded; the archived file remains the complete source.

这个主要是针对的是用户使用的是我没有规划好的工具，如果用户使用的是我没有规划的工具就走这个文件的逻辑，直接所搜里面的信息，不管格式方面的
"""
from __future__ import annotations

import csv
import hashlib
import io
import itertools
import json
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from . import normalize
from .parsers.base import RawEntity, RawObservation
from .parsers._urls import EndpointSink

MAX_BYTES = 16 * 1024 * 1024
MAX_FIELDS = 2000
DETAIL_LIMIT = 200
KINDS = ("auto", "url", "domain", "ip", "port", "status", "method", "technology", "ignore", "unknown")
HINTS = {
    "url": {"url", "uri", "target", "link", "endpoint", "address"},
    "domain": {"host", "hostname", "domain", "fqdn"},
    "ip": {"ip", "ipv4", "ipv6", "server_address", "ip_address"},
    "port": {"port", "ports", "open_ports"},
    "status": {"status", "status_code", "http_status"},
    "method": {"method", "http_method"},
    "technology": {"tech", "technology", "technologies", "server", "webserver"},
}


def validate_mapping(mapping: dict[str, str] | None) -> dict[str, str]:
    mapping = dict(mapping or {})
    if len(mapping) > MAX_FIELDS or any(
        not isinstance(k, str) or len(k) > 512 or v not in KINDS
        for k, v in mapping.items()
    ):
        raise ValueError("映射字段过多、路径过长或映射类型无效")
    return mapping


def detect(text: str, filename: str = "") -> str:
    sample = text.lstrip()
    if Path(filename).suffix.lower() in (".jsonl", ".ndjson"):
        return "jsonl"
    if sample.startswith(("{", "[")):
        try:
            json.loads(sample)
            return "json"
        except ValueError:
            first = sample.splitlines()[0] if sample else ""
            try:
                json.loads(first)
                return "jsonl"
            except ValueError:
                return "json"
    if sample.startswith("<"):
        return "xml"
    first = next((s for s in sample.splitlines() if s.strip()), "")
    if "\t" in first:
        return "tsv"
    if "," in first:
        return "csv"
    return "text"


def leaves(value: Any, path: str = "$", depth: int = 0):
    if depth > 24:
        raise ValueError("嵌套深度超过 24 层")
    if isinstance(value, dict):
        for k, v in value.items():
            segment = f".{k}" if str(k).isidentifier() else f"[{json.dumps(str(k), ensure_ascii=False)}]"
            yield from leaves(v, path + segment, depth + 1)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from leaves(v, f"{path}[{i}]", depth + 1)
    elif value is not None and value != "":
        yield path, value


def decode(text: str, fmt: str):
    """Yield (source line, record path, value, error). Never hide malformed rows."""
    if fmt == "json":
        try:
            obj = json.loads(text)
            rows = obj if isinstance(obj, list) else [obj]
            for i, value in enumerate(rows):
                yield 1, f"$[{i}]" if isinstance(obj, list) else "$", value, None
        except (ValueError, RecursionError) as exc:
            raise ValueError(f"JSON 文件解码失败：{exc}") from exc
    elif fmt == "xml":
        if "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
            raise ValueError("XML 不允许 DTD 或实体声明")
        try:
            root = ElementTree.fromstring(text)
        except ElementTree.ParseError as exc:
            raise ValueError(f"XML 文件解码失败：{exc}") from exc

        def obj(el, depth=0):
            if depth > 24:
                raise ValueError("XML 嵌套深度超过 24 层")
            result = dict(el.attrib)
            if (el.text or "").strip() and (len(el) or el.attrib):
                result["$text"] = el.text.strip()
            for child in el:
                val = obj(child, depth + 1)
                if child.tag in result:
                    old = result[child.tag]
                    result[child.tag] = old + [val] if isinstance(old, list) else [old, val]
                else:
                    result[child.tag] = val
            return result if result else (el.text or "").strip()

        rows = list(root) if len(root) and len({e.tag for e in root}) == 1 and all(
            len(e) or e.attrib for e in root) else [root]
        for i, el in enumerate(rows):
            yield 1, f"/{root.tag}/{el.tag}[{i + 1}]", obj(el), None
    elif fmt in ("csv", "tsv"):
        reader = csv.DictReader(io.StringIO(text), delimiter="\t" if fmt == "tsv" else ",")
        if not reader.fieldnames or len(set(reader.fieldnames)) != len(reader.fieldnames):
            raise ValueError("CSV/TSV 缺少表头或存在重复列名")
        try:
            for index, row in enumerate(reader, 1):
                error = "列数与表头不一致" if None in row or any(v is None for v in row.values()) else None
                yield reader.line_num, f"$[{index - 1}]", row, error
        except csv.Error as exc:
            raise ValueError(f"CSV 解码失败：{exc}") from exc
    else:
        for number, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line) if fmt == "jsonl" else {"value": line.strip()}
                yield number, f"line:{number}", value, None
            except (ValueError, RecursionError):
                yield number, f"line:{number}", None, "JSONL 行解码失败"


def classify(path: str, value: Any, forced: str = "auto"):
    key = path.rsplit(".", 1)[-1].split("[", 1)[0].lower()
    text = str(value).strip()
    if forced in ("ignore", "unknown"):
        return forced, text, "人工选择"
    kinds = [forced] if forced != "auto" else [k for k, names in HINTS.items() if key in names]
    if not kinds:
        # A value-shaped match alone remains a candidate, never an asset.
        possible = "ip" if normalize.try_ip(text) else "domain" if normalize.try_domain(text) else "unknown"
        return "unknown", text, f"仅值形状候选：{possible}，缺少明确字段语义"
    kind = kinds[0]
    try:
        if kind == "url":
            if not text.startswith(("http://", "https://")):
                raise ValueError("需要完整 HTTP(S) URL；相对路径保留待确认")
            normalize.endpoint_identity(text)
        elif kind == "domain":
            if normalize.try_ip(text):
                return "ip", normalize.normalize_ip(text), "主机字段包含 IP"
            text = normalize.normalize_domain(text)
        elif kind == "ip":
            text = normalize.normalize_ip(text)
        elif kind == "port":
            if isinstance(value, bool) or not text.isdecimal() or not 1 <= int(text) <= 65535:
                raise ValueError("端口必须为 1–65535")
        elif kind == "status":
            if not text.isdecimal() or not 100 <= int(text) <= 599:
                raise ValueError("HTTP 状态码必须为 100–599")
        elif kind == "method":
            text = text.upper()
            if text not in {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS", "TRACE", "CONNECT"}:
                raise ValueError("未知 HTTP 方法")
        elif kind == "technology" and len(text) > 200:
            raise ValueError("技术信息过长")
        return kind, text, "人工映射并校验" if forced != "auto" else "字段提示与值校验"
    except (ValueError, TypeError):
        return "unknown", text, f"{kind} 类型校验失败或信息不完整"


def fingerprint(mapping: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(mapping, sort_keys=True).encode()).hexdigest()[:16]


def can_auto_extract(sample: str) -> bool:
    """Auto fallback requires an explicit object field; nested schemas need review."""
    fmt = detect(sample)
    if fmt == "text":
        return False
    try:
        for _, _, row, error in decode(sample, fmt):
            if error or not isinstance(row, dict):
                continue
            for key, value in row.items():
                if not isinstance(value, (dict, list)) and classify(f"$.{key}", value)[0] in ("url", "domain", "ip"):
                    return True
    except (ValueError, RecursionError):
        return False
    return False


def parser_for(mapping: dict[str, str] | None = None):
    configured = validate_mapping(mapping)

    class GenericParser:
        tool = "generic"
        format_version = "1-" + fingerprint(configured)

        @staticmethod
        def definition_extra():
            # Helper changes must invalidate the recorded implementation hash too.
            return Path(__file__).read_text(encoding="utf-8")

        def parse(self, lines, ctx):
            coverage = {"format": None, "records_total": 0, "mapped_complete": 0,
                        "mapped_partial": 0, "unrecognized": 0, "ignored": 0,
                        "record_error": 0, "details": [], "details_truncated": False,
                        "fields": {}, "mapping": configured, "complete": False}
            coverage.update({"fields_total": 0, "fields_mapped": 0, "fields_ignored": 0,
                             "fields_unresolved": 0, "field_catalog_truncated": False})
            ctx.import_coverage = coverage
            parts, size = [], 0
            for line in lines:
                size += len(line.encode("utf-8"))
                if size > MAX_BYTES:
                    raise ValueError("通用导入上限 16 MiB，请拆分文件；原始文件仍保留")
                parts.append(line if line.endswith("\n") else line + "\n")
            text = "".join(parts)
            fmt = detect(text, getattr(ctx, "input_filename", ""))
            coverage["format"] = fmt
            sink = EndpointSink()
            for lineno, record_path, row, error in decode(text, fmt):
                if ctx.cancelled():
                    return
                ctx.current_line = lineno
                ctx.current_line_text = json.dumps(row, ensure_ascii=False, default=str) if row is not None else ""
                ctx.record_locator = {"record_path": record_path}
                coverage["records_total"] += 1
                records, decisions = [], []
                if error:
                    ctx.on_bad_line(lineno, error)
                    disposition = "record_error"
                else:
                    try:
                        fields = list(itertools.islice(leaves(row), MAX_FIELDS + 1))
                        if len(fields) > MAX_FIELDS:
                            raise ValueError("单记录字段数超过 2000")
                        for field, value in fields:
                            kind, norm, reason = classify(field, value, configured.get(field, "auto"))
                            decisions.append({"field": field, "kind": kind, "value": norm,
                                              "reason": reason, "mapped": False})
                        urls = [d for d in decisions if d["kind"] == "url"]
                        hosts = [d for d in decisions if d["kind"] in ("domain", "ip")]
                        methods = {d["value"] for d in decisions if d["kind"] == "method"}
                        for d in urls:
                            records.extend(sink.emit(d["value"], ctx, lineno,
                                                     method=next(iter(methods)) if len(methods) == 1 else ""))
                            d["mapped"] = True
                        for d in hosts:
                            records.append(RawEntity(d["kind"], d["value"], d["value"]))
                            d["mapped"] = True
                        # Context attributes attach only to one unambiguous object.
                        target = None
                        if len(urls) == 1:
                            target = ("endpoint", normalize.endpoint_identity(urls[0]["value"])["key"])
                        elif len(hosts) == 1:
                            target = (hosts[0]["kind"], hosts[0]["value"])
                        for d in decisions:
                            if target and d["kind"] in ("port", "status", "method", "technology"):
                                name = {"status": "status_code"}.get(d["kind"], d["kind"])
                                records.append(RawObservation(target, name, d["value"]))
                                d["mapped"] = True
                            if d["kind"] == "ignore":
                                d["mapped"] = True
                            if not d["mapped"]:
                                ctx.on_unknown_field(d["field"], lineno)
                            if d["field"] not in coverage["fields"] and len(coverage["fields"]) < MAX_FIELDS:
                                coverage["fields"][d["field"]] = {
                                    "sample": str(normalize.redact_secrets(d["value"]))[:160],
                                    "suggested": d["kind"] if d["kind"] != "unknown" else "auto"}
                            elif d["field"] not in coverage["fields"]:
                                coverage["field_catalog_truncated"] = True
                            coverage["fields_total"] += 1
                            category = ("fields_ignored" if d["kind"] == "ignore" else
                                        "fields_mapped" if d["mapped"] else "fields_unresolved")
                            coverage[category] += 1
                        mapped = sum(d["mapped"] and d["kind"] != "ignore" for d in decisions)
                        ignored = sum(d["kind"] == "ignore" for d in decisions)
                        if decisions and ignored == len(decisions):
                            disposition = "ignored"
                        elif mapped and all(d["mapped"] for d in decisions):
                            disposition = "mapped_complete"
                        elif mapped:
                            disposition = "mapped_partial"
                        else:
                            disposition = "unrecognized"
                    except (ValueError, TypeError, RecursionError) as exc:
                        records = []
                        error = str(exc)
                        ctx.on_bad_line(lineno, error)
                        disposition = "record_error"
                coverage[disposition] += 1
                if len(coverage["details"]) < DETAIL_LIMIT:
                    coverage["details"].append({"line": lineno, "record_path": record_path,
                        "state": disposition, "error": error,
                        "fields": [{**d, "value": str(normalize.redact_secrets(d["value"]))[:160]}
                                   for d in decisions[:50]], "fields_truncated": len(decisions) > 50})
                else:
                    coverage["details_truncated"] = True
                yield from records
            coverage["complete"] = True

    return GenericParser


def preview(path: Path, mapping: dict[str, str] | None = None) -> dict:
    from .ingest import ParseContext, iter_lines, probe_file, sha256_file
    path = Path(path)
    if not path.is_file():
        raise ValueError("请选择一个可读取的文件")
    if path.stat().st_size > MAX_BYTES:
        raise ValueError("通用预览上限 16 MiB，请拆分文件")
    digest, _ = sha256_file(path)
    probe = probe_file(path)
    if probe.binary:
        raise ValueError(f"不支持的二进制格式：{probe.binary}；未执行导入")
    ctx = ParseContext(project_id=0, source_name="generic")
    ctx.input_filename = path.name
    error = None
    count = 0
    try:
        for _ in parser_for(mapping)().parse((line for _, line in iter_lines(path, probe)), ctx):
            count += 1
    except (ValueError, RecursionError) as exc:
        error = str(exc)
    result = ctx.import_coverage
    result.update({"sha256": digest, "path": str(path), "error": error,
                   "output_records": count, "encoding": probe.encoding})
    # Signatures describe schema paths, never samples or credentials.
    result["structure_signature"] = fingerprint({k: "field" for k in result["fields"]})
    return result


def profiles_path(data_dir: Path, project_id: int) -> Path:
    return Path(data_dir) / "import-profiles" / f"{int(project_id)}.json"


def saved_report(data_dir: Path, job_id: int) -> dict:
    """Read the already-redacted import report written by the existing pipeline."""
    path = Path(data_dir) / "reports" / f"{int(job_id)}.json"
    if not path.is_file():
        raise ValueError("此作业没有可读取的导入报告（旧作业或未启用报告存储）")
    return json.loads(path.read_text(encoding="utf-8"))


def load_profiles(data_dir: Path, project_id: int) -> list:
    path = profiles_path(data_dir, project_id)
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def save_profile(data_dir: Path, project_id: int, name: str, signature: str,
                 mapping: dict[str, str]) -> dict:
    mapping = validate_mapping(mapping)
    if not name.strip() or len(name) > 80 or len(signature) != 16:
        raise ValueError("配置名称或结构签名无效")
    item = {"id": fingerprint({"name": name.strip(), "signature": signature}),
            "name": name.strip(), "structure_signature": signature, "mapping": mapping,
            "version": 1}
    items = load_profiles(data_dir, project_id)
    items = [p for p in items if p["id"] != item["id"]] + [item]
    if len(items) > 100:
        raise ValueError("每个项目最多保存 100 个映射配置")
    path = profiles_path(data_dir, project_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)
    return item
