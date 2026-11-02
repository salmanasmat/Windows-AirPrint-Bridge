# Social Media Announcements — v1.4.2

## LinkedIn Post

Windows AirPrint Bridge v1.4.2 is now live! 🚀🖨️

Over the past few weeks, community members put the bridge through real-world testing across macOS, iOS, and USB inkjet printers and reported four great deep-dive issues (#7, #8, #9, and #10). Today's release resolves all four with major reliability and protocol upgrades:

1. Full macOS Paper Size & Orientation Support (Issue #10):
Why did macOS only show US Letter in the paper size dropdown even when the bridge advertised A4, A5, and A6 in media-supported? Because macOS CUPS (_ppdCreateFromIPP) requires structured IPP collection attributes (media-col-database, media-size-supported, and media-col-default) to build its paper size menu, otherwise it falls back to US Letter. v1.4.2 implements full RFC 8010 collection encoding, fixes a silent win32con import bug that masked the printer's default A4 form, and adds full IPP orientation-requested support.

2. USB Inkjet Spooler & URF/PWG Raster Fixes (Issue #9):
Resolved a bug where jobs sent to USB inkjet printers (like the Canon TS5000 series) got stuck in the Windows print queue at 85+ MB per page or printed skewed when sending photos. We fixed the Apple URF PackBits scanline decoder, added native PWG Raster (RaS2) decoding, enforced 4-pixel GDI scanline alignment, capped intermediate rendering at 300 DPI (configurable via max_render_dpi), and switched to 256-row banded GDI printing with full private driver DEVMODE allocation.

3. Session-0 Safe Default Printer Detection (Issue #7):
Because a Windows Service runs as NT AUTHORITY\SYSTEM, Windows often keeps SYSTEM's registry printer stuck on "Microsoft Print to PDF" even after you change your default printer or delete Microsoft Print to PDF. The service now inspects active user registry hives under HKEY_USERS, validates printers against EnumPrinters, and prioritizes physical printers (plus supports an explicit printer_name override in config.json).

4. Built-in Windows Firewall Rules in Installer (Issue #8):
The standalone Windows installer now configures inbound local-subnet rules for IPP (TCP 631) and mDNS (UDP 5353) out of the box.

Download v1.4.2 or explore the source code on GitHub:
https://github.com/salmanasmat/Windows-AirPrint-Bridge/releases/latest

#OpenSource #Python #Windows #macOS #iOS #AirPrint #IPPEverywhere #SystemAdministration #SoftwareEngineering

---

## Reddit Post

**Target Subreddits:** r/selfhosted, r/sysadmin, r/macsysadmin, r/windows, r/opensource, r/python

**Title:** Windows AirPrint Bridge v1.4.2 — Fixing macOS CUPS paper size dropdowns (media-col-database), Session 0 SYSTEM default printer traps, and 85MB USB inkjet GDI spooler stalls

**Post Content:**

Hey everyone!

I maintain **Windows AirPrint Bridge**, a lightweight, open-source Python Windows Service that turns any Windows-connected USB or legacy printer into a native **AirPrint (iOS/macOS)** and **IPP Everywhere (Android/Mopria)** printer.

We just released **v1.4.2**, which resolves four community-reported issues ([#7](https://github.com/salmanasmat/Windows-AirPrint-Bridge/issues/7), [#8](https://github.com/salmanasmat/Windows-AirPrint-Bridge/issues/8), [#9](https://github.com/salmanasmat/Windows-AirPrint-Bridge/issues/9), and [#10](https://github.com/salmanasmat/Windows-AirPrint-Bridge/issues/10)) involving some fascinating Windows GDI, Session 0, and macOS CUPS IPP internals:

### 1. Why macOS CUPS ignored `media-supported` and only offered US Letter ([Issue #10](https://github.com/salmanasmat/Windows-AirPrint-Bridge/issues/10))
Even though the bridge advertised `iso_a4_210x297mm`, `iso_a5_148x210mm`, and label sizes in `media-supported`, macOS Sonoma/Sequoia only displayed **US Letter** in the Print dialog.
- **Root Cause:** When macOS CUPS (`_ppdCreateFromIPP`) sees `media-col-supported` advertised without `media-col-database` / `media-size-supported` / `media-col-default` collections, it only generates a `*PageSize` entry for `media-default`. Worse, a missing top-level `import win32con` triggered a caught `NameError` when reading the Windows printer's default form, silently falling back to `na_letter_8.5x11in`.
- **The Fix:** Fixed the top-level imports, implemented binary RFC 8010 collection encoders (`0x34` `begCollection` / `0x37` `endCollection` / `0x4A` `memberAttrName`), advertised full `media-col-database`, `media-size-supported`, `media-col-default`, and `media-col-ready` collections, and added `orientation-requested` (`IPP_TAG_ENUM = 0x23`) and `Cancel-Job` / `Get-Job-Attributes` advertisement.

### 2. 85.3 MB GDI Spooler Stalls & Skewed Photo Prints on USB Inkjets ([Issue #9](https://github.com/salmanasmat/Windows-AirPrint-Bridge/issues/9))
On a Canon TS5000 series USB printer, PDF jobs sat stuck in the Windows print queue at **85.3 MB** (1 page) or **170 MB** (2 pages), and iOS photo prints came out diagonally sheared with distorted colors.
- **Root Cause (Spooler Hang):** Rendering a full page at 600×600 DPI RGB (`4800 × 6212 × 3` bytes) and blasting it in a single `Dib.draw()` (`StretchDIBits`) call creates an ~85 MB uncompressed EMF record per page, and creating the printer DC with `GetPrinter(2)['pDevMode']` omits the driver's private `dmDriverExtra` tail bytes.
- **Root Cause (Skewed Photos):** Two bugs combined: (a) in Apple's `UNIRAST` (`image/urf`), each scanline starts with a `line_repeat` byte and `0x80` in PackBits fills the rest of the scanline with white—missing that desynchronized every scanline; and (b) `PIL.ImageWin.Dib` passes tightly-packed `width * 3` RGB rows to Win32 `StretchDIBits`, which requires `DWORD`-aligned scanlines (`width % 4 == 0`).
- **The Fix:** Rewrote the URF PackBits decoder, added a native `image/pwg-raster` (`RaS2`) decoder, aligned source/destination widths to 4-pixel boundaries, allocated full driver `DEVMODE` buffers via 3-step `DocumentProperties(..., DM_OUT_BUFFER)`, capped default rasterization at `300 DPI` (`max_render_dpi` in `config.json`), and switched to 256-row banded `Dib.draw()` calls.

### 3. Windows Service `SYSTEM` Account Stuck on "Microsoft Print to PDF" ([Issue #7](https://github.com/salmanasmat/Windows-AirPrint-Bridge/issues/7))
- **Root Cause:** Running as `NT AUTHORITY\SYSTEM` (`S-1-5-18`), `win32print.GetDefaultPrinter()` reads `HKU\.DEFAULT\...\Windows\Device`, which defaults to `Microsoft Print to PDF` and never updates when the logged-in user changes their default printer (or even deletes `Microsoft Print to PDF`).
- **The Fix:** `get_default_printer()` now supports an explicit `"printer_name"` in `config.json`, inspects interactive user SIDs under `HKEY_USERS\<S-1-5-21-*>\Software\Microsoft\Windows NT\CurrentVersion\Windows\Device`, verifies candidates against `EnumPrinters`, and prefers physical printers over virtual sinks.

### 4. Automatic Windows Firewall Rules in Installer ([Issue #8](https://github.com/salmanasmat/Windows-AirPrint-Bridge/issues/8))
- The Inno Setup installer (`AirPrintBridge_Setup_v1.4.2.exe`) now includes a checked-by-default task that adds inbound local-subnet rules for **TCP 631 (IPP)** and **UDP 5353 (mDNS)** and cleans them up on uninstall.

### Links
- **GitHub Repo:** [https://github.com/salmanasmat/Windows-AirPrint-Bridge](https://github.com/salmanasmat/Windows-AirPrint-Bridge)
- **Download v1.4.2 Installer:** [https://github.com/salmanasmat/Windows-AirPrint-Bridge/releases/latest](https://github.com/salmanasmat/Windows-AirPrint-Bridge/releases/latest)

Huge thanks to `@lsemprini` and `@aykutdizdar` for the detailed logs and bug reports!

---

## X (Twitter) Post

Windows AirPrint Bridge v1.4.2 is out! 🖨️🚀
• Full macOS paper size dropdowns via IPP media-col-database (#10)
• Fixed USB inkjet spooler stalls & URF/PWG photo skew (#9)
• Smart SYSTEM printer lookup (#7) & Firewall setup (#8)
https://github.com/salmanasmat/Windows-AirPrint-Bridge/releases/latest

---

# Social Media Announcements — v1.4.1

## LinkedIn Post

Printing in full color from macOS and iOS to any Windows-connected printer just got a major upgrade! 🎨🖨️

I'm happy to share that Windows AirPrint Bridge v1.4.1 is now live! 🚀

A community member reported an interesting protocol quirk (Issue #6): while iOS printed in full color through the bridge, macOS built a grayscale-only print queue (*ColorModel: *Gray) for the exact same color laser printer.

Why? Because iOS relied on SRGB24 in the URF capability header and ignored color-supported=false, whereas macOS CUPS strictly evaluated color-supported and print-color-mode-supported when generating the local queue.

What's new in v1.4.1:
- Dynamic Win32 Color Detection: Queries DeviceCapabilities(DC_COLORDEVICE) and DEVMODE to detect whether your Windows printer is color or monochrome.
- Full macOS & IPP Everywhere Color Support: Advertises color-supported, print-color-mode-supported (monochrome, color), print-color-mode-default (color), and pwg-raster-document-type-supported (sgray_8, srgb_8).
- Per-Job Color/Monochrome Switching: Honors the client's print-color-mode selection per job by configuring DMCOLOR_COLOR / DMCOLOR_MONOCHROME in the Windows DEVMODE structure and raster pipeline.
- Consistent mDNS & URF Capabilities: Keeps Bonjour TXT records and IPP attributes 100% aligned for both color and monochrome printers.

Download the standalone Windows installer or check out the source code on GitHub:
https://github.com/salmanasmat/Windows-AirPrint-Bridge/releases/latest

#OpenSource #Python #Windows #macOS #AirPrint #IPPEverywhere #Printing #SoftwareEngineering

---

## Reddit Post

**Target Subreddits:** r/selfhosted, r/sysadmin, r/macsysadmin, r/windows, r/opensource, r/python

**Title:** Windows AirPrint Bridge v1.4.1 — Fixes macOS grayscale-only queues on color printers + full per-job Color/Monochrome switching

**Post Content:**

Hey everyone!

I maintain **Windows AirPrint Bridge**, a lightweight, open-source Python service that turns any Windows-connected USB or legacy printer into a native **AirPrint (iOS/macOS)** and **IPP Everywhere (Android/Mopria)** network printer.

We just shipped **v1.4.1** to fix an interesting protocol discrepancy between iOS and macOS reported by the community ([Issue #6](https://github.com/salmanasmat/Windows-AirPrint-Bridge/issues/6)).

### The Bug: Why iOS printed in Color while macOS locked the queue to Grayscale
Previously, the bridge advertised `SRGB24` inside `urf-supported` and `Color=T` in the mDNS TXT record, but had `color-supported = false` hardcoded in the binary IPP `Get-Printer-Attributes` response and omitted `print-color-mode-supported`.
- **iOS** saw `SRGB24` in `urf-supported` and happily sent color jobs.
- **macOS (CUPS)** strictly respected `color-supported = false` and built a grayscale-only queue (`ColorModel/Color Mode: *Gray`).

### What's New in v1.4.1:
- **Dynamic Win32 Color Capability Detection:** Queries `win32print.DeviceCapabilities(printer_name, "", DC_COLORDEVICE)` (with a `DEVMODE` fallback) to determine whether the underlying Windows printer supports color.
- **Complete IPP Color Attributes:** Dynamically advertises `color-supported`, `print-color-mode-supported` (`monochrome, color`), `print-color-mode-default` (`color`), and `pwg-raster-document-type-supported` (`sgray_8, srgb_8`).
- **Per-Job Color & Monochrome Enforcement:** Reads `print-color-mode` from incoming IPP `Print-Job` requests, sets `DMCOLOR_COLOR` or `DMCOLOR_MONOCHROME` in the Windows `DEVMODE` (merged via `DocumentProperties`), and converts rendered page bitmaps to grayscale when monochrome is requested.
- **mDNS & URF Consistency:** Automatically omits `SRGB24` and sets `Color=F` when sharing a monochrome-only printer.

### Links:
- **GitHub Repository & Source:** [https://github.com/salmanasmat/Windows-AirPrint-Bridge](https://github.com/salmanasmat/Windows-AirPrint-Bridge)
- **Latest Installer (`AirPrintBridge_Setup_v1.4.1.exe`):** [https://github.com/salmanasmat/Windows-AirPrint-Bridge/releases/latest](https://github.com/salmanasmat/Windows-AirPrint-Bridge/releases/latest)

Thanks to `@syeedmansur` for the detailed bug report and `ipptool` output! Feedback and test reports are always welcome.

---

## X (Twitter) Post

Windows AirPrint Bridge v1.4.1 is out! 🎨🖨️

Fixes macOS creating grayscale-only queues for color printers (#6):
✅ Dynamic Win32 DC_COLORDEVICE detection
✅ Full IPP print-color-mode & srgb_8 support
✅ Per-job Color/Monochrome DEVMODE switching

https://github.com/salmanasmat/Windows-AirPrint-Bridge

---

# Social Media Announcements — v1.2.0

## LinkedIn Post

Printing from mobile devices directly to any PC-connected printer without third-party apps just got even better.

I'm excited to announce Windows AirPrint Bridge v1.2.0! 🚀

While iOS (AirPrint) support has been seamless, Android devices running the Default Print Service (IPP Everywhere / Mopria standard) occasionally struggled to discover bridge-hosted printers. In this release, we completely resolved this by aligning our mDNS TXT record UUID generation with IPP response attributes, strictly adhering to RFC 8011 tag standards, and expanding support for PWG-Raster formats.

Now, both iOS and Android devices on the same Wi-Fi network can discover and print natively to any USB/local printer on Windows with zero mobile apps required.

Key Updates in v1.2.0:
- Full Native Android Discovery: Strict UUID synchronization between mDNS and IPP headers.
- RFC 8011 Compliance: Proper IPP keyword attribute tag formatting.
- Expanded PDL Support: Added image/pwg-raster support for Android/Mopria clients.
- Automated Windows Installer: Clean setup wizard running as a background Windows Service.

Check out the open-source release and standalone installer on GitHub:
https://github.com/salmanasmat/Windows-AirPrint-Bridge/releases/latest

#OpenSource #Python #Windows #AirPrint #Android #DevCommunity #SoftwareEngineering

---

## Reddit Post

**Target Subreddits:** r/selfhosted, r/sysadmin, r/windows, r/androidapps, r/opensource

**Title:** Windows AirPrint Bridge v1.2.0: Native driverless printing for both iOS (AirPrint) and Android (IPP Everywhere) from any PC-connected printer

**Post Content:**

Hey everyone,

A common headache with older or USB-only printers connected to a Windows PC is sharing them seamlessly to smartphones without installing proprietary manufacturer apps.

I built **Windows AirPrint Bridge** — a standalone, zero-config Python bridge that turns any Windows printer into a native AirPrint and IPP Everywhere network printer.

We just released **v1.2.0**, bringing **full native Android compatibility** alongside existing iOS AirPrint support!

### What's New in v1.2.0:
- **Android BIPS Discovery Fix:** Solved the issue where Android's Default Print Service (com.android.bips) discarded printers due to UUID discrepancies between mDNS TXT records and IPP Get-Printer-Attributes payloads.
- **RFC 8011 Attribute Encoding:** Updated authentication and security attribute tags to strictly match IPP specs.
- **PWG-Raster PDL Support:** Added image/pwg-raster to supported format headers.
- **Background Windows Service:** Bundled as an automated Inno Setup wizard that installs and manages the background service automatically.

### How it works:
1. Broadcasts mDNS _ipp._tcp and _universal._sub._ipp._tcp on your local network.
2. Accepts standard binary IPP Print-Job requests on port 631.
3. Renders PDF/URF/image payloads headlessly and spools directly to your default Windows printer via Win32 DC APIs.

Project link & pre-built installer:
https://github.com/salmanasmat/Windows-AirPrint-Bridge

Feedback and bug reports are welcome!

---

## X (Twitter) Post

Windows AirPrint Bridge v1.2.0 is out! 🚀

Print natively to any PC-connected printer from both iOS & Android with ZERO mobile apps.

✨ Native Android (IPP Everywhere) discovery fixed
✨ RFC 8011 tag compliance
✨ Background Windows Service installer

Get it: https://github.com/salmanasmat/Windows-AirPrint-Bridge
