"""SOAP 1.2 回执子集的请求构造与响应校验。"""
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from urllib.parse import unquote

from . import mime
from .mime import ProtocolError

SOAP = 'http://www.w3.org/2003/05/soap-envelope'
RECEIPT = 'urn:service:receipt:1'
XOP = 'http://www.w3.org/2004/08/xop/include'


@dataclass(frozen=True)
class PhotoDecl:
    cid: str
    sha256: str
    length: int


@dataclass(frozen=True)
class ReceiptDecl:
    work_order: str
    revision: int
    photos: tuple


@dataclass(frozen=True)
class BatchDecl:
    batch_id: str
    next_cursor: str
    receipts: tuple
    attachments: dict


def build_query(window, cursor):
    """构造 Query 请求正文；cursor 为空表示窗口首页。"""
    safe_window = _escape(window)
    safe_cursor = _escape(cursor)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<s:Envelope xmlns:s="' + SOAP + '" xmlns:r="' + RECEIPT + '">'
        '<s:Body><r:Query window="' + safe_window + '" cursor="' + safe_cursor + '"/></s:Body>'
        '</s:Envelope>'
    ).encode('utf-8')


def _escape(value):
    return (value.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                 .replace('"', '&quot;').replace("'", '&apos;'))


def parse_message(content_type, body):
    """解析 XML-only 或 multipart/related 响应，返回 :class:`BatchDecl`。"""
    media_type, params = mime.parse_content_type(content_type)
    if media_type == 'multipart/related':
        parts, part_params = mime.parse_multipart(content_type, body)
        attachments = mime.index_content_ids(parts)
        root = mime.select_root(parts, part_params)
        root_type, _ = mime.parse_content_type(root.headers.get('content-type', ''))
        if root_type not in ('application/xop+xml', 'application/soap+xml'):
            raise ProtocolError('Root part is not an XML media type: ' + root_type)
        xml_body = root.content
    elif media_type in ('application/soap+xml', 'text/xml', 'application/xml'):
        attachments = {}
        xml_body = body
    else:
        raise ProtocolError('Unsupported response Content-Type: ' + media_type)
    return parse_envelope(xml_body, attachments)


def parse_envelope(xml_body, attachments):
    try:
        root = ET.fromstring(xml_body)
    except ET.ParseError as error:
        raise ProtocolError('Response is not well-formed XML: ' + str(error))
    if root.tag != '{' + SOAP + '}Envelope':
        raise ProtocolError('Expected SOAP 1.2 Envelope namespace ' + SOAP)
    body = root.find('{' + SOAP + '}Body')
    if body is None:
        raise ProtocolError('Missing SOAP Body')
    if body.find('{' + SOAP + '}Fault') is not None:
        raise ProtocolError('SOAP Fault returned for business request')
    batch = body.find('{' + RECEIPT + '}Batch')
    if batch is None:
        raise ProtocolError('Body has no receipt Batch element')
    batch_id = batch.get('id')
    next_cursor = batch.get('next')
    if not batch_id or next_cursor is None:
        raise ProtocolError('Batch requires id and next attributes')
    receipts = []
    for node in batch.findall('{' + RECEIPT + '}Receipt'):
        receipts.append(_parse_receipt(node, attachments))
    return BatchDecl(batch_id, next_cursor, tuple(receipts), attachments)


def _parse_receipt(node, attachments):
    work_order = node.get('id')
    raw_revision = node.get('revision')
    if not work_order:
        raise ProtocolError('Receipt requires an id attribute')
    try:
        revision = int(raw_revision)
    except (TypeError, ValueError):
        raise ProtocolError('Receipt ' + str(work_order) + ' has non-integer revision')
    if revision < 0:
        raise ProtocolError('Receipt ' + work_order + ' has negative revision')
    photos = []
    for photo_node in node.findall('{' + RECEIPT + '}Photo'):
        include = photo_node.find('{' + XOP + '}Include')
        if include is None or include.tag != '{' + XOP + '}Include':
            raise ProtocolError('Photo on ' + work_order + ' has no xop:Include reference')
        href = include.get('href', '')
        if not href.startswith('cid:'):
            raise ProtocolError('Photo xop:Include must use a cid: href')
        cid = unquote(href[4:])
        digest = photo_node.get('sha256')
        raw_length = photo_node.get('length')
        if not digest or not re_full_sha256(digest):
            raise ProtocolError('Photo on ' + work_order + ' declares an invalid sha256')
        try:
            length = int(raw_length)
        except (TypeError, ValueError):
            raise ProtocolError('Photo on ' + work_order + ' has non-integer length')
        if length < 0:
            raise ProtocolError('Photo on ' + work_order + ' has negative length')
        photos.append(PhotoDecl(cid, digest.lower(), length))
    if not photos:
        raise ProtocolError('Receipt ' + work_order + ' declares no photos')
    return ReceiptDecl(work_order, revision, tuple(photos))


def re_full_sha256(value):
    if len(value) != 64:
        return False
    return all(char in '0123456789abcdefABCDEF' for char in value)
