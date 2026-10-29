"""
Decode the exact IPP binary response our server sends, to find what
iOS rejects. Mimics the exact request iOS sends.
"""
import socket
import struct
import sys

sys.stdout.reconfigure(encoding='utf-8')

# Accept host from CLI, or auto-detect local IP
if len(sys.argv) >= 2:
    HOST = sys.argv[1]
else:
    _sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        _sock.connect(("8.8.8.8", 80))
        HOST = _sock.getsockname()[0]
    except OSError:
        HOST = "127.0.0.1"
    finally:
        _sock.close()

PORT = int(sys.argv[2]) if len(sys.argv) >= 3 else 631
print(f"Target: {HOST}:{PORT}")

# Build Get-Printer-Attributes request (mimicking iOS)
def build_request():
    header = struct.pack("!BBHi", 2, 0, 0x000B, 42)  # IPP 2.0 like iOS
    body = struct.pack("!B", 0x01)  # operation-attributes-tag
    
    def text_attr(tag, name, val):
        nb = name.encode("ascii")
        vb = val.encode("utf-8")
        return struct.pack("!B", tag) + struct.pack("!H", len(nb)) + nb + struct.pack("!H", len(vb)) + vb
    
    body += text_attr(0x47, "attributes-charset", "utf-8")
    body += text_attr(0x48, "attributes-natural-language", "en-us")
    body += text_attr(0x45, "printer-uri", f"ipp://{HOST}:{PORT}/ipp/print")
    
    # iOS requests these specific attribute groups
    body += text_attr(0x44, "requested-attributes", "all")
    
    body += struct.pack("!B", 0x03)  # end-of-attributes
    return header + body

# Value tag names for display
TAG_NAMES = {
    0x21: "integer", 0x22: "boolean", 0x23: "enum",
    0x30: "octetString", 0x31: "dateTime", 0x32: "resolution", 0x33: "rangeOfInteger",
    0x41: "textWithoutLanguage", 0x42: "nameWithoutLanguage",
    0x44: "keyword", 0x45: "uri", 0x46: "uriScheme",
    0x47: "charset", 0x48: "naturalLanguage", 0x49: "mimeMediaType",
}

DELIMITER_NAMES = {
    0x00: "reserved", 0x01: "operation-attributes",
    0x02: "job-attributes", 0x03: "end-of-attributes",
    0x04: "printer-attributes", 0x05: "unsupported-attributes",
}

def decode_value(tag, data):
    """Decode a value based on its tag."""
    if tag == 0x21:  # integer
        return str(struct.unpack("!i", data)[0]) if len(data) == 4 else data.hex()
    elif tag == 0x22:  # boolean
        return "true" if data[0] else "false"
    elif tag == 0x23:  # enum
        return str(struct.unpack("!i", data)[0]) if len(data) == 4 else data.hex()
    elif tag == 0x33:  # rangeOfInteger
        if len(data) == 8:
            lo, hi = struct.unpack("!ii", data)
            return f"{lo}-{hi}"
        return data.hex()
    elif 0x40 <= tag <= 0x49:  # text/name/keyword/uri/etc
        return data.decode("utf-8", errors="replace")
    else:
        return data.hex()

def parse_response(data):
    """Parse and display all IPP attributes."""
    if len(data) < 8:
        print("  Response too short!")
        return
    
    ver_maj, ver_min, status, req_id = struct.unpack("!BBHi", data[:8])
    print(f"\n  IPP Version: {ver_maj}.{ver_min}")
    print(f"  Status Code: 0x{status:04X} ({'OK' if status == 0 else 'ERROR'})")
    print(f"  Request ID:  {req_id}")
    print()
    
    idx = 8
    current_group = ""
    current_attr_name = ""
    attr_count = 0
    
    while idx < len(data):
        tag = data[idx]
        idx += 1
        
        # Delimiter tags
        if tag <= 0x0F:
            group_name = DELIMITER_NAMES.get(tag, f"unknown-0x{tag:02X}")
            if tag == 0x03:
                print(f"  --- {group_name} ---")
                remaining = len(data) - idx
                if remaining > 0:
                    print(f"  ({remaining} bytes of document data follow)")
                break
            current_group = group_name
            print(f"\n  === {group_name} ===")
            continue
        
        # Value tag - read name-length + name + value-length + value
        if idx + 2 > len(data):
            break
        name_len = struct.unpack("!H", data[idx:idx+2])[0]
        idx += 2
        
        if name_len > 0:
            name = data[idx:idx+name_len].decode("ascii", errors="replace")
            idx += name_len
            current_attr_name = name
            attr_count += 1
        else:
            idx += name_len
            name = ""  # additional value for multi-valued attribute
        
        if idx + 2 > len(data):
            break
        val_len = struct.unpack("!H", data[idx:idx+2])[0]
        idx += 2
        
        val_data = data[idx:idx+val_len]
        idx += val_len
        
        tag_name = TAG_NAMES.get(tag, f"0x{tag:02X}")
        decoded = decode_value(tag, val_data)
        
        if name:
            print(f"    {name} ({tag_name}) = {decoded}")
        else:
            print(f"      + ({tag_name}) = {decoded}")
    
    print(f"\n  Total attributes: {attr_count}")

# Send the request
import http.client
conn = http.client.HTTPConnection(HOST, PORT, timeout=10)
req = build_request()
conn.request("POST", "/ipp/print", body=req, headers={
    "Content-Type": "application/ipp",
})
resp = conn.getresponse()
data = resp.read()

print(f"HTTP {resp.status} | Content-Type: {resp.getheader('Content-Type')} | Size: {len(data)} bytes")
parse_response(data)

# === AIRPRINT COMPLIANCE CHECKLIST ===
print("\n" + "=" * 60)
print("AIRPRINT COMPLIANCE CHECKLIST")
print("=" * 60)

required_attrs = [
    "printer-uri-supported",
    "uri-security-supported",
    "uri-authentication-supported",
    "printer-name",
    "printer-state",
    "printer-state-reasons",
    "document-format-supported",
    "color-supported",
    "print-color-mode-supported",
    "print-color-mode-default",
    "pwg-raster-document-type-supported",
    "printer-is-accepting-jobs",
    "operations-supported",
    "charset-supported",
    "natural-language-configured",
    "generated-natural-language-supported",
    "printer-make-and-model",
    "printer-uuid",
    "urf-supported",
    "pdl-override-supported",
    "printer-more-info",
    "copies-supported",
    "copies-default",
    "media-default",
    "media-supported",
    "sides-default",
    "sides-supported",
    "print-quality-default",
    "print-quality-supported",
    "pages-per-minute",
    "ipp-versions-supported",
]

response_bytes = data[8:]  # skip header
for attr in required_attrs:
    found = attr.encode("ascii") in response_bytes
    icon = "✅" if found else "❌ MISSING"
    print(f"  {icon}  {attr}")

# Check specific values
print("\n  Value checks:")
if b"image/urf" in response_bytes:
    print("  ✅  document-format-supported includes image/urf")
else:
    print("  ❌  document-format-supported MISSING image/urf")

# Check boolean encoding of color-supported
# Find color-supported in binary and check the value tag byte
idx = response_bytes.find(b"color-supported")
if idx > 0:
    # The value tag byte is 2 bytes before the name-length field
    # name-length(2) is before the name, and value-tag(1) is before that
    # But we need to search for the tag byte before the name-length
    tag_offset = idx - 2 - 1  # -2 for name-length, -1 for value-tag
    if tag_offset >= 0:
        vtag = response_bytes[tag_offset]
        if vtag == 0x22:
            print(f"  ✅  color-supported value-tag=0x{vtag:02X} (boolean)")
        else:
            print(f"  ❌  color-supported value-tag=0x{vtag:02X} (SHOULD be 0x22/boolean, got {TAG_NAMES.get(vtag, 'unknown')})")

conn.close()
