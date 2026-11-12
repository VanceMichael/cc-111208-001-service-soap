"""multipart/related 解析（仅覆盖回执协议子集）。

分段只承认完整的边界分隔行（``--boundary`` 独占一行），因此二进制附件中
出现看似边界的字节不会误切分。头部参数支持引号，根部由 Content-Type 的
``start`` 参数指定，与片段排列顺序无关。
"""
from dataclasses import dataclass


class ProtocolError(ValueError):
    """报文违反协议子集（错误分段、重复标识、根部缺失等）。"""


@dataclass(frozen=True)
class MimePart:
    headers: dict
    content: bytes

    @property
    def content_id(self):
        value = self.headers.get('content-id', '').strip()
        if value.startswith('<') and value.endswith('>'):
            value = value[1:-1]
        return value.strip()


def parse_content_type(header):
    """返回 (小写媒体类型, 参数名小写的字典)。"""
    if not header:
        raise ProtocolError('Missing Content-Type header')
    tokens = _split_parameters(header)
    media_type = tokens[0].strip().lower()
    params = {}
    for name, value in tokens[1:]:
        params[name.strip().lower()] = value.strip()
    return media_type, params


def _split_parameters(header):
    pieces = []
    current = []
    quoted = False
    for char in header:
        if char == '"':
            quoted = not quoted
            current.append(char)
        elif char == ';' and not quoted:
            pieces.append(''.join(current))
            current = []
        else:
            current.append(char)
    pieces.append(''.join(current))
    result = [pieces[0]]
    for piece in pieces[1:]:
        if '=' not in piece:
            continue
        name, value = piece.split('=', 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
            value = value[1:-1]
        result.append((name, value))
    return result


def _iter_delimiters(body, delimiter):
    """生成 (起点, 分隔符之后的位置, 是否闭合分隔)。"""
    index = 0
    width = len(delimiter)
    total = len(body)
    while index <= total:
        position = body.find(delimiter, index)
        if position < 0:
            return
        at_line_start = position == 0 or body[position - 2:position] == b'\r\n'
        following = body[position + width:position + width + 2]
        if at_line_start and following in (b'\r\n', b'--'):
            closing = following == b'--'
            yield position, position + width + 2, closing
            index = position + width + 2
        else:
            index = position + 1


def parse_multipart(content_type, body):
    """把 multipart/related 报文解析为 :class:`MimePart` 列表。"""
    media_type, params = parse_content_type(content_type)
    if media_type != 'multipart/related':
        raise ProtocolError('Expected multipart/related Content-Type')
    boundary = params.get('boundary')
    if not boundary:
        raise ProtocolError('multipart/related response lacks boundary parameter')
    delimiter = b'--' + boundary.encode('ascii')
    marks = list(_iter_delimiters(body, delimiter))
    if not marks:
        raise ProtocolError('multipart body has no boundary delimiter')
    if marks[0][0] != 0 and body[0:marks[0][0]].strip():
        raise ProtocolError('multipart body has unexpected preamble before first boundary')
    if not any(closing for _, _, closing in marks):
        raise ProtocolError('multipart body is missing the closing boundary')
    parts = []
    for ordinal in range(len(marks) - 1):
        start, end, closing = marks[ordinal]
        if closing:
            break
        next_start = marks[ordinal + 1][0]
        # 每个片段正文前跳过分隔行后的 CRLF，结尾去掉下一分隔行前的 CRLF。
        segment = body[end:next_start - 2]
        if b'\r\n\r\n' not in segment:
            raise ProtocolError('multipart part is missing header/body separator')
        raw_headers, content = segment.split(b'\r\n\r\n', 1)
        parts.append(MimePart(_parse_headers(raw_headers), content))
    if not parts:
        raise ProtocolError('multipart body contains no parts')
    if not params.get('start', '').strip():
        raise ProtocolError('multipart/related response lacks start root reference')
    return parts, params


def _parse_headers(raw_headers):
    headers = {}
    for line in raw_headers.split(b'\r\n'):
        if not line or b':' not in line:
            continue
        name, value = line.split(b':', 1)
        headers[name.decode('latin-1').strip().lower()] = value.decode('latin-1').strip()
    return headers


def index_content_ids(parts):
    """按去尖括号的 Content-ID 建索引，重复标识直接拒绝。"""
    indexed = {}
    for part in parts:
        cid = part.content_id
        if not cid:
            raise ProtocolError('multipart part is missing Content-ID')
        if cid in indexed:
            raise ProtocolError('Duplicate Content-ID in multipart response: ' + cid)
        indexed[cid] = part
    return indexed


def select_root(parts, params):
    start = params.get('start', '').strip()
    if start.startswith('<') and start.endswith('>'):
        start = start[1:-1]
    if not start:
        raise ProtocolError('multipart/related response lacks start root reference')
    for part in parts:
        if part.content_id == start:
            return part
    raise ProtocolError('Root part ' + start + ' declared by start is absent')
