## v1.3.1 - 2026-09-14

### 🏷️ Complete Label Printer & PDF Scaling Resolution (Issue #3)
- **IPP Collection Attributes (`media-col`):** Enhanced the IPP binary attribute parser to properly decode collection member attributes (`0x4A`), extracting `media-size-name`, `media`, and dimensional `x-dimension` / `y-dimension` values sent by iOS PrintKit.
- **Pre-Document Byte Scanning Fallback:** Added a binary keyword scanner across the pre-document IPP header as an additional layer of protection to catch requested paper sizes.
- **Automatic PDF Page Dimension Fallback:** When printing directly from Safari, Files, or Photos where iOS omits the IPP `media` attribute, the bridge now inspects page 0 of the PDF to auto-detect its true physical dimensions (e.g., 105 × 148.5 mm for A6).
- **Robust Win32 GDI DEVMODE & DC Architecture:** Replaced the unsupported `PyCDC.ResetDC()` call with printer form matching via `win32print.DeviceCapabilities` (`DC_PAPERS`, `DC_PAPERSIZE`, `DC_PAPERNAMES`), standard form fallbacks (`DMPAPER_A6 = 70`), driver validation via `win32print.DocumentProperties()`, and direct DC instantiation via `win32gui.CreateDC('WINSPOOL', printer_name, devmode)` + `win32ui.CreateDCFromHandle()`.
- **Thermal Label Aspect Ratio Guard:** Added a safety guard preventing aggressive vertical shrinking on thermal label printers when the document width matches the physical printhead but the driver's default form height is smaller.
- **Dynamic `media-default` Discovery:** Automatically inspects the printer's active Windows DEVMODE so AirPrint discovery advertises the printer's actual form (e.g. A6 for label printers) as `media-default`.

## v1.3.0 - 2026-09-11

### 🎯 Dynamic Media Size Handling & Label Printer Scaling Fix
- **IPP Media Attribute Parsing:** Implemented structured binary IPP job attribute extraction (`extract_ipp_job_attributes`) to parse client-selected paper sizes such as `media` keywords (e.g., `iso_a6_105x148mm`, `na_index-4x6_4x6in`).
- **Physical Media Dimensions Mapping:** Added `IPP_MEDIA_SIZES` lookup mapping standard PWG media keywords across ISO A/B/C series, North American formats, and label/receipt sizes directly to millimeter dimensions.
- **Windows DC DEVMODE Configuration:** Dynamically adjusts the printer Device Context `DEVMODE` (`DMPAPER_USER` with `PaperWidth` and `PaperLength` in tenths of mm) via `ResetDC()`, ensuring Windows spooler respects the requested paper format.
- **True DPI-Aware 1:1 Scaling:** Replaced canvas-dependent stretch scaling with native DPI rendering (`printer_dpi / 72.0`), fixing the issue where A6 and thermal labels printed significantly smaller on label printers (such as Zebra ZD220D). Includes automatic shrink-to-fit safety clamping.
- **Expanded Media Advertisements:** Advertised A5, A6, A7, A8, US Legal, 4×6", and 100×150mm formats in `media-supported` and `media-ready` attributes so iOS and Android dialogs present label sizes natively.

## v1.2.0 - 2026-08-20

### 🤖 Native Android & IPP Everywhere Compatibility
- **Android BIPS Discovery:** Synchronized mDNS TXT UUIDs and IPP `printer-uuid` attributes to ensure seamless discovery by the Android Default Print Service and Mopria.
- **PWG-Raster & Format Support:** Added `image/pwg-raster` to supported MIME types for standard IPP Everywhere clients.
- **Protocol Parity:** Enforced RFC 8011 IPP version matching between requests and responses.

## v1.1.1 - 2026-08-12

### 🌐 Multi-PC Network & Display Enhancements
- **Multi-PC Network Identification:** Dynamic printer naming incorporating host PC name and printer name (`Printer (PC-NAME)`).
- **Unique mDNS UUIDs:** Machine-specific mDNS UUID generation (`uuid5` on `Printer@Hostname`) preventing iOS/AirPrint device collision across multiple PCs running the bridge on the same LAN.
- **Enhanced IPP & mDNS Attributes:** Updated `printer-make-and-model`, `printer-info`, `printer-location`, and mDNS TXT `note` fields for clear printer discovery UI on iOS, macOS, and Android.

## v1.1.0 - 2026-08-12

### 🛠️ Windows Service & Compatibility Fixes
- **Windows Service SCM Error 7039 Fixed:** Updated PyInstaller build settings to `--console` mode to ensure PID consistency with Windows Service Control Manager.
- **Persistent Service Logging:** Fixed log file path resolution when frozen with PyInstaller (`sys.executable` directory) so logs persist properly.
- **IPP Operation Expansion:** Added support for `Get-Job-Attributes` (`0x0009`) and `Cancel-Job` (`0x0008`) required by iOS PrintKit post-print routines.

### 🔒 Security & Code Quality Improvements
- **Document Extraction Safety:** Removed unsafe brute-force `0x03` delimiter fallback byte search that could truncate binary payloads.
- **XSS Prevention:** HTML-escaped printer names rendered in Web / HTTP status views.
- **Temp File Hardening:** Switched print job temp file creation to `tempfile.NamedTemporaryFile` with explicit cleanup in `finally` blocks.
- **UUID Compliance:** Standardized UUID generation across mDNS and IPP responses using RFC 4122 `uuid.uuid5()`.
- **Diagnostic Tool Cleanups:** Replaced hardcoded IP addresses with dynamic local IP auto-detection across `diagnose.py`, `decode_ipp.py`, and `quick_check.py`.

## v1.0.1 - 2026-08-11

### 🐛 Bug Fixes
- **Interactive Mode Fallback**: Fixed an issue (Error 1063) where running the standalone executable directly would crash. It now gracefully falls back to interactive debugging mode if it's not launched by the Service Control Manager.
- **Documentation**: Added troubleshooting steps for users with virtual printers (like RustDesk) that take over the Windows Default Printer setting, causing jobs to print to nowhere.

## v1.0.0 - 2026-08-10

### 🚀 New Features
- **Standalone Installer:** Now available as a fully automated Windows setup wizard that installs and runs the bridge as a background Windows Service.
- Print PDFs natively on Windows via PyMuPDF + win32 DC integration.
- Scale and center PDF pages to the printable area.
- Add URF (Apple Raster) support and AirPrint attributes.
- Register AirPrint subtype as a separate mDNS service.
- Enhance mDNS AirPrint advertisement for better iOS/macOS discovery.

### ⚡ Improvements
- Add IPP decoder and various AirPrint compatibility fixes.

### 📚 Documentation
- Revamp README for the Windows AirPrint Bridge project.
- Add supported devices documentation.

### 🏗️ Infrastructure & Maintenance
- Add PyInstaller (`build.ps1`) and Inno Setup (`installer.iss`) build scripts for generating distributable Windows executables.
- Add `diagnose.py` mDNS + IPP AirPrint diagnostic script for debugging.
- Add `quick_check.py` IPP probe script.
