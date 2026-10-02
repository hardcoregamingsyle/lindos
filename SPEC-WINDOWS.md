# Lindos — Engineering Specification, Addendum W (Every Windows format, Transfer, Play-anywhere)

> Extends [`SPEC.md`](SPEC.md) (§0–§13), [`SPEC-KERNEL.md`](SPEC-KERNEL.md) (§14–§19) and
> [`SPEC-VM.md`](SPEC-VM.md) (§20–§26). Same rules: **this file is the contract** — names, paths,
> CLIs, JSON shapes and module APIs below are binding. If code and this file disagree, one of them
> is wrong; fix one, never silently diverge. Section numbers continue at §27.
>
> Facts below were researched and adversarially re-verified on 2026-09-26 against primary sources
> (Microsoft Learn, MSIX SDK, winget-cli source, Wine/kernel/udisks/cryptsetup source, provider
> pages). Anything marked *(verify)* must be re-checked by the implementer before relying on it.

Goal, in the user's words: *"lightness of Linux, simplicity of Windows; runs .exe and .msi and all
other Windows packages and installers; easy transfer; and a way to play the games that need
Windows' kernel-level anti-cheat."* The first two exist (§3–§11). This addendum delivers the rest
**honestly**:

| Ask | What Lindos ships | § |
|---|---|---|
| "All other Windows packages and installers" | One dispatcher for ~30 Windows file types: MSIX/APPX (+bundles, upload files), `.msp` patches, `.reg`, `.ps1`, `.vbs`, `.url`, `.scr`, `.cpl`, `.inf`, `.cab`, ISO auto-mount + AutoPlay-style setup, ClickOnce, DOS programs (DOSBox-X), 16-bit Windows programs (Wine, mode-aware), `./setup.exe` from a terminal (binfmt_misc), `winget install <id>` | §28 |
| "Easy transfer" | `lindos-transfer`: Windows-Easy-Transfer-style migration from the Windows partition on the same PC (strictly read-only) or from a "transfer folder" made by a double-click kit on the old PC | §29 |
| "Kernel-level anti-cheat games" | **No emulation/spoofing (impossible; gets the PC hardware-banned).** Per-title *routes*: official cloud streaming, one-click "restart into Windows" (UEFI one-shot BootNext), with honest Secure-Boot/TPM guidance; VM only where allowed (currently never) | §30 |
| Kernel support for the above | NTFS3 (+CompactOS-compressed files), exFAT, casefold, binfmt_misc, efivarfs, dm-crypt + skcipher (BitLocker), loop/ISO9660/UDF, FUSE, LDM; Ubuntu-generic base config; Secure-Boot MOK signing | §31 |

---

## 27. Honesty & safety rules for this addendum (hard, binding — extend §0.1, §14, §20)

1. **No Windows kernel emulation for anti-cheat.** Lindos ships nothing that emulates, forges or
   hides from kernel-mode anti-cheat (Vanguard, Ricochet, Javelin, ACE, EAC/BattlEye kernel parts):
   no fake `.sys` loader, no "Windows kernel emulator", no attestation/TPM/Secure-Boot/HVCI forger,
   no HWID/SMBIOS/DMI/CPUID spoofing (including "look like a Steam Deck"), no VM hiding, no
   software TPM presented as hardware trust, no user-agent spoofing. A software stand-in cannot
   produce the hardware-rooted signatures the servers verify; it only gets the user **hardware-
   banned**. `FORBIDDEN_TOKENS` tests (§26) extend to every new file. §30 is the honest answer.
2. **A format handler never lies about status.** Every format has `status` ∈ `works | partial |
   unsupported` and a one-line `note`. `unsupported` formats (Windows kernel drivers `.sys`,
   Windows Update `.msu`, ARM-only programs, Store-encrypted packages, true UWP/WinUI apps, GDK
   `.msixvc`) are **explained**, never "tried anyway" in a way that looks like success.
3. **Transfer is read-only and never touches secrets.** Windows volumes are mounted **read-only
   only** (`ro`; never `force`, `remove_hiberfile`, `recover`, never read-write, never `-t ntfs`
   without `ro`). Nothing is written, moved or deleted on the source. The code carries a
   `SECRETS_DENYLIST` (unit-tested) and never opens: `SAM`, `SECURITY` (+ `.LOG1/.LOG2`),
   `Windows/System32/Microsoft/Protect/**`, `**/Microsoft/{Protect,Credentials,Vault}/**`,
   `config/systemprofile/**`, `ServiceProfiles/**`, `hiberfil.sys` (beyond its 4 KiB header),
   `pagefile.sys`, `swapfile.sys`, `*.dmp`, `MEMORY.DMP`, Chromium `Login Data*`, `Cookies`,
   `Network/Cookies`, `Web Data`, `Local State`, Wlansvc `keyMaterial`, and Firefox
   `key4.db`/`logins.json` unless the user ticks the explicit opt-in. Never decrypts DPAPI, never
   extracts LSA secrets/hashes. **Never hydrates cloud-only files** (OneDrive placeholders are
   detected, skipped and reported). Wi-Fi passwords move **only** via the user running Microsoft's
   `netsh wlan export profile key=clear` in the Windows kit with an explicit opt-in, and are written
   root-only (`0600`) through the helper, passed on **stdin** (never argv). BitLocker keys are
   typed by the user into udisks/cryptsetup/dislocker prompts; Lindos never sees, stores or passes
   them.
4. **No licence bypass.** Store-encrypted/licensed packages are refused with an explanation.
   Lindos never ships or copies Microsoft fonts (`C:\Windows\Fonts`) or stock wallpapers, never
   auto-installs .NET Framework (its licence is tied to a licensed Windows), never uses Store
   download-link generators. `winget install` downloads only the publisher URL named in the
   hash-verified official manifest, over HTTPS, and **refuses to run it unless the SHA-256
   matches** — there is no override flag.
5. **Routes are official and accurate.** Cloud links point only to providers' official apps/pages;
   each provider's *real* Linux status is shown (official app / works in Chrome-Edge but not
   officially listed / not in your region). "Restart into Windows" uses only a **Windows Boot
   Manager entry the firmware already has** (the helper re-validates it as root); it never edits
   Windows, BCD, Secure-Boot keys or firmware settings. The VM route is offered only where the
   matrix says the anti-cheat allows VMs (currently: none of the blocked titles). Region is taken
   from the system locale/timezone or asked once — never IP geolocation.
6. **Everything destructive or outward-facing asks first** (`.reg` import with a deletions preview,
   ISO setup, restart into Windows, firmware setup, app installs, Wi-Fi import, downloads from
   `.appinstaller`), mirroring Windows' own prompts. CLIs take `--yes` for scripted use.
7. **Not-supported wording.** A title kept off Linux by a kernel-level anti-cheat or by its
   publisher's choice is shown as **"Not supported yet"** (matrix `unsupported_kind`, §30.1), and
   the badge is never shown without who decides (the game's publisher) and that no date is given.
   Never "coming soon", "will be supported", a date or an ETA: only a publisher can deliver that,
   and some have said they will not. The Xbox app / PC Game Pass (Microsoft Store licensing, not an
   anti-cheat) does not get the badge. `tests/test_anticheat_disclaimer.py` guards every surface.

---

## 28. `lindos-compat` — "If Windows can open it, Lindos can open it (or tells you why not)"

### 28.1 Module map (`packages/lindos-compat/root/usr/lib/lindos-compat/lindos_compat/`)

| Module | Owner (§34) | Purpose |
|---|---|---|
| `formats.py` | W-A | registry, magic-byte classifier, per-format action plans |
| `dos.py` | W-A | DOSBox-X / DOSBox / DOSBox-Staging launcher; Wine WoW64-mode probe for Win16 |
| `diskimage.py` | W-A | ISO/IMG loop-mount via udisksctl + `autorun.inf` |
| `binfmt.py` | W-A | binfmt_misc status / conflict scan (terminal `./setup.exe`) |
| `msix.py` | W-B | MSIX/APPX/bundles/uploads/`.appinstaller`: classify, inspect, safe install |
| `handoff.py` | W-B | installers that hand an MSIX package to Windows: Wine file associations + leftover-package net (§28.4a) |
| `winget.py`, `wingetyaml.py` | W-C | winget index + manifests: search/show/install with hash chain |
| `cli_run.py`, `runner.py`, `prefix.py`, `doctor.py`, `recipes` data | W-A | integration (existing) |
| `cli_compat.py` | W-C | subcommands `formats`, `binfmt`, `winget` (existing file) |

All Wine invocations pass **Windows paths** (`Z:\…` via Wine's default `Z:` → `/` mapping, or the
prefix-relative `C:\…` from `lnk.unix_to_windows`) and argv lists (never a shell string). Wine
rebuilds the Windows command line from argv (it quotes tokens containing spaces), so switch strings
must be **split** into tokens first (CommandLineToArgvW rules) — no token may carry literal quotes.

### 28.2 `formats.py` API (binding)

```python
STATUSES = ("works", "partial", "unsupported")
HANDLERS = ("run", "msiexec-install", "msiexec-patch", "msix", "appinstaller", "dos", "win16",
            "wscript", "pwsh", "regedit", "open-url", "screensaver", "control-panel",
            "inf-install", "extract", "mount", "clickonce", "explain")

@dataclass(frozen=True)
class FormatSpec:
    id: str; label: str; suffixes: Tuple[str, ...]; mime: str
    handler: str; status: str; note: str

@dataclass
class Detection:
    format: FormatSpec
    reason: str                  # "PE32+ x64 GUI", "NE Windows 16-bit", "suffix .reg", …
    details: Dict[str, object]   # machine, subsystem, is_dll, clr, apphost, ne_exetyp, …

@dataclass
class ActionPlan:
    handler: str
    wine_tail: List[str]         # argv after wine/umu-run, e.g. ["msiexec", "/p", "Z:\\…", "REINSTALL=ALL", "REINSTALLMODE=omus"]
    host_argv: List[str]         # argv for host tools (dosbox-x, pwsh, xdg-open, udisksctl, cabextract)
    needs_prefix: bool
    force_runner: Optional[str]  # "wine" for msiexec/regedit/control/wscript/rundll32/win16
    arch: Optional[str]          # "win32" only for Win16 on old-WoW64 Wine
    prefix_hint: Optional[str]   # e.g. "win16"
    confirm: Optional[str]       # question to ask first
    message: str                 # explanation / partial note
    exit_code: int               # 0, or EXIT_UNSUPPORTED (3) for "explain"

FORMATS: Tuple[FormatSpec, ...]
def by_id(fid: str) -> Optional[FormatSpec]
def by_suffix(suffix: str) -> Optional[FormatSpec]
def detect(path: Path, *, head: Optional[bytes] = None) -> Detection      # never raises on bad input
def plan_action(path: Path, det: Detection, args: Sequence[str] = (), *,
                which=shutil.which) -> ActionPlan
def formats_table() -> List[Dict[str, object]]                              # `lindos-compat formats --json`
def to_windows_path(path: Path, prefix: Optional[Path] = None) -> str
```

**Classifier (exact):** `MZ` → `e_lfanew` = u32le @0x3C →
- `PE\0\0`: Machine (`0x014C` x86, `0x8664` x64; `0xAA64`, `0xA641`, `0xA64E`, `0x01C4`, `0x01C2`, `0x01C0` →
  ARM → `arm-exe`); Characteristics `0x2000` → DLL (`dll`); optional-header Magic → Subsystem (opt+68):
  10–13 = EFI → `dll`-style explain; CLR data directory (#14) non-empty → `.NET` (`clr=True`);
  native apphost with a sibling `<stem>.runtimeconfig.json`/`<stem>.dll` → `apphost=True`
  (.NET Core 3.0+; needs the .NET runtime for Windows in the C:\ drive — note, not blocker).
- `NE`: `ne_exetyp` @e_lfanew+0x36: 2 or 4 → `win16-exe`; `ne_flags` & 0x8000 (LIBMODULE) → `dll`;
  other values (e.g. 1 OS/2) → run the DOS stub → `dos-exe` (partial note).
- `LE`/`LX` → `dos-exe` (DOS-extender apps; partial note: "OS/2 or VxD files will not run").
- anything else after a valid `MZ` → `dos-exe`. A `.com`/`.pif` without `MZ` → `dos-com`.

Other magics: OLE `D0 CF 11 E0 A1 B1 1A E1` (→ msi/msp/mst by suffix); ZIP `PK\x03\x04` (→ MSIX
family only when `msix.classify` says so, else by suffix); encrypted MSIX `EXPH`/`EXSH`/`EXBH`;
CAB `MSCF`; ISO9660 `CD001` @0x8001 / UDF `NSR02|NSR03` descriptors; XML with `<AppInstaller>`
root. Suffix is the fallback, never the first signal for executables.

### 28.3 Format table (binding ids/handlers/status; notes may be reworded)

| id | suffixes | handler | status | behaviour |
|---|---|---|---|---|
| `exe` | .exe | run | works | existing flow (§9); a bootstrapper that hands an MSIX package to Windows is caught (§28.4a) |
| `dotnet-exe` | .exe (CLR/apphost) | run | partial | Wine/umu + ".NET: wine-mono covers many; some need Microsoft .NET (licence tied to Windows)" |
| `win16-exe` | .exe (NE 2/4) | win16 | partial | Wine, mode-aware (§28.5) |
| `dos-exe` | .exe (MZ/LE/LX/NE-other) | dos | works | DOSBox (§28.5) |
| `dos-com` | .com .pif | dos | works | DOSBox |
| `arm-exe` | .exe (ARM) | explain | unsupported | "built for ARM Windows — get the x64 (Intel/AMD) version" |
| `dll` | .dll .ocx .sys(PE) .efi | explain | unsupported | "a library/driver, not a program" |
| `msi` | .msi | msiexec-install | works | `msiexec /i <win> [TRANSFORMS=<mst>]` |
| `msp` | .msp | msiexec-patch | partial | `msiexec /p <win> REINSTALL=ALL REINSTALLMODE=omus` in the **product's** C:\ drive (`--prefix` or chooser) — fixes the old `/i` bug |
| `mst` | .mst | explain | partial | "transform: `lindos-run app.msi TRANSFORMS=x.mst`" |
| `msix` | .msix .appx | msix | partial | §28.4 |
| `msix-bundle` | .msixbundle .appxbundle | msix | partial | §28.4 |
| `msix-upload` | .msixupload .appxupload | msix | partial | inner bundle/package, one level |
| `msix-encrypted` | .emsix .eappx .emsixbundle .eappxbundle | explain | unsupported | Store-encrypted (DRM); shows Name/Version from the plaintext header only |
| `msixvc` | .msixvc | explain | unsupported | Xbox/GDK game package |
| `appinstaller` | .appinstaller | appinstaller | partial | shows publisher + URI host; download only on explicit consent, HTTPS only, into a quarantine dir; identity must match; then `msix` |
| `bat` | .bat .cmd | run | works | `cmd /c` (existing) |
| `ps1` | .ps1 | pwsh | partial | host PowerShell 7 `pwsh -NoProfile -File`; **default double-click opens the editor** (Windows behaviour); "Run with PowerShell" action; Linux-gap warning; Lindos confirm is the only gate (Linux pwsh policy = Unrestricted) |
| `vbs` | .vbs .vbe .wsf | wscript | partial | `wine wscript <win>` (`cscript //nologo` for console); .vbe/.wsf best-effort |
| `reg` | .reg | regedit | works | Lindos confirmation (Wine's regedit never asks) **listing keys/values to be deleted** (`[-KEY]`, `"x"=-`), then `wine regedit /S <win>` into a chosen C:\ drive |
| `lnk` | .lnk | run | works | existing |
| `url` | .url | open-url | works | `[InternetShortcut] URL=`; only `http`,`https`,`mailto`,`ftp` → `xdg-open`; anything else explained |
| `scr` | .scr | screensaver | works | `wine <win> /s` |
| `cpl` | .cpl | control-panel | partial | `wine control <win>` |
| `inf` | .inf | inf-install | partial | software INF (`[DefaultInstall]`, no `[Manufacturer]`/driver `Class`/`.sys` copy) → `wine rundll32 setupapi.dll,InstallHinfSection DefaultInstall 132 <win, UNQUOTED, ≤ 260 chars>`; **driver INF → explain + `lindos-drivers detect`** |
| `cab` | .cab | extract | works | `cabextract -d <dir>/<stem> <f>` then open the folder |
| `msu` | .msu | explain | unsupported | Windows Update package — not applicable (offer extract) |
| `iso` | .iso .img | mount | works | §28.6 |
| `clickonce` | .application .appref-ms | clickonce | partial | runs `wine rundll32 dfshim.dll,ShOpenVerbApplication <url>` **only if the C:\ drive already has .NET Framework 4.x**; otherwise explains (licence tied to Windows; Lindos won't auto-install) |

`EXIT_UNSUPPORTED = 3` joins `lindos-run`'s exit codes (0 ok · 1 error · 2 usage · 3 unsupported,
explained). `lindos-run --info` gains `"format": {id,label,status,handler,note,reason}`.

### 28.4 `msix.py` (binding API; behaviour from the MSIX SDK + Microsoft Learn)

```python
class MsixError(Exception): ...

KINDS = ("package", "bundle", "upload", "encrypted", "msixvc", "appinstaller", "unknown")
APP_CLASSES = ("win32", "uwp", "unknown", "needs-host")

@dataclass
class MsixApp:
    id: str; display_name: str; executable: str; entry_point: str
    app_class: str               # one of APP_CLASSES (rule below)
    parameters: str              # uap10/uap11:Parameters (macros expanded at launch)
    working_dir: str             # uap11:CurrentDirectoryPath or ""
    logo: str                    # resolved package-relative PNG ("" if none)
    list_entry: bool             # AppListEntry != "none"
    console: bool                # Subsystem="console"

@dataclass
class MsixInfo:
    path: str; kind: str
    name: str; publisher: str; publisher_display: str; version: str; arch: str
    resource_id: str; display_name: str
    publisher_id: str            # 13-char Crockford base32 (SHA-256 of UTF-16LE Publisher, first 8 bytes)
    package_full_name: str       # <Name>_<Version>_<Arch>_<ResourceId>_<PublisherId>
    package_family_name: str     # <Name>_<PublisherId>
    apps: List[MsixApp]
    dependencies: List[Dict[str, str]]   # PackageDependency Name/Publisher/MinVersion
    framework: bool; resource_package: bool
    signed: bool; unsigned_marker: bool; store_signals: List[str]
    status: str; reason: str
    selected_package: Optional[str]      # bundles: inner FileName chosen
    warnings: List[str]
    def as_dict(self) -> Dict[str, object]

@dataclass
class MsixInstall:
    info: MsixInfo
    install_dir: Path            # <prefix>/drive_c/Program Files/WindowsApps/<package_full_name>
    apps: List[Tuple[MsixApp, Path, Optional[Path]]]   # (app, exe path, logo png)
    notes: List[str]

def classify(path: Path) -> str                                   # one of KINDS, by CONTENT
def inspect(path: Path) -> MsixInfo                                # raises MsixError
def install(path: Path, prefix: Path, *, on_progress=None, max_bytes: int = 64 << 30,
            max_ratio: int = 200) -> MsixInstall
def parse_appinstaller(path: Path) -> Dict[str, str]              # {uri, kind, name, version, publisher, host}
def publisher_id(publisher: str) -> str
```

Rules (all binding):
- **Classify by content**, never extension: `EXPH/EXSH/EXBH` → encrypted; ZIP with exact entry
  `AppxMetadata/AppxBundleManifest.xml` → bundle; with `AppxManifest.xml` → package; ZIP holding
  inner `*.msix|*.appx|*.msixbundle|*.appxbundle` → upload (recurse once, via the zip library).
  Both manifests present → corrupt.
- Parse XML by **namespace URI + local name**, BOM-tolerant, child order-independent; accept Win8
  (`2010/manifest`, `2013/manifest` → always UWP) and Win10 foundation namespaces; bundle root in
  `appx/2013|2016|2017|2018/bundle`, plus `<b5:Package>` (`appx/2019/bundle`, `IsStub`).
- **App class** per `Application`: `win32` ⇔ `EntryPoint="Windows.FullTrustApplication"` *or*
  `uap10:RuntimeBehavior="packagedClassicApp"|"win32App"` with TrustLevel `mediumIL`
  (`packagedClassicApp` without TrustLevel defaults to appContainer → `uwp`); `uap10:HostId` →
  `needs-host`; `appSilo`, unknown values or contradictions → `unknown`; everything else (EntryPoint
  like `App.App`, `windowsApp`) → `uwp`. Having an `Executable` does **not** imply win32. Package
  status: all win32 → `partial` ("packaged desktop app — usually works"); some win32 → install only
  those, `partial`; none → `unsupported` ("UWP/WinUI app — Wine has no UWP app model; try the web
  version, a Linux alternative, or `lindos-vm`"). Framework/resource packages → "a component, not an
  app". Dependencies on `Microsoft.UI.Xaml.*`, `Microsoft.WindowsAppRuntime.*`,
  `Microsoft.NET.Native.*` or the UWP `Microsoft.VCLibs.140.00` → warn "expected to fail under
  Wine"; `Microsoft.VCLibs.140.00.UWPDesktop` → note `winetricks vcrun2022`.
- **Bundles:** candidates are `Type="application"` (**missing Type = resource**) and `IsStub!="true"`;
  rank x64 > neutral > x86; exclude arm/arm64/x86a64; tie-break TargetDeviceFamily
  `Windows.Desktop`/`Windows.Universal`, then highest Version. Locate bytes via the **central
  directory entry whose percent-decoded name equals FileName**; refuse compressed inner packages and
  size mismatches; bounds-check Offset/Size. Missing entry + no Offset → flat bundle: sibling file in
  the **same directory only** (never `..`, network, UNC or http). Inner manifest Name/Publisher must
  equal the bundle Identity; Version/Architecture must equal the `<Package>` element's (not the
  bundle's) values.
- **Extraction** (zip-slip safe): use the central directory; percent-decode names **first**, then
  split on `/` **and** `\`, reject absolute paths, drive letters, `..`, empty/`.` segments,
  NUL/control chars, case-duplicate targets, symlink entries; skip footprint files
  (`AppxManifest.xml` kept, `AppxBlockMap.xml`, `AppxSignature.p7x`, `[Content_Types].xml`,
  `AppxMetadata/**`, `microsoft.system.package.metadata/**`); enforce `max_bytes` and a
  compression-ratio cap. Target `drive_c/Program Files/WindowsApps/<package_full_name>/`.
  `VFS/<token>/` folders are materialized per Microsoft's table (`ProgramFilesX64`, `ProgramFilesX86`,
  `ProgramFilesCommonX64/X86`, `SystemX64`→system32, `SystemX86`→syswow64, `Windows`,
  `Common AppData`→ProgramData, `AppVSystem32*`) **in the app's own C:\ drive only**. `Registry.dat`
  (and `User.dat`, `Userclass.dat`/`UserClasses.dat`) is imported only if `python3-hivex` is
  available (else a warning); never touch a shared prefix.
- **Launch/registration:** one Start-Menu entry per app with `list_entry`; exe resolved
  separator-agnostic and case-insensitive; `PsfLauncher*.exe` → read `config.json` and launch the
  real target (never run PSF PowerShell `startScript`/`endScript`); working dir = CurrentDirectoryPath
  or exe dir; icon via qualifier-aware lookup (`targetsize-256_altform-unplated` → `scale-400` →
  `scale-200` → plain). Per-app C:\ drive slug = package family name. APPS_DB records
  `source="msix"`, `pfn`, `aumid` (`PFN!AppId`).
- **Trust display:** signature is *not verified*; show "Signed (not verified)" / "Unsigned (publisher
  marked unsigned: `OID.2.25.311729368913984317654407730594956997722=1`)". Store signals (e.g.
  `AppxMetadata/CodeIntegrity.cat`, Store signer OID) → honest message; "Try anyway" only for
  non-encrypted `win32` packages.
- **`.appinstaller`:** parse `MainPackage`/`MainBundle` (namespaces `appx/appinstaller/2017`,
  `/2017/2`, `/2018`, `/2021`); never auto-download; never register `ms-appinstaller:` with the host desktop (§28.4a records it inside a
  C:\ drive only); ignore UpdateSettings.

### 28.4a Installers that hand an app package to Windows (`handoff.py`, `cli_run.py`)

**The failure this fixes.** A bootstrapper `.exe` (e.g. the Claude desktop app's setup program, as first
seen on Lindos) downloads a large `.msix` into `%TEMP%` and `ShellExecute`s it (or `Add-AppxPackage`s it,
or opens an `ms-appinstaller:` link). Inside a Wine C:\ drive nothing was associated with those types, so
Wine's shell answered `SE_ERR_NOASSOC` — the dialog "There is no Windows program configured to open this
type of file" — and the bootstrapper's own fallback showed the temp folder. `lindos-run` never looked at the
leftover package: its post-run scan only diffs `.lnk/.exe/.desktop` files, and `_open_folder` is only ever
called for `.cab` extraction and disc mounts, so the file manager was almost certainly opened by Wine's own
shell (explorer/winebrowser) on the bootstrapper's behalf, not by Lindos. (Derived from the code and the
user's report; the exact Wine-side sequence is only confirmable in a real run.)

```python
# handoff.py (stdlib only, hermetic; every path stays inside the C:\ drive or the user's home)
HANDOFF_VERSION = 1; MARKER_KEY = "handoff"; PROG_ID = "Lindos.AppPackage"
PACKAGE_SUFFIXES = (".msix", ".appx", ".msixbundle", ".appxbundle", ".msixupload", ".appxupload",
                    ".emsix", ".eappx", ".emsixbundle", ".eappxbundle", ".appinstaller")
URI_SCHEMES = ("ms-appinstaller",)
def handler_command() -> str        # C:\windows\system32\cmd.exe /d /c echo "%1">>"C:\ProgramData\Lindos\handoff.log"
def uri_handler_command() -> str    # ...cmd.exe /d /c echo ms-appinstaller:>>"...handoff.log"  (NO %1: a link can hold " or &)
def registration_reg() -> str       # HKEY_CLASSES_ROOT: each suffix -> Lindos.AppPackage (shell\open + shell\runas
                                    # -> handler_command); ms-appinstaller URL protocol -> uri_handler_command
def prepare_registration(prefix: Path) -> Path   # <prefix>/.lindos-handoff/handoff.reg (UTF-16LE, BOM, CRLF) + queue folder
def is_registered(marker) -> bool; def mark_registered(prefix) -> bool
def snapshot_packages(prefix) -> Dict[str, Tuple[float, int]]
def find_packages(prefix, before, *, started=None, consume=True) -> List[Candidate]
def assess(candidate, msix) -> Verdict           # role: app | appinstaller | component | unsupported | encrypted | damaged | uri
def explain_text(installer: str, verdicts) -> str
def sniff_bootstrapper(path) -> List[str]        # hints only (--info, a log line); never a verdict
```

Rules (binding):
- **Association, not installation.** The handler only appends the (quoted) path or link to the queue file
  and returns; it never runs a Lindos or host program, so it works under every runner and inside a
  container. It is imported with `wine regedit /S` (through `build_plan`/`run_plan`, logged in
  `run-<slug>.log`) once per C:\ drive, recorded as `"handoff": 1` in `.lindos.json`; a failed import is a
  warning, never fatal, and is retried on the next run. Only for `handler == "run"`, runner `wine`, kind
  other than `game` (never Proton/`umu` game drives or Bottles). Written to `HKEY_CLASSES_ROOT`
  (= `HKLM\Software\Classes`, where `wine.inf` puts its own classes) and to both verbs `open`/`runas`.
- **Net after exit** (`handler == "run"`, runner ≠ bottles, kind ≠ game): `snapshot_packages` before the run;
  afterwards `find_packages` = the queue (each line mapped with `lnk.windows_to_unix`, kept only when it is
  a package-suffixed regular file inside the drive or the user's home; the queue is consumed and a symlinked
  queue is never followed) ∪ package-suffixed files that are new or changed in `windows/temp`, `Temp`,
  every `users/*/{Temp,AppData/Local/Temp,Local Settings/Temp}` (depth ≤ 4) and `users/*/{Downloads,
  Desktop}` (depth 1). Anything already there before the run is never offered again. Classification is by
  **content** (`msix.classify`/`inspect`), never the suffix.
- **Outcome.** ≥ 1 installable (`app`, or an `.appinstaller` that then asks/validates as in §28.4) → each goes
  through `_handle_msix` with a one-sentence context line ("`<installer>` downloaded this app and asked
  Windows to install it…"), i.e. the usual question (or `--yes`), into its package-family C:\ drive, with
  `new_prefix` forced off (`--new-prefix` must never move aside the drive that still holds the package). No
  installable but ≥ 1 of `unsupported | encrypted | damaged | uri` → **ONE** message (`explain_text`: what
  was handed over, why it cannot run, and the ways forward — a Linux/web version, `lindos-compat winget
  search "<name>"`, `lindos-vm`/`lindos-winapps` — plus where the file still is) and exit code 3. Only
  frameworks/resource packages → silence. When the net handled something, the generic "ended with exit code
  N" dialog is not shown on top (an installer-created Start-Menu entry still wins as before).
- **`ms-appinstaller:` links** are registered *inside the C:\ drive only* to record THAT one was handed over (never its
  text: a link can hold quotes or ampersands that must not reach cmd.exe, see uri_handler_command); the
  explanation therefore names no host, and the link is never fetched. The host desktop never gets an `x-scheme-handler/ms-appinstaller`.
- **Never** open a file manager (or anything else) on the installer's temp folder.
- **Big packages** (hundreds of MB): extraction streams 1 MiB chunks (unchanged); `MsixInfo.unpacked_bytes`
  (additive, from the extraction plan) feeds `msix.check_space(info, prefix)`, which `lindos-run` calls
  **before** asking — "Not enough free disk space: the app needs about X but only Y is free on that drive…"
  — the question shows "Disk space: about X once unpacked", the progress dialog updates once per percent,
  and `install()` deletes `.lindos-msix-*` staging folders older than a day (a killed install's leftovers).
- **Honest limits.** Not verifiable without a real Wine run: that the `HKCR` association intercepts the
  bootstrapper's `ShellExecute`; that Wine's `cmd.exe` writes the queue line as expected (and does not flash
  a console window); what a bootstrapper does after a "successful" hand-off. A bootstrapper that waits for
  Windows to report the app as installed still times out (Wine cannot answer) — the net then offers the
  package afterwards; one that deletes its download before exiting leaves nothing to offer. A package's own
  protocol/file-type registrations are not applied (browser sign-in may not return to the app).
- **Recipes** gain the optional `winget_id` field (exact package id, `""` until confirmed — never guessed);
  `claude-desktop.json` (`partial`) documents this flow.

### 28.5 DOS & Win16 (`dos.py`)

```python
def find_dosbox(which=shutil.which, run=subprocess.run) -> Optional[Tuple[List[str], str]]  # (argv prefix, flavor)
def dosbox_argv(path: Path, args: Sequence[str] = (), *, which=shutil.which, run=subprocess.run,
                home: Optional[Path] = None) -> List[str]
def wine_wow64_mode(*, which=shutil.which, run=subprocess.run, cache_dir: Optional[Path] = None) -> str
    # "old-wow64" | "new-wow64-16bit" | "new-wow64-no16bit" | "unknown"
DOSBOX_INSTALL_HINT: str   # "sudo apt install dosbox-x"  (Ubuntu 24.04 universe)
```
DOSBox order: `dosbox-x` (apt) → Flatpak `com.dosbox_x.DOSBox-X` → `dosbox` (apt 0.74; if
`--version` says Staging treat as Staging) → Flatpak `io.github.dosbox-staging`. argv per flavour:
dosbox-x `['-fastlaunch','-nopromptfolder','-exit',<abs>]`; Staging `[<abs>]` (auto-mounts C:,
quits on exit); 0.74 `[<abs>,'-exit']`. Flatpak adds `--filesystem=<exe dir>:ro` when outside
`$HOME`. Never rely on Wine's own winevdm→DOSBox fallback.
Win16: probe the Wine mode once (cached): old-WoW64 (Ubuntu's wine 9.0 + wine32:i386, WineHQ
i386+amd64) → dedicated `win16` C:\ drive with `WINEARCH=win32`; new-WoW64 ≥ 10.16/11.x → normal
64-bit prefix (16-bit supported); new-WoW64 older → explain. A `WINEARCH=win32 wineboot` on a
temp prefix failing with *"WINEARCH is set to 'win32' but this is not supported in wow64 mode."*
identifies new-WoW64. Runner forced to `wine` (Proton not used for Win16).

### 28.6 Disk images (`diskimage.py`)

```python
def loop_setup(image: Path, *, run=subprocess.run, which=shutil.which) -> str          # "/dev/loopN"
def mount_loop(device: str, *, run=subprocess.run) -> Path                              # mount point
def find_autorun(mount: Path) -> Optional[Dict[str, str]]    # {"open","icon","label"} case-insensitive; ANSI or UTF-16
def find_setup(mount: Path) -> Optional[Path]                # autorun open= target, else setup.exe/install.exe (top level)
def detach_command(device: str) -> List[List[str]]           # [["udisksctl","unmount","-b",…],["udisksctl","loop-delete","-b",…]]
```
`udisksctl loop-setup -r -f <img>` (parse *"Mapped file … as /dev/loopN."*) → pick `loopNp1` if
partitioned → explicit `udisksctl mount -b` (XFCE has no automounter) → Windows ISOs usually mount as
**UDF** → if a setup program exists ask *"Run <setup.exe> from <label>?"* → yes: re-enter
`lindos-run` on it (files may be 0400 — always launch through Wine, never exec); no/absent: open
the folder. Never auto-run.

### 28.7 Terminal `.exe` (`binfmt.py`, `/usr/lib/binfmt.d/lindos-pe.conf`)

```python
BINFMT_NAME = "lindos-pe"
CONF_PATH = "/usr/lib/binfmt.d/lindos-pe.conf"   # ":lindos-pe:M::MZ::/usr/libexec/lindos/lindos-binfmt:" (no flags)
MASK_PATH = "/etc/binfmt.d/lindos-pe.conf"       # symlink -> /dev/null persists "disabled"
PROC_DIR = "/proc/sys/fs/binfmt_misc"
def status(root: Optional[Path] = None) -> Dict[str, object]
    # {"registered","enabled","masked","conflicts":[{"name","interpreter"}],"interpreter","note"}
def parse_proc_entry(text: str) -> Dict[str, object]     # tolerant of unknown flag letters
def enable_commands() -> List[List[str]]                  # for the helper (below)
def disable_commands() -> List[List[str]]
```
Enable (helper `set-binfmt`): remove the mask, run `/usr/lib/systemd/systemd-binfmt
/usr/lib/binfmt.d/lindos-pe.conf` (registers **only** this file). Disable: write the mask symlink,
`echo -1 > /proc/sys/fs/binfmt_misc/lindos-pe` (ignore ENOENT). **Never** `systemctl
restart|stop systemd-binfmt` (it flushes every entry, e.g. qemu/wine). Conflicts: any other enabled
entry with offset 0 and magic `4d5a…`; binfmt tries newest-first and `binfmt-support` registers after
`systemd-binfmt`, so report honestly which one wins; never modify others. No `.com` extension
entries (would hijack native ELF names ending `.com`).
`/usr/libexec/lindos/lindos-binfmt` (bash): `$1` is the path *as passed to execve* (may be relative
or `/dev/fd/N/…`) → resolve against `$PWD`; re-classify via `lindos-run --info`-equivalent fast
check and refuse DLL/EFI/non-program MZ images with a clear message; then `exec /usr/bin/lindos-run
-- "$abs" "${@:2}"`.

### 28.8 Case-insensitive C:\ drives (casefold)

`prefix.ensure_prefix` pre-creates an **empty** `drive_c` for **new** prefixes and attempts
`chattr +F` (FS_IOC_SETFLAGS `0x40000000`) before wineboot; failure is silent (debug log). Doctor
reports `casefold: active | not enabled on this filesystem (optional speed-up; needs mkfs/tune2fs
-O casefold offline) | kernel lacks CONFIG_UNICODE`, deciding by the attempt's errno
(EOPNOTSUPP), not by `/sys/fs/ext4/features/casefold` alone. Never runs `tune2fs`.

### 28.9 Desktop integration

- Reuse shared-mime-info 2.4 types where they exist (**verify exact names in
  `freedesktop.org.xml` 2.4** — MSIX/APPX/bundles/appinstaller, `text/x-ms-regedit`,
  `application/x-mswinurl`, `application/vnd.ms-cab-compressed`, `application/x-cd-image`,
  `application/x-raw-disk-image`, `application/x-ms-ne-executable`); `lindos-windows.xml` defines
  only missing ones with `application/x-lindos-*` names: encrypted MSIX (magic `EXPH/EXSH/EXBH`),
  `.msixupload/.appxupload`, `.msixvc`, `.msp` (moved out of `x-msi`), `.cpl`, ClickOnce
  (`.application`, `.appref-ms`), WSH (`.vbs .vbe .wsf`), `.ps1` if absent.
- `lindos-run.desktop` `MimeType=` lists every type `lindos-run` handles except `.ps1` and disk
  images; `lindos-open-image.desktop` handles `.iso/.img` (AutoPlay); `mimeapps-lindos.list`
  defaults them; `.ps1` default = text editor (`org.xfce.mousepad.desktop`).
- Thunar actions: "Run with PowerShell", "Install into a C:\ drive (MSIX)", "Mount disk image",
  "Merge into a C:\ drive (.reg)".
- Doctor adds: DOSBox, PowerShell 7 (optional; install from Microsoft's repo with the **hard-coded**
  `…/config/ubuntu/24.04/packages-microsoft-prod.deb` — Mint's `VERSION_ID` 404s; no snap),
  cabextract, binfmt status + conflicts, casefold, udisks2, Wine WoW64 mode, python3-hivex.
- Errata fixed here: `riot-client.json` and `docs/WINDOWS-APPS.md` no longer claim League of
  Legends streams on GeForce NOW (removed 1 May 2024).

### 28.10 winget (`winget.py`, `wingetyaml.py`, `lindos-compat winget …`)

```
lindos-compat winget search <query> [--json] [--limit N]
lindos-compat winget show <PackageIdentifier> [--version V] [--json]
lindos-compat winget install <PackageIdentifier> [--version V] [--arch x64|x86] [--prefix NAME]
                             [--interactive] [--accept-package-agreements] [--dry-run] [--json]
lindos-compat winget list [--json]
lindos-compat winget update-index
```
```python
class WingetError(Exception): ...
INDEX_URL = "https://cdn.winget.microsoft.com/cache/source2.msix"
def load_index(*, fetch=None, cache_dir=None, max_age_s: int = 900) -> "sqlite3.Connection"   # checks metadata majorVersion==2 && minorVersion==0 else WingetError
def search(query: str, *, index=None, limit: int = 20) -> List[Dict[str, str]]    # case-insensitive over Id, Name, Moniker, Command, Tag (+ exact PFN/ProductCode)
def canonical_id(package_id: str, *, index=None) -> str                           # exact-case Id
def list_versions(package_id: str, *, fetch=None, index=None) -> List[str]        # newest first
def load_manifest(package_id: str, version: Optional[str] = None, *, fetch=None, index=None) -> Dict[str, object]
def effective_installers(manifest: Dict[str, object]) -> List[Dict[str, object]]  # root→installer inheritance (winget populator rules)
def select_installer(manifest, *, arch: Optional[str] = None, locale: Optional[str] = None) -> Dict[str, object]
def installer_argv(installer, *, windows_file: str) -> List[str]                  # split switches; tokens replaced
def download(installer, dest_dir: Path, *, fetch_stream=None) -> Path              # HTTPS-only final URL; SHA-256 must match
def install(package_id: str, **kw) -> Dict[str, object]
def compare_versions(a: str, b: str) -> int                                       # winget Version semantics
```
Data path (hash-chained, verified live): `source2.msix` (≈3.7 MB, ETag-cached, refresh ≤ every 15
min) → `Public/index.db` (sqlite3) → `packages/<Id exact case>/<hash[:8] lower>/versionData.mszyml`
(MSZIP; `sha256(raw) == packages.hash`) → merged manifest `cache/<rP>` (`sha256 == s256H`) →
installer (`sha256 == InstallerSha256`, case-insensitive). Fallback when the CDN layout is
unexpected: GitHub (`git/trees/master:<dir>?recursive=1`, then `raw.githubusercontent.com`),
honest message on 403/429. Path rule: `manifests/<id[0].lower()>/<id.replace('.', '/')>/<ver>/`.
YAML: `yaml.CBaseLoader`/`BaseLoader` from `python3-yaml` when importable (every scalar stays a
string), else `wingetyaml.parse` (stdlib subset: block maps/sequences both indent styles, comments,
single/double quotes incl. `\xNN \uNNNN \UNNNNNNNN`, multi-line folding, `|`/`>` block scalars
with chomping, empty `[]`, CRLF/BOM; refuses anchors/aliases/tags/complex keys). Installer
resolution copies root → installer, overrides scalars, merges `InstallerSwitches` per key, replaces
lists/Dependencies wholesale, inherits `Nested*` only for zip, fills missing switches from winget's
defaults by **effective** type (`msi/wix/burn`: `/quiet /norestart`; `nullsoft`: `/S`; `inno`: `/SP-
/VERYSILENT /SUPPRESSMSGBOXES /NORESTART`), always appends `Custom`, omits `Log`/`InstallLocation`,
replaces `<LOGPATH>`/`<INSTALLPATH>` tokens before splitting; NSIS `/D=` last and unquoted.
**Lindos selection (documented deviation from winget):** arch x64/x86/neutral (drop
UnsupportedOSArchitectures x64) → scope user > machine → system locale then en-US → type
msi/wix > inno > nullsoft > burn > exe > zip+portable > msix/appx (Wine cannot deploy MSIX; §28.4
handles them only when nothing else exists). Portable/zip: extract (zip-slip safe) to
`drive_c/users/<user>/AppData/Local/Microsoft/WinGet/Packages/<Id>_winget/`, only `.exe` targets,
record `PortableCommandAlias`. Before running: print License/LicenseUrl/Agreements (require `yes` or
`--accept-package-agreements`), Dependencies (offer to install through the same flow),
WindowsFeatures ("not available under Wine"), ElevationRequirement (Wine grants admin silently — a
trust note, not a blocker), exe-without-Silent (interactive only). `http://` URLs, `msstore` ids
(9N…/XP…), `pwa`, `font` → explained, never attempted. Exit codes mapped to messages (0 +
InstallerSuccessCodes ok; 3010/1641 ok+restart; 1602 cancelled; 1618 busy; 1638 already installed;
inno 2/5 cancelled). APPS_DB records `source="winget"`, `winget_id`, `winget_version`. `fetch`
injectable; tests never touch the network (fixtures modelled on real manifests: Edge, Notepad++,
PowerToys, FFmpeg, 7-Zip, VCRedist, Steam).

---

## 29. New package `lindos-transfer` — Windows Easy Transfer for Lindos

### 29.1 Layout

```
packages/lindos-transfer/
  DEBIAN/control postinst postrm      Depends python3, lindos-core; Recommends udisks2, ntfs-3g,
                                      rsync, zenity, network-manager, gir1.2-gtk-3.0, fontconfig,
                                      xdg-user-dirs; Suggests dislocker, cryptsetup-bin, libldm
  root/usr/bin/lindos-transfer                                   (W-D)
  root/usr/bin/lindos-transfer-gui                               (W-E)
  root/usr/lib/lindos-transfer/lindos_transfer/
      __init__.py secrets.py sources.py mounts.py regf.py winreg.py profiles.py plan.py
      copyengine.py browsers.py bookmarks.py fonts.py wallpaper.py wifi.py steam.py vdf.py
      apps.py report.py cli.py                                   (W-D)
      gui.py                                                     (W-E)
  root/usr/share/lindos/transfer/app-map.json                    (W-D)
  root/usr/share/lindos/transfer/windows/{LindosTransfer.ps1,LindosTransfer.cmd,README.txt}  (W-E)
  root/usr/share/applications/lindos-transfer.desktop            (W-E)
  root/usr/share/icons/hicolor/scalable/apps/lindos-transfer.svg (W-E)
  tests/ (test_transfer_*.py)
```

### 29.2 CLI (`lindos-transfer`, argparse, `--json` on every read)

```
lindos-transfer sources [--json]
lindos-transfer mount <device> [--json]
lindos-transfer users --from <PATH> [--json]
lindos-transfer plan  --from <PATH> [--user NAME] [--only CAT,…] [--exclude CAT,…] [--dest DIR]
                      [--firefox-passwords] [--json] [-o plan.json]
lindos-transfer run   (--plan plan.json | --from PATH [--user NAME] [--only …]) [--yes] [--dry-run]
                      [--json-progress]
lindos-transfer apps  --from <PATH> [--user NAME] [--json]
lindos-transfer install-apps --plan plan.json [--yes] [--dry-run] [--json]
lindos-transfer make-usb-kit <DIR> [--json]
lindos-transfer report [--json]
```
Exit codes 0 ok · 1 error · 2 usage · 4 nothing to transfer. `--from` accepts a mounted Windows
root (contains `Windows/` and `Users/`, matched case-insensitively) **or** a transfer folder
(contains `lindos-transfer.json`).

### 29.3 Sources & mounting (`sources.py`, `mounts.py`)

- `lsblk -J -b -o NAME,PATH,FSTYPE,LABEL,UUID,SIZE,MOUNTPOINT,TYPE,PARTTYPE,RO` → FSTYPE `ntfs`
  (never "ntfs3") → Windows candidate; `BitLocker` → locked; `refs`, LDM/Storage Spaces → explained
  ("use the Windows kit").
- **Mount read-only, ntfs-3g preferred**: `udisksctl mount -b <dev> -t ntfs -o ro` when
  `/sbin/mount.ntfs` (ntfs-3g) exists — it presents OneDrive placeholders as *unsupported reparse
  tag* symlinks instead of zero-filled files; else `-t ntfs3 -o ro` with **mandatory** placeholder
  detection (below). Expect a polkit prompt for internal disks (Ubuntu's desktop rules may skip it
  for `sudo` users) — never pass `--no-user-interaction`.
- **Hibernation / Fast Startup**: after the ro mount, read the first 4 bytes of `hiberfil.sys` —
  `hibr`/`HIBR` ⇒ hibernated: warn that **every** NTFS volume of that PC may be stale; offer "Restart
  Windows (Fast Startup doesn't apply to Restart) or shut down with `shutdown /s /t 0`, then come
  back", or continue read-only. Optionally read `HiberbootEnabled` from the offline `SYSTEM` hive
  via `Select\Current` → `ControlSet00N` (there is no `CurrentControlSet` offline).
- **BitLocker**: explain + commands only. `udisksctl unlock -b <dev> --read-only` in a terminal (it
  prompts for the 48-digit recovery key itself) or the file manager's unlock dialog. Noble's
  cryptsetup 2.7 **cannot** open device-encryption volumes that still have a *clear key* (local-
  account PCs, suspended BitLocker): for those print `sudo dislocker -r -c <dev> /mnt/bitlocker`
  or recommend the Windows kit. TPM-only unlock is impossible from Linux; link
  `https://aka.ms/myrecoverykey`.
- Removable media and `$HOME` are scanned (depth ≤ 2) for `lindos-transfer.json` bundles.
- `sources --json`:
```json
{"partitions":[{"device":"/dev/nvme0n1p3","label":"OS","size":512110190592,"fstype":"ntfs",
  "mountpoint":"/media/alice/OS","windows":true,"bitlocker":false,"hibernated":false,"note":""}],
 "bundles":[{"path":"/media/alice/USB/LindosTransfer-DESKTOP-1-20260926-1010","computer":"DESKTOP-1",
  "user":"alice","created":"2026-09-26T10:10:00Z"}]}
```

### 29.4 Users & folders (`profiles.py`, `winreg.py`)

Users from `SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList\<SID>\ProfileImagePath`
(folder names ≠ account names), skipping `Default`, `Default User`, `Public`, `All Users`,
`defaultuser0`, `WDAGUtilityAccount` and service SIDs (S-1-5-18/19/20); fallback: `Users/*` minus
the skip list. Folders from the user's `NTUSER.DAT` `…\Explorer\User Shell Folders`, resolving each
known folder **by its own value name** (legacy names `Desktop`, `Personal`, `My Music`, `My
Pictures`, `My Video`, `Favorites`; GUID names `{374DE290-123F-4565-9164-39C4925E467B}` Downloads,
`{4C5C32FF-BB9D-43B0-B5B4-2D72E54EAAA4}` Saved Games; never let the `Local*` GUIDs override),
expanding `%USERPROFILE%`, mapping drive letters via `SYSTEM\MountedDevices`, honouring OneDrive
Known-Folder-Move paths (`OneDrive\Documents`, `OneDrive - <Org>\…`); fallback to defaults.

### 29.5 Categories (binding ids) & destinations

`desktop documents downloads music pictures videos saved-games favorites onedrive bookmarks
firefox wallpaper fonts wifi apps steam-games`. Destinations: XDG user dirs (`xdg-user-dir
DOCUMENTS` …, fallback `~/Documents` …); `onedrive` → `~/OneDrive (from Windows)`; `saved-games`
→ `~/Documents/Saved Games`; generated artefacts (bookmark HTML, report) → `~/Documents/Transferred
from Windows/`.

### 29.6 Plan (`plan.py`) — JSON schema 1

```json
{"schema":1,"id":"20260926-103000-ab12","created":"2026-09-26T10:30:00Z",
 "source":{"type":"partition|bundle","root":"/media/alice/OS","computer":"DESKTOP-1","windows":"Windows 11 Pro 10.0.26100",
           "hibernated":false,"driver":"ntfs-3g|ntfs3|bundle"},
 "user":"alice","dest_home":"/home/alice",
 "items":[{"id":"documents","category":"documents","label":"Documents","src":"/media/…/Users/alice/Documents",
           "dest":"/home/alice/Documents","files":1234,"bytes":5678901,"selected":true,"notes":[]}],
 "apps":[{"windows_name":"Google Chrome","publisher":"Google LLC","version":"129.0","winget_id":"Google.Chrome",
          "actions":[{"type":"browser","id":"chrome","label":"Google Chrome for Linux"}],"chosen":0,"selected":true}],
 "options":{"firefox_passwords":false},
 "skipped":[{"path":"…/OneDrive/big.mkv","reason":"OneDrive online-only file (not on this disk)"}],
 "warnings":["Windows was hibernated (Fast Startup) — files may be slightly out of date"]}
```

### 29.7 Copy engine (`copyengine.py`)

Walk with `lstat`; **never follow** symlinks/junctions/reparse points. Skip: `desktop.ini`,
`Thumbs.db`, `~$*`, `NTUSER*`, `$RECYCLE.BIN`, `System Volume Information`, `AppData` (except the
explicit browser/font/wallpaper items), everything in `SECRETS_DENYLIST`. **Cloud-only placeholders**
are skipped and reported: on ntfs-3g they appear as symlinks whose target starts `unsupported
reparse tag`; on ntfs3 use `system.ntfs_attrib` (bit `0x400` REPARSE_POINT with `0x400000`
RECALL_ON_DATA_ACCESS or `0x1000` OFFLINE; not `0x40000` alone) or `st_blocks*512 < st_size` for
non-sparse regular files. EFS/dedup files (EOPNOTSUPP) → skipped+reported. Conflicts: identical
(size + mtime) → skip; else keep both as `name (from Windows).ext`. Preserve mtimes; verify size
after each copy; never copy xattrs/ACLs. Free-space check first. Resumable journal
`~/.local/state/lindos/transfer/<plan-id>/journal.jsonl`. `--json-progress` emits one JSON object
per line: `{"event":"start|item|file|progress|skip|error|done", "item", "done_bytes",
"total_bytes", "path", "message"}`.

### 29.8 Browsers, fonts, wallpaper, Wi-Fi, Steam

- **Chromium family** (Chrome, Edge, Brave, Opera, Vivaldi; `User Data/<profile>`): read
  `Bookmarks` and `AccountBookmarks` JSON (`roots.bookmark_bar|other|synced`, WebKit µs-since-1601
  dates → Unix seconds) → Netscape HTML `Bookmarks - <Browser> (<profile>).html`. If only
  `EncryptedBookmarks2` exists → "use the browser's own Export bookmarks". Passwords/cookies/
  payment data never transfer (Windows-encrypted) → report says *use browser sync or export
  passwords on Windows*.
- **Firefox**: never copy the whole profile. Create a new Linux profile `windows-import` (in
  `~/.mozilla/firefox` if it exists, else `~/.config/mozilla/firefox` — Firefox 147+) with
  `places.sqlite` (+ `-wal`) and `favicons.sqlite` (bookmarks + history), plus `key4.db` +
  `logins.json` **only with `--firefox-passwords`**; register in `profiles.ini` (default only if Linux
  Firefox has no profile); never copy `profiles.ini`/`installs.ini`/`extensions.json`/
  `compatibility.ini`. Always also write a bookmarks HTML (read `places.sqlite` from a temp copy,
  `?immutable=1`).
- **Fonts**: only user-installed fonts (`AppData/Local/Microsoft/Windows/Fonts`) →
  `~/.local/share/fonts/windows-user/` → `fc-cache -f`. Never `C:\Windows\Fonts`.
- **Wallpaper**: `AppData/Roaming/Microsoft/Windows/Themes/TranscodedWallpaper` (sniff magic for
  the extension) → `~/Pictures/Wallpapers/windows-wallpaper.<ext>`; style from `Control Panel\Desktop`
  (`WallpaperStyle`/`TileWallpaper`: Center/Tile/Stretch 2/Fit 6/Fill 10/Span 22) or the policy key
  `Policies\System` (own enum 0–5, checked first) → xfdesktop image-style → `lindos.theme`. Skip
  Microsoft stock/Spotlight images.
- **Wi-Fi**: offline → SSID, hidden, security type only (Wlansvc XML, match by local-name;
  `transitionMode` in the `…/profile/v4` namespace ⇒ `wpa-psk`), written as NM keyfiles with
  `psk-flags=1` (NetworkManager asks once); enterprise/802.1X skipped with a note. Bundle with
  `wifi.with_keys` → full import via helper `import-wifi` (stdin), only for XML whose
  `<protected>` is `false`.
- **Steam games**: parse `libraryfolders.vdf` + `appmanifest_*.acf` (`vdf.py`) → per-game items;
  copy `steamapps/common/<installdir>` (+ manifest) into the Linux library; the report tells the
  user to **force Proton for titles with a native Linux build before pressing Install**, then Steam
  "discovers existing files". Kernel-anti-cheat titles are listed with their §30 route, not as
  "transferred".

### 29.9 Apps (`apps.py`, `app-map.json`)

Inventory, best first: bundle `apps-winget.json` (from `winget export`, exact ids) and `apps.json`;
offline `SOFTWARE` (64-bit + `WOW6432Node`) and each `NTUSER.DAT` Uninstall keys via `regf.py`
(keep only entries with `DisplayName`; drop `SystemComponent=1`, `ParentKeyName`, `ReleaseType` in
Update/Hotfix/Security Update, `KB\d+`, redistributables/runtimes); fallback Start-Menu `.lnk` names.
`app-map.json` (schema 1): `{"schema":1,"apps":[{"id","match":["regex",…],"winget":["Id",…],
"publisher":[…],"actions":[…]}]}`; action types `builtin | apt | flatpak | browser | launcher |
recipe | winget | web | winapps | vm | not_possible`. Executors: apt → helper `install-packages`;
flatpak → helper `install-flatpaks`; browser → helper `install-browser`; launcher → helper
`install-gaming`; recipe/winget → `lindos-compat …`; web → `.desktop` web-app shortcut;
winapps/vm/not_possible → text. Nothing is installed without the user's selection. ≥ 80 entries.

### 29.10 Offline registry (`regf.py`, `winreg.py`)

Read-only, bounds-checked `regf` parser: base block (`regf`, root cell @0x24, hive-bins size @0x28,
minor 3–6, XOR checksum of dwords 0x000–0x1FB, primary/secondary sequence numbers → `dirty`);
`hbin` (32-byte header), cells (negative size = allocated), `nk` (KEY_COMP_NAME → ASCII/Latin-1 else
UTF-16LE), `lf/lh/li/ri`, `vk` (MSB of size = inline data; flag 0x0002 tombstone skipped), `db` big
data (>16344 bytes, minor > 3; concatenate segments). Loops/cycles and out-of-range offsets raise
`RegfError`. Dirty hives are **not** replayed: results carry `stale=True`. `winreg.py` exposes
`uninstall_entries(hive) -> List[dict]`, `user_shell_folders(ntuser) -> Dict[str,str]`,
`profile_list(software) -> List[dict]`, `current_control_set(system) -> str`,
`mounted_devices(system) -> Dict[str,bytes]`. Never opens hives in `SECRETS_DENYLIST`.

### 29.11 Windows-side kit (`LindosTransfer.cmd` + `LindosTransfer.ps1`, PowerShell 5.1, ASCII-only)

Double-click `LindosTransfer.cmd` (required: Windows' default policy is Restricted and "Run with
PowerShell" has no Bypass): `powershell.exe -NoProfile -ExecutionPolicy Bypass -File
"%~dp0LindosTransfer.ps1" %*`. The script relaunches itself 64-bit from a 32-bit host (Sysnative),
detects ConstrainedLanguage mode (explain + exit), never elevates except for the explicitly
requested Wi-Fi password step. Parameters: `-Destination <dir>` (default: the script's folder, e.g.
the USB stick), `-User`, `-Include <cats>`, `-IncludeWifiPasswords` (prompt: *"This writes your
Wi-Fi passwords in plain text into the transfer folder. Keep it private and delete it after
importing — deleting from a USB stick does not securely erase it."*; needs an elevated prompt),
`-IncludeSteamGames`, `-InventoryOnly`, `-WhatIf`. Output `LindosTransfer-<COMPUTERNAME>-<yyyyMMdd-HHmm>/`:
`files/<category>/` via `robocopy /E /COPY:DAT /DCOPY:DAT /R:1 /W:1 /XJ /XA:O /MT:8 /NP /NDL
/UNILOG+:<log> /XF desktop.ini Thumbs.db ~$*` (exit codes < 8 = success; `/XA:O` skips online-only
files without downloading them); `apps.json` (three Uninstall scopes, filtered as §29.9);
`apps-winget.json` via `winget export` when winget exists; `browsers/<browser>/<profile>/Bookmarks`
(+ `AccountBookmarks`); Firefox `places.sqlite`/`favicons.sqlite` (+ passwords files only if asked);
`wallpaper.<ext>`; `fonts/` (user fonts); `wifi/*.xml` (`netsh wlan export profile key=clear
folder=…` only with the switch); `steam/` (`libraryfolders.vdf` + `appmanifest_*.acf`; game folders
only with `-IncludeSteamGames`). Recommends an exFAT/NTFS stick (FAT32 fails > 4 GiB). JSON via
`ConvertTo-Json -InputObject … -Depth 10` with ISO-8601 UTC date strings; written with `Out-File
-Encoding utf8` (BOM) — **Lindos readers use `utf-8-sig`**. Manifest `lindos-transfer.json`:
```json
{"schema":1,"tool":"LindosTransfer.ps1","tool_version":"1.0.0","created":"2026-09-26T10:10:00Z",
 "computer":"DESKTOP-1","windows":{"caption":"Microsoft Windows 11 Pro","version":"10.0.26100"},
 "user":{"name":"alice","profile":"C:\\Users\\alice"},
 "folders":{"desktop":"files/desktop","documents":"files/documents"},
 "browsers":[{"browser":"chrome","profile":"Default","name":"Person 1","bookmarks":"browsers/chrome/Default/Bookmarks"},
             {"browser":"firefox","profile":"abcd.default-release","path":"browsers/firefox/abcd.default-release"}],
 "wallpaper":"wallpaper.jpg","wallpaper_style":"fill","fonts":"fonts","wifi":{"dir":"wifi","with_keys":false},
 "apps":"apps.json","winget_export":"apps-winget.json","steam":{"dir":"steam","games_copied":false},
 "skipped":[{"path":"C:\\Users\\alice\\OneDrive\\x.mkv","reason":"online-only"}]}
```
`apps.json`: `[{"name","version","publisher","install_location","scope":"machine|user","arch":"x64|x86"}]`.

### 29.12 GUI (`lindos-transfer-gui`, GTK 3, drives the CLI via subprocess `--json`)

Pages: Welcome → Source (detected partitions/bundles with their notes; "Use a transfer folder…";
"Make a transfer USB for another PC…") → User → What to bring (checkboxes + sizes; Firefox passwords
opt-in) → Apps (per-app choice from `actions`) → Transfer (progress from `--json-progress`) → Done
(report, "Open folder"). Import-safe without a display (gi guarded like every Lindos UI).

---

## 30. Play-anywhere — honest routes for games Linux cannot run

### 30.1 compat-matrix additions (`packages/lindos-gaming/root/usr/share/lindos/compat-matrix.json`)

Top level gains `"cloud_providers"` (only providers with a real way in from Lindos):
```json
"cloud_providers": {
  "geforce-now": {"name":"NVIDIA GeForce NOW","url":"https://www.nvidia.com/en-us/geforce-now/",
     "linux":"official-app","client":{"type":"flatpak","id":"com.nvidia.geforcenow","remote":"GeForceNOW"},
     "browsers_official":[],"regions_excluded":[],"subscription":"Free (1-h sessions) / Performance / Ultimate (100 h/month)",
     "note":"Official Linux app (Ubuntu 24.04). The browser client is not supported on Linux. Needs a GPU with Vulkan video decode."},
  "xbox-cloud": {"name":"Xbox Cloud Gaming","url":"https://www.xbox.com/play","linux":"browser-unofficial",
     "browsers_official":["edge","chrome"],"regions_excluded":[],
     "subscription":"Game Pass Essential/Premium/Ultimate (5/10/15 h/month from Nov 2026); NOT PC Game Pass; some free-to-play titles need no Game Pass",
     "note":"Works in Microsoft Edge or Google Chrome on Linux; Linux is not officially listed. Streams the console version."},
  "boosteroid": {"name":"Boosteroid","url":"https://boosteroid.com/downloads/","linux":"official-app",
     "client":{"type":"deb-download","page":"https://boosteroid.com/downloads/"},"regions":["EU","NA","BR"],
     "note":"Official Linux app (X11). Europe, North America, Brazil only."},
  "amazon-luna": {"name":"Amazon Luna","url":"https://luna.amazon.com/","linux":"browser-unofficial",
     "browsers_official":["chrome","edge"],"regions_excluded":["IN"],"note":"Linux not officially listed; not available in India."}}
```
Top level also gains `"disclaimer": {"badge","short","via","long","kinds":{…}}`, the **single source** of
the "Not supported yet" wording (§27 rule 7): `badge` is the label, `short` the one-line form (shown
above the routes), `via` a `{game}`/`{route}` template for a shortcut comment or a play line, `long`
the paragraph Settings shows and `docs/ANTI-CHEAT.md` §0 quotes verbatim, and `kinds` one cause
sentence per `unsupported_kind`. Settings, `lindos-game` and `tests/gen-compat-doc.py` read it; none of
them carries its own copy.
Each entry may gain:
```json
"unsupported_kind": "no-linux-version" | "publisher-disabled",
"routes": {"cloud":[{"provider":"geforce-now","url":"https://…","tier":"free|premium","note":"…"}],
           "windows": true, "windows_requires": ["secure-boot","tpm2"], "vm": false, "verified": "2026-09-26"}
```
`unsupported_kind` is optional, only on `status: not_possible` entries, and never replaces the status
(four consumers key on `not_possible`): `no-linux-version` = the anti-cheat exists only as a Windows
kernel driver, `publisher-disabled` = it has a Linux runtime the publisher has not enabled. It is
absent on the Xbox app / PC Game Pass.
Binding data (verified 2026-09-26): every `not_possible` entry has `routes` with `windows: true`,
`vm: false`. Cloud: **GeForce NOW** — Fortnite, Apex Legends (EA app), Rainbow Six Siege X, Destiny
2, Rust (premium), Delta Force, Battlefield 2042, Battlefield 6 (premium), Call of Duty; **Xbox
Cloud Gaming** — Fortnite (free, no Game Pass), Call of Duty Warzone (any Game Pass tier) / Black
Ops 7 (Ultimate, or purchase), Rainbow Six Siege (Free Access), Battlefield 2042 (Ultimate +
purchase); **Boosteroid** — Fortnite, Call of Duty; **Amazon Luna** — Fortnite. **No cloud route:**
Valorant, League of Legends (removed from GeForce NOW 1 May 2024), PUBG, GTA Online, Escape from
Tarkov. Never offer Boosteroid's "Install" remote desktop (a VM; Destiny 2 bans VMs).
`windows_requires`: Valorant `["secure-boot","tpm2"]`; League of Legends `["tpm2"]`; Call of Duty
`["secure-boot","tpm2"]`; Battlefield 6 `["secure-boot","tpm2"]`; Delta Force
`["secure-boot","tpm2"]` (being rolled out); Rainbow Six Siege `[]` (Ranked *Legend Division*:
secure-boot+tpm2+vbs, in `note`); Fortnite `[]` (tournaments need more, in `note`); Apex Legends,
GTA Online, Destiny 2, PUBG, Rust, Escape from Tarkov, Battlefield 2042 `[]`. Errata fixed in the
matrix/docs: no LoL-on-GeForce-NOW; Xbox Cloud Gaming is "Chrome or Edge (Linux not officially
listed)" — never Firefox; GeForce NOW on Linux = the official app; PC Game Pass does **not** include
cloud; Fortnite's anti-cheat is EAC (BattlEye removed June 2024); Apex moves to EA Javelin on
2026-09-29; Escape from Tarkov is on Steam. `tests/gen-compat-doc.py` renders an "Other ways to
play" section; `routes.verified` shown; a maintainer script may refresh data, runtime never scrapes.

**Maintenance policy for "Not supported yet".** An entry moves out of `not_possible` (to `works` or
`partial`, dropping `unsupported_kind`) only after a maintainer has checked the publisher's
announcement and Are We Anti-Cheat Yet? **and** tested the game; the same change bumps `updated` and
`routes.verified`, regenerates `docs/COMPATIBILITY.md`, and ships in a `lindos-gaming` update (a new
ISO or a sideloaded package until a Lindos update repository is hosted, `docs/UPDATES.md`). Nothing
is flipped on a rumour, a job posting or a date, and no "expected" field exists in the schema.

### 30.2 `lindos-game` additions

```
lindos-game route <title|exe|appid> [--json] [--region CC]
lindos-game play  <title> [--route cloud|windows|proton|native] [--provider ID] [--yes]
lindos-game shortcut <title> --route cloud|windows [--provider ID]
lindos-game cloud install geforce-now      # official Flatpak from NVIDIA's own remote: --user scope (no root) by default, --system via helper install-flatpaks
```
`route --json` → `{"title","id","status","anticheat","region","routes":[{"type":"native|proton|cloud|windows|vm",
"provider","label","available":bool,"why","requires":[…],"action":{…}|null}],"recommended":0,"notes":[…],
"disclaimer":{"badge","kind","cause","short","via"}|null}` (`disclaimer` is non-null only for an entry with
`unsupported_kind`; the text route output prints its `short` and `cause` right after the header).
`action` is `{"cmd":[argv…]}` for a directly launchable client (GeForce NOW Flatpak, Boosteroid), `{"open_url":"https://…",
"browser_path":"/usr/bin/…"}` for browser providers, or `null` when the route is unavailable. Provider `regions` may use the
group codes `EU`/`NA`, which `lindos-game` expands to ISO country codes before matching the user's region.
Availability: `windows` needs `lindos-dualboot status` → `can_reboot_to_windows`, and warns when
`windows_requires` includes `secure-boot` while Secure Boot is off (with the §30.3 checklist);
`cloud` needs the provider available in the region (`LC_ALL/LANG`/timezone → country, or
`--region`, or `~/.config/lindos/config.json` `"region"`) and its client (GFN Flatpak installed, or
Edge/Chrome for browser providers). `play --route cloud` launches the GFN app or opens the provider
URL in Edge/Chrome (never Firefox for xCloud/Luna; never a spoofed user agent); `play --route windows`
confirms, then runs `lindos-dualboot reboot-to-windows`. `shortcut` writes
`~/.local/share/applications/lindos-play-<id>-<route>.desktop` ("Valorant — restarts into Windows",
"Fortnite — Xbox Cloud Gaming"; its `Comment` starts with the disclaimer's `via` sentence for a disclaimed title). `lindos-run` on a `not_possible` title's exe prints `lindos-game
route <id>`. The Lindos VM is never offered for a `not_possible` title (VAN 9100, EOS "no VMs",
Bungie VM bans, Javelin).

### 30.3 `lindos-dualboot` (in `lindos-core`) and `lindos/dualboot.py`

```python
@dataclass
class BootEntry:
    num: str; label: str; active: bool
    loader: str        # e.g. "\\EFI\\Microsoft\\Boot\\bootmgfw.efi"
    partuuid: str      # from HD(…,GPT,<PARTUUID>,…) when present
    is_windows: bool   # loader == \EFI\Microsoft\Boot\bootmgfw.efi (case-insensitive); label secondary

def parse_efibootmgr(text: str) -> Dict[str, object]     # efibootmgr 18: "Boot0001* Label\t<devpath>"
def windows_entries(parsed) -> List[BootEntry]
def firmware(root: Optional[str] = None) -> str           # "uefi" if /sys/firmware/efi else "bios"
def secure_boot_state(root: Optional[str] = None, *, which=shutil.which, run=subprocess.run) -> str  # SecureBoot-8be4df61-93ca-11d2-aa0d-00e098032b8c efivar (5th byte) → enabled|disabled|unknown
def tpm_version(root: Optional[str] = None) -> Optional[int]   # /sys/class/tpm/tpm0/tpm_version_major
def grub_windows_entries(grub_cfg: str) -> List[Dict[str, str]] # [{"id":"osprober-efi-XXXX-XXXX","title":…}]
def grubenv_writable(root: Optional[str] = None, *, run=subprocess.run) -> bool   # False on btrfs/zfs/lvm/mdraid /boot
def status(*, run=subprocess.run, which=shutil.which, root=None) -> Dict[str, object]
def reboot_payload(st: Dict[str, object], entry: Optional[str] = None) -> Dict[str, object]
```
`status` → `{"firmware","secure_boot","tpm","windows_entries":[{num,label,partuuid,disk}],
"can_reboot_to_windows":bool,"method":"bootnext|grub-reboot|null","why",
"lindos_kernel_signed":bool|null,"bitlocker_hint":bool}`. CLI: `lindos-dualboot status [--json]`,
`lindos-dualboot reboot-to-windows [--entry XXXX] [--yes] [--no-reboot]`, `lindos-dualboot
firmware-setup [--yes]` (checklist first: Lindos kernel + DKMS modules MOK-signed or stock kernel
selected; *suspend BitLocker in Windows first* (a Secure-Boot change triggers recovery); disk must be
UEFI+GPT — **never** run `mbr2gpt` on a disk that also holds Lindos; then `systemctl reboot
--firmware-setup`), `lindos-dualboot shortcut`. UEFI uses one-shot `efibootmgr --bootnext XXXX`
(firmware clears it; next boot returns to Lindos). Several Windows entries → show which disk
(PARTUUID) each points to. Non-UEFI/no entry → `grub-reboot <osprober id>` (works regardless of
`GRUB_DEFAULT`) **only** when grubenv is writable; refuse on btrfs/zfs/LVM/mdraid `/boot` (the entry
would stick and every boot would go to Windows).

### 30.4 New helper actions (`lindos.helper.ACTIONS` + `lindos-helper` handlers)

| action | payload | effect |
|---|---|---|
| `reboot-to-windows` | `{"method":"bootnext","entry":"0001","reboot":true}` or `{"method":"grub-reboot","menuentry":"osprober-efi-…","reboot":true}` | helper re-runs `efibootmgr` (or reads the 0600 `grub.cfg`) **as root** and refuses unless the entry's loader is `\EFI\Microsoft\Boot\bootmgfw.efi` (or a `--class windows` os-prober id) and grubenv is writable; `efibootmgr --bootnext`/`grub-reboot`; `systemctl reboot` |
| `firmware-setup` | `{"confirm":true}` | `systemctl reboot --firmware-setup` |
| `import-wifi` | `{"networks":[{"ssid","security":"open|wpa-psk|sae","psk"?,"hidden":false,"agent_owned":false}]}` via **stdin** | writes `/etc/NetworkManager/system-connections/lindos-<safe>.nmconnection` root:root `0600`; `nmcli connection load <file>` |
| `set-binfmt` | `{"enabled":true|false}` | §28.7 commands; never restarts systemd-binfmt |

`lindos.helper.run_privileged` gains `stdin_payload: bool` so secrets never appear in argv/`/proc`.

---

## 31. Kernel support (`lindos-kernel`, `build/kernel/`)

1. `lindos.config` adds (required keys, unit-tested): `CONFIG_NTFS3_FS=m`,
   `CONFIG_NTFS3_LZX_XPRESS=y` (read CompactOS/WOF-compressed Windows files), `CONFIG_NTFS3_FS_POSIX_ACL=y`,
   `CONFIG_EXFAT_FS=m`, `CONFIG_UNICODE=y` (ext4 casefold), `CONFIG_BINFMT_MISC=y`,
   `CONFIG_EFIVAR_FS=y`, `CONFIG_DM_CRYPT=m`, `CONFIG_CRYPTO_USER_API_SKCIPHER=m` (cryptsetup
   BITLK), `CONFIG_BLK_DEV_LOOP=y`, `CONFIG_ISO9660_FS=m`, `CONFIG_JOLIET=y`, `CONFIG_UDF_FS=m`,
   `CONFIG_FUSE_FS=y` (ntfs-3g), `CONFIG_LDM_PARTITION=y` (Windows dynamic disks, MBR). Also
   required (unit-tested, CONTINUATION.md item 2 / boot-test run 36319809802): a working
   display/GPU stack, so a laptop (or a headless CI VM) with no bound KMS/DRM driver never hangs
   `graphical.target` waiting on seat0's `CanGraphical` — `CONFIG_DRM=y`,
   `CONFIG_DRM_KMS_HELPER=y`, `CONFIG_DRM_FBDEV_EMULATION=y`, `CONFIG_FRAMEBUFFER_CONSOLE=y`,
   `CONFIG_SYSFB_SIMPLEFB=y`, `CONFIG_DRM_SIMPLEDRM=y` (a firmware framebuffer becomes an early
   DRM device before/without a real GPU driver), `CONFIG_DRM_I915=m`, `CONFIG_DRM_XE=m`,
   `CONFIG_DRM_AMDGPU=m`, `CONFIG_DRM_RADEON=m`, `CONFIG_DRM_NOUVEAU=m` (real-laptop Intel/AMD/
   Nvidia GPUs), `CONFIG_DRM_BOCHS=m`, `CONFIG_DRM_VIRTIO_GPU=m`, `CONFIG_DRM_QXL=m` (QEMU/CI and
   other virtual GPUs). Confirmed present with these exact values in the real built
   `6.14.0-lindos` config (CI run 36319809802) — the Ubuntu base config (item 2 below) already
   carried all of it; these keys are a regression guard, not evidence of what caused that run's
   boot-test hang (see `CI-LOGS.md`'s corresponding entry for the real root cause).
2. **Base config** (fixes a latent bug: an upstream `defconfig` kernel lacks most real Wi-Fi/GPU/
   audio drivers): `build-kernel.sh --base-config ubuntu|defconfig|PATH`, default `ubuntu` = the
   Ubuntu *generic* flavour config (newest `/boot/config-*-generic`, else extracted from
   `linux-modules-*-generic` via `apt-get download` + `dpkg-deb --fsys-tarfile`), with
   `SYSTEM_TRUSTED_KEYS`/`SYSTEM_REVOCATION_KEYS` blanked (`scripts/config --set-str … ""`), then the
   fragment merged, then `olddefconfig`. `defconfig` remains for fast CI smoke builds. CI's kernel job
   passes `--base-config ubuntu`.
3. **Secure Boot**: `lindos-kernel secureboot status [--json]` and
   `/etc/kernel/postinst.d/zz-lindos-sbsign`: when the image is a `-lindos` kernel, a MOK exists
   (`/var/lib/shim-signed/mok/MOK.priv` + `MOK.der`) and `sbsign` is present, sign it (converting the
   DER cert to PEM in a temp file); otherwise print how to create/enroll one
   (`update-secureboot-policy --new-key` / `--enroll-key`, confirmed in MokManager at next boot).
   The grub drop-in selects the Lindos kernel only when Secure Boot is off **or** the image is signed
   — turning Secure Boot on for Windows games never leaves Lindos unbootable. Note in docs: Microsoft's
   2011 UEFI CA expired June 2026; existing shims keep booting while it stays in db; keep
   `shim-signed` updated.

---

## 32. UI

- **OOBE** (`lindos-setup`): optional page `transfer` ("Bring your stuff from Windows") before
  `summary`; lists sources (`lindos-transfer sources --json`, worker thread) and records
  `Selections.transfer = {"enabled": bool, "source_type": "partition"|"bundle"|"", "source": str}` (the Done page
  passes `--from <source>` to `lindos-transfer-gui`). Nothing is copied during OOBE; the Done page launches
  `lindos-transfer-gui` when chosen.
- **Settings** (`lindos-settings`):
  - *Windows apps*: "File types Lindos opens" (`lindos-compat formats --json`), "Run .exe from the
    terminal" switch (`lindos-compat binfmt status|enable|disable --json`, conflicts shown), "Get apps
    with winget" (search + install), "Transfer from Windows…".
  - *Gaming*: "Games that need Windows": each blocked title with routes (`lindos-game route --json`),
    region selector, "Install GeForce NOW", "Restart into Windows" (`lindos-dualboot`), Secure-Boot
    checklist.
  - `pages.json` keywords: `transfer migrate easy transfer winget msix appx msi reg powershell dos
    dual boot restart into windows cloud gaming geforce now xbox cloud`.

---

## 33. Build, CI, docs, tests

- `lindos-meta` Depends `lindos-transfer (= 1.0.0)`; `30-lindos-debs.sh` installs it before
  `lindos-setup`. `lindos-compat` Recommends `dosbox-x`, `python3-yaml`, `python3-hivex`;
  Suggests `powershell`. `lindos-core` Recommends `efibootmgr`, `mokutil`.
- New docs: `docs/WINDOWS-FORMATS.md`, `docs/WINGET.md`, `docs/TRANSFER.md`, `docs/DUALBOOT.md`;
  updated: `WINDOWS-APPS.md`, `ANTI-CHEAT.md`, `GAMING.md`, `KERNEL.md`, `SETTINGS.md`, `FAQ.md`,
  `ARCHITECTURE.md`, `README.md`, `CONTINUATION.md`.
- Tests: hermetic pytest for every module (no network, no Wine, no root; pass on Windows and Linux);
  fixtures are **generated** by the tests (synthetic MSIX/bundles/malicious zips, synthetic regf
  hives, synthetic PE/NE/MZ headers) — never vendored third-party/Store packages; `.ps1` parse-checked
  with PowerShell when available (Windows CI) and statically checked otherwise; `SECRETS_DENYLIST`
  and `FORBIDDEN_TOKENS` scans cover `lindos-transfer`, `lindos-compat`, `lindos-core/dualboot`.
  `bash tests/run.sh` stays green.

### 33.1 Cross-component call map (extends §13, §18)

| Caller | Calls |
|---|---|
| `lindos-run` | `formats.detect/plan_action`; `msix.classify/inspect/install`; `dos.*`; `diskimage.*`; hint `lindos-game route` |
| `lindos-compat winget install` | `winget.download` → `lindos-run --prefix … <installer> <tokens…>` |
| `lindos-compat binfmt enable|disable` | helper `set-binfmt` |
| `lindos-transfer install-apps` | helper `install-packages|install-flatpaks|install-browser|install-gaming`; `lindos-compat recipes|winget` |
| `lindos-transfer run` (Wi-Fi) | helper `import-wifi` (stdin) |
| `lindos-game play --route windows` | `lindos-dualboot reboot-to-windows` → helper `reboot-to-windows` |
| `lindos-game cloud install geforce-now` | `flatpak --user` (NVIDIA remote); `--system` via helper `install-flatpaks` |
| lindos-settings | `lindos-compat formats|binfmt|winget --json`, `lindos-game route --json`, `lindos-dualboot status --json`, `lindos-transfer-gui` |
| lindos-setup | `lindos-transfer sources --json`; Done page → `lindos-transfer-gui` |

---

## 34. Work split (implementation ownership — one owner per file)

| Owner | Files |
|---|---|
| **W-A** compat formats | `lindos_compat/{formats,dos,diskimage,binfmt}.py`, `cli_run.py`, `runner.py`, `prefix.py`, `doctor.py`, compat `DEBIAN/*`, MIME xml, `*.desktop`, `mimeapps-lindos.list`, `thunar-uca-lindos.xml`, recipes (`riot-client.json` errata), `/usr/lib/binfmt.d/lindos-pe.conf`, `/usr/libexec/lindos/lindos-binfmt`, compat tests `test_formats*.py test_dos.py test_diskimage.py test_binfmt.py test_run_formats.py`, `docs/WINDOWS-FORMATS.md`, `docs/WINDOWS-APPS.md` |
| **W-B** MSIX | `lindos_compat/msix.py`, `tests/test_msix*.py` |
| **W-C** winget + compat CLI | `lindos_compat/{winget,wingetyaml}.py`, `cli_compat.py`, `tests/test_winget*.py`, `tests/test_cli_compat_w.py`, `docs/WINGET.md` |
| **W-D** transfer engine | `packages/lindos-transfer/` except W-E files |
| **W-E** transfer kit + GUI | `…/transfer/windows/*`, `lindos_transfer/gui.py`, `bin/lindos-transfer-gui`, `lindos-transfer.desktop`, icon, `tests/test_transfer_kit.py`, `tests/test_transfer_gui.py`, `docs/TRANSFER.md` |
| **W-F** play-anywhere | `packages/lindos-gaming/**`, `tests/gen-compat-doc.py`, `docs/COMPATIBILITY.md`, `docs/ANTI-CHEAT.md`, `docs/GAMING.md` |
| **W-G1** core | `packages/lindos-core/**` (`dualboot.py`, `lindos-dualboot`, `helper.py`, `lindos-helper`, tests, control), `docs/DUALBOOT.md` |
| **W-G2** kernel | `packages/lindos-kernel/**`, `build/kernel/**`, `build/tests/test_build_kernel.py`, `.github/workflows/ci.yml` kernel job only, `docs/KERNEL.md` |
| **W-H** UI | `packages/lindos-setup/**`, `packages/lindos-settings/**`, `docs/SETTINGS.md` |
| **W-I** integration | `lindos-meta`, `build/chroot/*`, `.github/workflows/ci.yml` (rest), `tests/*` (except gen-compat-doc.py), `README.md`, `CONTINUATION.md`, `docs/{FAQ,ARCHITECTURE,BUILDING}.md`, `SPEC.md` pointer, cross-owner requests |
