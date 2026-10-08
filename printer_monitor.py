#!/usr/bin/env python3
"""Combined Toshiba/HP ink and toner monitor (Python 3.10+).

Quick start:
  python -m pip install 'pysnmp>=7.1,<8'
  python printer_monitor.py
  python printer_monitor.py --ips 10.0.0.20,10.0.0.21

Optional original Toshiba browser capture fallback:
  python -m pip install scapy
  python printer_monitor.py --capture-toshiba
  Windows: install Npcap and run an elevated terminal; Linux: capture privileges.
  Capture reads unencrypted HTTP only. Refresh TopAccess if no data appears.

Set PRINTER_SUBNETS below to scan one or multiple networks at startup.
With no arguments, a startup prompt accepts inclusive start-end IP ranges,
CIDR subnets, or individual addresses, separated by commas. Enter uses defaults.
Discovery checks SNMP supplies and TCP 9100 concurrently; it does not print.
Example: python printer_monitor.py --subnets 10.0.0.0/24 10.0.1.0/24
Saved addresses: printers.txt beside this script. --ips bypasses subnet scans.
--no-scan uses saved/manual addresses. HTTP-only printers without SNMP or
TCP 9100 require --ips. Discovered addresses are saved after a successful scan.
--raw prints HTTP responses and SNMP values for troubleshooting.
--status-url /path lets you supply a known Toshiba XML endpoint.
--monthly sends a report NOW; schedule the command externally for monthly runs.
--email-alerts sends newly low cartridge alerts, default threshold 20%.
Email environment variables: PRINTER_SMTP_HOST, PRINTER_SMTP_PORT (587),
PRINTER_SMTP_USER, PRINTER_SMTP_PASSWORD, PRINTER_EMAIL_FROM,
PRINTER_EMAIL_TO (comma-separated). SMTP uses STARTTLS.
SNMP community: PRINTER_SNMP_COMMUNITY (default public); read-only SNMPv2c.
No percentages are invented for unavailable or qualitative supply readings.
"""
import argparse
import asyncio
import ipaddress
import json
import os
import re
import smtplib
import socket
import ssl
import sys
import time
import urllib.request
import webbrowser
import xml.etree.ElementTree as ET
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlsplit

BASE = Path(__file__).resolve().parent
# Add your actual network(s) here. A nonempty list enables automatic discovery
# at startup, without an IP prompt. Only scan networks you manage.
PRINTER_SUBNETS = [
    # '129.100.42.0/24',
    # '129.100.43.0/24',
]
SCAN_WORKERS = 32
SCAN_TIMEOUT = 1.0
MAX_SCAN_HOSTS = 4096
COLORS = {'K': 'Black', 'C': 'Cyan', 'M': 'Magenta', 'Y': 'Yellow', 'EB': 'EB'}
SUPPLIES = '1.3.6.1.2.1.43.11.1.1'


def number(value):
    try:
        n = float(str(value).strip().rstrip('%'))
        return round(n, 1) if 0 <= n <= 100 else None
    except (ValueError, TypeError):
        return None


def local(tag):
    return tag.rsplit('}', 1)[-1]


def fields(element):
    return {local(e.tag): (e.text or '').strip() for e in element.iter()}


def parse_xml(text):
    """Return actual cartridge records, preserving unknown and status values."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        # Captured HTTP may contain headers, chunk markers or surrounding HTML.
        m = re.search(r'<Toner\b[^>]*>.*?</Toner>', text, re.S)
        if not m:
            return []
        try:
            root = ET.fromstring(m.group())
        except ET.ParseError:
            return []
    records = []
    for toner in root.iter():
        if local(toner.tag) != 'Toner':
            continue
        for cartridge in toner:
            name = COLORS.get(local(cartridge.tag))
            if name:
                f = fields(cartridge)
                p = number(f.get('RemainingQuantityDetails'))
                if p is None:
                    p = number(f.get('RemainingQuantity'))
                records.append({'name': name, 'percent': p,
                                'status': f.get('ThreeStepRemainingQuantityDetails', '')})
    if records:
        return records
    for element in root.iter():
        if local(element.tag) not in ('ConsumableInfo', 'Consumable'):
            continue
        f = fields(element)
        level = f.get('ConsumablePercentageLevelRemaining',
                      f.get('ConsumableRawPercentageLevelRemaining'))
        if level is None:
            continue
        name = (f.get('MarkerColor') or f.get('ConsumableLabelCode')
                or f.get('ConsumableTypeEnum') or 'Cartridge')
        records.append({'name': name, 'percent': number(level),
                        'status': f.get('ConsumableState', '')})
    return records


def parse_html(text):
    text = re.sub(r'<[^>]+>', ' ', text)
    result = []
    for color in ('Black', 'Cyan', 'Magenta', 'Yellow', 'Tri-color'):
        match = re.search(r'\b' + re.escape(color) + r'\b[^\d%]{0,80}(\d{1,3})\s*%', text, re.I)
        if match and number(match[1]) is not None:
            result.append({'name': color, 'percent': number(match[1]), 'status': ''})
    return result


def http_check(address, args):
    urls = [args.status_url] if args.status_url else []
    urls += ['/DevMgmt/ConsumableConfigDyn.xml', '/?MAIN=TOPACCESS',
             '/hp/device/SupplyStatus', '/hp/device/this.LCDispatcher?nav=hp.Supplies', '/']
    errors = []
    for path in dict.fromkeys(urls):
        for scheme in (('https',) if args.https else ('http', 'https')):
            url = f'{scheme}://{address}{path}'
            try:
                context = ssl._create_unverified_context() if args.insecure else ssl.create_default_context()
                request = urllib.request.Request(url, headers={'User-Agent': 'Printer-Monitor/2.0'})
                with urllib.request.urlopen(request, timeout=args.timeout, context=context) as response:
                    text = response.read(2_000_001)
                    if len(text) > 2_000_000:
                        raise ValueError('response exceeds 2 MB')
                    text = text.decode('utf-8', errors='replace')
                if args.raw:
                    print(f'\nRAW {url}\n{text}')
                records = parse_xml(text) or parse_html(text)
                if records:
                    return records, url, errors
            except Exception as exc:
                errors.append(f'{url}: {exc}')
    return [], '', errors


def snmp_records(rows):
    records = []
    for index, row in sorted(rows.items()):
        # Class 4 is a receptacle (e.g. waste toner), not remaining ink.
        if row.get(4) == 4:
            continue
        if row.get(5) not in (3, 4, 5, 6, 21):
            continue  # toner, waste toner, ink, ink cartridge, toner cartridge
        level, capacity = row.get(9), row.get(8)
        p = None
        if isinstance(level, int) and level >= 0:
            if row.get(7) == 19:  # percent unit
                p = number(level)
            elif isinstance(capacity, int) and capacity > 0:
                p = number(100 * level / capacity)
        status = {-1: 'Other', -2: 'Unknown', -3: 'Some remaining (amount unknown)'}.get(level, '')
        records.append({'name': row.get(6) or f'Supply {index}', 'percent': p, 'status': status})
    return records


async def snmp_check(address, args):
    try:
        from pysnmp.hlapi.v3arch.asyncio import (SnmpEngine, CommunityData,
            UdpTransportTarget, ContextData, ObjectType, ObjectIdentity, walk_cmd)
    except ImportError:
        return [], 'SNMP unavailable: install pysnmp>=7.1,<8'
    engine = SnmpEngine()
    rows = {}
    try:
        target = await UdpTransportTarget.create((address, 161), timeout=args.timeout, retries=0)
        async for error, status, _, bindings in walk_cmd(
            engine, CommunityData(os.getenv('PRINTER_SNMP_COMMUNITY', 'public'), mpModel=1),
            target, ContextData(), ObjectType(ObjectIdentity(SUPPLIES)),
            lexicographicMode=False, maxRows=512):
            if error or status:
                raise RuntimeError(str(error or status))
            for oid, value in bindings:
                key = str(oid)
                if not key.startswith(SUPPLIES + '.'):
                    continue
                parts = key[len(SUPPLIES) + 1:].split('.')
                col, index = int(parts[0]), '.'.join(parts[1:])
                if col == 6:
                    val = bytes(value.asOctets()).decode('utf-8', errors='replace')
                else:
                    try:
                        val = int(value)
                    except (TypeError, ValueError):
                        continue
                rows.setdefault(index, {})[col] = val
                if args.raw:
                    print(f'SNMP {address} {key} = {val}')
        return snmp_records(rows), ''
    except Exception as exc:
        return [], f'SNMP: {exc}'
    finally:
        engine.close_dispatcher()


def capture_toshiba(addresses, seconds):
    """Opt-in fallback matching the original Toshiba traffic parser."""
    try:
        from scapy.all import AsyncSniffer, IP, TCP, Raw
    except ImportError as exc:
        raise RuntimeError('Install scapy and a packet capture driver (Npcap on Windows).') from exc
    found, buffers = {}, {}
    def received(packet):
        if IP not in packet or TCP not in packet or Raw not in packet:
            return
        src = packet[IP].src
        if src not in addresses or packet[TCP].sport != 80:
            return
        key = (src, packet[IP].dst, packet[TCP].sport, packet[TCP].dport)
        seq = packet[TCP].seq
        segments = buffers.setdefault(key, {})
        segments[seq] = bytes(packet[Raw])
        if sum(map(len, segments.values())) > 2_000_000:
            buffers.pop(key, None)
            return
        # Order segments and remove retransmission overlap. Don't join across gaps.
        chunks, end = [], None
        for start, payload in sorted(segments.items()):
            if end is not None and start > end:
                chunks = []
            offset = max(0, (end or start) - start)
            chunks.append(payload[offset:])
            end = max(end or start, start + len(payload))
        records = parse_xml(b''.join(chunks).decode('utf-8', errors='ignore'))
        if records:
            found[src] = records
            buffers.pop(key, None)
    ready = __import__('threading').Event()
    sniffer = AsyncSniffer(filter='tcp and src port 80', prn=received,
                           store=False, started_callback=ready.set)
    sniffer.start()
    try:
        if not ready.wait(5):
            raise RuntimeError('Capture did not start; check capture permissions/driver.')
        for address in addresses:
            webbrowser.open_new_tab(f'http://{address}/?MAIN=TOPACCESS')
        print(f'Capturing Toshiba HTTP for {seconds}s; refresh TopAccess if needed.')
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and len(found) < len(addresses):
            time.sleep(.2)
    finally:
        if sniffer.running:
            sniffer.stop()
    return found


def normalize(entry):
    host = urlsplit(entry if '://' in entry else 'http://' + entry).hostname
    return str(ipaddress.IPv4Address(host))


def subnet_hosts(subnets):
    """Expand subnets, inclusive IPv4 ranges, and single addresses safely."""
    hosts = set()
    for subnet in subnets:
        subnet = subnet.strip()
        if '-' in subnet:
            endpoints = subnet.split('-')
            if len(endpoints) != 2:
                raise ValueError(f'Invalid range: {subnet}; use start-IP-end-IP.')
            start, end = (int(ipaddress.IPv4Address(x.strip())) for x in endpoints)
            if end < start:
                raise ValueError(f'{subnet}: end address must be at or after start address.')
            count = end - start + 1
            if count > MAX_SCAN_HOSTS:
                raise ValueError(f'{subnet}: range contains {count:,} addresses; limit is {MAX_SCAN_HOSTS:,}. Use a smaller range.')
            hosts.update(str(ipaddress.IPv4Address(x)) for x in range(start, end + 1))
            if len(hosts) > MAX_SCAN_HOSTS:
                raise ValueError(f'Combined targets exceed {MAX_SCAN_HOSTS} hosts.')
            continue
        network = ipaddress.ip_network(subnet.strip(), strict=False)
        if network.version != 4:
            raise ValueError('Subnet discovery currently supports IPv4 only.')
        if network.num_addresses > MAX_SCAN_HOSTS + 2:
            raise ValueError(f'{subnet}: subnet exceeds {MAX_SCAN_HOSTS} hosts; use smaller subnets.')
        hosts.update(str(host) for host in network.hosts())
        if len(hosts) > MAX_SCAN_HOSTS:
            raise ValueError(f'Combined subnets exceed {MAX_SCAN_HOSTS} hosts.')
    return sorted(hosts, key=ipaddress.IPv4Address)


def prompt_targets(defaults, saved):
    print('\nEnter printer IPs, inclusive ranges, or subnets (comma-separated).')
    print('Example: 10.0.0.20-10.0.0.80, 10.0.1.0/24, 10.0.2.15')
    print('Configured subnets: ' + (', '.join(defaults) or '(none)'))
    print('Saved printers: ' + (', '.join(saved) or '(none)'))
    while True:
        value = input('Targets [Enter = configured subnets, otherwise saved printers]: ').strip()
        if not value:
            return list(defaults), list(saved)
        targets = [x.strip() for x in value.split(',') if x.strip()]
        try:
            subnet_hosts(targets)  # Validate before any network requests.
            scan = [x for x in targets if '/' in x or '-' in x]
            direct = [normalize(x) for x in targets if '/' not in x and '-' not in x]
            return scan, direct
        except ValueError as exc:
            print(f'Invalid targets: {exc}')


def discover_printers(subnets, args):
    """Discover supply-reporting SNMP devices or raw-print TCP 9100 candidates.

    A listening print port is a candidate, not a verified printer. The normal
    status check confirms supply data. HTTP-only devices with SNMP disabled
    must be added using --ips. No print data is sent to port 9100.
    """
    hosts = subnet_hosts(subnets)
    print(f'Discovering printers across {len(hosts)} addresses: {", ".join(subnets)}', flush=True)
    try:
        import pysnmp  # noqa: F401
        has_snmp = True
    except ImportError:
        has_snmp = False
        print('SNMP discovery unavailable; install pysnmp>=7.1,<8. Checking TCP 9100 only.')
    probe_args = argparse.Namespace(**vars(args))
    probe_args.timeout = args.scan_timeout
    probe_args.raw = False
    def probe(host):
        if has_snmp:
            async def bounded():
                return await asyncio.wait_for(snmp_check(host, probe_args),
                                              timeout=args.scan_timeout * 3)
            try:
                records, _ = asyncio.run(bounded())
                if records:
                    return host, 'SNMP supplies'
            except Exception:
                pass
        try:
            with socket.create_connection((host, 9100), timeout=args.scan_timeout):
                return host, 'TCP 9100 candidate'
        except OSError:
            return None
    discovered = []
    with ThreadPoolExecutor(max_workers=args.scan_workers) as pool:
        futures = [pool.submit(probe, host) for host in hosts]
        for completed, future in enumerate(as_completed(futures), 1):
            result = future.result()
            if result:
                host, method = result
                discovered.append(host)
                print(f'  Found {host} ({method})', flush=True)
            if completed % 64 == 0 or completed == len(hosts):
                print(f'  Discovery progress: {completed}/{len(hosts)}', flush=True)
    return sorted(discovered, key=ipaddress.IPv4Address)


def send_email(subject, body):
    required = ('PRINTER_SMTP_HOST', 'PRINTER_EMAIL_FROM', 'PRINTER_EMAIL_TO')
    missing = [key for key in required if not os.getenv(key)]
    if missing:
        raise RuntimeError('Set email environment variables: ' + ', '.join(missing))
    message = EmailMessage()
    message['Subject'], message['From'] = subject, os.environ['PRINTER_EMAIL_FROM']
    message['To'] = os.environ['PRINTER_EMAIL_TO']
    message.set_content(body)
    with smtplib.SMTP(os.environ['PRINTER_SMTP_HOST'], int(os.getenv('PRINTER_SMTP_PORT', '587')), timeout=30) as smtp:
        smtp.starttls(context=ssl.create_default_context())
        if os.getenv('PRINTER_SMTP_USER'):
            smtp.login(os.environ['PRINTER_SMTP_USER'], os.environ['PRINTER_SMTP_PASSWORD'])
        smtp.send_message(message)


def report(results, threshold):
    lines = [f'Printer supplies — {datetime.now().isoformat(timespec="seconds")}']
    for address, result in results.items():
        lines.append(f'\n{address} [{result["source"] or "No usable data"}]')
        for record in result['records']:
            p = record['percent']
            value = 'UNKNOWN' if p is None else f'{p:g}%'
            low = ' LOW' if p is not None and p <= threshold else ''
            lines.append(f'  {record["name"]}: {value}{low} {record["status"]}')
        if not result['records']:
            lines.append('  Check reachability, SNMP settings, or try --capture-toshiba / --raw.')
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--ips', help='Comma-separated IPv4 addresses; bypass saved list/prompt')
    parser.add_argument('--subnets', nargs='+', help='Override PRINTER_SUBNETS, e.g. --subnets 10.0.0.0/24 10.0.1.0/24')
    parser.add_argument('--no-scan', action='store_true', help='Use saved/manual IPs instead of configured subnets')
    parser.add_argument('--scan-workers', type=int, default=SCAN_WORKERS)
    parser.add_argument('--scan-timeout', type=float, default=SCAN_TIMEOUT)
    parser.add_argument('--file', type=Path, default=BASE / 'printers.txt')
    parser.add_argument('--method', choices=['auto', 'http', 'snmp'], default='auto')
    parser.add_argument('--raw', action='store_true')
    parser.add_argument('--https', action='store_true', help='HTTPS only')
    parser.add_argument('--insecure', action='store_true', help='Accept self-signed printer HTTPS certificates')
    parser.add_argument('--status-url', help='Known XML endpoint path on each printer')
    parser.add_argument('--capture-toshiba', action='store_true', help='Open browser/capture HTTP for unresolved printers')
    parser.add_argument('--capture-seconds', type=int, default=45)
    parser.add_argument('--timeout', type=float, default=3)
    parser.add_argument('--threshold', type=float, default=20)
    parser.add_argument('--email-alerts', action='store_true')
    parser.add_argument('--monthly', action='store_true')
    args = parser.parse_args()
    if args.scan_timeout <= 0 or not 1 <= args.scan_workers <= 128:
        parser.error('Scan timeout must be positive; scan workers must be 1–128.')
    if args.ips is not None and args.subnets:
        parser.error('Use either --ips or --subnets, not both.')
    if args.timeout <= 0 or args.capture_seconds <= 0 or not 0 <= args.threshold <= 100:
        parser.error('Timeout/capture duration must be positive; threshold must be 0–100.')
    if args.status_url and (not args.status_url.startswith('/') or args.status_url.startswith('//')):
        parser.error('--status-url must be a path starting with a single slash')
    saved = args.file.read_text().splitlines() if args.file.exists() else []
    subnets = args.subnets if args.subnets is not None else PRINTER_SUBNETS
    prompted_direct = []
    if len(sys.argv) == 1 and sys.stdin.isatty():
        subnets, prompted_direct = prompt_targets(subnets, saved)
    scanning = bool(subnets) and not args.no_scan and args.ips is None
    if scanning:
        entries = discover_printers(subnets, args) + prompted_direct
    elif len(sys.argv) == 1 and sys.stdin.isatty():
        entries = prompted_direct
    elif args.ips is not None:
        entries = args.ips.split(',')
    elif sys.stdin.isatty():
        print('Saved printers: ' + ', '.join(saved))
        new = input('Add comma-separated IPs, or Enter to use saved: ').strip()
        entries = saved + new.split(',')
    else:
        entries = saved
    addresses = sorted({normalize(x.strip()) for x in entries if x.strip()})
    if not addresses:
        if scanning:
            print('No printers discovered. Check SNMP/read community or TCP 9100; use --ips for HTTP-only printers.')
            return 1
        parser.error('No printers configured. Use --ips or create printers.txt.')
    if args.ips is None:
        args.file.write_text('\n'.join(addresses) + '\n')
    results = {}
    for address in addresses:
        print(f'Checking {address} ...', flush=True)
        records, source, errors = [], '', []
        if args.method in ('auto', 'snmp'):
            records, error = asyncio.run(snmp_check(address, args))
            if error:
                errors.append(error)
            if records:
                source = 'SNMP'
        if args.method in ('auto', 'http') and not any(r['percent'] is not None for r in records):
            web_records, url, web_errors = http_check(address, args)
            errors.extend(web_errors)
            if web_records:
                records, source = web_records, url
        results[address] = {'records': records, 'source': source, 'errors': errors}
        if args.raw:
            print('\n'.join(errors))
    unresolved = [a for a, r in results.items() if not any(x['percent'] is not None for x in r['records'])]
    if args.capture_toshiba and unresolved:
        captured = capture_toshiba(unresolved, args.capture_seconds)
        for address, records in captured.items():
            results[address].update(records=records, source='Toshiba browser HTTP capture')
    body = report(results, args.threshold)
    print('\n' + body)
    if args.monthly:
        send_email('Printer supplies monthly report', body)
        print('Monthly report sent.')
    if args.email_alerts:
        state_path = BASE / 'printer_state.json'
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        for address, result in results.items():
            alerted = state.setdefault(address, {})
            newly_low = []
            for r in result['records']:
                p, name = r['percent'], r['name']
                if p is None:
                    continue
                if p > args.threshold:
                    alerted[name] = False
                elif not alerted.get(name):
                    newly_low.append(name)
            if newly_low:
                # Only acknowledge alerts after SMTP succeeds, so failures retry.
                send_email(f'Printer {address}: low ink/toner', report({address: result}, args.threshold))
                for name in newly_low:
                    alerted[name] = True
            temp = state_path.with_suffix('.tmp')
            temp.write_text(json.dumps(state, indent=2))
            temp.replace(state_path)
    return 0 if all(r['records'] for r in results.values()) else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print('\nStopped.')
        raise SystemExit(130)
    except Exception as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        raise SystemExit(1)
