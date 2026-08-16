<!-- GENERATED FILE — DO NOT EDIT BY HAND.
     Source : packages/lindos-gaming/root/usr/share/lindos/compat-matrix.json
     Rebuild: python3 tests/gen-compat-doc.py        (CI runs: --check)
     Edit the JSON, then regenerate; tests/run.sh fails if this file is stale. -->

# Lindos game compatibility matrix

> **Reality check.** Lindos runs Windows games through Wine/Proton, a translation layer, not through Windows. Whether a *multiplayer* game runs is decided by its **anti-cheat**, and that is the publisher's decision, not ours: **Valorant (Vanguard), Fortnite (Epic disabled EAC-Linux), League of Legends (Vanguard), Apex Legends (disabled Nov 2024), Rainbow Six Siege, Destiny 2 and PUBG do not run on any Linux, including Lindos.** Roblox works through **Sober** (a Linux runtime for the Android build), not the Windows client. Minecraft Java is native. For everything else the authoritative, always-current sources are [ProtonDB](https://www.protondb.com/) and [Are We Anti-Cheat Yet?](https://areweanticheatyet.com/) — statuses below are a snapshot and can change with a single publisher update.

This page is generated from the same data Lindos Settings shows in **Gaming → Compatibility** (`/usr/share/lindos/compat-matrix.json`). To correct or add an entry, edit the JSON in `packages/lindos-gaming` and run `python3 tests/gen-compat-doc.py`.

## Legend

| Status | Meaning |
|---|---|
| **Native** | The developer ships a Linux build. No compatibility layer involved. |
| **Works** | Runs through Proton (Steam / umu-launcher / Heroic), Wine (Lutris) or a dedicated Linux client. Performance is near-native; expect the occasional launcher quirk. |
| **Partial** | Runs, but with known limitations (missing features, unstable launcher, third-party tooling, or online modes that need verifying). |
| **Broken** | Does not currently work for a technical reason that could change (regressions, launcher updates). Not an anti-cheat block. |
| **Not possible** | **Will not run on any Linux distribution, including Lindos.** The publisher either uses a kernel-level anti-cheat with no Linux build or has explicitly disabled the Linux support of their anti-cheat. Nothing in Lindos can change this; only the publisher can. |
| **Unverified** | Entries whose status string is not one of the recognised values. Treat as unverified. |

## Summary

56 titles — Native: 6, Works: 28, Partial: 7, Not possible: 15.

Columns: **How** = launcher / runtime used on Lindos · **Anti-cheat** = system the game uses (blank if none) · **Reason / notes** = why it has this status and what to expect · **Link** = ProtonDB / Are We Anti-Cheat Yet / project page.

## Native Linux builds (6)

The developer ships a Linux build. No compatibility layer involved.

| Game | How | Anti-cheat | Reason / notes | Link |
|---|---|---|---|---|
| **Counter-Strike 2** | Native | VAC | Native Linux build from Valve; VAC works. Third-party leagues (FACEIT, ESEA) use their own kernel anti-cheat and are not available on Linux. | [protondb.com](https://www.protondb.com/app/730) |
| **Dota 2** | Native | VAC | Native Linux build from Valve. | [protondb.com](https://www.protondb.com/app/570) |
| **Minecraft Java Edition** | Native | none | Native Linux Java build. Prism Launcher (lindos-game install prism) manages instances, mods, shaders and bundled Java runtimes; a Microsoft account that owns Minecraft is required. Multiplayer servers work exactly as on Windows. | [prismlauncher.org](https://prismlauncher.org/) |
| **Stardew Valley** | Native | none | Native Linux build; SMAPI mods work. | [protondb.com](https://www.protondb.com/app/413150) |
| **Terraria** | Native | none | Native Linux build; tModLoader too. | [protondb.com](https://www.protondb.com/app/105600) |
| **War Thunder** | Native | Easy Anti-Cheat / BattlEye (native client) | Gaijin ships a native Linux client (Steam or standalone launcher). | [protondb.com](https://www.protondb.com/app/236390) |

## Works (Proton / Wine / Linux launcher) (28)

Runs through Proton (Steam / umu-launcher / Heroic), Wine (Lutris) or a dedicated Linux client. Performance is near-native; expect the occasional launcher quirk.

| Game | How | Anti-cheat | Reason / notes | Link |
|---|---|---|---|---|
| **Among Us** | Steam/Proton | none | Runs through Proton with cross-play (Steam Deck Verified). | [protondb.com](https://www.protondb.com/app/945360) |
| **Baldur's Gate 3** | Steam/Proton | none | Runs through Proton (Vulkan renderer recommended); GOG version via Heroic. Steam Deck Verified. Cross-save/multiplayer work. | [protondb.com](https://www.protondb.com/app/1086940) |
| **Battle.net (launcher)** | Lutris | Blizzard Warden (server-side) | Lutris' Battle.net installer sets up the client under Wine; Overwatch 2, WoW, Diablo, Hearthstone, StarCraft II work. Call of Duty titles do NOT (Ricochet). | [lutris.net](https://lutris.net/games/battlenet/) |
| **Cyberpunk 2077** | Steam/Proton | none | Runs through Proton (Steam or GOG via Heroic) including Phantom Liberty; ray tracing/FSR work with recent Mesa/NVIDIA drivers. | [protondb.com](https://www.protondb.com/app/1091500) |
| **Dead by Daylight** | Steam/Proton | Easy Anti-Cheat (Proton enabled) | Behaviour enabled EAC for Proton on Steam and Epic (Heroic); Steam Deck Verified. | [protondb.com](https://www.protondb.com/app/381210) |
| **Deadlock** | Steam/Proton | VAC | Valve's shooter runs through Proton with VAC (no native build yet). Verify current state on ProtonDB — early-access title. | [protondb.com](https://www.protondb.com/app/1422450) |
| **Diablo IV** | Steam/Proton | Blizzard Warden (server-side) | Steam version through Proton (Steam Deck Verified) or Battle.net via Lutris. | [protondb.com](https://www.protondb.com/app/2344520) |
| **Elden Ring** | Steam/Proton | Easy Anti-Cheat (Proton enabled) | Runs through Proton with EAC enabled; co-op/invasions work. Steam Deck Verified. Nightreign works the same way. | [protondb.com](https://www.protondb.com/app/1245620) |
| **Epic Games Store (launcher)** | Heroic | none | Use Heroic (lindos-game install heroic) instead of Epic's own launcher: it downloads, installs and runs Epic/GOG/Amazon titles through Proton. Individual games keep their own anti-cheat status (Fortnite: not possible). | [heroicgameslauncher.com](https://heroicgameslauncher.com/) |
| **Forza Horizon 5** | Steam/Proton | none (Arbiter, works) | Steam version runs through Proton with online play; Steam Deck Verified. The Microsoft Store/Game Pass version is not installable on Linux. | [protondb.com](https://www.protondb.com/app/1551360) |
| **Grand Theft Auto V (Story Mode)** | Steam/Proton | none in Story Mode | Single-player Story Mode runs well through Proton (Legacy and Enhanced editions). Turn BattlEye off in the Rockstar Games Launcher settings (Rockstar's FAQ describes this for Story-Mode-only play), then start Story Mode. GTA Online is a separate entry below. | [protondb.com](https://www.protondb.com/app/271590) |
| **Halo Infinite** | Steam/Proton | Easy Anti-Cheat + Arbiter (Proton enabled) | 343 enabled EAC for Linux in March 2024; multiplayer works with current Proton (use Proton Experimental or GE if a season update breaks it). | [protondb.com](https://www.protondb.com/app/1240440) |
| **Helldivers 2** | Steam/Proton | nProtect GameGuard (Proton enabled) | GameGuard was enabled for Proton at launch; runs well through Proton (Steam Deck Playable). | [protondb.com](https://www.protondb.com/app/553850) |
| **Hogwarts Legacy** | Steam/Proton | none | Runs through Proton (Steam Deck Verified). Needs vm.max_map_count raised — Lindos ships /etc/sysctl.d/80-lindos-gaming.conf for exactly this. | [protondb.com](https://www.protondb.com/app/990080) |
| **Hunt: Showdown 1896** | Steam/Proton | Easy Anti-Cheat (Proton enabled) | Crytek enabled EAC's Linux support in 2023; runs through Proton. | [protondb.com](https://www.protondb.com/app/594650) |
| **Lost Ark** | Steam/Proton | Easy Anti-Cheat (Proton enabled) | Amazon enabled EAC for Proton in 2022; runs through Proton (Steam Deck Playable). | [protondb.com](https://www.protondb.com/app/1599340) |
| **Marvel Rivals** | Steam/Proton | Easy Anti-Cheat (Proton enabled) | NetEase enabled EAC's Linux support at launch; runs through Proton and is Steam Deck Verified. | [protondb.com](https://www.protondb.com/app/2767030) |
| **New World: Aeternum** | Steam/Proton | Easy Anti-Cheat (Proton enabled) | Amazon enabled EAC's Linux support; runs through Proton (Steam Deck Playable). | [protondb.com](https://www.protondb.com/app/1063730) |
| **Overwatch 2** | Lutris | Blizzard Warden (server-side) | Runs through Proton on Steam, or via the Battle.net client installed by the Lutris 'Battle.net' script. Blizzard tolerates Wine/Proton. Steam Deck Verified. | [protondb.com](https://www.protondb.com/app/2357570) |
| **Palworld** | Steam/Proton | none | Runs through Proton including dedicated/co-op servers; Steam Deck Playable. | [protondb.com](https://www.protondb.com/app/1623730) |
| **Path of Exile 2** | Steam/Proton | none blocking | Runs through Proton (Vulkan renderer); Steam Deck Playable. Path of Exile 1 works the same way. | [protondb.com](https://www.protondb.com/app/2694490) |
| **Roblox** | Sober | Hyperion (Byfron) — Windows client only | Roblox's Windows player blocks Wine (Hyperion anti-cheat), but the community Sober runtime runs the official Android build natively on Linux with the normal Roblox account. Install: lindos-game install sober. Not affiliated with Roblox Corporation. | [sober.vinegarhq.org](https://sober.vinegarhq.org/) |
| **Rocket League** | Heroic | none blocking (server-side) | The Linux-native build was discontinued in 2020, but the Windows version runs through Proton: Epic version via Heroic, Steam version for existing owners. Cross-play and ranked work; not listed as blocked on areweanticheatyet.com. | [protondb.com](https://www.protondb.com/app/252950) |
| **Sea of Thieves** | Steam/Proton | Easy Anti-Cheat (Proton enabled) | Rare enabled EAC for Proton; Steam version runs (Steam Deck Playable). Microsoft Store/Game Pass version is not installable on Linux. | [protondb.com](https://www.protondb.com/app/1172620) |
| **Squad** | Steam/Proton | Easy Anti-Cheat (Proton enabled) | EAC enabled for Proton; runs with occasional launcher quirks (verify ProtonDB after major patches). | [protondb.com](https://www.protondb.com/app/393380) |
| **The Finals** | Steam/Proton | Easy Anti-Cheat (Proton enabled) | Embark enabled EAC's Linux support at launch; runs through Proton (Steam Deck Playable). | [protondb.com](https://www.protondb.com/app/2073850) |
| **Warframe** | Steam/Proton | Digital Extremes in-house (Proton enabled) | Digital Extremes officially supports Proton/Steam Deck; the launcher and game update fine. | [protondb.com](https://www.protondb.com/app/230410) |
| **World of Warcraft** | Lutris | Blizzard Warden (server-side) | Battle.net client installs through the Lutris 'Battle.net' script; WoW (Retail and Classic) runs well under Wine/DXVK. Blizzard tolerates Wine. | [lutris.net](https://lutris.net/games/battlenet/) |

## Partial (works with caveats) (7)

Runs, but with known limitations (missing features, unstable launcher, third-party tooling, or online modes that need verifying).

| Game | How | Anti-cheat | Reason / notes | Link |
|---|---|---|---|---|
| **EA app / Ubisoft Connect (launchers)** | Lutris | none (per game) | Both launchers install through Lutris/Heroic and run under Wine, but they are fragile (login loops, self-updates). Games with EA anticheat/Javelin (Battlefield 2042/6, Apex) or Ubisoft's BattlEye titles (Siege) remain not possible regardless. | [lutris.net](https://lutris.net/games/ea-app/) |
| **Fall Guys** | Heroic | Easy Anti-Cheat (Epic Online Services, not officially enabled for Linux) | Epic/Mediatonic do not officially support Linux, but the game currently runs through Proton GE / Proton Experimental (Epic version via Heroic, Steam version for pre-2022 owners). Community-reported as 'Running', can break with any update — verify before relying on it. | [protondb.com](https://www.protondb.com/app/1097150) |
| **Genshin Impact** | — | miHoYo Protect (mhyprot2) | Runs through Wine with the third-party 'An Anime Game Launcher' (AAGL) which patches the anti-cheat driver requirement. HoYoverse neither supports nor (so far) bans it, but that can change — use at your own risk. Not available through Steam. | [aagl.launcher.moe](https://aagl.launcher.moe/) |
| **Minecraft Bedrock Edition** | — | none | No official Linux build. The unofficial mcpelauncher (Flatpak io.mrarm.mcpelauncher; lindos-game install mcpelauncher) runs the Android version you own on Google Play. Marketplace and Realms may not work; Mojang does not support it. | [mcpelauncher.readthedocs.io](https://mcpelauncher.readthedocs.io/) |
| **Roblox Studio** | Vinegar | none | Roblox Studio (the editor, not the player) runs through Wine via Vinegar (Flatpak org.vinegarhq.Vinegar; lindos-game install vinegar). Works for building/testing places; occasional breakage after Studio updates. | [vinegarhq.org](https://vinegarhq.org/) |
| **Star Citizen** | Lutris | none | Runs through Wine/Proton with the community LUG helper (Lutris installer). Playable, but heavy: needs vm.max_map_count raised (Lindos ships it) and 16+ GB RAM; every patch can break it. Not on Steam. | [github.com](https://github.com/starcitizen-lug/lug-helper) |
| **The Sims 4** | Lutris | none | Runs through Proton, but the mandatory EA app is fragile under Wine (login loops, updates). Steam version via Proton is the smoothest; Heroic can also install the EA version. Mods work. | [protondb.com](https://www.protondb.com/app/1222670) |

## Not possible on Linux (15)

**Will not run on any Linux distribution, including Lindos.** The publisher either uses a kernel-level anti-cheat with no Linux build or has explicitly disabled the Linux support of their anti-cheat. Nothing in Lindos can change this; only the publisher can.

| Game | How | Anti-cheat | Reason / notes | Link |
|---|---|---|---|---|
| **Apex Legends** | — | Easy Anti-Cheat (Linux support disabled) | Worked on Proton until November 2024, when EA/Respawn disabled Linux and Steam Deck support in the name of anti-cheat. Launching now fails or results in an error; do not attempt with a valued account. | [areweanticheatyet.com](https://areweanticheatyet.com/game/apex-legends) |
| **Battlefield 2042** | — | EA anticheat (kernel driver) | EA's kernel-level anti-cheat (added 2023) has no Linux build; the game refuses to start under Proton. | [protondb.com](https://www.protondb.com/app/1517290) |
| **Battlefield 6** | — | EA Javelin (kernel driver, Secure Boot required) | Javelin is a Windows kernel driver that additionally requires Secure Boot/TPM attestation; EA has stated Linux and Steam Deck are not supported. | [protondb.com](https://www.protondb.com/app/2807960) |
| **Call of Duty (Warzone / Black Ops 6 / Modern Warfare III)** | — | Ricochet (kernel driver) | Ricochet is a Windows kernel-mode driver; Activision does not support Linux and bans are reported for attempts. Applies to every current Call of Duty title on Steam and Battle.net. | [protondb.com](https://www.protondb.com/app/1938090) |
| **Delta Force** | — | Anti-Cheat Expert / G-Presence (kernel driver) | Team Jade's kernel-level anti-cheat has no Linux component and the developers have said Linux/Steam Deck are not supported. | [protondb.com](https://www.protondb.com/app/2507950) |
| **Destiny 2** | — | BattlEye (Linux support not enabled by Bungie) | Bungie explicitly forbids running Destiny 2 through Proton/Wine and threatens bans; the game detects Linux and refuses to start. | [areweanticheatyet.com](https://areweanticheatyet.com/game/destiny-2) |
| **Escape from Tarkov** | — | BattlEye (Linux support not enabled by Battlestate) | The launcher and login can work under Wine, but raids require BattlEye's Linux runtime which Battlestate has not enabled — areweanticheatyet.com lists it as 'Planned', not working. Treat as not playable until that changes. | [areweanticheatyet.com](https://areweanticheatyet.com/game/escape-from-tarkov) |
| **Fortnite** | — | Easy Anti-Cheat + BattlEye (Linux support disabled by Epic) | Both anti-cheats support Proton, but Epic has explicitly chosen not to enable them for Linux and has repeatedly said it will not. Only Xbox Cloud Gaming / GeForce NOW streaming works, in a browser. | [areweanticheatyet.com](https://areweanticheatyet.com/game/fortnite) |
| **Grand Theft Auto Online** | — | BattlEye (kernel-level, Linux support not enabled by Rockstar) | Rockstar added BattlEye to GTA Online on 17 September 2024 without enabling its Linux/Proton support; Rockstar's FAQ states Steam Deck/Linux are not supported. Online has not worked since; Story Mode still does. Re-verify on areweanticheatyet.com — this is Rockstar's call, not a Lindos limitation. | [areweanticheatyet.com](https://areweanticheatyet.com/game/grand-theft-auto-v) |
| **League of Legends** | — | Riot Vanguard (kernel driver) | Ran under Wine/Lutris until Riot made Vanguard mandatory in 2024; since then it does not run on any Linux. Same for Teamfight Tactics on PC. | [areweanticheatyet.com](https://areweanticheatyet.com/game/league-of-legends) |
| **PUBG: Battlegrounds** | — | BattlEye + Zakynthos + Uncheater | Krafton uses several anti-cheats without Linux support; the game does not get past the anti-cheat check on Proton. | [protondb.com](https://www.protondb.com/app/578080) |
| **Rainbow Six Siege** | — | BattlEye + FairFight (Linux support not enabled by Ubisoft) | Ubisoft has not enabled BattlEye's Proton support and Siege X's kernel anti-cheat requirements make it worse; the game does not run on any Linux. | [areweanticheatyet.com](https://areweanticheatyet.com/game/rainbow-six-siege) |
| **Rust** | — | Easy Anti-Cheat (Linux support refused by Facepunch) | EAC supports Proton, but Facepunch stated (October 2022) they will not enable it for Linux/Steam Deck; the game kicks Linux clients from all official and most community servers. | [gamingonlinux.com](https://www.gamingonlinux.com/2022/10/facepunch-put-out-a-fresh-statement-on-rust-for-steam-deck-linux/) |
| **Valorant** | — | Riot Vanguard (kernel driver) | Vanguard is a Windows kernel-mode driver with no Linux build and Riot has stated it will not support Linux. Does not run on any Linux distribution, including Lindos — no workaround, and attempts risk the account. | [areweanticheatyet.com](https://areweanticheatyet.com/game/valorant) |
| **Xbox app / PC Game Pass** | — | n/a (UWP / Windows Store DRM) | The Xbox app and Microsoft Store game installs are Windows-only (UWP packaging, licensing) and cannot be installed under Wine. Xbox Cloud Gaming streaming works in a browser (Edge/Chrome/Firefox); Steam versions of the same games are usually fine. | [xbox.com](https://www.xbox.com/play) |

## How to read a status that is not listed here

1. Search the game on [ProtonDB](https://www.protondb.com/) — Platinum/Gold generally means *Works*, Silver/Bronze *Partial*, Borked *Broken*.
2. If it is multiplayer, check [Are We Anti-Cheat Yet?](https://areweanticheatyet.com/) — *Denied* or *Broken* there means *Not possible* here, whatever ProtonDB says.
3. Try it: `lindos-run game.exe` (Proton-GE via umu-launcher) or enable Steam Play for all titles in Steam → Settings → Compatibility. Report results with `lindos-tune report`.

_Generated from `packages/lindos-gaming/root/usr/share/lindos/compat-matrix.json` — 56 entries._
