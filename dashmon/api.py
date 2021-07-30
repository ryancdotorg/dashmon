__all__ = ['API']

import logging
logger = logging.getLogger(__name__)

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

class API(Klein):
    def __init__(self, db):
        super().__init__()
        self.db = db

        @self.route('/recent')
        def recent(req):
            return json_response(req, self.db.get_recent())

        # Invoke the decorator directly to add a constant valued route
        static = File('./static')
        self.route('/', branch=True)(lambda *_: static)

    def listen(self, reactor, endpoint):
        server = serverFromString(reactor, endpoint)
        server.listen(Site(self.resource()))
