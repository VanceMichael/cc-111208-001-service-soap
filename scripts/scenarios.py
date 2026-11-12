"""本地合成 SOAP/MTOM 响应构造器（全部为合成数据，无真实客户资料）。"""
import base64
import hashlib


def photo_decl(content, cid):
    return {'cid': cid, 'sha256': hashlib.sha256(content).hexdigest(),
            'length': len(content), 'content': content}


def xml_only_batch(batch_id, next_cursor, receipts):
    """receipts: {work_order: {'revision': int, 'photos': [photo_decl...]}}。

    XML-only 响应天然不带附件，因此声明的照片即为缺件，模拟“附件未收到”。
    """
    envelope = ['<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"',
                ' xmlns:r="urn:service:receipt:1"',
                ' xmlns:xop="http://www.w3.org/2004/08/xop/include"><s:Body>',
                '<r:Batch id="%s" next="%s">' % (batch_id, next_cursor)]
    for work_order, receipt in receipts.items():
        envelope.append('<r:Receipt id="%s" revision="%d">'
                        % (work_order, receipt['revision']))
        for photo in receipt['photos']:
            envelope.append(
                '<r:Photo sha256="%(sha256)s" length="%(length)d">'
                '<xop:Include href="cid:%(href)s"/></r:Photo>'
                % {'sha256': photo['sha256'], 'length': photo['length'],
                   'href': _escaped_cid(photo['cid'])})
        envelope.append('</r:Receipt>')
    envelope.append('</r:Batch></s:Body></s:Envelope>')
    return ''.join(envelope).encode('utf-8')


def fault_body():
    return ('<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
            '<s:Body><s:Fault><s:Code><s:Value>s:Receiver</s:Value></s:Code>'
            '</s:Fault></s:Body></s:Envelope>').encode('utf-8')


def _escaped_cid(cid):
    from urllib.parse import quote
    return quote(cid, safe='')


def build_multipart(boundary, root_cid, xml_body, attachments, *, root_first=False,
                    root_type='application/xop+xml; type="application/soap+xml"',
                    duplicate_cid=False):
    """attachments: 有序 [(cid, content_type, bytes)]，根部位置由 root_first 决定。"""
    root_part = ('Content-Type: %s\r\nContent-ID: <%s>\r\n\r\n'
                 % (root_type, root_cid)).encode('latin-1') + xml_body
    chunks = []

    def emit(part):
        chunks.append(b'--' + boundary.encode('ascii') + b'\r\n' + part + b'\r\n')

    ordered = list(attachments)
    if not root_first:
        for cid, content_type, content in ordered:
            emit(_attachment_part(cid, content_type, content))
        if duplicate_cid and ordered:
            cid, content_type, content = ordered[0]
            emit(_attachment_part(cid, content_type, content + b'-duplicate'))
    emit(root_part)
    if root_first:
        for cid, content_type, content in ordered:
            emit(_attachment_part(cid, content_type, content))
    chunks.append(b'--' + boundary.encode('ascii') + b'--\r\n')
    return b''.join(chunks)


def _attachment_part(cid, content_type, content):
    return ('Content-Type: %s\r\nContent-ID: <%s>\r\n\r\n'
            % (content_type, cid)).encode('latin-1') + content


def to_base64(payload):
    return base64.b64encode(payload).decode('ascii')


def wrong_namespace_body():
    return ('<Envelope xmlns="http://schemas.xmlsoap.org/soap/envelope/">'
            '<Body/></Envelope>').encode('utf-8')
