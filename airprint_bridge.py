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
import json
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
import winreg
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Optional, Tuple

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
    import pywintypes
    import win32api
    import win32con
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
VERSION: str = "1.4.2"
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
MAGIC_PWG: bytes = b"RaS2"      # PWG Raster format

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
# Configuration
# ---------------------------------------------------------------------------
CONFIG_FILE: Path = _app_dir / "config.json"

_CONFIG_DEFAULTS: dict[str, Any] = {
    "printer_name": "",
    "max_render_dpi": 300,
    "reverse_page_order": False,
    "auto_rotate_to_fit": False,
}


def load_config() -> dict[str, Any]:
    """Load config.json, creating it with defaults if it doesn't exist."""
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                user_config = json.load(f)
            merged = {**_CONFIG_DEFAULTS, **user_config}
            logger.debug("Loaded configuration from %s: %s", CONFIG_FILE, merged)
            return merged
        except Exception:
            logger.exception("Failed to load config.json, using defaults")
    else:
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(_CONFIG_DEFAULTS, f, indent=4)
            logger.info("Created default config.json at %s", CONFIG_FILE)
        except Exception:
            logger.debug("Could not create default config.json: %s", CONFIG_FILE)
    return dict(_CONFIG_DEFAULTS)


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
    if data[:4] == MAGIC_PWG:
        return ".pwg", "image/pwg-raster"
    if data[:3] == MAGIC_JPEG:
        return ".jpg", "image/jpeg"
    if data[:4] == MAGIC_PNG:
        return ".png", "image/png"
    return ".bin", "application/octet-stream"


def _decode_packbits_raster_page(
    data: bytes,
    offset: int,
    width: int,
    height: int,
    channels: int,
    fill_byte: int = 0xFF,
) -> Tuple[bytes, int]:
    """
    Decode a single URF or PWG Raster page encoded with per-line repeat counts
    and PackBits RLE pixel runs.

    Per the Apple URF / PWG 5102.4 specification:
    - Each scanline starts with a 1-byte ``line_repeat`` count; the decoded row
      is repeated ``line_repeat + 1`` times.
    - Within a scanline, each control byte ``code`` means:
      - ``0x00..0x7F`` (0..127): repeat the next 1 pixel ``code + 1`` times.
      - ``0x80`` (128): fill the remainder of the current scanline with white
        (``fill_byte``).
      - ``0x81..0xFF`` (129..255): copy ``(257 - code)`` literal pixels.
    """
    row_bytes = width * channels
    page_data = bytearray()
    rows_decoded = 0
    fill_pixel = bytes([fill_byte]) * channels

    while rows_decoded < height and offset < len(data):
        line_repeat = data[offset] + 1
        offset += 1

        row = bytearray()
        while len(row) < row_bytes and offset < len(data):
            code = data[offset]
            offset += 1
            if code == 0x80:
                # Fill remainder of scanline with white/background
                remaining_pixels = (row_bytes - len(row)) // channels
                if remaining_pixels > 0:
                    row.extend(fill_pixel * remaining_pixels)
                break
            elif code <= 0x7F:
                repeat_count = code + 1
                if offset + channels > len(data):
                    break
                pixel = data[offset:offset + channels]
                offset += channels
                row.extend(pixel * repeat_count)
            else:
                literal_count = 257 - code
                literal_bytes = literal_count * channels
                if offset + literal_bytes > len(data):
                    avail = len(data) - offset
                    row.extend(data[offset:offset + avail])
                    offset = len(data)
                    break
                row.extend(data[offset:offset + literal_bytes])
                offset += literal_bytes

        if len(row) < row_bytes:
            row.extend(bytes([fill_byte]) * (row_bytes - len(row)))
        elif len(row) > row_bytes:
            del row[row_bytes:]

        repeat_actual = min(line_repeat, height - rows_decoded)
        for _ in range(repeat_actual):
            page_data.extend(row)
        rows_decoded += repeat_actual

    return bytes(page_data), offset


def convert_urf_to_pdf(urf_path: str) -> str:
    """
    Convert an Apple Raster (URF) file to PDF so Windows can print it.

    URF format: 'UNIRAST\\x00' (8-byte header) followed by a 4-byte BE page
    count and one or more page records (each starting with a 32-byte header).
    """
    pdf_path = urf_path.rsplit(".", 1)[0] + ".pdf"
    try:
        from PIL import Image

        with open(urf_path, "rb") as f:
            data = f.read()

        if len(data) < 12 or data[:8] != b"UNIRAST\x00":
            logger.warning("URF file has invalid header, treating as raw")
            return urf_path

        offset = 12  # past 8-byte magic + 4-byte page count
        images = []
        last_dpi = 300

        while offset + 32 <= len(data):
            page_header = data[offset:offset + 32]
            bpp = page_header[0]          # 8=Gray, 24=RGB, 32=RGBA/CMYK
            color_space = page_header[1]  # 0=sGray, 1=sRGB, 3=AdobeRGB
            dpi = struct.unpack("!I", page_header[8:12])[0] or 300
            width = struct.unpack("!I", page_header[16:20])[0]
            height = struct.unpack("!I", page_header[20:24])[0]
            offset += 32

            if width == 0 or height == 0:
                break

            last_dpi = dpi
            if bpp == 8 or color_space == 0:
                channels = 1
                mode = "L"
            elif bpp == 32:
                channels = 4
                mode = "RGBA"
            else:
                channels = 3
                mode = "RGB"

            page_bytes, offset = _decode_packbits_raster_page(
                data, offset, width, height, channels, fill_byte=0xFF
            )
            row_bytes = width * channels
            if len(page_bytes) >= row_bytes:
                actual_height = min(height, len(page_bytes) // row_bytes)
                img = Image.frombytes(
                    mode,
                    (width, actual_height),
                    page_bytes[:actual_height * row_bytes],
                )
                if mode == "RGBA":
                    img = img.convert("RGB")
                images.append(img)
                logger.info(
                    "URF page decoded: %dx%d %s (bpp=%d, cs=%d) @ %d dpi",
                    width, actual_height, mode, bpp, color_space, dpi,
                )

        if images:
            first = images[0]
            if len(images) > 1:
                first.save(
                    pdf_path, "PDF", save_all=True,
                    append_images=images[1:], resolution=last_dpi,
                )
            else:
                first.save(pdf_path, "PDF", resolution=last_dpi)
            logger.info("URF converted to PDF: %s (%d pages)", pdf_path, len(images))
            return pdf_path

    except ImportError:
        logger.warning(
            "Pillow not installed — cannot convert URF to PDF. "
            "Install with: pip install Pillow"
        )
    except Exception:
        logger.exception("URF→PDF conversion failed")

    return urf_path


def convert_pwg_to_pdf(pwg_path: str) -> str:
    """
    Convert a PWG Raster ('RaS2') file to PDF so Windows can print it.

    PWG Raster format (PWG 5102.4): 4-byte 'RaS2' magic followed by one or
    more pages, each with a 1796-byte page header and PackBits-encoded rows.
    """
    pdf_path = pwg_path.rsplit(".", 1)[0] + ".pdf"
    try:
        from PIL import Image

        with open(pwg_path, "rb") as f:
            data = f.read()

        if len(data) < 1800 or data[:4] != MAGIC_PWG:
            logger.warning("PWG Raster file has invalid header, treating as raw")
            return pwg_path

        offset = 4
        images = []
        last_dpi = 300

        while offset + 1796 <= len(data):
            hdr = data[offset:offset + 1796]
            dpi_x = struct.unpack("!I", hdr[276:280])[0] or 300
            width = struct.unpack("!I", hdr[372:376])[0]
            height = struct.unpack("!I", hdr[376:380])[0]
            bpp = struct.unpack("!I", hdr[388:392])[0]
            color_space = struct.unpack("!I", hdr[400:404])[0]
            offset += 1796

            if width == 0 or height == 0:
                break

            last_dpi = dpi_x
            if bpp == 8:
                channels = 1
                mode = "L"
                # ColorSpace 3 = Black (0=white, 255=black); 18 = sGray (255=white)
                fill_byte = 0x00 if color_space == 3 else 0xFF
            elif bpp == 32:
                channels = 4
                mode = "RGBA"
                fill_byte = 0xFF
            else:
                channels = 3
                mode = "RGB"
                fill_byte = 0xFF

            page_bytes, offset = _decode_packbits_raster_page(
                data, offset, width, height, channels, fill_byte=fill_byte
            )
            row_bytes = width * channels
            if len(page_bytes) >= row_bytes:
                actual_height = min(height, len(page_bytes) // row_bytes)
                img = Image.frombytes(
                    mode,
                    (width, actual_height),
                    page_bytes[:actual_height * row_bytes],
                )
                if mode == "L" and color_space == 3:
                    from PIL import ImageOps
                    img = ImageOps.invert(img)
                elif mode == "RGBA":
                    img = img.convert("RGB")
                images.append(img)
                logger.info(
                    "PWG page decoded: %dx%d %s (bpp=%d, cs=%d) @ %d dpi",
                    width, actual_height, mode, bpp, color_space, dpi_x,
                )

        if images:
            first = images[0]
            if len(images) > 1:
                first.save(
                    pdf_path, "PDF", save_all=True,
                    append_images=images[1:], resolution=last_dpi,
                )
            else:
                first.save(pdf_path, "PDF", resolution=last_dpi)
            logger.info("PWG Raster converted to PDF: %s (%d pages)", pdf_path, len(images))
            return pdf_path

    except Exception:
        logger.exception("PWG→PDF conversion failed")

    return pwg_path


_VIRTUAL_PRINTER_PREFIXES: Tuple[str, ...] = (
    "microsoft print to pdf",
    "microsoft xps document writer",
    "onenote",
    "send to onenote",
    "fax",
)


def _is_virtual_printer(name: str) -> bool:
    """Return True if *name* is a built-in Windows virtual/non-physical printer."""
    lower = name.strip().lower()
    return any(lower.startswith(prefix) for prefix in _VIRTUAL_PRINTER_PREFIXES)


def _get_installed_printers() -> list[str]:
    """Return a list of all currently installed local and network printers."""
    printers: list[str] = []
    try:
        flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
        for entry in win32print.EnumPrinters(flags):
            if len(entry) > 2 and entry[2]:
                printers.append(entry[2])
    except Exception:
        logger.debug("EnumPrinters failed", exc_info=True)
    return printers


def _get_user_registry_default_printers() -> list[str]:
    """
    Inspect interactive user profiles in ``HKEY_USERS\\<S-1-5-21-*>`` to find
    the logged-in user's default printer when running as the ``SYSTEM`` service
    (Issue #7).
    """
    results: list[str] = []
    try:
        idx = 0
        while True:
            try:
                sid = winreg.EnumKey(winreg.HKEY_USERS, idx)
                idx += 1
            except OSError:
                break
            if not sid.startswith("S-1-5-21-") or sid.endswith("_Classes"):
                continue
            subkey_path = f"{sid}\\Software\\Microsoft\\Windows NT\\CurrentVersion\\Windows"
            try:
                with winreg.OpenKey(winreg.HKEY_USERS, subkey_path, 0, winreg.KEY_READ) as k:
                    device_val, _ = winreg.QueryValueEx(k, "Device")
                    if isinstance(device_val, str) and device_val.strip():
                        printer_name = device_val.split(",")[0].strip()
                        if printer_name and printer_name not in results:
                            results.append(printer_name)
            except OSError:
                continue
    except Exception:
        logger.debug("Could not query HKEY_USERS for user default printer", exc_info=True)
    return results


def get_default_printer() -> str:
    """
    Resolve the target Windows printer name (Issue #7).

    Priority:
    1. Explicit ``printer_name`` in ``config.json`` (if set).
    2. Interactive user's default printer from ``HKEY_USERS\\<S-1-5-21-*>``
       (avoids ``NT AUTHORITY\\SYSTEM`` registry staying stuck on
       ``Microsoft Print to PDF`` when running as a Windows Service).
    3. ``win32print.GetDefaultPrinterW()`` / ``GetDefaultPrinter()``.
    4. Validates against ``EnumPrinters`` so deleted printers are never chosen,
       and prefers physical printers over built-in Windows virtual printers.
    """
    config = load_config()
    installed = _get_installed_printers()
    installed_map = {p.lower(): p for p in installed}
    if installed:
        logger.info("Installed Windows printers: %s", installed)

    # 1. Explicit config override
    configured_name = str(config.get("printer_name", "") or "").strip()
    if configured_name:
        if configured_name.lower() in installed_map:
            resolved = installed_map[configured_name.lower()]
            logger.info("Using printer configured in config.json: %s", resolved)
            return resolved
        logger.warning(
            "Configured printer '%s' not found in installed printers %s",
            configured_name, installed,
        )
        if not installed:
            return configured_name

    # 2. Collect default printer candidates (interactive user SIDs first, then current context)
    candidates: list[str] = []
    for reg_printer in _get_user_registry_default_printers():
        if reg_printer not in candidates:
            candidates.append(reg_printer)

    for getter_name in ("GetDefaultPrinterW", "GetDefaultPrinter"):
        getter = getattr(win32print, getter_name, None)
        if callable(getter):
            try:
                p = getter()
                if p and p not in candidates:
                    candidates.append(p)
            except Exception:
                pass

    logger.debug("Default printer candidates: %s", candidates)

    # Filter candidates to printers that actually exist on the system
    if installed:
        valid_candidates = [
            installed_map[c.lower()]
            for c in candidates
            if c.lower() in installed_map
        ]
    else:
        valid_candidates = list(candidates)

    # Prefer a non-virtual candidate
    for cand in valid_candidates:
        if not _is_virtual_printer(cand):
            logger.info("Default Windows printer: %s", cand)
            return cand

    # If the default candidate is virtual (e.g. Microsoft Print to PDF) or was
    # deleted, check if a real physical printer is installed and prefer it.
    physical_installed = [p for p in installed if not _is_virtual_printer(p)]
    if physical_installed:
        chosen = physical_installed[0]
        logger.info(
            "Default printer candidate (%s) is virtual or missing; "
            "selected installed physical printer: %s",
            candidates[0] if candidates else "none",
            chosen,
        )
        return chosen

    if valid_candidates:
        logger.info("Default Windows printer (virtual fallback): %s", valid_candidates[0])
        return valid_candidates[0]

    if installed:
        logger.info("Default Windows printer (installed fallback): %s", installed[0])
        return installed[0]

    fallback = candidates[0] if candidates else ""
    logger.info("Default Windows printer: %s", fallback)
    return fallback


def _get_printer_devmode(hprinter: int, printer_name: str):
    """
    Retrieve a complete driver-initialized ``PyDEVMODE`` for *printer_name*
    using the 3-step Win32 ``DocumentProperties`` sequence, falling back to
    ``GetPrinter(hprinter, 2)``.
    """
    try:
        size = win32print.DocumentProperties(0, hprinter, printer_name, None, None, 0)
        if size > 0:
            base_size = pywintypes.DEVMODEType().Size
            extra = max(0, size - base_size)
            dm = pywintypes.DEVMODEType(extra)
            res = win32print.DocumentProperties(
                0, hprinter, printer_name, dm, None, win32con.DM_OUT_BUFFER
            )
            if res >= 0:
                return dm
    except Exception:
        logger.debug("DocumentProperties(DM_OUT_BUFFER) failed for '%s'", printer_name)

    try:
        props = win32print.GetPrinter(hprinter, 2)
        return props.get("pDevMode")
    except Exception:
        return None


def is_color_printer(printer_name: str) -> bool:
    """Return True if the Windows printer supports color printing."""
    try:
        dc_colordevice = getattr(win32con, "DC_COLORDEVICE", 32)
        res = win32print.DeviceCapabilities(printer_name, "", dc_colordevice)
        if res >= 0:
            color_capable = bool(res)
            logger.info(
                "Printer '%s' DC_COLORDEVICE=%d (color=%s)",
                printer_name, res, color_capable,
            )
            return color_capable
    except Exception:
        logger.debug("DeviceCapabilities(DC_COLORDEVICE) failed for '%s'", printer_name)

    try:
        hprinter = win32print.OpenPrinter(printer_name)
        try:
            devmode = _get_printer_devmode(hprinter, printer_name)
            if devmode is not None and (devmode.Fields & win32con.DM_COLOR):
                return devmode.Color == win32con.DMCOLOR_COLOR
        finally:
            win32print.ClosePrinter(hprinter)
    except Exception:
        pass

    return True


_MONOCHROME_MODES = frozenset({
    "monochrome",
    "bi-level",
    "process-monochrome",
    "auto-monochrome",
})


def spool_to_printer(
    file_path: str,
    printer_name: str,
    media_size_mm: Optional[Tuple[float, float]] = None,
    copies: int = 1,
    color_mode: Optional[str] = None,
    orientation_requested: Optional[int] = None,
    job_name: Optional[str] = None,
) -> None:
    """
    Send *file_path* to the Windows print queue of *printer_name*.

    Uses PyMuPDF to rasterize the document and win32ui / win32gui to send it
    in 4-pixel-aligned horizontal bands to the printer's Device Context (DC)
    configured for the target physical paper size, orientation, color mode,
    and requested copies.
    """
    copies = max(1, min(99, int(copies)))
    normalized_color_mode = color_mode.strip().lower() if color_mode else None
    is_monochrome = normalized_color_mode in _MONOCHROME_MODES
    logger.info(
        "Spooling '%s' to printer '%s' (copies=%d, color_mode=%s, orientation=%s)",
        file_path,
        printer_name,
        copies,
        normalized_color_mode or "default",
        orientation_requested or "auto",
    )

    try:
        import win32ui
        import pythoncom
        import fitz
        from PIL import Image, ImageWin
    except ImportError as e:
        logger.error("Missing dependency for printing: %s", e)
        raise

    pythoncom.CoInitialize()
    try:
        config = load_config()
        reverse_page_order = bool(config.get("reverse_page_order", False))
        auto_rotate_to_fit = bool(config.get("auto_rotate_to_fit", False))
        try:
            max_render_dpi = max(72, min(1200, int(config.get("max_render_dpi", 300) or 300)))
        except (ValueError, TypeError):
            max_render_dpi = 300

        # Inspect document first page for media auto-detection and landscape orientation
        doc_is_landscape = False
        try:
            _inspect_doc = fitz.open(file_path)
            if len(_inspect_doc) > 0:
                _p0 = _inspect_doc.load_page(0)
                _pdf_w_mm = _p0.rect.width * 25.4 / 72.0
                _pdf_h_mm = _p0.rect.height * 25.4 / 72.0
                doc_is_landscape = _p0.rect.width > _p0.rect.height
                if media_size_mm is None:
                    media_size_mm = (_pdf_w_mm, _pdf_h_mm)
                    logger.info(
                        "No IPP media specified — auto-detected media size from document: %.1f × %.1f mm",
                        _pdf_w_mm, _pdf_h_mm,
                    )
            _inspect_doc.close()
        except Exception:
            logger.exception("Failed to inspect document dimensions")

        if media_size_mm:
            logger.info(
                "Target media size: %.1f × %.1f mm",
                media_size_mm[0], media_size_mm[1],
            )

        # Determine whether the print job is requesting Landscape orientation (Issue #10)
        # IPP orientation-requested enum: 3=portrait, 4=landscape, 5=reverse-landscape, 6=reverse-portrait
        want_landscape = False
        if orientation_requested in (4, 5):
            want_landscape = True
        elif orientation_requested in (3, 6):
            want_landscape = doc_is_landscape and not auto_rotate_to_fit
        elif media_size_mm and media_size_mm[0] > media_size_mm[1]:
            want_landscape = True
        elif doc_is_landscape and not auto_rotate_to_fit:
            want_landscape = True

        hprinter = win32print.OpenPrinter(printer_name)
        try:
            # ----------------------------------------------------------
            # Configure DEVMODE to match target paper size, color & copy
            # ----------------------------------------------------------
            devmode = _get_printer_devmode(hprinter, printer_name)
            if devmode is not None:
                try:
                    # Enforce single copy in hardware DEVMODE so driver doesn't
                    # multiply our software collated copy loop.
                    devmode.Copies = 1
                    devmode.Fields |= win32con.DM_COPIES

                    # Configure color vs monochrome mode in DEVMODE (Issue #6)
                    if is_monochrome:
                        devmode.Color = win32con.DMCOLOR_MONOCHROME
                        devmode.Fields |= win32con.DM_COLOR
                        logger.info("DEVMODE Color set to DMCOLOR_MONOCHROME (1)")
                    elif normalized_color_mode in ("color", "auto") or (
                        normalized_color_mode is None and is_color_printer(printer_name)
                    ):
                        devmode.Color = win32con.DMCOLOR_COLOR
                        devmode.Fields |= win32con.DM_COLOR
                        logger.info("DEVMODE Color set to DMCOLOR_COLOR (2)")

                    # Configure orientation (Issue #10)
                    if want_landscape:
                        devmode.Orientation = win32con.DMORIENT_LANDSCAPE
                    else:
                        devmode.Orientation = win32con.DMORIENT_PORTRAIT
                    devmode.Fields |= win32con.DM_ORIENTATION

                    if media_size_mm:
                        width_mm, height_mm = media_size_mm

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
                            short_mm = min(width_mm, height_mm)
                            long_mm = max(width_mm, height_mm)
                            devmode.PaperWidth = int(round(short_mm * 10))
                            devmode.PaperLength = int(round(long_mm * 10))
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
                        "DEVMODE configured: PaperSize=%d, Orientation=%d, Color=%d",
                        devmode.PaperSize, devmode.Orientation, getattr(devmode, "Color", 0),
                    )
                except Exception:
                    logger.exception(
                        "Failed to configure DEVMODE — falling back to driver defaults"
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
            render_dpi_x = min(printer_dpi_x, max_render_dpi)
            render_dpi_y = min(printer_dpi_y, max_render_dpi)
            logger.info(
                "Printer DPI: %d × %d (rasterizing at %d × %d DPI)",
                printer_dpi_x, printer_dpi_y, render_dpi_x, render_dpi_y,
            )

            pdf_doc = fitz.open(file_path)
            page_count = len(pdf_doc)

            # Pre-pass: align page rotation with target orientation / paper (Issues #5 & #10)
            if page_count > 0:
                dc_w = hdc.GetDeviceCaps(win32con.HORZRES)
                dc_h = hdc.GetDeviceCaps(win32con.VERTRES)
                dc_is_landscape = dc_w > dc_h

                for pno in range(page_count):
                    p = pdf_doc.load_page(pno)
                    if orientation_requested == 6:
                        p.set_rotation((p.rotation + 180) % 360)
                    is_page_landscape = p.rect.width > p.rect.height
                    if auto_rotate_to_fit or orientation_requested in (4, 5) or want_landscape:
                        if is_page_landscape != dc_is_landscape:
                            rot_delta = 270 if orientation_requested == 5 else 90
                            p.set_rotation((p.rotation + rot_delta) % 360)
                            logger.info(
                                "Rotated page %d (%d° CW) to match printer DC orientation (dc_landscape=%s)",
                                pno + 1, rot_delta, dc_is_landscape,
                            )

            # Determine page sequence (Issue #4)
            if reverse_page_order:
                page_indices = list(range(page_count - 1, -1, -1))
                logger.info(
                    "Reverse page order enabled — printing %d pages in reverse order: %s",
                    page_count, [idx + 1 for idx in page_indices],
                )
            else:
                page_indices = list(range(page_count))

            doc_title = (job_name or "").strip() or os.path.basename(file_path)
            hdc.StartDoc(doc_title)

            for copy_idx in range(copies):
                if copies > 1:
                    logger.info("Spooling copy %d/%d (%d pages)...", copy_idx + 1, copies, page_count)

                for page_num in page_indices:
                    logger.info(
                        "Rendering page %d/%d (copy %d/%d)...",
                        page_num + 1, page_count, copy_idx + 1, copies,
                    )
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
                    # Compute 1:1 physical target size in Printer DC px
                    # --------------------------------------------------
                    dc_scale_x = printer_dpi_x / 72.0
                    dc_scale_y = printer_dpi_y / 72.0

                    native_dc_w = page.rect.width * dc_scale_x
                    native_dc_h = page.rect.height * dc_scale_y

                    scale_w = printable_width / native_dc_w if native_dc_w > 0 else 1.0
                    scale_h = printable_height / native_dc_h if native_dc_h > 0 else 1.0

                    # Thermal label printer protection: if width matches within 5%
                    # but height is much smaller, preserve 1:1 width scale.
                    if scale_w >= 0.95 and scale_h < 0.7:
                        logger.warning(
                            "Printer DC height (%d px) is significantly smaller than document height (%d px), "
                            "but width matches (%.1f%%). Preserving 1:1 scale to avoid shrunken label.",
                            printable_height, int(native_dc_h), scale_w * 100.0,
                        )
                        fit_scale = min(1.0, scale_w)
                    elif native_dc_w > printable_width or native_dc_h > printable_height:
                        fit_scale = min(scale_w, scale_h)
                        logger.info("Page exceeds printable area — shrink-to-fit scale=%.4f", fit_scale)
                    else:
                        fit_scale = 1.0

                    # Rasterize at render_dpi (capped by max_render_dpi to prevent
                    # 90MB+ monolithic GDI allocations on 600 DPI inkjets, Issue #9)
                    render_scale_x = (render_dpi_x / 72.0) * fit_scale
                    render_scale_y = (render_dpi_y / 72.0) * fit_scale

                    matrix = fitz.Matrix(render_scale_x, render_scale_y)
                    pix = page.get_pixmap(matrix=matrix, alpha=False)

                    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                    if is_monochrome:
                        img = img.convert("L").convert("RGB")

                    # Ensure image width is a multiple of 4 pixels so 24-bit DIB
                    # scanlines have zero DWORD-alignment padding bytes (Issue #9)
                    rem = img.width % 4
                    if rem != 0:
                        pad_w = 4 - rem
                        padded = Image.new("RGB", (img.width + pad_w, img.height), (255, 255, 255))
                        padded.paste(img, (0, 0))
                        img = padded

                    dst_w = max(4, int(round(native_dc_w * fit_scale)))
                    dst_h = max(1, int(round(native_dc_h * fit_scale)))
                    # Align destination width to 4 pixels as well
                    dst_w = min(printable_width, max(4, dst_w - (dst_w % 4)))

                    x_offset = max(0, (printable_width - dst_w) // 2)
                    y_offset = max(0, (printable_height - dst_h) // 2)

                    # Draw in horizontal bands (256 rows per StretchDIBits call)
                    # so GDI EMF records stay ~1.5 MB instead of ~90 MB (Issue #9).
                    band_height = 256
                    src_w, src_h = img.size
                    hdc_out = hdc.GetHandleOutput()
                    for src_y0 in range(0, src_h, band_height):
                        src_y1 = min(src_h, src_y0 + band_height)
                        dst_y0 = y_offset + int(round(src_y0 * dst_h / src_h))
                        dst_y1 = y_offset + int(round(src_y1 * dst_h / src_h))
                        if dst_y1 <= dst_y0:
                            continue
                        band_img = img.crop((0, src_y0, src_w, src_y1))
                        band_dib = ImageWin.Dib(band_img)
                        band_dib.draw(
                            hdc_out,
                            (x_offset, dst_y0, x_offset + dst_w, dst_y1),
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


def _encode_beg_collection(name: str = "") -> bytes:
    """
    Encode a ``begCollection`` (0x34) tag per RFC 8010 §3.1.6.

    *name* is non-empty for the first value of a collection attribute and empty
    for additional values in a ``1setOf collection`` or nested collections.
    """
    name_bytes = name.encode("ascii") if name else b""
    return (
        struct.pack("!B", IPP_TAG_BEG_COLLECTION)
        + struct.pack("!H", len(name_bytes))
        + name_bytes
        + struct.pack("!H", 0)
    )


def _encode_end_collection() -> bytes:
    """Encode an ``endCollection`` (0x37) tag per RFC 8010 §3.1.6."""
    return (
        struct.pack("!B", IPP_TAG_END_COLLECTION)
        + struct.pack("!H", 0)
        + struct.pack("!H", 0)
    )


def _encode_member_name(member_name: str) -> bytes:
    """Encode a ``memberAttrName`` (0x4A) tag inside a collection."""
    name_bytes = member_name.encode("ascii")
    return (
        struct.pack("!B", IPP_TAG_MEMBER_ATTR_NAME)
        + struct.pack("!H", 0)
        + struct.pack("!H", len(name_bytes))
        + name_bytes
    )


def _encode_member_integer(member_name: str, value: int) -> bytes:
    """Encode a 32-bit integer member attribute inside an IPP collection."""
    return (
        _encode_member_name(member_name)
        + _encode_additional_value(IPP_TAG_INTEGER, struct.pack("!i", value))
    )


def _encode_member_keyword(member_name: str, keyword: str) -> bytes:
    """Encode a keyword member attribute inside an IPP collection."""
    return (
        _encode_member_name(member_name)
        + _encode_additional_value(IPP_TAG_KEYWORD, keyword.encode("ascii"))
    )


def _encode_media_size_member(w_mm: float, h_mm: float) -> bytes:
    """Encode a nested ``media-size`` collection member with dimensions in 1/100 mm."""
    x_dim = int(round(w_mm * 100))
    y_dim = int(round(h_mm * 100))
    return (
        _encode_member_name("media-size")
        + _encode_beg_collection("")
        + _encode_member_integer("x-dimension", x_dim)
        + _encode_member_integer("y-dimension", y_dim)
        + _encode_end_collection()
    )


def _encode_media_col_entry(
    attr_name: str,
    w_mm: float,
    h_mm: float,
    margin_hmm: int = 423,
    media_source: str = "main",
    media_type: str = "stationery",
) -> bytes:
    """
    Encode a ``media-col`` collection value (used for ``media-col-default``,
    ``media-col-ready``, and ``media-col-database`` entries).
    """
    return (
        _encode_beg_collection(attr_name)
        + _encode_media_size_member(w_mm, h_mm)
        + _encode_member_integer("media-bottom-margin", margin_hmm)
        + _encode_member_integer("media-left-margin", margin_hmm)
        + _encode_member_integer("media-right-margin", margin_hmm)
        + _encode_member_integer("media-top-margin", margin_hmm)
        + _encode_member_keyword("media-source", media_source)
        + _encode_member_keyword("media-type", media_type)
        + _encode_end_collection()
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
            elif tag in (IPP_TAG_INTEGER, IPP_TAG_ENUM) and value_len == 4:
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

    # Operations supported (Print-Job, Validate-Job, Cancel-Job,
    # Get-Job-Attributes, Get-Jobs, Get-Printer-Attributes — Issue #10)
    attrs += _encode_enum_attribute("operations-supported", IPP_OP_PRINT_JOB)
    attrs += _encode_additional_value(IPP_TAG_ENUM, struct.pack("!i", IPP_OP_VALIDATE_JOB))
    attrs += _encode_additional_value(IPP_TAG_ENUM, struct.pack("!i", IPP_OP_CANCEL_JOB))
    attrs += _encode_additional_value(IPP_TAG_ENUM, struct.pack("!i", IPP_OP_GET_JOB_ATTRIBUTES))
    attrs += _encode_additional_value(IPP_TAG_ENUM, struct.pack("!i", IPP_OP_GET_JOBS))
    attrs += _encode_additional_value(IPP_TAG_ENUM, struct.pack("!i", IPP_OP_GET_PRINTER_ATTRIBUTES))

    # Charset & language
    attrs += _encode_text_attribute(IPP_TAG_CHARSET, "charset-configured", "utf-8")
    attrs += _encode_text_attribute(IPP_TAG_CHARSET, "charset-supported", "utf-8")
    attrs += _encode_text_attribute(IPP_TAG_LANGUAGE, "natural-language-configured", "en-us")
    attrs += _encode_text_attribute(IPP_TAG_LANGUAGE, "generated-natural-language-supported", "en-us")

    # Color support — MUST be boolean per IPP spec; iOS rejects keyword encoding.
    color_capable = is_color_printer(printer_name)
    attrs += _encode_boolean_attribute("color-supported", color_capable)

    # print-color-mode — required by macOS / IPP Everywhere so CUPS does not
    # default to a grayscale-only queue (Issue #6).
    if color_capable:
        attrs += _encode_text_attribute(IPP_TAG_KEYWORD, "print-color-mode-supported", "monochrome")
        attrs += _encode_additional_value(IPP_TAG_KEYWORD, b"color")
        attrs += _encode_text_attribute(IPP_TAG_KEYWORD, "print-color-mode-default", "color")
    else:
        attrs += _encode_text_attribute(IPP_TAG_KEYWORD, "print-color-mode-supported", "monochrome")
        attrs += _encode_text_attribute(IPP_TAG_KEYWORD, "print-color-mode-default", "monochrome")

    # PWG Raster document types (sgray_8 + srgb_8 when color-capable)
    attrs += _encode_text_attribute(IPP_TAG_KEYWORD, "pwg-raster-document-type-supported", "sgray_8")
    if color_capable:
        attrs += _encode_additional_value(IPP_TAG_KEYWORD, b"srgb_8")

    # Pages-per-minute (informational)
    attrs += _encode_integer_attribute("pages-per-minute", 10)

    # Detect the printer's actual active paper form in Windows DEVMODE
    default_media = "iso_a4_210x297mm"
    try:
        hprinter = win32print.OpenPrinter(printer_name)
        try:
            devmode = _get_printer_devmode(hprinter, printer_name)
            if devmode is not None:
                if devmode.PaperSize == win32con.DMPAPER_A4:
                    default_media = "iso_a4_210x297mm"
                elif devmode.PaperSize == win32con.DMPAPER_A3:
                    default_media = "iso_a3_297x420mm"
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

    # media-ready — advertise the active loaded paper size (Issue #10).
    attrs += _encode_text_attribute(IPP_TAG_KEYWORD, "media-ready", unique_media[0])

    # media-col-supported — iOS 16+ & macOS CUPS check for these collection members.
    attrs += _encode_text_attribute(IPP_TAG_KEYWORD, "media-col-supported", "media-size")
    attrs += _encode_additional_value(IPP_TAG_KEYWORD, b"media-top-margin")
    attrs += _encode_additional_value(IPP_TAG_KEYWORD, b"media-left-margin")
    attrs += _encode_additional_value(IPP_TAG_KEYWORD, b"media-right-margin")
    attrs += _encode_additional_value(IPP_TAG_KEYWORD, b"media-bottom-margin")
    attrs += _encode_additional_value(IPP_TAG_KEYWORD, b"media-type")
    attrs += _encode_additional_value(IPP_TAG_KEYWORD, b"media-source")

    # Margin, source, and type capabilities required by CUPS _ppdCreateFromIPP (Issue #10)
    for margin_attr in (
        "media-bottom-margin-supported",
        "media-left-margin-supported",
        "media-right-margin-supported",
        "media-top-margin-supported",
    ):
        attrs += _encode_integer_attribute(margin_attr, 423)
        attrs += _encode_additional_value(IPP_TAG_INTEGER, struct.pack("!i", 0))
        attrs += _encode_additional_value(IPP_TAG_INTEGER, struct.pack("!i", 635))

    attrs += _encode_text_attribute(IPP_TAG_KEYWORD, "media-source-supported", "main")
    attrs += _encode_additional_value(IPP_TAG_KEYWORD, b"auto")
    attrs += _encode_text_attribute(IPP_TAG_KEYWORD, "media-type-supported", "stationery")
    attrs += _encode_additional_value(IPP_TAG_KEYWORD, b"auto")

    # media-col-default & media-col-ready (Issue #10 — prevents macOS/CUPS from
    # failing IPP conformance and falling back to *Letter when printer is A4)
    def_w_mm, def_h_mm = IPP_MEDIA_SIZES.get(unique_media[0], (210.0, 297.0))
    attrs += _encode_media_col_entry("media-col-default", def_w_mm, def_h_mm)
    attrs += _encode_media_col_entry("media-col-ready", def_w_mm, def_h_mm)

    # media-size-supported (1setOf collection of x-dimension / y-dimension)
    for idx, media_key in enumerate(unique_media):
        mw, mh = IPP_MEDIA_SIZES.get(media_key, (210.0, 297.0))
        attrs += _encode_beg_collection("media-size-supported" if idx == 0 else "")
        attrs += _encode_member_integer("x-dimension", int(round(mw * 100)))
        attrs += _encode_member_integer("y-dimension", int(round(mh * 100)))
        attrs += _encode_end_collection()

    # media-col-database (1setOf collection for CUPS driverless PageSize synthesis)
    for idx, media_key in enumerate(unique_media):
        mw, mh = IPP_MEDIA_SIZES.get(media_key, (210.0, 297.0))
        attrs += _encode_media_col_entry(
            "media-col-database" if idx == 0 else "", mw, mh
        )

    # Orientation capabilities (3=portrait, 4=landscape, 5=reverse-landscape, 6=reverse-portrait)
    attrs += _encode_enum_attribute("orientation-requested-default", 3)
    attrs += _encode_enum_attribute("orientation-requested-supported", 3)
    attrs += _encode_additional_value(IPP_TAG_ENUM, struct.pack("!i", 4))
    attrs += _encode_additional_value(IPP_TAG_ENUM, struct.pack("!i", 5))
    attrs += _encode_additional_value(IPP_TAG_ENUM, struct.pack("!i", 6))

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
    if color_capable:
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

        # Extract requested number of copies (default 1, clamped 1..99)
        copies = 1
        if "copies" in job_attrs_parsed:
            try:
                copies = max(1, min(99, int(job_attrs_parsed["copies"])))
                logger.info("IPP requested copies: %d", copies)
            except (ValueError, TypeError):
                logger.warning(
                    "Invalid copies attribute '%s', defaulting to 1",
                    job_attrs_parsed["copies"],
                )
                copies = 1

        # Extract requested print-color-mode (e.g. 'color', 'monochrome')
        color_mode = job_attrs_parsed.get("print-color-mode")
        if color_mode:
            logger.info("IPP requested print-color-mode: %s", color_mode)

        # Extract requested orientation-requested (3=portrait, 4=landscape, 5=rev-landscape, 6=rev-portrait)
        orientation_requested: Optional[int] = None
        if "orientation-requested" in job_attrs_parsed:
            try:
                orientation_requested = int(job_attrs_parsed["orientation-requested"])
                logger.info("IPP requested orientation-requested: %d", orientation_requested)
            except (ValueError, TypeError):
                pass

        job_name = job_attrs_parsed.get("job-name")

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

            # Convert Apple Raster (URF) or PWG Raster to PDF — Windows can't print them natively.
            spool_path = tmp_path
            if ext == ".urf":
                spool_path = convert_urf_to_pdf(tmp_path)
                logger.info("Spool path after URF conversion: %s", spool_path)
            elif ext == ".pwg":
                spool_path = convert_pwg_to_pdf(tmp_path)
                logger.info("Spool path after PWG conversion: %s", spool_path)

            # Spool — pass media size, copies, color mode, orientation, and job name
            spool_to_printer(
                spool_path,
                self.printer_name,
                media_size_mm=media_size_mm,
                copies=copies,
                color_mode=color_mode,
                orientation_requested=orientation_requested,
                job_name=job_name,
            )


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
        color_capable = is_color_printer(self._printer_name)
        urf_caps = "W8,SRGB24,V1.4,RS300-600,DM1" if color_capable else "W8,V1.4,RS300-600,DM1"

        txt_props = {
            "txtvers": "1",
            "qtotal": "1",
            "rp": "ipp/print",
            "ty": display_name,
            "product": f"({self._printer_name})",
            "note": f"AirPrint Bridge on {hostname}",
            "pdl": "application/pdf,image/urf,image/jpeg,image/png,image/pwg-raster",
            "Color": "T" if color_capable else "F",  # Must match SRGB24 in URF
            "Duplex": "F",
            "adminurl": f"http://{self._host_ip}:{self._port}/",
            "priority": "50",
            "URF": urf_caps,
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
