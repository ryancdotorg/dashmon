__all__ = ['MQTTService']

import logging
logger = logging.getLogger(__name__)

import os
import re
import sys
import json
import datetime

from base64 import b64encode as b64e

from time import time, sleep, monotonic

from twisted.internet.endpoints import clientFromString
from twisted.application.internet import ClientService, backoffPolicy
from twisted.internet.defer import inlineCallbacks, DeferredList

from mqtt.client.factory import MQTTFactory

def _logFailure(failure):
    logger.debug(f'reported {failure.getErrorMessage()}')

def _logGrantedQoS(value):
    logger.debug(f'response {value!r}')
    return True

class MQTTService(ClientService):
    def __init__(self, reactor, broker, topics=[], username=None, password=None):
        self.broker, self.topics = broker, topics
        self.username, self.password = username, password
        self.topic_handlers, self.topic_map = [], {}
        endpoint = clientFromString(reactor, self.broker)
        factory = MQTTFactory(profile=MQTTFactory.PUBLISHER|MQTTFactory.SUBSCRIBER)
        ClientService.__init__(self, endpoint, factory, retryPolicy=backoffPolicy())

    def startService(self):
        # invoke whenConnected() inherited method
        self.whenConnected().addCallback(self.connectToBroker)
        ClientService.startService(self)

    @inlineCallbacks
    def connectToBroker(self, protocol):
        self.protocol = protocol
        self.protocol.onPublish       = self.onPublish
        self.protocol.onDisconnection = self.onDisconnection
        self.protocol.setWindowSize(16)
        try:
            yield self.protocol.connect('dashmon', keepalive=60,
                username=self.username, password=self.password,
            )
            yield self.subscribe(self.topics)
        except Exception as e:
            logger.error(e)
        else:
            logger.info(f'Connected to {self.broker}')

    def onDisconnection(self, reason):
        logger.debug(f'Connection lost: {reason}')
        self.whenConnected().addCallback(self.connectToBroker)

    def onPublish(self, topic, payload, qos, dup, retain, msgId):
        try:
            payload = json.loads(payload)
        except:
            try:
                payload = {'__raw_text': payload.decode('utf-8', 'strict')}
            except UnicodeDecodeError:
                payload = {'__raw_base64': b64e(payload)}

        #logger.debug(f'topic={topic} msg={payload}')

        if topic in self.topic_map:
            regex, handler = self.topic_map[topic]
            m = regex.fullmatch(topic)
            handler(m, topic, payload)
        else:
            for regex, handler in self.topic_handlers:
                m = regex.fullmatch(topic)
                if m:
                    self.topic_map[topic] = (regex, handler)
                    handler(m, topic, payload)
                    break

    def route(self, regex):
        def register(handler):
            self.topic_handlers.append((re.compile(regex), handler))

        return register

    def _logFailure(self, d):
        d.addErrback(_logFailure)
        return d

    def subscribe(self, topics, qos=2):
        d = []
        for topic in topics:
            logger.debug(f'subscribe: {topic}')
            x = self.protocol.subscribe(topic, qos)
            x.addCallbacks(_logGrantedQoS, _logFailure)
            d.append(x)

        dlist = DeferredList(d, consumeErrors=True)
        dlist.addCallback(lambda *a: logger.debug(f'All subscriptions complete args={a!r}'))
        return dlist

    def unsubscribe(self, topics, qos=1):
        d = []
        for topic in topics:
            x = self.protocol.unsubscribe(topic, qos)
            x.addCallbacks(_logGrantedQoS, _logFailure)
            d.append(x)

        dlist = DeferredList(d, consumeErrors=True)
        dlist.addCallback(lambda *a: logger.debug(f'All unsubscribes complete args={a!r}'))
        return dlist

    def publish(self, topic, message, qos=2):
        return self._logFailure(self.protocol.publish(topic, message, qos))
