"""Read-only API research predicates over existing effective observations."""
from __future__ import annotations

import json
import re

from . import query

LOCATIONS = ('query', 'body', 'path', 'header', 'cookie', 'unknown')
CREDENTIALS = ('cookie', 'bearer', 'basic', 'api_key', 'other', 'unknown',
               'no_recognized_credentials_observed')
ORIGINS = ('http_sample', 'js_only', 'declaration_only')


def sample_value(value):
    """Unknown formats stay in the original store, outside these predicates."""
    if not isinstance(value, str) or len(value) > 8000:
        return None
    try:
        result = json.loads(value)
    except (ValueError, TypeError, RecursionError):
        return None
    if (not isinstance(result, dict) or type(result.get('version')) is not int
            or result['version'] != 1 or any(not isinstance(result.get(field), str)
                                            or not result[field] for field in ('url', 'method', 'record_path'))):
        return None
    return result


def _names(sample, field):
    value = sample.get(field)
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def _sample_matches(value, name, location, status, mime, credential, method=''):
    sample = sample_value(value)
    if sample is None:
        return 0
    if method and sample['method'].upper() != method.upper():
        return 0
    if status is not None and (type(sample.get('status')) is not int or sample['status'] != status):
        return 0
    observed_mime = sample.get('response_mime')
    if mime and (not isinstance(observed_mime, str)
                 or observed_mime.split(';', 1)[0].strip().lower() != mime):
        return 0
    if credential and credential not in _names(sample, 'credentials'):
        return 0
    if name or location:
        fields = {'query': 'query_parameter_names', 'body': 'body_parameter_names',
                  'header': 'request_header_names'}
        names = [n for loc, field in fields.items() if not location or loc == location
                 for n in _names(sample, field)]
        if not (name in names if name else bool(names)):
            return 0
    return 1


def _parameter_matches(field, value, name, location):
    if field == 'js_static_param':
        return int((not location or location == 'query') and (not name or value == name))
    parts = field.split('.', 2)
    if len(parts) != 3 or parts[0] != 'param' or parts[1] not in LOCATIONS:
        return 0
    # Type/required/description attributes are not parameter-presence records.
    return int(value == parts[1] and (not location or location == parts[1])
               and (not name or name == parts[2]))


def _exists(predicate):
    return ("EXISTS (SELECT 1 FROM observations ao WHERE ao.project_id=e.project_id"
            " AND ao.entity_id=e.id AND ao.is_current=1 AND " + predicate + ")")


def filters(conn, category, *, parameter_name='', parameter_location='',
            response_status=None, response_mime='', credential='', api_origin='', method=''):
    """Return SQL and bound parameters shared by grouping, totals and pages."""
    if (len(parameter_name) > 120 or len(response_mime) > 120
            or parameter_location not in ('', *LOCATIONS)
            or credential not in ('', *CREDENTIALS) or api_origin not in ('', *ORIGINS)
            or (response_status is not None and (type(response_status) is not int
                                                or not 100 <= response_status <= 599))):
        raise query.QueryError('E_UNSUPPORTED_FILTER', 'API 研究筛选值无效或过长')
    mime = response_mime.strip().lower()
    if mime and not re.fullmatch(r"[a-z0-9!#$%&'*+.^_`|~\-]+/[a-z0-9!#$%&'*+.^_`|~\-]+", mime):
        raise query.QueryError('E_UNSUPPORTED_FILTER', '请输入完整 MIME 类型，例如 application/json（不含 charset）')
    active = bool(parameter_name or parameter_location or response_status is not None
                  or mime or credential or api_origin)
    if active and category != 'api':
        raise query.QueryError('E_UNSUPPORTED_FILTER', '研究筛选仅适用于 API 工作区')
    if not active:
        return [], []
    conn.create_function('api_valid_sample', 1, lambda v: int(sample_value(v) is not None), deterministic=True)
    conn.create_function('api_sample_matches', 7, _sample_matches, deterministic=True)
    conn.create_function('api_parameter_matches', 4, _parameter_matches, deterministic=True)
    http = _exists("ao.semantic_field='http_request_sample' AND api_valid_sample(ao.value)=1")
    # An unrecognized sample is not proof that no HTTP sample was imported.
    any_http = _exists("ao.semantic_field='http_request_sample'")
    js = _exists("ao.semantic_field IN ('js_source_ref','js_static_method')")
    declared = "(e.kind='apischema' OR " + _exists("ao.semantic_field='declared_method'") + ")"
    where, args = [], []
    if api_origin == 'http_sample':
        where.append(http)
    elif api_origin == 'js_only':
        where.extend([js, 'NOT ' + any_http])
    elif api_origin == 'declaration_only':
        where.extend([declared, 'NOT ' + any_http, 'NOT ' + js])
    sample = _exists("ao.semantic_field='http_request_sample'"
                     " AND api_sample_matches(ao.value,?,?,?,?,?,?)=1")
    if response_status is not None or mime or credential or api_origin == 'http_sample':
        where.append(sample)
        args.extend([parameter_name, parameter_location, response_status, mime, credential, method])
    elif parameter_name or parameter_location:
        parameter = _exists("api_parameter_matches(ao.semantic_field,ao.value,?,?)=1")
        where.append('(' + parameter + ' OR ' + sample + ')')
        args.extend([parameter_name, parameter_location, parameter_name, parameter_location, None, '', '', method])
    return where, args
