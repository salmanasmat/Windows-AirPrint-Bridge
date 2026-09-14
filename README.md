# Windows AirPrint Bridge

[![Python 3.8+](https://img.shields.io/badge/python-3.8+-blue.svg)](https://www.python.org/downloads/)
[![Platform: Windows](https://img.shields.io/badge/platform-Windows-lightgray.svg)](https://www.microsoft.com/windows)
[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)
[![GitHub release](https://img.shields.io/github/v/release/salmanasmat/Windows-AirPrint-Bridge?color=brightgreen)](https://github.com/salmanasmat/Windows-AirPrint-Bridge/releases/latest)
[![GitHub All Releases](https://img.shields.io/github/downloads/salmanasmat/Windows-AirPrint-Bridge/total)](https://github.com/salmanasmat/Windows-AirPrint-Bridge/releases)

A production-ready, standalone Python script that acts as an AirPrint and IPP (Internet Printing Protocol) bridge server. It enables iOS (AirPrint) and Android (IPP Everywhere) devices connected to the same network to print documents to Windows printers—including legacy, offline printers that don't support Ethernet or Wi-Fi connections. Transform any offline printer into a network-accessible device, enabling seamless printing from phones and tablets over your local network.

## Features

- **Native Zero-Configuration Discovery:** Broadcasts strictly compliant mDNS `_ipp._tcp.local.` and Apple AirPrint `_universal._sub._ipp._tcp.local.` services. Automatically binds to your active network interface.
- **Strict iOS 18 Compatibility:** Implements Apple's required TXT record properties (like deterministic `UUID` matching and URF capability strings) to pass the strict iOS 18 discovery validation checks.
- **HTTP/1.1 & Chunked Transfer Encoding:** A custom HTTP server that natively handles the `Expect: 100-continue` and chunked HTTP payloads sent by iOS for large print jobs.
- **Robust Headless PDF Rendering:** Avoids unreliable Windows shell commands (like `ShellExecute printto`) which break when Microsoft Edge is the default PDF viewer. Instead, it uses `PyMuPDF` to render documents natively.
- **Scale-to-Fit & DPI-Aware 1:1 Rendering:** Dynamically parses IPP `media` paper size attributes (A4, A5, A6, 4×6" labels, etc.) and configures Windows printer `DEVMODE` settings per print job. Renders at true physical 1:1 scale using the printer's native DPI with automatic safety clamping, fixing shrunken prints on thermal label printers (like Zebra ZD220D).

## Supported Devices

By strictly adhering to Apple AirPrint and standard IPP Everywhere requirements, this service achieves seamless driverless printing across platforms:

- **iOS & iPadOS:** Fully tested and verified. Supports virtually all versions from **iOS 4.2 up through iOS 18+**. Passes strict zero-configuration checks introduced in iOS 16–18.
- **macOS:** Natively supported via Bonjour / AirPrint discovery.
- **Android:** Natively supported via the Android Default Print Service and Mopria (IPP Everywhere).

> [!TIP]
> **v1.3.1 Release Highlights:** v1.3.1 introduces a comprehensive fix for thermal label printers (like the Zebra ZD220D) and custom paper formats: full IPP `media-col` collection parsing, automatic PDF page dimension auto-detection, robust Win32 GDI printer form matching and DEVMODE configuration via `win32gui.CreateDC`, and thermal label aspect ratio preservation to guarantee 1:1 scale prints without shrinking.

## Android Configuration (Important)

Unlike iOS (where AirPrint is always enabled by default in the sharing sheet), **Android often has its built-in print service disabled by default** depending on the device manufacturer (e.g., Samsung, Xiaomi, OnePlus, Motorola).

To make your Windows printer discoverable on Android:

1. Open **Settings** on your Android device.
2. Navigate to **Connected devices → Connection preferences → Printing** *(or search for **"Printing"** in the Settings search bar)*.
3. Tap **Default Print Service** and toggle the switch to **ON**.
4. Now, open any photo, document, or webpage, tap **Share → Print** (or menu **Print**), and your Windows printer (`<Printer Name> (<PC-Hostname>)`) will appear in the printer selection dropdown.

> [!NOTE]
> - **Alternative Print Service:** If your device manufacturer removed the Default Print Service, install the official [Mopria Print Service](https://play.google.com/store/apps/details?id=org.mopria.clara) app from Google Play.
> - **Wi-Fi Network / AP Isolation:** Ensure your Android phone and Windows PC are connected to the same Wi-Fi network and subnet. Make sure **"AP Isolation" / "Client Isolation"** or **"Guest Network"** is disabled in your Wi-Fi router settings so mDNS (UDP 5353) multicast traffic can pass between devices.

## Installation (Recommended)

The easiest way to install and run AirPrint Bridge on Windows is using the pre-compiled installer. **No Python installation or dependencies are required.** Everything is bundled into a self-contained background Windows Service that automatically starts when your PC boots.

1. Download the latest `AirPrintBridge_Setup_v1.3.1.exe` from the [Releases page](https://github.com/salmanasmat/Windows-AirPrint-Bridge/releases/latest).
2. Run the installer as Administrator and follow the setup wizard.
3. The AirPrint Bridge service will automatically start in the background.
4. On your iOS or Android device (connected to the same Wi-Fi network), open a document or photo, tap **Print**, and select your Windows printer.

## Development & Manual Setup (Developers)

If you prefer to run from source code or contribute to development:

### Prerequisites

- **Windows OS**
- **Python 3.8+**

Install required dependencies:

```powershell
pip install zeroconf pymupdf pillow pywin32
```

### Running from Source

1. Set the printer you want to share as your **Default Printer** in Windows.
2. Run the bridge script:

```powershell
python airprint_bridge.py
```

3. The server will detect your local IP address and default printer, bind to port `631` (the standard IPP port), and begin broadcasting on your local network.

### Building the Executable & Installer

To build the standalone executable and setup wizard from source:

```powershell
# 1. Build PyInstaller single-file console executable
.\build.ps1

# 2. Compile Inno Setup installer wizard (requires Inno Setup 6)
& "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" .\installer.iss
```

## Diagnostics and Troubleshooting

If your device cannot find the printer, you can run the included diagnostic script:

```powershell
python diagnose.py
```

### Common Issues

- **Firewall Blocking:** Ensure that **TCP Port 631** (IPP) and **UDP Port 5353** (mDNS/Bonjour) are allowed through the Windows Defender Firewall for inbound connections.
- **Ghost Processes:** If the script crashes or is terminated forcefully, a zombie Python process might hold port `631` open, preventing the script from restarting. You can forcefully kill any process using the port with:
  ```powershell
  $pidToKill = (netstat -ano | Select-String ":631" | Select-String "LISTENING").Line.Split(' ', [System.StringSplitOptions]::RemoveEmptyEntries)[-1]; if ($pidToKill) { Stop-Process -Id $pidToKill -Force }
  ```
- **Different Subnets:** Your mobile device and Windows PC must be on the exact same local Wi-Fi subnet for mDNS multicast packets to reach the device.
- **Virtual Printers (e.g. RustDesk, PDF Printers):** If the service is running but printing does nothing (or you see a virtual printer name), a program likely changed your Windows Default Printer. To fix:
  1. Open the Windows Settings app.
  2. Go to **Bluetooth & devices > Printers & scanners**.
  3. Under "Printer preferences", make sure **"Let Windows manage my default printer"** is turned **OFF**.
  4. Click on your actual, physical printer in the list.
  5. Click the **"Set as default"** button at the top.

## How It Works (Technical Architecture)

1. **mDNS Registration:** Uses a dual-instance `zeroconf` approach to simultaneously register the primary IPP service and the Apple-specific `_universal` subtype pointing to the same instance name in the local domain.
2. **IPP Binary Protocol:** The server implements a minimal IPP 1.1 / 2.0 binary protocol parser, handling `Print-Job`, `Validate-Job`, and `Get-Printer-Attributes` operations. It responds with mandatory and extended attributes required by modern iOS releases.
3. **Print Spooling:** When a document is received, it is dumped to a temporary file. `win32ui` and `win32print` are used in combination with `fitz` (PyMuPDF) to draw the document natively into the printer's device context, bypassing the problematic shell-based PDF printing methods.
