__all__ = ['ImageReceiverFactory', 'ImageServerFactory']

import logging
logger = logging.getLogger(__name__)

from twisted.internet.protocol import Factory
from twisted.internet.endpoints import serverFromString
from twisted.protocols.basic import Int32StringReceiver

from autobahn.twisted.resource import WebSocketResource
from autobahn.twisted.websocket import WebSocketServerProtocol, WebSocketServerFactory

class ImageReceiver(Int32StringReceiver):
    def stringReceived(self, string):
        self.factory.ws_factory.sendMessageAll(b'image\0' + string, True)

class ImageReceiverFactory(Factory):
    protocol = ImageReceiver

    def __init__(self, ws_factory=None):
        if ws_factory is None:
            self.ws_factory = ImageServerFactory()
        else:
            self.ws_factory = ws_factory

    def Resource(self, req):
        return WebSocketResource(self.ws_factory)

    def listen(self, reactor, endpoint):
        server = serverFromString(reactor, endpoint)
        server.listen(self)

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

    def Resource(self, req):
        return WebSocketResource(self)

    def sendMessageAll(payload, isBinary):
        for client in list(self.clients):
            if client.transport.is_closing(): self.clients.discard(client)
            else: client.sendMessage(payload, isBinary)
