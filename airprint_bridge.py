#!/usr/bin/env python3
"""
AirPrint / IPP Bridge Server for Windows
=========================================

Advertises a PC-connected USB printer on the local network via mDNS (DNS-SD)
so that iOS (AirPrint) and Android (IPP Everywhere) devices can discover and
print to it natively — no mobile apps required.

Architecture
------------
1. Zeroconf broadcasts ``_ipp._tcp.local.`` on the LAN.
2. A lightweight HTTP server on port 631 accepts IPP ``Print-Job`` requests.
3. The document payload (PDF / JPEG / PNG) is extracted from the binary IPP
   envelope and spooled to the default Windows printer via the Win32 API.

Author : Salman Asmat
Created: 2026-08-10
License: GPL-3.0
"""

from __future__ import annotations

import atexit
import hashlib
import html
import logging
import os
import signal
import socket
import struct
import sys
import tempfile
import threading
import time
import uuid
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Optional, Tuple

# ---------------------------------------------------------------------------
# Third-party imports
# ---------------------------------------------------------------------------
try:
    from zeroconf import ServiceInfo, Zeroconf
except ImportError:
    raise SystemExit(
        "Missing dependency: zeroconf\n"
        "Install with:  pip install zeroconf"
    )

try:
    import win32api
    import win32gui
    import win32print
    import win32serviceutil
    import win32service
    import win32event
    import servicemanager
except ImportError:
    raise SystemExit(
        "Missing dependency: pywin32\n"
        "Install with:  pip install pywin32"
    )

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
VERSION: str = "1.3.1"
IPP_PORT: int = 631
IPP_SERVICE_TYPE: str = "_ipp._tcp.local."

# IPP binary protocol constants
IPP_VERSION_MAJOR: int = 1
IPP_VERSION_MINOR: int = 1

# IPP operation IDs we handle
IPP_OP_PRINT_JOB: int = 0x0002
IPP_OP_VALIDATE_JOB: int = 0x0004
IPP_OP_CANCEL_JOB: int = 0x0008
IPP_OP_GET_JOB_ATTRIBUTES: int = 0x0009
IPP_OP_GET_JOBS: int = 0x000A
IPP_OP_GET_PRINTER_ATTRIBUTES: int = 0x000B

# IPP status codes
IPP_STATUS_OK: int = 0x0000
IPP_STATUS_OK_IGNORED: int = 0x0001
IPP_STATUS_BAD_REQUEST: int = 0x0400
IPP_STATUS_NOT_FOUND: int = 0x0406
IPP_STATUS_INTERNAL_ERROR: int = 0x0500

# IPP attribute tags
IPP_TAG_OPERATION: int = 0x01
IPP_TAG_JOB: int = 0x02
IPP_TAG_END: int = 0x03
IPP_TAG_PRINTER: int = 0x04
IPP_TAG_UNSUPPORTED: int = 0x05

# IPP value tags
IPP_TAG_INTEGER: int = 0x21
IPP_TAG_BOOLEAN: int = 0x22
IPP_TAG_ENUM: int = 0x23
IPP_TAG_RANGE: int = 0x33
IPP_TAG_BEG_COLLECTION: int = 0x34
IPP_TAG_END_COLLECTION: int = 0x37
IPP_TAG_TEXT: int = 0x41
IPP_TAG_NAME: int = 0x42
IPP_TAG_KEYWORD: int = 0x44
IPP_TAG_URI: int = 0x45
IPP_TAG_URISCHEME: int = 0x46
IPP_TAG_CHARSET: int = 0x47
IPP_TAG_LANGUAGE: int = 0x48
IPP_TAG_MIMETYPE: int = 0x49
IPP_TAG_MEMBER_ATTR_NAME: int = 0x4A

# File‑type magic bytes
MAGIC_PDF: bytes = b"%PDF"
MAGIC_JPEG: bytes = b"\xFF\xD8\xFF"
MAGIC_PNG: bytes = b"\x89PNG"
MAGIC_URF: bytes = b"UNIRAST"   # Apple Raster (URF) format

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
# When frozen by PyInstaller, __file__ resolves to a temp _MEIXXXXXX dir.
# Use the executable's directory instead so logs persist across restarts.
if getattr(sys, "frozen", False):
    _app_dir = Path(sys.executable).resolve().parent
else:
    _app_dir = Path(__file__).resolve().parent

LOG_FILE: str = str(_app_dir / "airprint_bridge.log")

logger = logging.getLogger("airprint_bridge")
logger.setLevel(logging.DEBUG)

_file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
_file_handler.setLevel(logging.DEBUG)
_file_handler.setFormatter(
    logging.Formatter(
        fmt="%(asctime)s  [%(levelname)-8s]  %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
)
logger.addHandler(_file_handler)

# Prevent any output to stdout/stderr (headless-safe for pythonw.exe)
logging.getLogger().handlers = []


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def get_local_ip() -> str:
    """Return the primary LAN IPv4 address of this machine."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # Does not actually send data; used to determine the outbound iface.
        sock.connect(("8.8.8.8", 80))
        ip: str = sock.getsockname()[0]
    except OSError:
        ip = "127.0.0.1"
    finally:
        sock.close()
    logger.info("Detected local IP address: %s", ip)
    return ip


def detect_file_type(data: bytes) -> Tuple[str, str]:
    """
    Sniff the first few bytes of *data* and return ``(extension, mime_type)``.

    Falls back to ``('.bin', 'application/octet-stream')`` for unknown types.
    """
    if data[:4] == MAGIC_PDF:
        return ".pdf", "application/pdf"
    if data[:7] == MAGIC_URF:
        return ".urf", "image/urf"
    if data[:3] == MAGIC_JPEG:
        return ".jpg", "image/jpeg"
    if data[:4] == MAGIC_PNG:
        return ".png", "image/png"
    return ".bin", "application/octet-stream"


def convert_urf_to_pdf(urf_path: str) -> str:
    """
    Convert an Apple Raster (URF) file to PDF so Windows can print it.

    URF format: 'UNIRAST\x00' (8-byte header) followed by one or more
    page records.  Each page record has a 4-byte big-endian page header
    length, then pixel data.  We extract page images and compose them
    into a simple PDF.

    If the ``Pillow`` library is available, we decode the raster pages
    into images and wrap them in a PDF.  Otherwise, we fall back to
    writing raw bytes as a single-page PDF image.
    """
    pdf_path = urf_path.rsplit(".", 1)[0] + ".pdf"
    try:
        # Attempt conversion with Pillow (best quality)
        from PIL import Image
        import io

        with open(urf_path, "rb") as f:
            data = f.read()

        # URF header: 'UNIRAST\x00' (8 bytes) then page count (4 bytes BE)
        if len(data) < 12 or data[:8] != b"UNIRAST\x00":
            logger.warning("URF file has invalid header, treating as raw")
            return urf_path

        # Skip the 8-byte magic + 4-byte page count to the page data.
        # Each page has a 32-byte page header, then raw pixel data.
        # For a simpler approach, we try to find embedded JPEG or image data.
        offset = 12  # past magic + page count

        images = []
        while offset < len(data):
            # Page header is typically 32 bytes
            if offset + 32 > len(data):
                break
            # Bytes 16-19: width (BE), bytes 20-23: height (BE)
            # Byte 0: bits per pixel / color space info
            page_header = data[offset:offset + 32]
            bpp = page_header[0]  # bits per component
            color_space = page_header[1]  # 1=sGray, 3=sRGB
            # Duplex/quality fields at bytes 2,3
            width = struct.unpack("!I", page_header[16:20])[0]
            height = struct.unpack("!I", page_header[20:24])[0]
            # Resolution at bytes 8-11 (dpi)
            dpi = struct.unpack("!I", page_header[8:12])[0]
            if dpi == 0:
                dpi = 300

            offset += 32  # advance past page header

            # Determine channels and bytes per pixel
            if color_space == 1:
                channels = 1
                mode = "L"
            else:
                channels = 3
                mode = "RGB"

            row_bytes = width * channels
            page_data = bytearray()

            # URF uses a simple run-length encoding per row
            for _row in range(height):
                if offset >= len(data):
                    break
                row = bytearray()
                while len(row) < row_bytes:
                    if offset >= len(data):
                        break
                    count_byte = data[offset]
                    offset += 1
                    if count_byte == 0:
                        # Repeat the next pixel (count+1) times
                        # count_byte 0 = 1 repeat of next pixel
                        if offset + channels > len(data):
                            break
                        pixel = data[offset:offset + channels]
                        offset += channels
                        row.extend(pixel)
                    elif count_byte <= 127:
                        # Repeat next pixel (count_byte + 1) times
                        if offset + channels > len(data):
                            break
                        pixel = data[offset:offset + channels]
                        offset += channels
                        row.extend(pixel * (count_byte + 1))
                    else:
                        # (257 - count_byte) literal pixels follow
                        literal_count = 257 - count_byte
                        literal_bytes = literal_count * channels
                        if offset + literal_bytes > len(data):
                            break
                        row.extend(data[offset:offset + literal_bytes])
                        offset += literal_bytes
                # Pad or truncate to exact row width
                page_data.extend(row[:row_bytes])

            if width > 0 and height > 0 and len(page_data) >= row_bytes:
                actual_height = min(height, len(page_data) // row_bytes)
                img = Image.frombytes(
                    mode, (width, actual_height),
                    bytes(page_data[:actual_height * row_bytes]),
                )
                images.append(img)
                logger.info(
                    "URF page decoded: %dx%d %s @ %d dpi",
                    width, actual_height, mode, dpi,
                )

        if images:
            # Save all pages as a multi-page PDF
            first = images[0]
            if len(images) > 1:
                first.save(
                    pdf_path, "PDF", save_all=True,
                    append_images=images[1:], resolution=dpi,
                )
            else:
                first.save(pdf_path, "PDF", resolution=dpi)
            logger.info("URF converted to PDF: %s (%d pages)", pdf_path, len(images))
            return pdf_path

    except ImportError:
        logger.warning(
            "Pillow not installed — cannot convert URF to PDF. "
            "Install with: pip install Pillow"
        )
    except Exception:
        logger.exception("URF→PDF conversion failed")

    # Fallback: return original URF path (ShellExecute may still work
    # if the user has a URF-capable viewer installed)
    return urf_path


def get_default_printer() -> str:
    """Return the name of the Windows default printer."""
    name: str = win32print.GetDefaultPrinter()
    logger.info("Default Windows printer: %s", name)
    return name


def spool_to_printer(
    file_path: str,
    printer_name: str,
    media_size_mm: Optional[Tuple[float, float]] = None,
) -> None:
    """
    Send *file_path* to the Windows print queue of *printer_name*.

    Uses PyMuPDF to rasterize the PDF and win32ui / win32gui to send it
    directly to the printer's Device Context (DC) configured for the
    correct physical paper size.
    """
    logger.info("Spooling '%s' to printer '%s'", file_path, printer_name)

    try:
        import win32print
        import win32gui
        import win32ui
        import win32con
        import pythoncom
        import fitz
        from PIL import Image, ImageWin
    except ImportError as e:
        logger.error("Missing dependency for printing: %s", e)
        raise

    pythoncom.CoInitialize()
    try:
        # Auto-detect media size from PDF document if not explicitly supplied
        if media_size_mm is None and file_path.lower().endswith(".pdf"):
            try:
                _inspect_doc = fitz.open(file_path)
                if len(_inspect_doc) > 0:
                    _p0 = _inspect_doc.load_page(0)
                    _pdf_w_mm = _p0.rect.width * 25.4 / 72.0
                    _pdf_h_mm = _p0.rect.height * 25.4 / 72.0
                    media_size_mm = (_pdf_w_mm, _pdf_h_mm)
                    logger.info(
                        "No IPP media specified — auto-detected media size from PDF: %.1f × %.1f mm",
                        _pdf_w_mm, _pdf_h_mm,
                    )
                _inspect_doc.close()
            except Exception:
                logger.exception("Failed to auto-detect media size from PDF")

        if media_size_mm:
            logger.info(
                "Target media size: %.1f × %.1f mm",
                media_size_mm[0], media_size_mm[1],
            )

        hprinter = win32print.OpenPrinter(printer_name)
        try:
            # ----------------------------------------------------------
            # Configure DEVMODE to match the target paper size
            # ----------------------------------------------------------
            devmode = None
            if media_size_mm:
                try:
                    width_mm, height_mm = media_size_mm
                    props = win32print.GetPrinter(hprinter, 2)
                    devmode = props["pDevMode"]

                    # Set orientation: Landscape if wider than tall, else Portrait
                    if width_mm > height_mm:
                        devmode.Orientation = win32con.DMORIENT_LANDSCAPE
                    else:
                        devmode.Orientation = win32con.DMORIENT_PORTRAIT
                    devmode.Fields |= win32con.DM_ORIENTATION

                    # Search printer's supported paper forms for a size match
                    matched_id = None
                    try:
                        papers = win32print.DeviceCapabilities(printer_name, "", win32con.DC_PAPERS)
                        sizes = win32print.DeviceCapabilities(printer_name, "", win32con.DC_PAPERSIZE)
                        names = win32print.DeviceCapabilities(printer_name, "", win32con.DC_PAPERNAMES)
                        for pid, s, name in zip(papers, sizes, names):
                            pw = s["x"] / 10.0
                            ph = s["y"] / 10.0
                            if (abs(pw - width_mm) < 3.0 and abs(ph - height_mm) < 3.0) or \
                               (abs(pw - height_mm) < 3.0 and abs(ph - width_mm) < 3.0):
                                matched_id = pid
                                logger.info(
                                    "Matched printer form ID=%d (%s) for %.1f × %.1f mm",
                                    pid, name.strip(), width_mm, height_mm,
                                )
                                break
                    except Exception:
                        logger.debug("DeviceCapabilities lookup failed, trying standard forms")

                    # Fall back to standard Windows DMPAPER IDs
                    if matched_id is None:
                        standard_paper_map = {
                            (210.0, 297.0): win32con.DMPAPER_A4,
                            (148.0, 210.0): win32con.DMPAPER_A5,
                            (105.0, 148.0): win32con.DMPAPER_A6,
                            (105.0, 148.5): win32con.DMPAPER_A6,
                            (297.0, 420.0): win32con.DMPAPER_A3,
                            (215.9, 279.4): win32con.DMPAPER_LETTER,
                            (215.9, 355.6): win32con.DMPAPER_LEGAL,
                        }
                        for (sw, sh), sid in standard_paper_map.items():
                            if (abs(sw - width_mm) < 3.0 and abs(sh - height_mm) < 3.0) or \
                               (abs(sw - height_mm) < 3.0 and abs(sh - width_mm) < 3.0):
                                matched_id = sid
                                logger.info(
                                    "Matched standard Windows form ID=%d for %.1f × %.1f mm",
                                    sid, width_mm, height_mm,
                                )
                                break

                    if matched_id is not None:
                        devmode.PaperSize = matched_id
                        devmode.Fields |= win32con.DM_PAPERSIZE
                    else:
                        devmode.PaperSize = 256  # DMPAPER_USER (custom)
                        devmode.PaperWidth = int(round(width_mm * 10))
                        devmode.PaperLength = int(round(height_mm * 10))
                        devmode.Fields |= (
                            win32con.DM_PAPERSIZE
                            | win32con.DM_PAPERWIDTH
                            | win32con.DM_PAPERLENGTH
                        )

                    # Validate and merge DEVMODE with printer driver
                    try:
                        win32print.DocumentProperties(
                            0, hprinter, printer_name, devmode, devmode,
                            win32con.DM_IN_BUFFER | win32con.DM_OUT_BUFFER,
                        )
                    except Exception:
                        logger.warning("DocumentProperties merge failed, using direct DEVMODE")

                    logger.info(
                        "DEVMODE configured: PaperSize=%d, Orientation=%d",
                        devmode.PaperSize, devmode.Orientation,
                    )
                except Exception:
                    logger.exception(
                        "Failed to configure DEVMODE for media size — falling back to driver defaults"
                    )
                    devmode = None

            # ----------------------------------------------------------
            # Create the printer DC (using win32gui.CreateDC with devmode)
            # ----------------------------------------------------------
            hdc = None
            if devmode is not None:
                try:
                    hdc_handle = win32gui.CreateDC("WINSPOOL", printer_name, devmode)
                    if hdc_handle:
                        hdc = win32ui.CreateDCFromHandle(hdc_handle)
                        logger.info("Printer DC successfully created with custom DEVMODE")
                except Exception:
                    logger.exception("Failed to create DC with custom DEVMODE, trying default DC")
                    hdc = None

            if hdc is None:
                hdc = win32ui.CreateDC()
                hdc.CreatePrinterDC(printer_name)
                logger.info("Printer DC created with default settings")

            printer_dpi_x = hdc.GetDeviceCaps(win32con.LOGPIXELSX)
            printer_dpi_y = hdc.GetDeviceCaps(win32con.LOGPIXELSY)
            logger.info("Printer DPI: %d × %d", printer_dpi_x, printer_dpi_y)

            hdc.StartDoc(file_path)

            pdf_doc = fitz.open(file_path)
            for page_num in range(len(pdf_doc)):
                logger.info("Rendering page %d/%d...", page_num + 1, len(pdf_doc))
                hdc.StartPage()

                page = pdf_doc.load_page(page_num)

                printable_width = hdc.GetDeviceCaps(win32con.HORZRES)
                printable_height = hdc.GetDeviceCaps(win32con.VERTRES)
                logger.info(
                    "DC imageable area: %d × %d px  (page PDF rect: %.1f × %.1f pts)",
                    printable_width, printable_height,
                    page.rect.width, page.rect.height,
                )

                # --------------------------------------------------
                # DPI-based scaling: render at native printer DPI
                # (1 PDF pt = DPI/72 pixels).
                # --------------------------------------------------
                dpi_scale_x = printer_dpi_x / 72.0
                dpi_scale_y = printer_dpi_y / 72.0

                rendered_w = page.rect.width * dpi_scale_x
                rendered_h = page.rect.height * dpi_scale_y

                scale_w = printable_width / rendered_w if rendered_w > 0 else 1.0
                scale_h = printable_height / rendered_h if rendered_h > 0 else 1.0

                # If width matches printable area (within 5%) but height is much smaller
                # (typical for thermal label printers where the driver form height is smaller
                # than the actual label stock), DO NOT shrink width to fit height!
                if scale_w >= 0.95 and scale_h < 0.7:
                    logger.warning(
                        "Printer DC height (%d px) is significantly smaller than document height (%d px), "
                        "but width matches (%.1f%%). Preserving 1:1 scale to avoid shrunken label.",
                        printable_height, int(rendered_h), scale_w * 100.0,
                    )
                    fit_scale = min(1.0, scale_w)
                elif rendered_w > printable_width or rendered_h > printable_height:
                    fit_scale = min(scale_w, scale_h)
                    logger.info("Page exceeds printable area — shrink-to-fit scale=%.4f", fit_scale)
                else:
                    fit_scale = 1.0

                final_scale_x = dpi_scale_x * fit_scale
                final_scale_y = dpi_scale_y * fit_scale

                matrix = fitz.Matrix(final_scale_x, final_scale_y)
                pix = page.get_pixmap(matrix=matrix, alpha=False)

                img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)

                x_offset = max(0, (printable_width - pix.width) // 2)
                y_offset = max(0, (printable_height - pix.height) // 2)

                dib = ImageWin.Dib(img)
                dib.draw(
                    hdc.GetHandleOutput(),
                    (x_offset, y_offset, x_offset + pix.width, y_offset + pix.height)
                )

                hdc.EndPage()

            pdf_doc.close()
            hdc.EndDoc()
            hdc.DeleteDC()
            logger.info("Successfully spooled to printer.")
        finally:
            win32print.ClosePrinter(hprinter)
    except Exception:
        logger.exception("win32ui printing FAILED for '%s'", file_path)
        raise
    finally:
        pythoncom.CoUninitialize()



# ---------------------------------------------------------------------------
# IPP binary protocol helpers
# ---------------------------------------------------------------------------

def _encode_attribute(
    value_tag: int,
    name: str,
    value: bytes,
) -> bytes:
    """Encode a single IPP attribute into its binary wire format."""
    name_bytes = name.encode("ascii")
    return (
        struct.pack("!B", value_tag)
        + struct.pack("!H", len(name_bytes))
        + name_bytes
        + struct.pack("!H", len(value))
        + value
    )


def _encode_text_attribute(
    value_tag: int,
    name: str,
    text: str,
) -> bytes:
    """Convenience wrapper for text-valued attributes."""
    return _encode_attribute(value_tag, name, text.encode("utf-8"))


def _encode_integer_attribute(name: str, value: int) -> bytes:
    """Encode a 32-bit signed integer attribute."""
    return _encode_attribute(
        IPP_TAG_INTEGER, name, struct.pack("!i", value)
    )


def _encode_boolean_attribute(name: str, value: bool) -> bytes:
    """Encode a boolean attribute."""
    return _encode_attribute(
        IPP_TAG_BOOLEAN, name, struct.pack("!B", int(value))
    )


def _encode_enum_attribute(name: str, value: int) -> bytes:
    """Encode an enum (32-bit) attribute."""
    return _encode_attribute(
        IPP_TAG_ENUM, name, struct.pack("!i", value)
    )


def _encode_range_attribute(name: str, lower: int, upper: int) -> bytes:
    """Encode a rangeOfInteger attribute."""
    return _encode_attribute(
        IPP_TAG_RANGE, name, struct.pack("!ii", lower, upper)
    )


def _encode_additional_value(value_tag: int, value: bytes) -> bytes:
    """
    Encode an *additional value* for a multi-valued attribute.

    Per RFC 8011 §3.1.4 the name-length is 0 and no name follows.
    """
    return (
        struct.pack("!B", value_tag)
        + struct.pack("!H", 0)     # zero-length name
        + struct.pack("!H", len(value))
        + value
    )


def build_ipp_response(
    request_id: int,
    status_code: int,
    extra_groups: bytes = b"",
    version_major: int = 2,
    version_minor: int = 0,
) -> bytes:
    """
    Assemble a minimal valid IPP response.

    Parameters
    ----------
    request_id : int
        Must echo the ``request-id`` from the client's request.
    status_code : int
        IPP status-code (``0x0000`` = successful-ok).
    extra_groups : bytes
        Pre-encoded attribute groups to append between the mandatory
        operation-attributes group and the end-of-attributes tag.
    version_major : int
        IPP major version — should echo the client's request version.
    version_minor : int
        IPP minor version — should echo the client's request version.
    """
    # ---- header (8 bytes) ----
    # Per RFC 8011 §4.1.8, the response version MUST match the request.
    header = struct.pack(
        "!BBHi",
        version_major,
        version_minor,
        status_code,
        request_id,
    )

    # ---- mandatory operation attributes group ----
    body = struct.pack("!B", IPP_TAG_OPERATION)
    body += _encode_text_attribute(
        IPP_TAG_CHARSET, "attributes-charset", "utf-8"
    )
    body += _encode_text_attribute(
        IPP_TAG_LANGUAGE, "attributes-natural-language", "en-us"
    )

    # ---- optional extra attribute groups ----
    body += extra_groups

    # ---- end of attributes ----
    body += struct.pack("!B", IPP_TAG_END)

    return header + body


def parse_ipp_request(raw: bytes) -> Tuple[int, int, int, int, int]:
    """
    Parse the 8-byte IPP header.

    Returns ``(version_major, version_minor, operation_id, request_id)``.
    The combined version is also returned for back-compat logging.
    Returns a 5-tuple: ``(version_combined, op_id, req_id, ver_major, ver_minor)``.
    """
    if len(raw) < 8:
        raise ValueError("IPP payload too short (< 8 bytes)")
    ver_major, ver_minor, op_id, req_id = struct.unpack("!BBHi", raw[:8])
    return (ver_major << 8 | ver_minor), op_id, req_id, ver_major, ver_minor


def extract_document_data(raw: bytes) -> bytes:
    """
    Locate the document payload inside a ``Print-Job`` request.

    The IPP spec says document data follows the *end-of-attributes-tag*
    (``0x03``).  We search for the tag that genuinely marks the boundary
    by walking through attribute groups properly.
    """
    # Walk through the IPP attributes to find the real end-of-attributes tag.
    # The header is 8 bytes; attributes start at offset 8.
    idx = 8
    while idx < len(raw):
        tag = raw[idx]
        idx += 1

        # Delimiter tags (0x00-0x05) — they occupy a single byte.
        if tag <= 0x05:
            if tag == IPP_TAG_END:
                # Everything after this byte is document data.
                return raw[idx:]
            # Other delimiter tags (operation, job, printer, …) — continue.
            continue

        # Value tag — followed by name-length(2) + name + value-length(2) + value
        if idx + 2 > len(raw):
            break
        name_len = struct.unpack("!H", raw[idx:idx + 2])[0]
        idx += 2 + name_len
        if idx + 2 > len(raw):
            break
        value_len = struct.unpack("!H", raw[idx:idx + 2])[0]
        idx += 2 + value_len

    # Structured walk failed to find end-of-attributes tag.
    # Do NOT brute-force scan for 0x03 — it can appear inside attribute
    # values (URIs, text) and would corrupt the document boundary.
    logger.warning("Structured IPP parse did not find end-of-attributes tag")
    return b""


# ---------------------------------------------------------------------------
# IPP media-size keyword → physical dimensions (width_mm, height_mm)
# ---------------------------------------------------------------------------
# Keys are the standard IPP "media" keyword values defined in PWG 5101.1.
# Values are (width_mm, height_mm).
IPP_MEDIA_SIZES: dict[str, Tuple[float, float]] = {
    # ISO A-series
    "iso_a3_297x420mm":    (297.0, 420.0),
    "iso_a4_210x297mm":    (210.0, 297.0),
    "iso_a5_148x210mm":    (148.0, 210.0),
    "iso_a6_105x148mm":    (105.0, 148.0),
    "iso_a7_74x105mm":     (74.0,  105.0),
    "iso_a8_52x74mm":      (52.0,  74.0),
    # ISO B-series
    "iso_b5_176x250mm":    (176.0, 250.0),
    "iso_b6_125x176mm":    (125.0, 176.0),
    # ISO C-series (envelopes)
    "iso_c5_162x229mm":    (162.0, 229.0),
    "iso_c6_114x162mm":    (114.0, 162.0),
    # ISO DL envelope
    "iso_dl_110x220mm":    (110.0, 220.0),
    # North American sizes
    "na_letter_8.5x11in":  (215.9, 279.4),
    "na_legal_8.5x14in":   (215.9, 355.6),
    "na_executive_7.25x10.5in": (184.15, 266.7),
    "na_invoice_5.5x8.5in":    (139.7, 215.9),
    # Common label / receipt sizes
    "na_index-4x6_4x6in": (101.6, 152.4),
    "om_small-photo_100x150mm": (100.0, 150.0),
    "om_100x150mm_100x150mm":   (100.0, 150.0),
    "custom_4x6in_4x6in":      (101.6, 152.4),
}


def extract_ipp_job_attributes(raw: bytes) -> dict[str, str]:
    """
    Walk the IPP attribute groups in *raw* and return a dict of
    interesting job-template attributes (string-valued).

    Extracts: ``media``, ``media-type``, ``copies``, ``sides``,
    ``x-dimension``, ``y-dimension``, collection members (e.g. from ``media-col``),
    and falls back to scanning pre-document header bytes for known media keywords.
    """
    attrs: dict[str, str] = {}
    idx = 8  # skip the 8-byte IPP header

    # Value-tag ranges that encode text / keyword / name / uri etc.
    _TEXT_TAGS = {
        IPP_TAG_TEXT, IPP_TAG_NAME, IPP_TAG_KEYWORD,
        IPP_TAG_URI, IPP_TAG_URISCHEME, IPP_TAG_CHARSET,
        IPP_TAG_LANGUAGE, IPP_TAG_MIMETYPE,
    }

    current_member_name = ""

    while idx < len(raw):
        tag = raw[idx]
        idx += 1

        # Delimiter tags (0x00-0x05)
        if tag <= 0x05:
            if tag == IPP_TAG_END:
                break
            continue

        # Value tag — parse name + value
        if idx + 2 > len(raw):
            break
        name_len = struct.unpack("!H", raw[idx:idx + 2])[0]
        idx += 2
        if idx + name_len > len(raw):
            break
        attr_name = raw[idx:idx + name_len].decode("ascii", errors="replace") if name_len else ""
        idx += name_len

        if idx + 2 > len(raw):
            break
        value_len = struct.unpack("!H", raw[idx:idx + 2])[0]
        idx += 2
        if idx + value_len > len(raw):
            break
        attr_value_raw = raw[idx:idx + value_len]
        idx += value_len

        # In IPP collections, member attribute names have tag 0x4A and name_len == 0
        if tag == IPP_TAG_MEMBER_ATTR_NAME:
            current_member_name = attr_value_raw.decode("ascii", errors="replace")
            continue

        # Effective attribute name (regular or collection member)
        effective_name = attr_name or current_member_name

        if effective_name:
            if tag in _TEXT_TAGS:
                val = attr_value_raw.decode("utf-8", errors="replace")
                if effective_name in ("media-size-name", "media"):
                    attrs["media"] = val
                else:
                    attrs[effective_name] = val
            elif tag == IPP_TAG_INTEGER and value_len == 4:
                val_int = struct.unpack("!i", attr_value_raw)[0]
                attrs[effective_name] = str(val_int)

    # Fallback: If 'media' was not parsed, scan the IPP header bytes
    # (before end-of-attributes) for any known keyword in IPP_MEDIA_SIZES
    if "media" not in attrs:
        header_bytes = raw[:idx]
        for keyword in sorted(IPP_MEDIA_SIZES.keys(), key=len, reverse=True):
            if keyword.encode("ascii") in header_bytes:
                logger.info("Found media keyword '%s' via header byte scan", keyword)
                attrs["media"] = keyword
                break

    logger.info("Parsed IPP job attributes: %s", attrs)
    return attrs


# ---------------------------------------------------------------------------
# Build rich Get-Printer-Attributes response
# ---------------------------------------------------------------------------

def _build_printer_attributes(
    printer_name: str,
    host_ip: str,
) -> bytes:
    """
    Return the pre-encoded *printer-attributes* group that iOS / Android
    require in order to accept the printer as AirPrint-compatible.
    """
    hostname = socket.gethostname()
    display_name = f"{printer_name} ({hostname})"
    printer_uri = f"ipp://{host_ip}:{IPP_PORT}/ipp/print"
    attrs = struct.pack("!B", IPP_TAG_PRINTER)

    # --- Identity ---
    attrs += _encode_text_attribute(IPP_TAG_URI, "printer-uri-supported", printer_uri)
    attrs += _encode_text_attribute(IPP_TAG_KEYWORD, "uri-security-supported", "none")
    attrs += _encode_text_attribute(IPP_TAG_KEYWORD, "uri-authentication-supported", "none")
    attrs += _encode_text_attribute(IPP_TAG_NAME, "printer-name", printer_name)
    attrs += _encode_text_attribute(IPP_TAG_TEXT, "printer-info", display_name)
    attrs += _encode_text_attribute(IPP_TAG_TEXT, "printer-location", f"PC: {hostname}")
    attrs += _encode_text_attribute(IPP_TAG_TEXT, "printer-make-and-model", display_name)

    # --- AirPrint feature declaration (CRITICAL for iOS) ---
    # iOS uses this attribute to confirm AirPrint capability.
    # Without "airprint-1.8", iOS silently rejects the printer.
    attrs += _encode_text_attribute(
        IPP_TAG_KEYWORD, "ipp-features-supported", "airprint-1.8"
    )

    # --- State ---
    # 3 = idle, 4 = processing, 5 = stopped
    attrs += _encode_enum_attribute("printer-state", 3)
    attrs += _encode_text_attribute(IPP_TAG_KEYWORD, "printer-state-reasons", "none")

    # --- Capabilities ---
    # iOS AirPrint and Android IPP Everywhere / Mopria supported formats list.
    attrs += _encode_text_attribute(IPP_TAG_MIMETYPE, "document-format-supported", "application/pdf")
    attrs += _encode_additional_value(IPP_TAG_MIMETYPE, b"image/urf")
    attrs += _encode_additional_value(IPP_TAG_MIMETYPE, b"image/jpeg")
    attrs += _encode_additional_value(IPP_TAG_MIMETYPE, b"image/png")
    attrs += _encode_additional_value(IPP_TAG_MIMETYPE, b"image/pwg-raster")
    attrs += _encode_additional_value(IPP_TAG_MIMETYPE, b"application/octet-stream")

    attrs += _encode_text_attribute(IPP_TAG_MIMETYPE, "document-format-default", "application/pdf")

    # Operations supported (Print-Job, Validate-Job, Get-Printer-Attributes, Get-Jobs)
    attrs += _encode_enum_attribute("operations-supported", IPP_OP_PRINT_JOB)
    attrs += _encode_additional_value(IPP_TAG_ENUM, struct.pack("!i", IPP_OP_VALIDATE_JOB))
    attrs += _encode_additional_value(IPP_TAG_ENUM, struct.pack("!i", IPP_OP_GET_PRINTER_ATTRIBUTES))
    attrs += _encode_additional_value(IPP_TAG_ENUM, struct.pack("!i", IPP_OP_GET_JOBS))

    # Charset & language
    attrs += _encode_text_attribute(IPP_TAG_CHARSET, "charset-configured", "utf-8")
    attrs += _encode_text_attribute(IPP_TAG_CHARSET, "charset-supported", "utf-8")
    attrs += _encode_text_attribute(IPP_TAG_LANGUAGE, "natural-language-configured", "en-us")
    attrs += _encode_text_attribute(IPP_TAG_LANGUAGE, "generated-natural-language-supported", "en-us")

    # Color support — MUST be boolean per IPP spec; iOS rejects keyword encoding.
    attrs += _encode_boolean_attribute("color-supported", False)

    # Pages-per-minute (informational)
    attrs += _encode_integer_attribute("pages-per-minute", 10)

    # Detect the printer's actual active paper form in Windows DEVMODE
    default_media = "iso_a4_210x297mm"
    try:
        hprinter = win32print.OpenPrinter(printer_name)
        try:
            props = win32print.GetPrinter(hprinter, 2)
            devmode = props["pDevMode"]
            if devmode.PaperSize == win32con.DMPAPER_A4:
                default_media = "iso_a4_210x297mm"
            elif devmode.PaperSize == win32con.DMPAPER_A5:
                default_media = "iso_a5_148x210mm"
            elif devmode.PaperSize == win32con.DMPAPER_A6:
                default_media = "iso_a6_105x148mm"
            elif devmode.PaperSize == win32con.DMPAPER_LETTER:
                default_media = "na_letter_8.5x11in"
            elif devmode.PaperSize == win32con.DMPAPER_LEGAL:
                default_media = "na_legal_8.5x14in"
            elif devmode.PaperWidth > 0 and devmode.PaperLength > 0:
                pw_mm = devmode.PaperWidth / 10.0
                ph_mm = devmode.PaperLength / 10.0
                for k, (mw, mh) in IPP_MEDIA_SIZES.items():
                    if (abs(mw - pw_mm) < 3.0 and abs(mh - ph_mm) < 3.0) or \
                       (abs(mw - ph_mm) < 3.0 and abs(mh - pw_mm) < 3.0):
                        default_media = k
                        break
        finally:
            win32print.ClosePrinter(hprinter)
    except Exception:
        pass
    logger.info("Active printer default media detected: %s", default_media)

    # Media & page size — advertise sizes with default_media first
    all_media_sizes = [
        default_media,
        "iso_a4_210x297mm",
        "na_letter_8.5x11in",
        "iso_a5_148x210mm",
        "iso_a6_105x148mm",
        "iso_a7_74x105mm",
        "iso_a8_52x74mm",
        "na_legal_8.5x14in",
        "na_index-4x6_4x6in",
        "om_small-photo_100x150mm",
    ]
    # Remove duplicates while preserving order
    seen_media = set()
    unique_media = []
    for m in all_media_sizes:
        if m not in seen_media:
            seen_media.add(m)
            unique_media.append(m)

    attrs += _encode_text_attribute(IPP_TAG_KEYWORD, "media-default", unique_media[0])
    attrs += _encode_text_attribute(IPP_TAG_KEYWORD, "media-supported", unique_media[0])
    for m in unique_media[1:]:
        attrs += _encode_additional_value(IPP_TAG_KEYWORD, m.encode("ascii"))

    # media-ready — iOS 16+ requires this to show the printer.
    attrs += _encode_text_attribute(IPP_TAG_KEYWORD, "media-ready", unique_media[0])
    for m in unique_media[1:]:
        attrs += _encode_additional_value(IPP_TAG_KEYWORD, m.encode("ascii"))


    # media-col-supported — iOS 16+ checks for this collection attribute.
    attrs += _encode_text_attribute(IPP_TAG_KEYWORD, "media-col-supported", "media-size")
    attrs += _encode_additional_value(IPP_TAG_KEYWORD, b"media-type")
    attrs += _encode_additional_value(IPP_TAG_KEYWORD, b"media-source")

    # Sides (duplex) — we report simplex only for safety
    attrs += _encode_text_attribute(IPP_TAG_KEYWORD, "sides-default", "one-sided")
    attrs += _encode_text_attribute(IPP_TAG_KEYWORD, "sides-supported", "one-sided")

    # Copies
    attrs += _encode_range_attribute("copies-supported", 1, 99)
    attrs += _encode_integer_attribute("copies-default", 1)

    # IPP versions
    attrs += _encode_text_attribute(IPP_TAG_KEYWORD, "ipp-versions-supported", "1.1")
    attrs += _encode_additional_value(IPP_TAG_KEYWORD, b"2.0")

    # Multiple document handling
    attrs += _encode_text_attribute(
        IPP_TAG_KEYWORD,
        "multiple-document-jobs-supported",
        "false",
    )

    # Accepting jobs
    attrs += _encode_boolean_attribute("printer-is-accepting-jobs", True)

    # Number of queued jobs
    attrs += _encode_integer_attribute("queued-job-count", 0)

    # PDL override
    attrs += _encode_text_attribute(
        IPP_TAG_KEYWORD, "pdl-override-supported", "not-attempted"
    )

    # Compression
    attrs += _encode_text_attribute(IPP_TAG_KEYWORD, "compression-supported", "none")

    # Print quality
    attrs += _encode_enum_attribute("print-quality-default", 4)  # 4 = normal
    attrs += _encode_enum_attribute("print-quality-supported", 3)  # draft
    attrs += _encode_additional_value(IPP_TAG_ENUM, struct.pack("!i", 4))  # normal
    attrs += _encode_additional_value(IPP_TAG_ENUM, struct.pack("!i", 5))  # high

    # Printer UUID (deterministic from printer name + hostname, RFC 4122 UUID5)
    # MUST strictly match the UUID in the mDNS TXT record so Android BIPS does not reject it.
    printer_uuid = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{printer_name}@{hostname}"))
    attrs += _encode_text_attribute(
        IPP_TAG_URI, "printer-uuid", f"urn:uuid:{printer_uuid}"
    )

    # printer-more-info — iOS checks for this URI
    attrs += _encode_text_attribute(
        IPP_TAG_URI, "printer-more-info",
        f"http://{host_ip}:{IPP_PORT}/",
    )

    # URF (Apple Raster) capabilities — iOS requires this attribute.
    # W8 = max width 8 inches, SRGB24 = 24-bit sRGB, V1.4 = URF version,
    # RS300-600 = supported resolutions, DM1 = duplex mode 1 (simplex).
    attrs += _encode_text_attribute(
        IPP_TAG_KEYWORD, "urf-supported", "W8"
    )
    attrs += _encode_additional_value(IPP_TAG_KEYWORD, b"SRGB24")
    attrs += _encode_additional_value(IPP_TAG_KEYWORD, b"V1.4")
    attrs += _encode_additional_value(IPP_TAG_KEYWORD, b"RS300-600")
    attrs += _encode_additional_value(IPP_TAG_KEYWORD, b"DM1")

    # AirPrint-specific: printer-type flags
    # Bit 0 = local, Bit 2 = can print — 0x05 covers the basics
    attrs += _encode_integer_attribute("printer-type", 0x00000005)

    return attrs


# ---------------------------------------------------------------------------
# IPP-aware HTTP request handler
# ---------------------------------------------------------------------------

class IPPRequestHandler(BaseHTTPRequestHandler):
    """
    Minimal HTTP handler that speaks enough IPP to satisfy AirPrint and
    Android's built-in IPP client.
    """

    # Reference to the Zeroconf-registered printer name (set on class).
    printer_name: str = ""
    host_ip: str = "127.0.0.1"

    # Silence the default stderr logging — we log to file.
    def log_message(self, fmt: str, *args: object) -> None:  # noqa: D401
        logger.info("HTTP  %s  %s", self.address_string(), fmt % args)

    # Support HTTP/1.1 so that we can handle Expect: 100-continue and Chunked transfer
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------------ #
    # POST — the only verb iOS / Android use for IPP                      #
    # ------------------------------------------------------------------ #
    def do_POST(self) -> None:  # noqa: N802
        logger.info("Headers received from %s:\n%s", self.client_address[0], self.headers)
        
        # Handle chunked transfer encoding (common in iOS AirPrint for large jobs)
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            logger.info("Client is using chunked transfer encoding!")
            raw = bytearray()
            while True:
                line = self.rfile.readline().strip()
                if not line:
                    continue
                chunk_size = int(line, 16)
                if chunk_size == 0:
                    self.rfile.readline()  # Read trailing \r\n
                    break
                raw.extend(self.rfile.read(chunk_size))
                self.rfile.readline()  # Read trailing \r\n
            raw = bytes(raw)
            content_length = len(raw)
        else:
            content_length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(content_length)

        logger.info(
            "POST %s  Content-Length=%d  from %s",
            self.path,
            content_length,
            self.client_address[0],
        )

        if len(raw) < 8:
            self._send_ipp_error(0, IPP_STATUS_BAD_REQUEST)
            return

        try:
            _version, op_id, req_id, ver_maj, ver_min = parse_ipp_request(raw)
        except ValueError as exc:
            logger.warning("Malformed IPP header: %s", exc)
            self._send_ipp_error(0, IPP_STATUS_BAD_REQUEST)
            return

        logger.info(
            "IPP %d.%d  operation=0x%04X  request-id=%d",
            ver_maj, ver_min, op_id, req_id,
        )

        if op_id == IPP_OP_PRINT_JOB:
            self._handle_print_job(raw, req_id, ver_maj, ver_min)
        elif op_id == IPP_OP_VALIDATE_JOB:
            self._handle_validate_job(req_id, ver_maj, ver_min)
        elif op_id == IPP_OP_GET_PRINTER_ATTRIBUTES:
            self._handle_get_printer_attributes(req_id, ver_maj, ver_min)
        elif op_id == IPP_OP_GET_JOBS:
            self._handle_get_jobs(req_id, ver_maj, ver_min)
        elif op_id == IPP_OP_GET_JOB_ATTRIBUTES:
            self._handle_get_job_attributes(req_id, ver_maj, ver_min)
        elif op_id == IPP_OP_CANCEL_JOB:
            self._handle_cancel_job(req_id, ver_maj, ver_min)
        else:
            logger.warning("Unsupported IPP operation 0x%04X", op_id)
            self._send_ipp_error(req_id, IPP_STATUS_BAD_REQUEST)

    # ------------------------------------------------------------------ #
    # GET — some clients probe the root or /ipp/print via GET             #
    # ------------------------------------------------------------------ #
    def do_GET(self) -> None:  # noqa: N802
        logger.info(
            "GET %s  from %s", self.path, self.client_address[0]
        )
        # Return a simple human-readable status page.
        safe_name = html.escape(self.printer_name)
        body = (
            "<html><body>"
            "<h1>AirPrint Bridge</h1>"
            f"<p>Printer: <strong>{safe_name}</strong></p>"
            "<p>IPP endpoint: <code>POST /ipp/print</code></p>"
            "</body></html>"
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ------------------------------------------------------------------ #
    # IPP operation handlers                                              #
    # ------------------------------------------------------------------ #

    def _handle_print_job(
        self, raw: bytes, req_id: int, ver_maj: int, ver_min: int,
    ) -> None:
        """Extract the document payload and spool it."""
        # ---- Parse IPP job attributes (media, copies, etc.) ----
        job_attrs_parsed = extract_ipp_job_attributes(raw)
        media_keyword = job_attrs_parsed.get("media", "")
        media_size_mm: Optional[Tuple[float, float]] = None
        if media_keyword:
            media_size_mm = IPP_MEDIA_SIZES.get(media_keyword)
            if media_size_mm:
                logger.info(
                    "IPP media='%s' → %.1f × %.1f mm",
                    media_keyword, media_size_mm[0], media_size_mm[1],
                )
            else:
                logger.warning(
                    "IPP media='%s' not found in lookup table",
                    media_keyword,
                )
        elif "x-dimension" in job_attrs_parsed and "y-dimension" in job_attrs_parsed:
            try:
                x_dim = int(job_attrs_parsed["x-dimension"]) / 100.0
                y_dim = int(job_attrs_parsed["y-dimension"]) / 100.0
                if x_dim > 0 and y_dim > 0:
                    media_size_mm = (x_dim, y_dim)
                    logger.info(
                        "Parsed media-col dimensions: %.1f × %.1f mm",
                        x_dim, y_dim,
                    )
            except (ValueError, TypeError):
                pass

        doc_data = extract_document_data(raw)
        if not doc_data:
            logger.error("No document data found in Print-Job payload")
            self._send_ipp_error(req_id, IPP_STATUS_BAD_REQUEST)
            return

        ext, mime = detect_file_type(doc_data)
        logger.info(
            "Document extracted: %d bytes, type=%s (%s)",
            len(doc_data),
            ext,
            mime,
        )

        # Write to a temp file with unpredictable name (avoid TOCTOU race)
        tmp_path = None
        spool_path = None
        try:
            tmp_fd = tempfile.NamedTemporaryFile(
                delete=False, suffix=ext, prefix="airprint_"
            )
            tmp_path = tmp_fd.name
            tmp_fd.write(doc_data)
            tmp_fd.close()
            logger.info("Temp file written: %s", tmp_path)

            # Convert Apple Raster (URF) to PDF — Windows can't print URF natively.
            spool_path = tmp_path
            if ext == ".urf":
                spool_path = convert_urf_to_pdf(tmp_path)
                logger.info("Spool path after conversion: %s", spool_path)

            # Spool — pass the media size so the printer DC gets the right DEVMODE
            spool_to_printer(spool_path, self.printer_name, media_size_mm=media_size_mm)


        except OSError:
            logger.exception("Failed to write temp file")
            self._send_ipp_error(req_id, IPP_STATUS_INTERNAL_ERROR)
            return
        except Exception:
            logger.exception("Spooling failed")
            self._send_ipp_error(req_id, IPP_STATUS_INTERNAL_ERROR)
            return
        finally:
            # Clean up temp files
            for path in (tmp_path, spool_path):
                if path and os.path.exists(path):
                    try:
                        os.remove(path)
                    except OSError:
                        pass

        # Build a successful response with a job-attributes group
        job_attrs = struct.pack("!B", IPP_TAG_JOB)
        job_attrs += _encode_integer_attribute("job-id", req_id)
        job_attrs += _encode_text_attribute(
            IPP_TAG_URI,
            "job-uri",
            f"ipp://{self.host_ip}:{IPP_PORT}/ipp/print/job/{req_id}",
        )
        job_attrs += _encode_enum_attribute("job-state", 9)  # 9 = completed

        response = build_ipp_response(
            req_id, IPP_STATUS_OK, extra_groups=job_attrs,
            version_major=ver_maj, version_minor=ver_min,
        )
        self._send_raw_ipp(response)
        logger.info("Print-Job #%d accepted and spooled successfully", req_id)

    def _handle_validate_job(
        self, req_id: int, ver_maj: int, ver_min: int,
    ) -> None:
        """Respond to Validate-Job with successful-ok."""
        response = build_ipp_response(
            req_id, IPP_STATUS_OK,
            version_major=ver_maj, version_minor=ver_min,
        )
        self._send_raw_ipp(response)
        logger.info("Validate-Job #%d → successful-ok", req_id)

    def _handle_get_printer_attributes(
        self, req_id: int, ver_maj: int, ver_min: int,
    ) -> None:
        """Return a rich set of printer attributes for discovery."""
        attrs = _build_printer_attributes(self.printer_name, self.host_ip)
        response = build_ipp_response(
            req_id, IPP_STATUS_OK, extra_groups=attrs,
            version_major=ver_maj, version_minor=ver_min,
        )
        self._send_raw_ipp(response)
        logger.info("Get-Printer-Attributes #%d → sent attributes", req_id)

    def _handle_get_jobs(
        self, req_id: int, ver_maj: int, ver_min: int,
    ) -> None:
        """Return an empty job list (we don't queue)."""
        response = build_ipp_response(
            req_id, IPP_STATUS_OK,
            version_major=ver_maj, version_minor=ver_min,
        )
        self._send_raw_ipp(response)
        logger.info("Get-Jobs #%d → empty list", req_id)

    def _handle_get_job_attributes(
        self, req_id: int, ver_maj: int, ver_min: int,
    ) -> None:
        """Return job-state = completed for any job ID iOS queries."""
        job_attrs = struct.pack("!B", IPP_TAG_JOB)
        job_attrs += _encode_integer_attribute("job-id", req_id)
        job_attrs += _encode_enum_attribute("job-state", 9)  # completed
        job_attrs += _encode_text_attribute(
            IPP_TAG_KEYWORD, "job-state-reasons", "job-completed-successfully"
        )
        response = build_ipp_response(
            req_id, IPP_STATUS_OK, extra_groups=job_attrs,
            version_major=ver_maj, version_minor=ver_min,
        )
        self._send_raw_ipp(response)
        logger.info("Get-Job-Attributes #%d → completed", req_id)

    def _handle_cancel_job(
        self, req_id: int, ver_maj: int, ver_min: int,
    ) -> None:
        """Acknowledge a Cancel-Job request (job already printed)."""
        response = build_ipp_response(
            req_id, IPP_STATUS_OK,
            version_major=ver_maj, version_minor=ver_min,
        )
        self._send_raw_ipp(response)
        logger.info("Cancel-Job #%d → acknowledged", req_id)

    # ------------------------------------------------------------------ #
    # Low-level response helpers                                          #
    # ------------------------------------------------------------------ #

    def _send_raw_ipp(self, data: bytes) -> None:
        """Send a raw IPP binary response over HTTP 200."""
        self.send_response(200)
        self.send_header("Content-Type", "application/ipp")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_ipp_error(self, req_id: int, status: int) -> None:
        """Send a minimal IPP error response."""
        response = build_ipp_response(req_id, status)
        self.send_response(200)  # IPP errors still ride on HTTP 200
        self.send_header("Content-Type", "application/ipp")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)


# ---------------------------------------------------------------------------
# Threaded HTTP server wrapper
# ---------------------------------------------------------------------------

class ThreadedIPPServer(HTTPServer):
    """HTTPServer that handles each request in a new daemon thread."""

    allow_reuse_address = True
    daemon_threads = True

    def process_request(self, request, client_address) -> None:  # type: ignore[override]
        """Start a daemon thread for each incoming connection."""
        t = threading.Thread(
            target=self.process_request_thread,
            args=(request, client_address),
            daemon=True,
        )
        t.start()

    def process_request_thread(self, request, client_address) -> None:  # type: ignore[override]
        """Handle one request then close the socket."""
        try:
            self.finish_request(request, client_address)
        except Exception:
            self.handle_error(request, client_address)
        finally:
            self.shutdown_request(request)


# ---------------------------------------------------------------------------
# mDNS / DNS-SD registration
# ---------------------------------------------------------------------------

class MDNSAdvertiser:
    """
    Registers (and later unregisters) an ``_ipp._tcp.local.`` service
    using the ``zeroconf`` library so that AirPrint / IPP Everywhere
    clients discover the printer automatically.

    Also registers the ``_universal._sub._ipp._tcp.local.`` subtype
    which iOS requires to classify the service as AirPrint-compatible.
    """

    def __init__(
        self,
        printer_name: str,
        host_ip: str,
        port: int = IPP_PORT,
    ) -> None:
        self._zc: Optional[Zeroconf] = None
        self._zc_subtype: Optional[Zeroconf] = None
        self._info: Optional[ServiceInfo] = None
        self._subtype_info: Optional[ServiceInfo] = None
        self._printer_name = printer_name
        self._host_ip = host_ip
        self._port = port

    def register(self) -> None:
        """Broadcast the service on the LAN."""
        hostname = socket.gethostname()
        display_name = f"{self._printer_name} ({hostname})"

        # Clean display name for mDNS instance label (allow spaces, escape dot/slashes, limit length)
        clean_instance = (
            display_name.replace("/", "_")
            .replace("\\", "_")
            .replace(".", "_")[:60]
        )
        service_name = f"{clean_instance}.{IPP_SERVICE_TYPE}"

        # Clean host name for target DNS host (must be a valid single-label DNS host)
        safe_host = (
            hostname.replace(" ", "-")
            .replace("/", "-")
            .replace("\\", "-")
            .replace(".", "-")[:60]
        )

        # Unique UUID per printer + machine so multiple PCs don't collide on AirPrint clients
        printer_uuid_str = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{self._printer_name}@{hostname}"))

        txt_props = {
            "txtvers": "1",
            "qtotal": "1",
            "rp": "ipp/print",
            "ty": display_name,
            "product": f"({self._printer_name})",
            "note": f"AirPrint Bridge on {hostname}",
            "pdl": "application/pdf,image/urf,image/jpeg,image/png,image/pwg-raster",
            "Color": "T",                         # Must match SRGB24 in URF
            "Duplex": "F",
            "adminurl": f"http://{self._host_ip}:{self._port}/",
            "priority": "50",
            "URF": "W8,SRGB24,V1.4,RS300-600,DM1",
            "UUID": printer_uuid_str,
            "TLS": "none",
        }

        # --- Primary service: _ipp._tcp.local. ---
        self._info = ServiceInfo(
            type_=IPP_SERVICE_TYPE,
            name=service_name,
            addresses=[socket.inet_aton(self._host_ip)],
            port=self._port,
            properties=txt_props,
            server=f"{safe_host}.local.",
        )

        # Force Zeroconf to bind specifically to the Wi-Fi interface!
        # Windows often routes multicast traffic to the Ethernet/VPN adapter by default.
        self._zc = Zeroconf(interfaces=[self._host_ip])
        self._zc.register_service(self._info, strict=False)
        logger.info(
            "mDNS service registered: %s  (IP=%s  port=%d)",
            service_name,
            self._host_ip,
            self._port,
        )

        # --- AirPrint subtype: _universal._sub._ipp._tcp.local. ---
        airprint_subtype = f"_universal._sub.{IPP_SERVICE_TYPE}"

        self._subtype_info = ServiceInfo(
            type_=airprint_subtype,
            name=service_name,  # MUST use the primary name here!
            addresses=[socket.inet_aton(self._host_ip)],
            port=self._port,
            properties=txt_props,
            server=f"{safe_host}.local.",
        )
        try:
            self._zc_subtype = Zeroconf(interfaces=[self._host_ip])
            self._zc_subtype.register_service(self._subtype_info, strict=False)
            logger.info("mDNS subtype registered: %s", airprint_subtype)
        except Exception:
            logger.exception("Failed to register _universal subtype")


    def unregister(self) -> None:
        """Remove the service from the LAN."""
        if self._zc or self._zc_subtype:
            logger.info("Unregistering mDNS service …")
            
            if self._subtype_info is not None and self._zc_subtype is not None:
                try:
                    self._zc_subtype.unregister_service(self._subtype_info)
                except Exception:
                    pass
            
            if self._info is not None and self._zc is not None:
                try:
                    self._zc.unregister_service(self._info)
                except Exception:
                    pass

            try:
                if self._zc:
                    self._zc.close()
            except Exception:
                pass
            try:
                if self._zc_subtype:
                    self._zc_subtype.close()
            except Exception:
                pass

            self._zc = None
            self._zc_subtype = None
            self._info = None
            self._subtype_info = None
            logger.info("mDNS service unregistered.")


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def main(shutdown_event: threading.Event) -> None:
    """Start the AirPrint bridge server."""
    logger.info("=" * 60)
    logger.info("AirPrint Bridge starting up")
    logger.info("=" * 60)

    # ---- Detect environment ----
    host_ip = get_local_ip()
    printer_name = get_default_printer()
    if not printer_name:
        logger.critical("No default printer configured — aborting.")
        sys.exit(1)

    # ---- Configure the request handler class ----
    IPPRequestHandler.printer_name = printer_name
    IPPRequestHandler.host_ip = host_ip

    # ---- Start mDNS advertiser ----
    mdns = MDNSAdvertiser(printer_name, host_ip, IPP_PORT)
    mdns.register()

    # ---- Start HTTP / IPP server ----
    server = ThreadedIPPServer(("0.0.0.0", IPP_PORT), IPPRequestHandler)
    logger.info("IPP server listening on 0.0.0.0:%d", IPP_PORT)

    def _cleanup() -> None:
        """atexit hook — ensures mDNS is always unregistered."""
        mdns.unregister()
        logger.info("AirPrint Bridge shut down cleanly.")

    atexit.register(_cleanup)

    # Run the server in a background thread so we can wait on the event.
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    logger.info("Server thread started — ready to accept print jobs.")

    try:
        # Block the main thread until a shutdown signal fires.
        while not shutdown_event.is_set():
            shutdown_event.wait(timeout=1.0)
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt caught — shutting down …")
    finally:
        server.shutdown()
        _cleanup()


class AirPrintBridgeService(win32serviceutil.ServiceFramework):
    _svc_name_ = "AirPrintBridge"
    _svc_display_name_ = "AirPrint Bridge Service"
    _svc_description_ = "Advertises local printers to Apple devices via mDNS and IPP."

    def __init__(self, args):
        win32serviceutil.ServiceFramework.__init__(self, args)
        self.hWaitStop = win32event.CreateEvent(None, 0, 0, None)
        self.shutdown_event = threading.Event()

    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        win32event.SetEvent(self.hWaitStop)
        self.shutdown_event.set()

    def SvcDoRun(self):
        servicemanager.LogMsg(
            servicemanager.EVENTLOG_INFORMATION_TYPE,
            servicemanager.PYS_SERVICE_STARTED,
            (self._svc_name_, '')
        )
        main(self.shutdown_event)


def _run_interactive() -> None:
    """Run the server interactively (not as a Windows service)."""
    shutdown_event = threading.Event()

    def _on_signal(signum=None, frame=None):
        shutdown_event.set()

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _on_signal)

    main(shutdown_event)


if __name__ == "__main__":
    import pywintypes
    if len(sys.argv) == 1:
        try:
            # Run natively as a Windows Service
            servicemanager.Initialize()
            servicemanager.PrepareToHostSingle(AirPrintBridgeService)
            servicemanager.StartServiceCtrlDispatcher()
        except pywintypes.error as e:
            if e.winerror == 1063:
                print("Running interactively (not started by Service Control Manager)...")
                _run_interactive()
            else:
                raise
    else:
        # Command-line usage
        if sys.argv[1] == 'debug':
            _run_interactive()
        else:
            win32serviceutil.HandleCommandLine(AirPrintBridgeService)
