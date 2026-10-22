"""Stage bounded text as native input. Never writes application facts."""
from __future__ import annotations

import hashlib
import json
import re
import secrets

from ..core import normalize

MAX_BYTES = 16 * 1024 * 1024


def stage(data_dir, *, text, name='pasted.txt', resource_url=None):
    encoded = text.encode('utf-8')
    if not encoded or len(encoded) > MAX_BYTES:
        raise ValueError('输入须为 1 字节至 16 MiB 的 UTF-8 文本')
    # Both Windows and POSIX separators are rejected rather than interpreted.
    if (not name or len(name) > 200 or re.search(r'[\\/:*?<>|\x00-\x1f]',name)
            or name.rstrip(' .') != name or name in ('.','..')
            or re.fullmatch(r'(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?',name,re.I)):
        raise ValueError('文件名无效，请使用不含路径的普通文件名')
    if resource_url is not None:
        normalize.endpoint_identity(resource_url, method='')
        doc = {'magpie_js':1,'url':resource_url,'content':text,
               'source_name':name,'source_sha256':hashlib.sha256(encoded).hexdigest()}
        payload = json.dumps(doc,ensure_ascii=False,separators=(',',':')).encode('utf-8')
        output_name = 'local-js.manifest.json'
    else:
        payload, output_name = encoded, name
    if len(payload) > MAX_BYTES:
        raise ValueError('生成的 JS manifest 超过 16 MiB；请拆分输入内容')
    batch = data_dir / 'uploads' / ('text-' + secrets.token_hex(12))
    batch.mkdir(parents=True)
    target = batch / output_name
    target.write_bytes(payload)
    return {'path':str(target.resolve()),'name':name,'bytes':len(encoded),
            'sha256':hashlib.sha256(payload).hexdigest(),'source_sha256':hashlib.sha256(encoded).hexdigest()}
