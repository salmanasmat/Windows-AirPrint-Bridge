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
