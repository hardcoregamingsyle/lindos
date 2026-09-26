LINDOS TRANSFER KIT - move your files from this Windows PC to Lindos
====================================================================

What it does
------------
The kit copies your personal files and a few settings from this Windows PC
into ONE folder on a USB stick or external drive. On your Lindos PC, open
"Transfer from Windows", choose "Use a transfer folder" and pick that
folder: Lindos puts everything in the right place.

The kit only COPIES. Nothing on this PC is changed, moved or deleted.

You need Windows 10 or Windows 11.


Before you start
----------------
1. Use a USB stick or external drive with enough free space. The kit
   measures your folders first and tells you if the drive is too small,
   before it copies anything.
2. Drives formatted as exFAT or NTFS work best. FAT32 drives (many small
   or older USB sticks) cannot hold files bigger than 4 GB: such files are
   skipped and listed, never cut in half.
3. If you use Firefox, close it, so its bookmarks and history copy cleanly.
4. OneDrive: files that are only in the cloud (the cloud icon in File
   Explorer) are NOT downloaded by the kit. They are listed instead. You
   can get them on Lindos from onedrive.com, or right-click them in
   Windows and choose "Always keep on this device" before running the kit.


Steps
-----
1. If you downloaded the kit as a .zip file, extract it first
   (right-click the zip > Extract All).
2. Copy the whole LindosTransfer folder to your USB stick (if it is not
   there already).
3. Open the USB stick in File Explorer and double-click
   LindosTransfer.cmd
4. If Windows says "Windows protected your PC", click "More info" and then
   "Run anyway". (The kit is a plain text script; you can read it with
   Notepad.)
5. A black window shows what the kit is doing. Wait until it says "Done!"
   and press a key to close it.
6. Safely remove the USB stick, plug it into your Lindos PC and open
   "Transfer from Windows" from the menu. Choose "Use a transfer folder"
   and pick the folder called LindosTransfer-<your PC name>-<date>.


What is copied
--------------
- Your Desktop, Documents, Downloads, Music, Pictures, Videos, Saved Games
  and Favorites folders (also when they were moved into OneDrive).
- The OneDrive files that are already on this PC.
- Bookmarks from Google Chrome, Microsoft Edge, Brave, Opera and Vivaldi.
- Firefox bookmarks and history.
- Your desktop wallpaper (if it is your own picture).
- Fonts you installed yourself.
- The names of your Wi-Fi networks (Lindos asks for each password once).
- A LIST of the programs installed on this PC (names only). Lindos uses it
  to suggest the Linux version, a replacement, or a way to run the Windows
  version. Programs themselves cannot be copied.
- The list of your installed Steam games (the game files themselves only
  if you ask for it, see below).


What is never copied, and why
-----------------------------
- Saved browser passwords, cookies and credit cards. Windows keeps them
  encrypted so that they only work on this PC. Use your browser's own
  sync, or export the passwords from the browser's settings.
- Windows itself, and the fonts and pictures that come with Windows: they
  are licensed to this PC only.
- System files, other people's files, and anything the kit cannot read.
- Files that are only in OneDrive or another cloud (see above).


Options (for people who like a command line)
--------------------------------------------
Open PowerShell in the kit's folder and run, for example:

  powershell -NoProfile -ExecutionPolicy Bypass -File .\LindosTransfer.ps1 -WhatIf
      Test run: shows what would be copied and how big it is. Writes nothing.

  ... -File .\LindosTransfer.ps1 -Include documents,pictures
      Copy only some things. Names: desktop, documents, downloads, music,
      pictures, videos, saved-games, favorites, onedrive, bookmarks,
      firefox, wallpaper, fonts, wifi, apps, steam-games

  ... -File .\LindosTransfer.ps1 -Destination E:\
      Put the transfer folder somewhere else (default: next to the kit).

  ... -File .\LindosTransfer.ps1 -InventoryOnly
      Copy nothing; only save the list of apps and show how much space a
      full transfer needs.

  ... -File .\LindosTransfer.ps1 -IncludeSteamGames
      Also copy your installed Steam games (can be very large).

  ... -File .\LindosTransfer.ps1 -IncludeWifiPasswords
      Also export your Wi-Fi passwords. The kit asks first, and Windows asks
      for administrator permission for this step only. The passwords are
      written IN PLAIN TEXT into the transfer folder: keep it private and
      delete it after importing. Deleting from a USB stick does not
      securely erase it.

  ... -File .\LindosTransfer.ps1 -IncludeFirefoxPasswords
      Also copy Firefox's saved-password files unchanged (asks first).
      Firefox Sync is the safer way to move passwords.

  ... -File .\LindosTransfer.ps1 -User <name>
      Copy another person's files (only what your account may read; for
      the best result sign in as that person and run the kit there).


If something goes wrong
-----------------------
- "cannot run on this PC" / restricted mode: your PC blocks scripts
  (company policy or Smart App Control). The kit does not get around that.
  Copy your folders to the USB stick with File Explorer instead. On a PC
  that has both Windows and Lindos, "Transfer from Windows" on Lindos can
  also read the Windows drive directly (it never changes it).
- "Not enough free space": use a bigger drive, or copy fewer things with
  -Include.
- Some files could not be copied: the transfer folder contains
  LindosTransfer.log (what happened) and robocopy-log.txt (every file).
- Exit codes (for scripts): 0 done, 1 finished with errors, 2 wrong
  options, 3 PowerShell is restricted on this PC, 4 nothing to transfer,
  5 not enough free space.

Lindos is free software (GPL-3.0-or-later). It is not Windows and is not
made by Microsoft.
