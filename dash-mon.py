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

from time import time, sleep, monotonic
from binascii import hexlify

from twisted.internet import reactor

import dashmon
from dashmon import (
    Database, API, ImageReceiverFactory, ImageServerFactory, MQTTService
)

global logger
logger = logging.getLogger('dashmon')
logger.setLevel(logging.DEBUG)
ch = logging.StreamHandler()
ch.setLevel(logging.DEBUG)
logger.addHandler(ch)

class Actions:
    def __init__(self, mqtt=None):
        self.mqtt = mqtt

    def publish(self, pr, topic, message):
        self.mqtt.publish(topic, message)

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

    # set up database
    db = Database()

    # set up actions
    act = Actions()

    # set up packet capture
    prefix = sys.argv[1]
    hex_prefix = hexlify(prefix.encode()).decode()
    ifs = sys.argv[2:]
    probe_req = '((wlan[0] & 0xfc) = 0x40)' # subtype probe-req
    # assumes ssid will be first tlv in probe request
    bpf = f'{probe_req} and wlan[24] = 0 and wlan[25] > 4 and wlan[26:4] = 0x{hex_prefix}'

    def handler(pr):
        print(pr.mac, pr.signal, pr.channel, pr.ident, pr.ssid)
        mqtt_service.publish(f'event/button/{pr.ident}/press', pr.to_json())
        db.log_press(pr)
        for action in db.get_actions(pr):
            name = action['name']
            args = action['args']
            try:
                getattr(act, name)(pr, *args)
            except Exception as e:
                logger.error(traceback.format_exc())

    decoder = dashmon.packet.probe_decoder(prefix)
    #reactor.callInThread(drop_privs)
    for iface in ifs:
        cap = pcapy.open_live(iface, 4096, 1, 100)
        cap.setfilter(bpf)
        logger.info(f'Listening on {iface}: linktype={cap.datalink()}')
        reactor.callInThread(_capture, iface, cap, decoder, handler)

    # set up mqtt client
    mqtt_service = MQTTService(
        reactor, 'tcp:127.0.0.1:1883',
        username='dev', password='changeme',
        topics=('status/#', 'event/#', 'tele/+/SENSOR')
    )

    @mqtt_service.route(r'tele/(.+)/SENSOR')
    def sensor(m, topic, payload):
        db.log_event(topic, payload)

    @mqtt_service.route(r'(?:status|event)/(.+)')
    def status(m, topic, payload):
        db.log_event(topic, payload)

    act.mqtt = mqtt_service
    mqtt_service.startService()

    # set up klein api server
    api = API(db)
    api.listen(reactor, 'tcp:8040:interface=0.0.0.0')

    # set up image receiver
    ir_factory = ImageReceiverFactory(klein=api)
    ir_factory.listen(reactor, 'unix:./img.sock')

    # start event loop
    reactor.run()

if __name__ == '__main__':
    if len(sys.argv) < 2: sys.exit()
    if len(sys.argv[1]) != 4: sys.exit()

    main()
