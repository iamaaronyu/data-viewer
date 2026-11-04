"""TFDS RLDS Example reader. Keeps original feature names and action units."""
from __future__ import annotations
import struct
from pathlib import Path


def varint(b, p):
    value = 0
    for shift in range(0, 70, 7):
        if p >= len(b):
            raise ValueError('Truncated protobuf varint')
        x = b[p]; p += 1; value |= (x & 127) << shift
        if x < 128:
            return value, p
    raise ValueError('Invalid protobuf varint')


def fields(b):
    p = 0
    while p < len(b):
        tag, p = varint(b, p); number, wire = tag >> 3, tag & 7
        if number == 0:
            raise ValueError('Invalid protobuf field')
        if wire == 2:
            n, p = varint(b, p); end = p + n
            if end > len(b):
                raise ValueError('Truncated protobuf field')
            value = b[p:end]; p = end
        elif wire == 0:
            value, p = varint(b, p)
        elif wire in (1, 5):
            n = 8 if wire == 1 else 4; value = b[p:p+n]; p += n
            if len(value) != n:
                raise ValueError('Truncated fixed protobuf field')
        else:
            raise ValueError(f'Unsupported protobuf wire type {wire}')
        yield number, wire, value


def example(data):
    result = {}
    for n, _, features in fields(data):
        if n != 1:
            continue
        for _, _, entry in fields(features):
            key = None; feature = None
            for en, _, value in fields(entry):
                if en == 1: key = value.decode('utf8')
                if en == 2: feature = value
            if key is None or feature is None:
                raise ValueError('Invalid Example feature entry')
            values = []
            for kind, _, value_list in fields(feature):
                for vn, wire, value in fields(value_list):
                    if vn != 1: continue
                    if kind == 1: values.append(value)
                    elif kind == 2:
                        values.extend(struct.unpack('<' + 'f' * (len(value)//4), value))
                    elif kind == 3:
                        ints = []
                        if wire == 0: ints = [value]
                        else:
                            q = 0
                            while q < len(value):
                                x, q = varint(value, q); ints.append(x)
                        values.extend(x if x < 2**63 else x-2**64 for x in ints)
                    else: raise ValueError('Unknown Example feature kind')
            result[key] = values
    return result


def masked_crc(data):
    import google_crc32c
    crc = google_crc32c.value(data)
    return (((crc >> 15) | (crc << 17)) + 0xa282ead8) & 0xffffffff


def record_offsets(path):
    offsets = []; size = Path(path).stat().st_size
    with open(path, 'rb') as f:
        while f.tell() < size:
            offset = f.tell(); header = f.read(12)
            if len(header) != 12: raise ValueError('Truncated TFRecord header')
            length, crc = struct.unpack('<QI', header)
            if masked_crc(header[:8]) != crc: raise ValueError('TFRecord length CRC mismatch')
            if length > 512*1024*1024 or f.tell()+length+4 > size:
                raise ValueError('Invalid or truncated TFRecord payload')
            offsets.append(offset); f.seek(length+4, 1)
    return offsets


def read_record(path, offset):
    with open(path, 'rb') as f:
        f.seek(offset); header = f.read(12)
        length, crc = struct.unpack('<QI', header)
        if masked_crc(header[:8]) != crc: raise ValueError('TFRecord length CRC mismatch')
        if length > 512*1024*1024: raise ValueError('TFRecord too large')
        payload = f.read(length); tail = f.read(4)
        if len(payload) != length or len(tail) != 4 or masked_crc(payload) != struct.unpack('<I', tail)[0]:
            raise ValueError('TFRecord payload CRC mismatch')
    return example(payload)
