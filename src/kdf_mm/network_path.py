"""Passive Linux path metadata. No packet probes, domains, IP/MAC/SSID logging."""
from __future__ import annotations
import ipaddress
from pathlib import Path
import re
import subprocess
import threading
import time


def _read(path, limit=65536):
    with Path(path).open() as stream: return stream.read(limit)


def _default_route():
    routes = []
    for line in _read('/proc/net/route').splitlines()[1:]:
        row = line.split()
        if len(row) >= 8 and row[1] == '00000000' and int(row[3], 16) & 1:
            interface = row[0]
            if re.fullmatch(r'[A-Za-z0-9_.:-]{1,32}', interface):
                gateway = ipaddress.ip_address(int(row[2], 16).to_bytes(4, 'little'))
                routes.append((int(row[6]), interface, gateway))
    return min(routes, default=(None, None, None))[1:]


def _addresses(value):
    addresses = []
    for token in value.split():
        try: addresses.append(ipaddress.ip_address(token.split('%', 1)[0]))
        except ValueError: pass
    return addresses


def _scope(addresses, gateway=None):
    scopes = {'local_stub' if ip.is_loopback else
              'gateway' if ip == gateway else
              'private' if ip.is_private else 'public' for ip in addresses}
    return next(iter(scopes)) if len(scopes) == 1 else 'mixed' if scopes else 'unknown'


def _resolver_config(interface, gateway):
    result = {'resolver_frontend': 'unknown', 'upstream_dns_scope': 'unknown',
              'resolver_config_available': False, 'resolver_stats_available': False}
    try:
        addresses = []
        for line in _read('/etc/resolv.conf').splitlines():
            row = line.split()
            if len(row) >= 2 and row[0] == 'nameserver': addresses += _addresses(row[1])
        result.update(resolver_frontend=_scope(addresses, gateway),
                      resolver_config_available=bool(addresses))
    except OSError: pass
    try:
        r = subprocess.run(['/usr/bin/resolvectl', 'dns'], capture_output=True,
                           text=True, timeout=1)
        if r.returncode == 0:
            selected = []
            global_servers = []
            for line in r.stdout[:65536].splitlines():
                if line.startswith('Global:'): global_servers += _addresses(line.split(':', 1)[1])
                if re.match(r'Link \d+ \('+re.escape(interface)+r'\):', line):
                    selected += _addresses(line.split(':', 1)[1])
            result['upstream_dns_scope'] = _scope(selected or global_servers, gateway)
        # Only aggregate counters. Permission denial is unavailable, never zero.
        r = subprocess.run(['/usr/bin/resolvectl', '--json=short', 'statistics'],
                           capture_output=True, text=True, timeout=1)
        if r.returncode == 0:
            import json
            stats = json.loads(r.stdout[:65536])
            # Accept only known numeric counters, no query/cache contents.
            for group, key, label in [('transactions', 'total', 'resolver_transactions_total'),
                                     ('cache', 'hits', 'resolver_cache_hits_total'),
                                     ('cache', 'misses', 'resolver_cache_misses_total')]:
                value = stats.get(group, {}).get(key) if isinstance(stats, dict) else None
                if type(value) is int and value >= 0: result[label] = value
            result['resolver_stats_available'] = any(k.startswith('resolver_cache_') or
                k == 'resolver_transactions_total' for k in result)
    except (OSError, subprocess.TimeoutExpired, ValueError, TypeError, AttributeError): pass
    return result


class PathSampler:
    def __init__(self):
        self.last = None
        self.last_time = None

    def sample(self):
        now = time.monotonic()
        interface, gateway = _default_route()
        if interface is None:
            self.last = None
            return {'network_path_available': False, 'reason': 'no_ipv4_default_route'}
        base = Path('/sys/class/net') / interface
        result = {'network_path_available': True, 'link_kind':
                  'wifi' if (base/'wireless').exists() else 'other',
                  'link_sample_reset': False, 'wifi_stats_available': False}
        for file, label in [('ifindex', 'interface_index'), ('carrier', 'link_carrier'),
                            ('carrier_changes', 'link_carrier_changes_total')]:
            try: result[label] = int(_read(base/file, 64))
            except (OSError, ValueError): pass
        for field in ['rx_errors','tx_errors','rx_dropped','tx_dropped']:
            try: result['link_'+field+'_total'] = int(_read(base/'statistics'/field, 64))
            except (OSError, ValueError): pass
        try:
            for line in _read('/proc/net/wireless').splitlines():
                if ':' not in line or line.split(':', 1)[0].strip() != interface: continue
                row = line.split(':', 1)[1].split()
                if len(row) >= 10:
                    result['link_kind'] = 'wifi'
                    result['wifi_stats_available'] = True
                    result['wifi_quality_raw'] = float(row[1].rstrip('.'))
                    level = float(row[2].rstrip('.'))
                    if -120 <= level < 0: result['wifi_signal_dbm'] = level
                    for index, label in [(7,'wifi_retry_discard_total'),
                                         (8,'wifi_misc_discard_total'),(9,'wifi_missed_beacon_total')]:
                        result[label] = int(row[index])
        except (OSError, ValueError): pass
        result.update(_resolver_config(interface, gateway))
        if self.last is not None and self.last.get('interface_index') == result.get('interface_index'):
            result['path_sample_interval_ms'] = round((now-self.last_time)*1000)
            for key, value in list(result.items()):
                if key.endswith('_total') and type(value) is int and key in self.last:
                    if value >= self.last[key]: result[key[:-6]+'_delta'] = value-self.last[key]
                    else: result['link_sample_reset'] = True
        else: result['link_sample_reset'] = True
        self.last, self.last_time = result.copy(), now
        return result


def start_path_sampler(sink):
    if not Path('/proc/net/route').exists(): return
    def run():
        sampler = PathSampler()
        while True:
            try: sink.emit('network_path_sample', route='host', **sampler.sample())
            except Exception:
                sink.emit('network_path_sample', route='host', outcome='unavailable',
                          network_path_available=False)
            time.sleep(30)
    threading.Thread(target=run, name='network-path-diagnostics', daemon=True).start()
