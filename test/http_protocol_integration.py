#!/usr/bin/env python3
"""HTTP framing, TLS and HTTP/2 trailer/duplex regressions. Requires h2>=3.2.

Run: python3 test/http_protocol_integration.py /path/to/build/kangle
Uses isolated configuration and loopback ports. Failures preserve their logs.
"""
import http.client
import os
import shutil
import signal
import socket
import socketserver
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import h2.config
import h2.connection
import h2.events


def port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    def handle_error(self, request, address):
        pass  # Deliberately disconnected peers.


def exact(f, n):
    data = b''
    while len(data) < n:
        chunk = f.read(n-len(data))
        if not chunk:
            raise EOFError()
        data += chunk
    return data


def fields(f):
    result = {}
    while True:
        line = f.readline()
        if line == b'\r\n':
            return result
        if not line:
            raise EOFError()
        key, value = line.rstrip(b'\r\n').split(b':', 1)
        result[key.lower()] = value.strip()


def chunks(f):
    body = b''
    while True:
        n = int(f.readline().split(b';', 1)[0], 16)
        if n == 0:
            return body, fields(f)
        body += exact(f, n)
        assert exact(f, 2) == b'\r\n'


class HttpOrigin(socketserver.StreamRequestHandler):
    def handle(self):
        self.request.settimeout(8)
        while True:
            first = self.rfile.readline()
            if not first:
                return
            _, path, _ = first.split(b' ', 2)
            headers = fields(self.rfile)
            trailers = {}
            if headers.get(b'transfer-encoding') == b'chunked':
                body, trailers = chunks(self.rfile)
            else:
                body = exact(self.rfile, int(headers.get(b'content-length', b'0')))
            if path == b'/bad-length':
                self.wfile.write(b'HTTP/1.1 200 OK\r\nContent-Length: 1junk\r\n\r\nx')
                return
            if path == b'/bad-name':
                self.wfile.write(b'HTTP/1.1 200 OK\r\nContent-Length : 1\r\n\r\nx')
                return
            if path in (b'/trailers', b'/empty', b'/upload'):
                payload = body if path == b'/upload' else (b'abc' if path == b'/trailers' else b'')
                self.wfile.write(b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nTrailer: X-Meta\r\n\r\n')
                if payload:
                    self.wfile.write(('%x\r\n' % len(payload)).encode()+payload+b'\r\n')
                self.wfile.write(b'0\r\nX-Meta: '+trailers.get(b'x-upload', b'ok')+b'\r\n\r\n')
            else:
                self.wfile.write(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok')
            self.wfile.flush()


class H2Origin(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(10)
        config = h2.config.H2Configuration(client_side=False, header_encoding='utf-8', validate_outbound_headers=False)
        cn = h2.connection.H2Connection(config=config)
        cn.initiate_connection()
        self.request.sendall(cn.data_to_send())
        requests, started = {}, set()
        while True:
            data = self.request.recv(65536)
            if not data:
                return
            resets = b''
            for event in cn.receive_data(data):
                if isinstance(event, h2.events.RequestReceived):
                    requests[event.stream_id] = dict(event.headers)
                elif isinstance(event, h2.events.TrailersReceived):
                    requests[event.stream_id].update(event.headers)
                elif isinstance(event, h2.events.DataReceived):
                    sid = event.stream_id
                    cn.acknowledge_received_data(event.flow_controlled_length, sid)
                    if requests[sid][':path'] == '/duplex':
                        if sid not in started:
                            cn.send_headers(sid, [(':status', '200'), ('content-type', 'application/grpc')])
                            started.add(sid)
                        cn.send_data(sid, event.data)
                elif isinstance(event, h2.events.StreamReset):
                    with self.server.lock:
                        self.server.resets += 1
                elif isinstance(event, h2.events.StreamEnded):
                    sid = event.stream_id
                    path = requests[sid][':path']
                    if path == '/duplex':
                        cn.send_headers(sid, [('grpc-status', '0'), ('grpc-message', 'done%20ok')], end_stream=True)
                    elif path == '/trailers-only':
                        cn.send_headers(sid, [(':status', '200'), ('content-type', 'application/grpc'), ('grpc-status', '7'), ('grpc-message', 'denied')], end_stream=True)
                    elif path == '/bad-trailer':
                        cn.send_headers(sid, [(':status', '200')])
                        cn.send_headers(sid, [('host', 'evil')], end_stream=True)
                    elif path == '/bad-final':
                        cn.send_headers(sid, [(':status', '200'), ('content-length', '2')])
                        cn.send_data(sid, b'x', end_stream=True)
                    else:
                        payload = b'' if path in ('/zero', '/empty') else b'abc'
                        content_type = 'text/plain' if path == '/generic' else 'application/grpc'
                        cn.send_headers(sid, [(':status', '200'), ('content-type', content_type), ('content-length', str(len(payload)))])
                        if payload:
                            cn.send_data(sid, payload)
                        cn.send_headers(sid, [('grpc-status', '0'), ('x-final', requests[sid].get('x-upload', 'yes'))], end_stream=True)
                        if path == '/no-error-reset':
                            resets += b'\x00\x00\x04\x03\x00'+sid.to_bytes(4, 'big')+b'\x00\x00\x00\x00'
            outgoing = cn.data_to_send()
            if outgoing:
                self.request.sendall(outgoing+resets)


class H2Client:
    def __init__(self, frontend):
        self.sock = socket.create_connection(('127.0.0.1', frontend), timeout=5)
        self.cn = h2.connection.H2Connection(config=h2.config.H2Configuration(client_side=True, header_encoding='utf-8', validate_outbound_headers=False, normalize_outbound_headers=False))
        self.cn.initiate_connection()
        self.flush()
        self.headers, self.trailers, self.body, self.ended, self.reset = {}, {}, {}, set(), set()

    def flush(self):
        data = self.cn.data_to_send()
        if data:
            self.sock.sendall(data)

    def request(self, path, end=True, extra=(), headers=None):
        sid = self.cn.get_next_available_stream_id()
        values = headers if headers is not None else [(':method', 'POST'), (':scheme', 'http'), (':authority', 'localhost'), (':path', path), ('content-type', 'application/grpc'), ('te', 'trailers')]+list(extra)
        self.cn.send_headers(sid, values, end_stream=end)
        self.flush()
        return sid

    def receive(self, predicate):
        deadline = time.time()+6
        while not predicate():
            self.sock.settimeout(max(0.1, deadline-time.time()))
            data = self.sock.recv(65536)
            if not data:
                raise EOFError('HTTP/2 peer closed')
            for event in self.cn.receive_data(data):
                sid = getattr(event, 'stream_id', 0)
                if isinstance(event, h2.events.ResponseReceived):
                    self.headers[sid] = dict(event.headers)
                elif isinstance(event, h2.events.TrailersReceived):
                    self.trailers[sid] = dict(event.headers)
                elif isinstance(event, h2.events.DataReceived):
                    self.body[sid] = self.body.get(sid, b'')+event.data
                    self.cn.acknowledge_received_data(event.flow_controlled_length, sid)
                elif isinstance(event, h2.events.StreamEnded):
                    self.ended.add(sid)
                elif isinstance(event, h2.events.StreamReset):
                    self.reset.add(sid)
            self.flush()

    def done(self, sid):
        self.receive(lambda: sid in self.ended or sid in self.reset)


def run(binary):
    root = tempfile.mkdtemp(prefix='kangle-protocol-')
    servers, proc = [], None
    count = 0
    def check(ok, label):
        nonlocal count
        if not ok:
            raise AssertionError(label)
        count += 1
        print('PASS: '+label, flush=True)
    try:
        for name in ['etc', 'ext', 'bin', 'var', 'tmp', 'log', 'www']:
            os.makedirs(root+'/'+name)
        shutil.copy2(binary, root+'/bin/kangle')
        for handler in [HttpOrigin, H2Origin]:
            s = Server(('127.0.0.1', 0), handler)
            s.lock, s.resets = threading.Lock(), 0
            servers.append(s)
            threading.Thread(target=s.serve_forever, daemon=True).start()
        http_origin, h2_origin = servers
        frontend, tlsport, legacyport = port(), port(), port()
        subprocess.check_call(['openssl', 'req', '-x509', '-nodes', '-newkey', 'rsa:2048', '-keyout', root+'/etc/key.pem', '-out', root+'/etc/cert.pem', '-days', '2', '-subj', '/CN=localhost'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cfg = '''<config><worker_thread>2</worker_thread><timeout rw='8' connect='2'/>
<listen ip='127.0.0.1' port='{front}h' type='http'/>
<listen ip='127.0.0.1' port='{tls}' type='https' certificate='etc/cert.pem' certificate_key='etc/key.pem' http2='1'/>
<listen ip='127.0.0.1' port='{legacy}' type='https' certificate='etc/cert.pem' certificate_key='etc/key.pem' protocols='TLSv1 TLSv1.1 TLSv1.2 TLSv1.3'/>
<cache default='0' memory='16M' disk='0'/><request action='allow'/><response action='allow'/>
<server name='http' host='127.0.0.1' port='{http}' proto='http' life_time='30'/>
<server name='h2' host='127.0.0.1' port='{h2}h' proto='http' life_time='30'/>
<vh name='test' doc_root='www' inherit='on'><host>localhost</host>
<map path='/h1' extend='server:http' confirm_file='0' allow_method='*' replace='/h1/' replace_value='/'/>
<map path='/' extend='server:h2' confirm_file='0' allow_method='*'/></vh></config>'''.format(front=frontend,tls=tlsport,legacy=legacyport,http=http_origin.server_address[1],h2=h2_origin.server_address[1])
        # Match the existing map syntax: separate virtual host for HTTP/1 origin.
        cfg = cfg.replace("<map path='/h1' extend='server:http' confirm_file='0' allow_method='*' replace='/h1/' replace_value='/'/>", '')
        cfg = cfg.replace('</config>', "<vh name='http' doc_root='www' inherit='on'><host>http.test</host><map path='/' extend='server:http' confirm_file='0' allow_method='*'/></vh></config>")
        with open(root+'/etc/config.xml', 'w') as f:
            f.write(cfg)
        log = open(root+'/console.log', 'wb')
        proc = subprocess.Popen([root+'/bin/kangle', '-n', '-g'], stdout=log, stderr=log)
        for _ in range(100):
            try:
                socket.create_connection(('127.0.0.1', frontend), timeout=.1).close()
                break
            except OSError:
                time.sleep(.05)
        else:
            raise AssertionError('startup failed: '+root)

        def raw(request):
            s = socket.create_connection(('127.0.0.1', frontend), timeout=5)
            s.sendall(request)
            f = s.makefile('rb')
            first = f.readline()
            if not first:
                s.close()
                return 0, {}, b'', {}
            status = int(first.split()[1])
            headers = fields(f)
            if headers.get(b'transfer-encoding') == b'chunked':
                body, trailer = chunks(f)
            else:
                body, trailer = exact(f,int(headers.get(b'content-length',b'0'))), {}
            s.close()
            return status, headers, body, trailer

        for path, body in [('trailers', b'abc'), ('empty', b'')]:
            result = raw(('GET /'+path+' HTTP/1.1\r\nHost: http.test\r\nConnection: close\r\n\r\n').encode())
            check(result[0]==200 and result[2]==body and result[3].get(b'x-meta')==b'ok', 'HTTP/1 response '+path+' and trailer order')
        result = raw(b'POST /upload HTTP/1.1\r\nHost: http.test\r\nTransfer-Encoding: chunked\r\nTrailer: X-Upload\r\nConnection: close\r\n\r\n3\r\nabc\r\n0\r\nX-Upload: request-ok\r\n\r\n')
        check(result[2]==b'abc' and result[3].get(b'x-meta')==b'request-ok', 'HTTP/1 request and response trailers')
        for bad in [b'Host: http.test\r\nHost: http.test', b'Host: x:65536', b'Host: http.test\r\nContent-Length: 1\r\nTransfer-Encoding: chunked', b'Host: http.test\r\nTransfer-Encoding: chunked\r\nTransfer-Encoding: chunked', b'Host: http.test\r\nX-Bad: a\x00b', b'Host: http.test\r\nContent-Length : 0', b'Host: http.test\r\nContent-Length: -1']:
            try:
                result = raw(b'POST / HTTP/1.1\r\n'+bad+b'\r\nConnection: close\r\n\r\n')
                check(result[0] != 200, 'HTTP/1 malformed header rejected: '+repr(bad))
            except (OSError, EOFError):
                check(True, 'HTTP/1 malformed header connection closed')
        for path in ['bad-length', 'bad-name']:
            result = raw(('GET /'+path+' HTTP/1.1\r\nHost: http.test\r\nConnection: close\r\n\r\n').encode())
            check(result[0]==504, 'malformed upstream '+path+' rejected')
        result = raw(b'GET /generic HTTP/1.1\r\nHost: localhost\r\nTE: trailers\r\nConnection: close\r\n\r\n')
        check(result[0] == 200 and result[2] == b'abc' and result[3].get(b'x-final') == b'yes',
            'HTTP/2 known length response trailers converted for HTTP/1 TE: trailers client')

        cn = H2Client(frontend)
        for authority in ['localhost', 'http.test']:
            sid = cn.request('/upload', end=False, headers=[(':method', 'POST'), (':scheme', 'http'),
                (':authority', authority), (':path', '/upload'), ('content-type', 'application/grpc'), ('content-length', '3')])
            cn.cn.send_data(sid, b'abc')
            cn.cn.send_headers(sid, [('x-upload', 'native-request')], end_stream=True)
            cn.flush()
            cn.done(sid)
            trailer_key = 'x-final' if authority == 'localhost' else 'x-meta'
            check(sid not in cn.reset and cn.body.get(sid) == b'abc' and cn.trailers.get(sid, {}).get(trailer_key) == 'native-request',
                'HTTP/2 Content-Length request trailers forwarded to '+authority)
            sid = cn.request('/empty', end=False, headers=[(':method', 'POST'), (':scheme', 'http'),
                (':authority', authority), (':path', '/empty'), ('content-type', 'text/plain'), ('content-length', '0')])
            time.sleep(.05)
            cn.cn.send_headers(sid, [('x-upload', 'empty-request')], end_stream=True)
            cn.flush()
            cn.done(sid)
            check(sid not in cn.reset and not cn.body.get(sid) and cn.trailers.get(sid, {}).get(trailer_key) == 'empty-request',
                'HTTP/2 zero length request waits for trailers forwarded to '+authority)
        for path in ['/known', '/zero', '/trailers-only', '/no-error-reset']:
            sid = cn.request(path)
            cn.done(sid)
            check(sid not in cn.reset and cn.headers[sid][':status']=='200', 'HTTP/2 '+path+' completed')
            if path=='/trailers-only':
                check(cn.trailers.get(sid,cn.headers[sid]).get('grpc-status')=='7', 'gRPC trailers-only status preserved')
            else:
                check(cn.trailers[sid]['grpc-status']=='0' and cn.trailers[sid]['x-final']=='yes', 'HTTP/2 known length trailers preserved '+path)
        sid = cn.request('/duplex', end=False)
        for n in range(12):
            payload = bytes([n])*8192
            cn.cn.send_data(sid, payload)
            cn.flush()
            cn.receive(lambda: len(cn.body.get(sid,b'')) >= (n+1)*8192)
        check(sid not in cn.ended and len(cn.body[sid])==12*8192, 'duplex response arrives before request EOF across flow-control windows')
        cn.cn.end_stream(sid)
        cn.flush()
        cn.done(sid)
        check(cn.trailers[sid]['grpc-status']=='0' and cn.trailers[sid]['grpc-message']=='done%20ok', 'duplex completion metadata preserved')
        sids = [cn.request('/known') for _ in range(12)]
        for sid in sids:
            cn.done(sid)
        check(all(cn.body.get(sid)==b'abc' and sid in cn.trailers for sid in sids), 'multiplexed streams and upstream connection reuse')
        sid = cn.request('/duplex', end=False)
        cn.cn.send_data(sid,b'cancel')
        cn.flush()
        cn.receive(lambda: cn.body.get(sid)==b'cancel')
        cn.cn.reset_stream(sid)
        cn.flush()
        time.sleep(.2)
        sid = cn.request('/known')
        cn.done(sid)
        check(cn.body.get(sid)==b'abc' and h2_origin.resets>0, 'client cancellation resets upstream stream and preserves connection')
        cn.sock.close()

        for path in ['/bad-trailer', '/bad-final']:
            cn=H2Client(frontend)
            sid=cn.request(path)
            cn.done(sid)
            check(sid in cn.reset or cn.headers.get(sid,{}).get(':status')=='504', 'malformed HTTP/2 upstream rejected '+path)
            cn.sock.close()
        for extra in [[('connection','keep-alive')], [('te','gzip')], [(':path','/duplicate')], [('x-bad','a\x00b')]]:
            cn=H2Client(frontend)
            sid=cn.request('/known',extra=extra)
            cn.done(sid)
            check(sid in cn.reset, 'HTTP/2 invalid fields reset stream '+repr(extra))
            cn.sock.close()

        def tls(version, target):
            executable=os.environ.get('KANGLE_TEST_OPENSSL','openssl')
            result=subprocess.run([executable,'s_client','-ign_eof','-connect','127.0.0.1:'+str(target),'-servername','http.test','-'+version], input=b'GET / HTTP/1.1\r\nHost: http.test\r\nConnection: close\r\n\r\n',stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=8)
            with open(root+'/tls-'+str(target)+'-'+version+'.log','wb') as f:
                f.write(result.stdout)
            return b'HTTP/1.1 200' in result.stdout
        check(not tls('tls1',tlsport) and not tls('tls1_1',tlsport), 'default TLS rejects 1.0 and 1.1')
        check(tls('tls1_2',tlsport), 'default TLS accepts 1.2')
        check(tls('tls1',legacyport) and tls('tls1_1',legacyport), 'explicit legacy TLS configuration retained')
        if os.environ.get('KANGLE_TEST_TLS13'):
            check(tls('tls1_3',tlsport), 'modern OpenSSL default TLS accepts 1.3')
        check(proc.poll() is None, 'isolated server remains alive after all protocol cases')
        print('%d protocol integration checks passed; artifacts: %s' % (count,root),flush=True)
    except BaseException:
        print('FAILED; artifacts preserved: '+root, file=sys.stderr,flush=True)
        raise
    finally:
        if proc is not None and proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        for server in servers:
            server.shutdown()
            server.server_close()


if __name__=='__main__':
    run(os.path.abspath(sys.argv[1]))
