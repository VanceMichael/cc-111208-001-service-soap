import xml.etree.ElementTree as ET

SOAP = 'http://www.w3.org/2003/05/soap-envelope'
RECEIPT = 'urn:service:receipt:1'


def inspect(payload):
    root = ET.fromstring(payload)
    if root.tag != '{' + SOAP + '}Envelope':
        raise ValueError('Expected SOAP 1.2 Envelope')
    body = root.find('{' + SOAP + '}Body')
    if body is None:
        raise ValueError('Missing SOAP Body')
    if body.find('{' + SOAP + '}Fault') is not None:
        raise ValueError('SOAP Fault response')
    receipts = body.findall('.//{' + RECEIPT + '}Receipt')
    return {'receipts': [{'id': node.get('id'), 'revision': node.get('revision')}
                         for node in receipts]}
