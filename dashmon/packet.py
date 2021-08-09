import logging
global logger
logger = logging.getLogger(__name__)

import json

from impacket.ImpactDecoder import RadioTapDecoder
from dataclasses import dataclass, asdict
from time import time, sleep, monotonic

format_mac = lambda b: ':'.join(map(lambda c: f'{c:02x}', b))

def probe_decoder(prefix=None, debounce=5.0):
    @dataclass
    class ProbeRequest:
        iface: str
        cap: object
        mac: str
        signal: int
        channel: tuple
        ident: str
        ssid: str

        def to_json(self):
            return json.dumps({
                'iface': self.iface, 'mac': self.mac, 'signal': self.signal,
                'channel': self.channel, 'ident': self.ident, 'ssid': self.ssid,
            })

    cooldown = {}
    radiotap_decode = RadioTapDecoder().decode
    def decoder(iface, cap, callback):
        nonlocal cooldown, radiotap_decode, prefix, debounce
        def handler(hdr, pkt):
            nonlocal cooldown, radiotap_decode, prefix, debounce
            nonlocal iface, cap, callback
            # get protocol objects
            rt = radiotap_decode(pkt)
            dot11 = rt.child()
            mgmt = dot11.child()
            probe = mgmt.child()
            # decode
            ident = None
            ssid = probe.get_ssid().decode()
            # reject fucky/empty ssids
            ssid_okay = False
            for c in map(ord, ssid):
                if c >= 0x20 and c <= 0x7e: ssid_okay = True
                else: return
            if not ssid_okay: return
            mac = format_mac(mgmt.get_source_address().tobytes())
            # debounce
            last = cooldown.get(mac, float('-inf'))
            now = monotonic()
            cooldown[mac] = now
            if last + debounce > now: return
            # get other metadata
            if ssid.startswith(prefix): ident = ssid[len(prefix):]
            channel = rt.get_channel()
            signal = (rt.get_dBm_ant_signal() or rt.get_dB_ant_signal() or 0) - 255
            #print(rt.header, rt.get_bytes().tobytes(), rt.tail)
            pr = ProbeRequest(iface, cap, mac, signal, channel, ident, ssid)
            callback(pr)

        return handler

    return decoder
