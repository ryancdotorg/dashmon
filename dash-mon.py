#!/usr/bin/env python3
def venv_exec():
    import os, sys
    from pathlib import Path
    basedir = Path(__file__).parent.resolve()
    executable = Path(sys.executable)
    try: executable.relative_to(basedir)
    except ValueError as e:
        p = next(basedir.glob('*/bin/python3'))
        cmd = str(Path(basedir, p))
        args = list(sys.argv)
        args.insert(0, cmd)
        os.execvp(cmd, args)
        raise e

if __name__ == '__main__':
    venv_exec()

import os
import re
import sys
import json
import signal
import logging
import datetime
import pcapy

import dashmon

from time import time, sleep, monotonic
from binascii import hexlify

from twisted.internet import reactor, task
from twisted.internet.defer import inlineCallbacks, DeferredList
from twisted.internet.protocol import Factory
from twisted.internet.endpoints import clientFromString, serverFromString
from twisted.application.internet import ClientService, backoffPolicy
from twisted.protocols.basic import Int32StringReceiver

from twisted.web import http, resource
from twisted.web.resource import Resource
from twisted.web.server import Site
from twisted.web.static import File as _File

from klein import Klein

from autobahn.twisted.resource import WebSocketResource
from autobahn.twisted.websocket import WebSocketServerProtocol, WebSocketServerFactory

from mqtt.client.factory import MQTTFactory

BROKER = 'tcp:127.0.0.1:1883'

global logger
logger = logging.getLogger('dash-mon')
logger.setLevel(logging.INFO)
ch = logging.StreamHandler()
ch.setLevel(logging.DEBUG)
logger.addHandler(ch)

# create a function that just returns a given value regardless of arguments
def const(x):
    return lambda *a, **k: x

class File(_File):
    contentEncodings = {}
    indexNames = ["index.html"]
    forbidden = re.compile(rb'(?:[.].*|Makefile|[.](?:sh|swp|bak))')

    def __init__(self, path, defaultType='application/octet-stream', *args, **kwargs):
        defaultType = kwargs.pop('defaultType', defaultType)
        super().__init__(path, defaultType, *args, **kwargs)

    def getChild(self, path, request):
        if self.forbidden.fullmatch(path):
            return resource.ForbiddenResource()
        else:
            return super().getChild(path, request)

    def directoryListing(self):
        return resource.ForbiddenResource()

class ExtEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, datetime.datetime):
            return int(obj.timestamp() * 1000)

        return super().default(obj)

class Actions:
    def __init__(self, mqtt):
        self.mqtt = mqtt

    def publish(self, pr, topic, message):
        self.mqtt.publish(topic, message)

class ImageReceiver(Int32StringReceiver):
    def stringReceived(self, string):
        self.factory.ws_factory.sendMessageAll(b'image\0' + string, True)

class ImageReceiverFactory(Factory):
    protocol = ImageReceiver

    def __init__(self, ws_factory):
        self.ws_factory = ws_factory

class ImageProtocol(WebSocketServerProtocol):
    def onOpen(self):
        self.factory.clients.add(self)

    def onClose(self, wasClean, code, reason):
        self.factory.clients.discard(self)

    def onPingMessage(self, isBinary):
        self.sendMessage(b'pong', isBinary)

    def onMessage(self, payload, isBinary):
        if payload == b'ping': return self.onPingMessage(isBinary)
        logger.info(f'binary:{isBinary} {payload}')

class ImageServerFactory(WebSocketServerFactory):
    protocol = ImageProtocol
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.clients = set()

    def sendMessageAll(payload, isBinary):
        for client in list(self.clients):
            if client.transport.is_closing(): self.clients.discard(client)
            else: client.sendMessage(payload, isBinary)

class MQTTService(ClientService):
    def __init__(self, endpoint, factory, username=None, password=None):
        self.username, self.password = username, password
        ClientService.__init__(self, endpoint, factory, retryPolicy=backoffPolicy())

    def startService(self):
        # invoke whenConnected() inherited method
        self.whenConnected().addCallback(self.connectToBroker)
        ClientService.startService(self)

    @inlineCallbacks
    def connectToBroker(self, protocol):
        self.protocol = protocol
        self.protocol.onDisconnection = self.onDisconnection
        try:
            yield self.protocol.connect("dash-mon", keepalive=60,
                username=self.username, password=self.password,
            )
        except Exception as e:
            logger.error(e)
        else:
            logger.info(f'Connected to {BROKER}')

    def onDisconnection(self, reason):
        logger.debug(f'Connection lost: {reason}')
        self.whenConnected().addCallback(self.connectToBroker)

    def publish(self, topic, message):
        def _logFailure(failure):
            logger.debug(f'reported {failure.getErrorMessage()}')

        d = self.protocol.publish(topic=topic, message=message, qos=0)
        d.addErrback(_logFailure)
        return d

def _capture(iface, cap, decoder, handler):
    #while os.getuid() == 0: sleep(0.01)
    logger.info(f'Starting packet handling loop for {iface}')
    fn = decoder(iface, cap, handler)
    cap.loop(-1, lambda h, d: reactor.callFromThread(fn, h, d))

api = Klein()
db = dashmon.db.Database()

def response(req, content, type_='text/html'):
    if   isinstance(content, bytes):     pass
    elif isinstance(content, bytearray): content = bytes(content)
    elif isinstance(content, str):       content = content.encode()
    else:                                content = str(content).encode()

    req.setHeader('Content-Type', type_)
    req.setHeader('Content-Length', str(len(content)))
    return content

def json_response(req, content):
    j = json.dumps(content, cls=ExtEncoder) + '\n'
    return response(req, j, 'application/json')

ws_factory = ImageServerFactory()

@api.route('/recent')
def api_recent(req):
    return json_response(req, db.get_recent())

@api.route('/ws')
def api_websocket(req):
    return WebSocketResource(ws_factory)

# Invoke the decorator directly to add a constant valued route
api.route('/', branch=True)(
    const(File('./static'))
)

# without the signal handler, ctrl-c doesn't work...
def exit(n, *args):
    logger.warning(f'Signal {n} caught, exiting...')
    reactor.stop()
    os._exit(0)

def main():
    # set up signal handlers
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, exit)

    prefix = sys.argv[1]
    hex_prefix = hexlify(prefix.encode()).decode()
    ifs = sys.argv[2:]
    #probe_req = 'subtype probe-req'
    probe_req = '((wlan[0] & 0xfc) = 0x40)'
    # assumes ssid will be first tlv in probe request
    bpf = f'{probe_req} and wlan[24] = 0 and wlan[25] > 4 and wlan[26:4] = 0x{hex_prefix}'
    #bpf = f'{probe_req}'

    mqtt_factory = MQTTFactory(profile=MQTTFactory.PUBLISHER)
    mqtt_endpoint = clientFromString(reactor, BROKER)
    mqtt_service = MQTTService(mqtt_endpoint, mqtt_factory, username='dev', password='changeme')

    act = Actions(mqtt_service)

    def handler(pr):
        print(pr.mac, pr.signal, pr.channel, pr.ident, pr.ssid)
        mqtt_service.publish(f'event/button/{pr.ident}/press', pr.to_json())
        db.log_press(pr)
        for action in db.get_actions(pr):
            name = action['name']
            args = action['args']

    decoder = dashmon.packet.probe_decoder(prefix)
    #reactor.callInThread(drop_privs)
    for iface in ifs:
        cap = pcapy.open_live(iface, 4096, 1, 100)
        cap.setfilter(bpf)
        logger.info(f'Listening on {iface}: linktype={cap.datalink()}')
        reactor.callInThread(_capture, iface, cap, decoder, handler)

    # mqtt client
    mqtt_service.startService()

    # klein api server
    api_endpoint = serverFromString(reactor, 'tcp:8040:interface=0.0.0.0')
    api_endpoint.listen(Site(api.resource()))

    # image receiver
    image_receiver = serverFromString(reactor, 'unix:./img.sock')
    image_receiver.listen(ImageReceiverFactory(ws_factory))

    reactor.run()

if __name__ == '__main__':
    if len(sys.argv) < 2: sys.exit()
    if len(sys.argv[1]) != 4: sys.exit()

    main()
