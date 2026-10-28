#!/usr/bin/env python3
"""Ask every Telegram DC for resPQ through an MTProto proxy, the way a real client starts.
usage: tg-check.py HOST PORT SECRET_HEX    (exit 1 if any DC did not answer)
Needs the 'cryptography' module (the tg-ws-proxy venv has it)."""
import hashlib, os, socket, struct, sys, time
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

host, port, secret = sys.argv[1], int(sys.argv[2]), bytes.fromhex(sys.argv[3][-32:])


def ctr(key, iv):
    return Cipher(algorithms.AES(key), modes.CTR(iv)).encryptor()


def recv(s, n):
    b = b''
    while len(b) < n:
        if not (c := s.recv(n - len(b))):
            raise ConnectionError('closed')
        b += c
    return b


def check(dc):
    while True:                                            # obfuscated2 init, padded intermediate ("dd")
        init = bytearray(os.urandom(64))
        if init[0] != 0xef and init[4:8] != bytes(4) and bytes(init[:4]) not in (
                b'HEAD', b'POST', b'GET ', b'OPTI', b'\x16\x03\x01\x02', b'\xdd' * 4, b'\xee' * 4):
            break
    init[56:60] = b'\xdd' * 4
    init[60:62] = struct.pack('<h', dc)
    rev = bytes(init[8:56])[::-1]
    enc = ctr(hashlib.sha256(init[8:40] + secret).digest(), bytes(init[40:56]))
    dec = ctr(hashlib.sha256(rev[:32] + secret).digest(), rev[32:48])
    head = bytes(init[:56]) + enc.update(bytes(init))[56:]

    nonce = os.urandom(16)
    body = struct.pack('<I', 0xbe7e8ef1) + nonce               # req_pq_multi
    msg = bytes(8) + struct.pack('<qi', int(time.time() * 2**32) & ~3, len(body)) + body
    with socket.create_connection((host, port), timeout=15) as s:
        s.sendall(head + enc.update(struct.pack('<i', len(msg)) + msg))
        n = struct.unpack('<i', dec.update(recv(s, 4)))[0]
        resp = dec.update(recv(s, n))
    if resp[20:24] != struct.pack('<I', 0x05162463) or resp[24:40] != nonce:
        raise ValueError('not a resPQ answer')


failed = 0
for dc in (1, 2, 3, 4, 5):
    t = time.time()
    for attempt in (1, 2, 3):                              # the ISP drops some new connections at random
        try:
            check(dc)
            print('    DC%d: OK (%.1fs%s)' % (dc, time.time() - t, ', try %d' % attempt if attempt > 1 else ''))
            break
        except Exception as e:
            err = e
    else:
        failed += 1
        print('    DC%d: FAIL %r' % (dc, err))
sys.exit(1 if failed else 0)
