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
from dashmon import Database, API, ImageReceiverFactory, ImageServerFactory

from time import time, sleep, monotonic
from binascii import hexlify

from twisted.internet import reactor, task
from twisted.internet.defer import inlineCallbacks, DeferredList
from twisted.internet.protocol import Factory
from twisted.internet.endpoints import clientFromString, serverFromString
from twisted.application.internet import ClientService, backoffPolicy
from twisted.protocols.basic import Int32StringReceiver

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

class Actions:
    def __init__(self, mqtt):
        self.mqtt = mqtt

    def publish(self, pr, topic, message):
        self.mqtt.publish(topic, message)

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

    db = Database()
    api = API(db)
    ir_factory = ImageReceiverFactory()
    api.route('/ws')(ir_factory.Resource)

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
    api.listen(reactor, 'tcp:8040:interface=0.0.0.0')

    # image receiver
    ir_factory.listen(reactor, 'unix:./img.sock')

    reactor.run()

if __name__ == '__main__':
    if len(sys.argv) < 2: sys.exit()
    if len(sys.argv[1]) != 4: sys.exit()

    main()
