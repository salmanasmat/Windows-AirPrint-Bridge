"""
Diagnostic script: Probes mDNS to see what an iOS device would discover,
and tests the IPP endpoint response for AirPrint compatibility.
"""
import socket
import struct
import sys
import json

# Auto-detect local IP for testing
_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
try:
    _sock.connect(("8.8.8.8", 80))
    LOCAL_IP = _sock.getsockname()[0]
except OSError:
    LOCAL_IP = "127.0.0.1"
finally:
    _sock.close()

# --- 1. Scan mDNS for _ipp._tcp services ---
print("=" * 60)
print("STEP 1: Scanning mDNS for _ipp._tcp.local. services")
print("=" * 60)

from zeroconf import Zeroconf, ServiceBrowser, ServiceStateChange

found_services = []

def on_state_change(zeroconf, service_type, name, state_change):
    if state_change == ServiceStateChange.Added:
        info = zeroconf.get_service_info(service_type, name)
        if info:
            addresses = [socket.inet_ntoa(a) for a in info.addresses]
            props = {}
            if info.properties:
                for k, v in info.properties.items():
                    key = k.decode() if isinstance(k, bytes) else k
                    val = v.decode() if isinstance(v, bytes) else str(v)
                    props[key] = val
            found_services.append({
                "name": name,
                "type": service_type,
                "server": info.server,
                "port": info.port,
                "addresses": addresses,
                "properties": props,
            })
            print(f"  FOUND: {name}")
            print(f"    server={info.server}  port={info.port}  addresses={addresses}")
            print(f"    TXT properties:")
            for k, v in props.items():
                print(f"      {k} = {v}")
            print()

zc = Zeroconf()
browser1 = ServiceBrowser(zc, "_ipp._tcp.local.", handlers=[on_state_change])

# Also check for _universal subtype
browser2 = ServiceBrowser(zc, "_universal._sub._ipp._tcp.local.", handlers=[on_state_change])

import time
print("  Listening for 5 seconds...")
time.sleep(5)

zc.close()

if not found_services:
    print("  ❌ NO SERVICES FOUND! mDNS advertisement is not working.")
else:
    print(f"  ✅ Found {len(found_services)} service(s)")

# --- 2. Test IPP endpoint directly ---
print()
print("=" * 60)
print("STEP 2: Testing IPP endpoint (Get-Printer-Attributes)")
print("=" * 60)

import http.client

# Build a minimal IPP Get-Printer-Attributes request
def build_get_attrs_request():
    """Build IPP Get-Printer-Attributes binary request."""
    # Header: version 1.1, operation 0x000B, request-id 1
    header = struct.pack("!BBHi", 1, 1, 0x000B, 1)
    
    # Operation attributes group
    body = struct.pack("!B", 0x01)  # operation-attributes-tag
    
    # attributes-charset = utf-8
    body += struct.pack("!B", 0x47)  # charset tag
    body += struct.pack("!H", 18) + b"attributes-charset"
    body += struct.pack("!H", 5) + b"utf-8"
    
    # attributes-natural-language = en-us
    body += struct.pack("!B", 0x48)  # natural-language tag
    body += struct.pack("!H", 27) + b"attributes-natural-language"
    body += struct.pack("!H", 5) + b"en-us"
    
    # printer-uri
    printer_uri = f"ipp://{LOCAL_IP}:631/ipp/print".encode("ascii")
    body += struct.pack("!B", 0x45)  # uri tag
    body += struct.pack("!H", 11) + b"printer-uri"
    body += struct.pack("!H", len(printer_uri)) + printer_uri
    
    # end-of-attributes
    body += struct.pack("!B", 0x03)
    
    return header + body

try:
    conn = http.client.HTTPConnection(LOCAL_IP, 631, timeout=5)
    ipp_request = build_get_attrs_request()
    conn.request("POST", "/ipp/print", body=ipp_request, headers={
        "Content-Type": "application/ipp",
        "Content-Length": str(len(ipp_request)),
    })
    resp = conn.getresponse()
    data = resp.read()
    
    print(f"  HTTP status: {resp.status}")
    print(f"  Content-Type: {resp.getheader('Content-Type')}")
    print(f"  Response size: {len(data)} bytes")
    
    if len(data) >= 8:
        ver_maj, ver_min, status, req_id = struct.unpack("!BBHi", data[:8])
        print(f"  IPP version: {ver_maj}.{ver_min}")
        print(f"  IPP status: 0x{status:04X} ({'successful-ok' if status == 0 else 'ERROR'})")
        print(f"  Request ID: {req_id}")
        
        # Check for key AirPrint attributes in response
        response_text = data[8:]
        checks = {
            b"printer-uri-supported": "printer-uri-supported",
            b"document-format-supported": "document-format-supported",
            b"printer-is-accepting-jobs": "printer-is-accepting-jobs",
            b"color-supported": "color-supported",
            b"print-color-mode-supported": "print-color-mode-supported",
            b"print-color-mode-default": "print-color-mode-default",
            b"pwg-raster-document-type-supported": "pwg-raster-document-type-supported",
            b"urf-supported": "urf-supported",
            b"printer-uuid": "printer-uuid",
            b"printer-more-info": "printer-more-info",
            b"image/urf": "image/urf in PDL list (AirPrint)",
            b"image/pwg-raster": "image/pwg-raster in PDL list (Android/Mopria)",
        }
        print()
        print("  AirPrint attribute check:")
        for needle, label in checks.items():
            present = needle in response_text
            icon = "✅" if present else "❌"
            print(f"    {icon} {label}")
        
        if status == 0:
            print()
            print("  ✅ IPP endpoint is responding correctly")
        else:
            print()
            print(f"  ❌ IPP returned error status 0x{status:04X}")
    else:
        print("  ❌ Response too short to be valid IPP")
        
    conn.close()
except Exception as e:
    print(f"  ❌ Connection failed: {e}")

# --- 3. Network connectivity check ---
print()
print("=" * 60)
print("STEP 3: Network & Configuration Summary")
print("=" * 60)

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
try:
    sock.connect(("8.8.8.8", 80))
    local_ip = sock.getsockname()[0]
except OSError:
    local_ip = "UNKNOWN"
finally:
    sock.close()

import win32print
printer = win32print.GetDefaultPrinter()

print(f"  Local IP:        {local_ip}")
print(f"  Default Printer: {printer}")
print(f"  IPP Port:        631")
print(f"  Expected URI:    ipp://{local_ip}:631/ipp/print")

# Check if Bonjour / mDNS responder is available
import subprocess
result = subprocess.run(
    ["netstat", "-ano"], capture_output=True, text=True
)
mdns_port = ":5353" in result.stdout
print(f"  UDP 5353 (mDNS): {'✅ Active' if mdns_port else '❌ Not found'}")

ipp_port = ":631" in result.stdout
print(f"  TCP 631 (IPP):   {'✅ Listening' if ipp_port else '❌ Not listening'}")
