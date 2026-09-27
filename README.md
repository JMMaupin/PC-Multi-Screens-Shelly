# Shelly PC Screens

Controls the power of a PC's screens and peripherals through one or more
**Shelly Power Strip 4 Gen4**, from an icon in the Windows notification
area. Screen profiles decide which outlets are powered; windows left on a
screen that has just been switched off are brought back onto a screen that
is on.

## At a glance

| | |
| --- | --- |
| Devices | **validated**: Shelly Power Strip 4 Gen4 (S4PL-00416EU, firmware 2.0.1-beta3); other Shelly devices with switchable outputs untested |
| Discovery | mDNS name `<device-id>.local`, then known address, then scan |
| Protocol | JSON-RPC over HTTP, optional SHA-256 Digest authentication |
| Installation | one executable, installed for every account of the PC |
| Dependencies | none — Python 3.12+ standard library (only to run from the sources) |

## Installation

Download `ShellyPCScreens-<version>.exe` from the
[releases page](https://github.com/JMMaupin/PC-Multi-Screens-Shelly/releases)
and run it from anywhere. It offers to:

| What is installed | Offered |
| --- | --- |
| Nothing | **Install**, or **Run without installing** |
| An older version | **Update** — the running application closes in every session, is replaced, and starts again |
| The same or a newer version | **Open** the installed copy |

Windows asks for an administrator once, through UAC. The application
itself never runs elevated.

**For every account, on purpose.** The application drives screens that
every account on the PC shares: one account cannot own them. It is
therefore installed in `C:\Program Files\Shelly PC Screens`, starts at
sign-in for every account, and appears in the common Start menu and in
*Settings → Apps → Installed apps*, where it is uninstalled — optionally
keeping the configuration and history.

**Coming from version 1.x** (run from the sources with
`install-startup.ps1`): the installation finds the old configuration
through its startup shortcut, imports it — passwords included, re-encrypted
for the machine — copies the power history, then removes the old shortcuts
so that both versions do not start side by side. The old files are left
untouched.

The executable is not signed yet: on the first launch of a downloaded
copy, SmartScreen shows *Windows protected your PC* → *More info* → *Run
anyway*.

### Running from the sources

For development, `python main.py` runs the application directly, and
`run-console.cmd` does so with a console showing the logs live.
`install-startup.ps1` still creates console-free shortcuts for that mode.

### Building the executable

```bat
build.cmd
```

produces `dist\ShellyPCScreens-<version>.exe`. PyInstaller lives in a
dedicated environment, `.venv-build`, pinned to one version: the system
Python is left untouched. To sign, set `SHELLY_SIGN_CERT` to a `.pfx` file
(and `SHELLY_SIGN_PASSWORD` if needed) with `signtool.exe` on the PATH.

## Where the data lives

| File | Contents | Who writes it |
| --- | --- | --- |
| `%ProgramData%\Shelly PC Screens\machine.json` | devices, outlets, roles, power sensing, sleep behaviour, the profiles a new account starts with | **an administrator** |
| `%ProgramData%\Shelly PC Screens\state\state.json` | what the application notes by itself: device addresses and passwords, outlets to restore on wake, screen positions | the application, any account |
| `%ProgramData%\Shelly PC Screens\history\` | power consumption history | the application, any account |
| `%ProgramData%\Shelly PC Screens\logs\` | one log per account | the application, any account |
| `%APPDATA%\Shelly PC Screens\user.json` | **this account's** profiles and preferences (shortcut, theme, language) | the account |

The hardware is shared, its use is personal: every account has its own
profiles, and a new account starts from the default ones. Each file is
written atomically, forced to disk, with the previous version kept as
`.bak` and used if the file is ever damaged.

`SHELLY_SCREENS_DATA` points everything at one folder instead — a sandbox
for development or tests.

### Hardware settings need an administrator

Devices, outlets, roles, power sensing and the sleep settings apply to every
account, and include the safeguards — the PC's outlet, critical outlets.
An ordinary account cannot change them by mistake: a change applies at
once, but a banner then offers **Save changes (administrator)** — one UAC
prompt for all of them — or **Discard**. Closing the window asks the same
question, so that nothing unapproved stays in force.

This protects against mistakes, not against a determined user: every
account's instance must be able to decrypt the power strips' password to
switch its screens, and the strips also have their own web page and
buttons.

### Several accounts signed in

Each account runs its own instance, but only **the session shown on the
screen drives the power strips**. The others turn their icon blue, keep the
consumption history and the log, and explain who drives when asked for a
profile or the settings. When the screen switches to another session
(fast user switching, unlocking), control follows — without switching
anything: the screens stay as the previous account left them. A Remote
Desktop session is not in front of these screens, so it never drives.

## In the notification area

The icon appears in the notification area: one box per outlet, green when
powered, grey otherwise, all red when nothing responds any more. Beyond four
outlets the boxes wrap onto two rows.

* **Right click**: profiles menu, outlet states, power consumption.
* **Left click**: settings window.

## Getting started

### 1. Plug in

Screens, USB hubs and the PC tower are spread across the power strips,
**which are themselves plugged directly into the wall**. None of them should
sit behind a master power strip: it would be switched off with everything
else, lose the network and take ten seconds or so to come back.

### 2. Add the devices

**Devices** tab → **Add device**. Scanning the network takes about twenty
seconds, so it does not start on its own: **Scan network** starts it. Typing
the address directly is instant.

**New or factory-reset device**: the **First setup of a new device...**
button opens a five-step wizard.

1. **The model** — for now, the Shelly Power Strip 4 Gen4.
2. **Open its access point**: buttons 1 and 4 together, 5 seconds; all four
   outlets blink red. Not 10 seconds: that would be a factory reset.
3. **Connect the PC to it**: the wizard lists the visible
   `ShellyPStripG4-…` access points and connects by itself (`netsh`, no
   administrator rights) — or you go through the Windows Wi-Fi menu, and it
   notices. *Next* only becomes active once the device answers at
   `192.168.33.1`, with its identity and firmware shown.
4. **The home Wi-Fi**: the 2.4 GHz networks the PC can see — the device's
   only band —, with their signal in dBm and its quality; below
   **-60 dBm**, the wizard flags it. Editable SSID (hidden network),
   maskable password.
5. **Sending**: `WiFi.SetConfig`, then waiting for real confirmation — the
   device must report an address (`got ip`); an accepted configuration
   proves nothing, since a wrong password is accepted too. On failure, the
   last status is shown and *Back* returns to the password.
   The PC then leaves the access point and gets its previous Wi-Fi back; the
   scan starts and **preselects the new device**, recognised by its MAC — or,
   failing that, queries it at the reported address.

**The device is never asked to scan for networks.** It has a single radio:
to scan, it leaves its access point's channel, and some units shut the
access point down altogether — setup then stops dead. That is why the
network list comes from the PC: the signal is an indication, all the more
accurate the closer the PC is to where the device will stay.

Each device gets a **short key** (`strip`, `strip2`, `plug`), and that is
what profiles refer to — an outlet is written `strip2:1`. The key can be
renamed at any time; references follow.

The **Signal** column gives the strength of the device's Wi-Fi link, along
with what it is worth: *excellent* above -60 dBm, *good* down to -70, *fair*
down to -78, *weak* below. A negative number in decibels only means
something to those used to it; the qualifier reads at a glance. A power
strip at -79 dBm hangs on at the edge of dropping out with nothing to warn
you, and its internal roaming threshold is precisely -80.
The signal is re-read once a minute, never more: a link does not change in
the blink of an eye, and every query puts load on the device.

Two columns separate **Reached via** and **IP address**. The app prefers to
reach the device by its mDNS name, which is more stable than its DHCP lease,
but the address is what you want to read — to open its web interface, or to
notice that it changed. **Clicking the address opens the device's web
interface** in the browser; the cursor turns into a hand on hover. The
*Open web UI* button does the same for the selected device.

### 3. Match each outlet to its screen

**Outlets** tab → **Identify displays**. The wizard switches each outlet off
in turn and watches which screen Windows removes, then switches it back on.
The other screens stay on meanwhile: the wizard cannot pull the rug out from
under itself. Allow about twelve seconds per outlet.

An outlet with no screen (USB hub, PC tower) simply comes out as not
identified, which is normal.

Identification relies on the monitor's interface path, which contains the
UID of the graphics output. Two screens of the same model therefore remain
distinct, and the match survives reboots.

### 4. Say what is plugged in

**Type** column, in the editor below the list:

| Type | For | Effect |
| --- | --- | --- |
| **Screen** | A screen | The only type included in the identification wizard |
| **Accessory** | USB hub, speakers | Still controllable by profiles, but **outside the screen scope** |
| **Not set** | Not filled in yet | **No automation touches it** |

**Declaring the type of each screen is mandatory.** The wizard only operates
what explicitly carries the *Screen* type; as long as an outlet stays *Not
set*, it leaves it alone and says so.

This is deliberately the opposite of a permissive choice: what we don't
know, we don't touch. A forgotten outlet is precisely the one whose load is
unknown — and that is the one you must not switch off just to see.

An accessory, for its part, will never make a screen disappear: testing it
would only waste time and cause a pointless power cut.

### 5. Assign the roles

Still in **Outlets**, three checkboxes decide what must never switch off at
the wrong moment:

The type says *what is at the end of the cord*; the roles below say *how to
treat the outlet*. The two are independent.

| Role | Effect | Tick it on |
| --- | --- | --- |
| **Boot screen** | Fallback when the remembered profile is unusable | The main screen |
| **Critical** | Never switched off, neither by a profile nor at shutdown | The keyboard's USB hub |
| **Powers the PC** | Same, and its power draw tells whether the PC is running | The PC tower |
| **Follows the PC** | Switched off while the PC sleeps | Screens; an accessory only if asked |

The *Follows the PC* box is ticked automatically as soon as an outlet is
declared *Screen* — that is the whole point of the setup. Accessories keep
it empty: switching off a USB hub or speakers is anything but obvious, and
doing it automatically has already caught people by surprise.

**Why these roles exist.** During POST, the BIOS and the sign-in screen,
nothing runs on the PC to control the outlets. A screen therefore cannot be
switched on at boot: it must already be on. Same for the keyboard — without
its USB hub powered, there is no way to enter the BIOS or type your PIN.

The **Behaviour** tab sums up in plain words what will stay powered at
shutdown.

### 6. Build the profiles

**Profiles** tab: tick the outlets each profile powers. Protected outlets
appear greyed out and ticked, since they are never switched off.

The **order** of profiles is set with **▲ Move up / ▼ Move down**, or by
**dragging** a profile in the list. It is the order of the icon menu and of
keys **1** to **9** in the shortcut window.

**All on** — *Tous en marche* in French — is a **built-in** profile, always
first: in the settings, the icon menu and the shortcut window, where it
answers key **1**. It switches on **every outlet**, including ones added
later, since it is recomputed on every use and never saved. It cannot be
edited, renamed or deleted, and its name is reserved in both languages. An
earlier configuration that had an "All on" profile sees it replaced if it
already switched on every screen, and renamed "All on (custom)" otherwise.

A profile says **which screens are on**, not what you do on them. *All on*
hosts CAD, trading, development and accounting in turn, each with its own
windows; the app therefore remembers no window layout per profile — that
would be the job of an activity profile, which it does not handle. It simply
brings back windows left on a switched-off screen (see *Lost windows are
brought back*).

Below the checkboxes, the **Screens** frame draws the screens **arranged as
on the Windows desktop**, each under its outlet's name. Those the profile
switches on are filled and outlined in green; those it switches off are just
a dotted outline. **Clicking a screen** switches its outlet on or off in the
profile, exactly like its checkbox.

Each screen is drawn at its **physical size**: the **diagonal** its **EDID**
reports — the VESA-standard identification block the screen sends over the
cable —, in the proportions of its resolution. A 27″ 4K and a 27″ QHD are the
same size on the map, as on the desk. Under the name: diagonal, resolution
and the scaling set in Windows.

* The EDID is read from the registry
  (`HKLM\SYSTEM\CurrentControlSet\Enum\DISPLAY\…\Device Parameters\EDID`),
  without administrator rights, and it stays there when the screen is off.
* It carries the size twice: in millimetres in the first timing descriptor,
  in centimetres in the header — or `0×0`, "undefined". The more precise of
  the two that is filled in wins.
* Only the **diagonal** is kept: some screens fill width and height from a
  template that does not even match their aspect ratio (609×355 mm for a
  16:9), while the diagonal remains plausible.
* **The EDID can lie.** Portable screens with a generic controller often
  share the same one: two UPerfect screens of different sizes both report
  27.8″ here. The map draws what is reported.
* A screen without a usable EDID keeps its **effective size** — its
  resolution divided by the Windows scaling —, converted at 96 dots per
  inch, the density Windows assumes at 100 %.

Since Windows coordinates are in pixels, they no longer fit together once
each screen is brought back to its real size: the map is rebuilt step by
step from the main screen, following shared edges.

Screen positions are the ones **Windows** defines. But Windows forgets a
screen as soon as its outlet is switched off, and may then shift the others
— with the main screen off, another one takes its place at 0,0. The app
therefore **remembers the layout** in `state.json` (`screens` section), and
only updates it when **every screen matched to an outlet is on**: that is the
only combination that places them all relative to each other.

Three occasions capture it:

| When | How |
| --- | --- |
| **On demand** | **Capture layout...** button under the map. The capture switches to **All on**: the map clears, the outlets that are off switch on, and each screen **appears on the map as Windows detects it**. Once the layout is captured, a dialog says what was learned and offers **Keep 'All on'** — which becomes the current profile, selected in the list so the map shows it lit; closing the dialog does the same — or **Back to '&lt;profile&gt;'**, which reapplies the previous profile, windows included. From the icon menu → *Screens* → *Capture screen layout*, with no window to ask the question in, everything goes straight back. A screen that Windows does not see come back makes the capture fail, naming it: an incomplete layout is never kept. |
| **Identify displays** | The wizard switches everything on for its tests: it captures the layout along the way, before putting the outlets back as they were. |
| **Continuously** | At launch, on every refresh and on every display change reported by Windows, as soon as everything is on — a profile such as *All on* is enough. Two successive readings must agree. |

**No capture without agreement.** Before each one, what the outlets say and
what Windows sees must match exactly; otherwise the capture is refused, and
the reason is shown under the map:

| Refused if… | Why |
| --- | --- |
| a *Screen* outlet is not linked to any screen | its screen could not be checked |
| the device of a screen outlet does not respond | its state is unknown |
| **a screen outlet is off** | Windows removed its screen and may have shifted the others — even if it still sees it, as with a screen powered from the PC's USB-C |
| **a screen outlet is on but Windows sees no screen** | screen asleep, cable, screen replaced |
| **the count does not add up**: screen outlets on ≠ physical screens detected | sums up everything else at a glance |
| an unknown physical screen is connected | see below |
| two outlets linked to the same screen, or two mirrored screens | the layout would be ambiguous |
| Windows has not finished arranging the screens | two readings 2 s apart differ |

**Virtual and wireless** screens (Parsec, Sunshine, spacedesk, Miracast) are
set aside before counting: the output technology reported by
`QueryDisplayConfig` identifies them. A USB dock (DisplayLink), on the other
hand, counts as a real screen.

**A screen plugged into the wall cannot be recognised by anything** —
neither the output technology nor the EDID say where its power comes from.
Only the **Identify displays** test proves it: once each screen outlet has
found its screen, those that stayed on through every power cut depend on
none. They are remembered as such (`unswitched_screens` in `machine.json`),
drawn in grey *on no outlet*, and counted separately. Any other unlinked
physical screen is **unknown** and blocks the capture until the wizard is
run again.

After moving a screen or changing its scaling in the Windows display
settings, **Capture layout...** updates the map right away.

Why switch everything on: Windows keeps **one layout per combination of
connected screens**. With only the LG and one UPerfect, it may place the
latter on the left, whereas it sits on top when all four are there — the
live map shows it during the capture. Only the full combination tells where
each screen is.

The icon menu → **Screens** gives the current layout in plain words —
*UPerfect 27 — left*, *UPerfect 24 — top, shifted right, above LG Ultra and
Acer QHD*.

## The PC tower's outlet is never switched off

Switching off a running PC loses the work in progress. The outlet carrying
the **Powers the PC** role is therefore protected by six independent locks,
not just one.

| Where | What it does |
| --- | --- |
| `device.py` | `set_switch(off)` on a protected output is refused **before anything is sent over the network** |
| `controller.py` | `set_outlet` and `_switch_many` refuse and log it |
| Identification wizard | only operates *Screen* outlets, excludes those drawing more than 80 W, and re-checks before each power cut |
| `pc_sensing` script | on the device, restores the output if the command came from elsewhere |
| Output `initial_state` | set to `on`, so that a power strip reboot leaves it powered |
| Outlet button | detached: pressing it on the power strip no longer toggles the output |

**Why several and not one.** The first bug came from exactly that: the only
protection was building the list of outlets to switch off correctly, and
`_switch_many` then sent the commands without re-checking anything. A badly
built list was enough. The lock now sits as close as possible to the network
call, where no code path can get around it.

**The 80 W threshold does not depend on any marking.** A monitor, even a
large one, rarely exceeds 60 W; a PC tower draws more than 100. This limit
therefore protects even if the role was never assigned, or was lost.

**What the on-device guard can and cannot do.** The firmware offers no way
to *refuse* a switch-off command: it can only be corrected after the fact.
Measured at about **180 ms**, while an ATX power supply only holds for 16 to
20 ms without mains. The guard restores power, **it does not prevent the
shutdown**. It is there against commands coming from elsewhere — mobile app,
cloud, another tool — not to catch a software bug. The real protection is
upstream.

Moving the role to another outlet moves the guard with it and updates the
locks immediately, without a restart.

### The fifth lock lives in the power strip

Each output has an `initial_state` setting that decides its fate **when the
device boots**, out of reach of both the script and the app. Shipped as
`off`, it opens every output at the slightest reboot — firmware update,
brief power dip, watchdog — the PC's outlet included.

It has happened: a firmware crash switched off the PC tower mid-session,
without any of the software protections getting a say. They all refuse
*commands*; this was not one.

The app therefore enforces this setting every time it connects to a device:
`on` for the PC's outlet and critical outlets, `restore_last` for the
others. A new device, a reset one or one coming back from a firmware change
thus regains its guarantees without anyone having to think about it.

Screens use `restore_last` rather than `off` for a specific reason: if their
power strip rebooted while the PC is running, `off` would leave them off
indefinitely — the script only reacts to changes in the PC's state, and that
would not have moved.

### The sixth: the outlet button

Each outlet on the Power Strip has its own button, which toggles it at the
slightest press — bypassing every protection above. A sweep of the broom, a
cable being tidied, and the PC goes off instantly.

The app therefore **detaches** the button of the PC's outlet (`in_mode:
detached`): it no longer controls anything, and the output can only be
driven by software. Like `initial_state`, this setting lives in the device
and is lost on reset; it is reapplied on every connection. It takes effect
live, without a restart.

## Really switching everything off: power-based detection

**PC power** tab. Once the PC tower is plugged into a metered outlet and
marked **Powers the PC**, the power strip can switch the screens back on by
itself as soon as it sees the PC drawing power. That is what makes it
possible to switch everything off at shutdown, boot screen included: during
POST no software runs on the PC, but the power strip keeps measuring.

The logic therefore lives in an **on-device mJS script**, not in the app.
The app only generates it, installs it, and leaves in the device's KVS the
list of outlets to switch back on at the next boot.

### The four settings

| Setting | Default | Role |
| --- | --- | --- |
| `PC seen as running above` | 25 W | Above this, the PC is considered active |
| `PC seen as off below` | 15 W | Below this, it is considered off |
| `Confirm before switching on` | 3 s | Short: we want to see the POST |
| `Confirm before switching off` | 90 s | **Long**, see below |

Two thresholds rather than one: between them lies a dead band where the
current state holds. Without it, a power draw hovering around a single value
would make the relay click in a loop — relays are rated for about 100,000
cycles.

The switch-off delay is deliberately long. When Windows restarts, the PC
drops below the threshold for ten to fifteen seconds; switching the screens
off at that exact moment would be the worst possible timing. Ninety seconds
lets a restart go by without a flinch.

### Calibrate from measurements, not guesswork

**Start measuring** installs a second script that samples the outlet and
summarises what it sees — minimum, maximum, histogram — in the KVS. Use the
PC normally: leave it idle, put it to sleep, shut it down, turn it back on.
**Read now** reads the results back and suggests thresholds.

The levels are separated at the largest gap in the histogram. A PC produces
four levels — off, asleep, idle, under load — and what matters is the gap
between "off or asleep" and "on". The wizard refuses to suggest thresholds
if it has seen only one level, or if the gap is too small to be reliable.

### What it does without the app

Once installed, the script lives its own life on the power strip: it works
with the PC off, the app closed, or even uninstalled.

| It sees | It does |
| --- | --- |
| The PC's power draw cross the upper threshold | Switches on **the outlets of the last profile** |
| It fall back below the lower threshold | **Switches everything off**, boot screen included |

### The boot screen is a fallback, not a privilege

When the remembered profile is usable, this outlet switches on — or not —
**along with the others**, like any other: if the profile does not include
it, it stays off.

It is only switched on automatically when the profile is worthless, and then
on its own: the PC is drawing power, so it is booting, and one screen is
better than booting blind. Four cases trigger this fallback, all verified on
the device:

| KVS content | Consequence |
| --- | --- |
| Valid list containing the boot screen | It switches on, like the others in the profile |
| Valid list **without** it | **It stays off** |
| Empty list | Fallback: it alone switches on |
| Unreadable text | Fallback |
| Index matching no outlet | Fallback |

The shape of the KVS value is checked **before** it is parsed: mJS has no
`try`/`catch`, and a `JSON.parse` on invalid text would abort the function
without ever reaching the fallback — the PC would then boot with no picture,
precisely the case the fallback is meant to cover.

Consequence on the app side: while detection is active, the boot screen **is
no longer kept powered at shutdown**. The script will switch it back on if
needed, and keeping it on would defeat the purpose — switching everything
off. If detection is disabled, it becomes the only guarantee of seeing the
POST again, and therefore stays powered.

The outlets to switch back on travel through the device's KVS: the app
writes the list there on every profile change. That is the only moment it
can tell the device — afterwards, it is no longer around.

If no profile has ever been applied, the KVS is empty and the script sticks
to the boot screen. To avoid that single-screen boot with no explanation,
the app publishes the **current state** at launch when it finds nothing:
what is on right now is most likely what you want to get back.

### What the script never touches

Outlets marked **Critical** are excluded from the script. That is where the
USB hub carrying the keyboard belongs: switched on at the same time as the
screens, it would not be enumerated early enough to enter the BIOS.

## Protecting the device with a password

**Devices** tab → **Password...**. Protection is **optional** and is set
from the app: enter it, confirm it, then *Apply to device*. The user is
always `admin` — only the password is chosen.

Without it, any device on the local network can control the outlets, run a
script on the power strip or change its Wi-Fi configuration, without having
to provide anything.

Three actions:

| Button | Effect |
| --- | --- |
| *Apply to device* | Sets the password on the device and remembers it |
| *Remember only* | Remembers a password already set elsewhere, without touching the device |
| *Remove password* | Removes the protection from the device |

### Where the password is stored

Encrypted with **DPAPI**, the Windows service designed for this, and the
ciphertext is stored in `state.json`. The key is the machine's: it is
neither in the file nor in the source code. A copy of the file taken to
another machine yields nothing.

The machine's key rather than an account's, because every account's
instance must be able to command the shared power strips. It is not a
vault: a program running **on this PC** can ask Windows to decrypt it. This
protects against the file travelling, not against a local user or malware
already in place. Encryption with a key embedded in the code would only
have been obfuscation.

The password sits with the state, not with the administrator's hardware
configuration, because it must mirror what the device holds: changing it
changes the device at once, and waiting for an approval that could be
refused would leave the application locked out.

### If the password is lost

The app detects it: the device shows `auth failed` in the list, a banner
appears in the **Devices** tab, the icon menu shows *Password refused*, and
a balloon warns you once. The **Recovery steps** button recalls the
procedure:

1. Unplug the power strip, then plug it back in.
2. Within the **first 60 seconds**, press buttons **1 and 4** together.
3. Hold them for **a full 10 seconds**, then release.

Releasing around 5 seconds only performs a *network reset*, which turns the
built-in Wi-Fi access point back on — **open** on this model. Hold for the
full 10 seconds.

A factory reset erases everything: password, Wi-Fi credentials, scripts and
outlet names.

## Profile shortcut

**Ctrl+Win+Alt+P**, from anywhere: a small window opens in the foreground,
centred on the main screen, with one button per profile. A click applies the
profile, without confirmation, and the window closes.

| Key | Effect |
| --- | --- |
| ↑ ↓ (or ← →) | moves the selection, wrapping around |
| Enter | applies the selected profile — or, if screens were toggled on the map, that selection |
| 1 to 9 | directly applies one of the first nine profiles |
| Esc, or the shortcut again | closes without changing anything |

The selection starts on the current profile, marked with a check mark.
Hovering with the mouse moves that same selection: there are never two
highlights. The window also closes by itself as soon as you click elsewhere.

Below the buttons, **the screen map**, drawn as in the settings. On opening,
it shows what is on right now; **as soon as the selection moves** — arrows,
Home, End, hover —, it shows **what the selected profile would give**.
**Clicking a screen** starts from what is displayed and toggles its planned
state, **without switching anything**: you can start from a profile and
adjust it for this one time. **Enter** or **Apply** carries out the
selection, Esc discards it; changing the selection discards the clicks, and
the map always shows what will happen if you confirm.

It is always a **one-off configuration, outside the profiles** — even if it
looks like one of them: the clicks **modify no profile**, and none becomes
"current". Only the toggled screens are switched, with the same sequence as
a profile (switch on first, switch off next, bring windows back). On wake,
the outlets return to how they were before sleep.

The shortcut is changed in **Behaviour** → **Profile shortcut**: Ctrl / Win
/ Alt / Shift checkboxes and a key (letter, digit or F1 to F12). Every edit
is checked with Windows: *available*, *already used by another program*, or
*active*. **Apply** only becomes active on a free combination, and a
combination without Ctrl, Alt or Win is refused — it would steal the key
from every other program. If the shortcut is taken at startup, a balloon
says so.

One limitation: a few Windows shortcuts, such as Win+L, do not go through
the registration mechanism. Their test answers "available", but Windows
intercepts them before the app does.

## LED rings and buttons

**Devices** tab → **LEDs...**. Shipped at full brightness, the outlets'
rings light up a room in the dark. The dialog sets, on the selected device
or on all Power Strips at once:

| Setting | Effect |
| --- | --- |
| *Power* | the colour follows the power drawn; one brightness |
| *State* | one colour when on, another when off, each with its own brightness |
| *Off* | rings off |
| *Night mode* | dims the rings between two times — suggested at 5 % from 22:00 to 07:00 |
| *Push buttons* | detaches an outlet's button, which no longer toggles it |

The firmware has **only one set of colours**, shared by all outlets: it
refuses any per-outlet colour. Night mode times follow the device's clock.

Settings apply live. If the device still asks for a restart — seen only
once, the very first time night mode was enabled — the *Restart to apply*
button takes care of it; since the relays are bistable, no outlet toggles.

The button checkbox for the PC's outlet is ticked and greyed out: the app
enforces it (see *The sixth: the outlet button*).

## Icon

The icon set is in `windows-icons/`, at the root, exactly as produced by the
`icongen_windows.py` generator from the `App web ico` project — the folder
is copied without being reorganised, so that regenerating it comes down to
a replacement.

Nine sizes — 16, 20, 24, 32, 40, 48, 64, 96 and 256 — bundled in
`icon.ico`, as BMP up to 48 px and PNG above, all with an alpha channel.
The PNGs alongside are used to compose the notification icon and the window
icon.

In the notification area, the icon is not static: the image carries a
**status dot** in the bottom right — green when screens are powered, grey
when everything is off, orange when a device is missing or refuses its
password, red when nothing responds any more. The outlet count and the
power fit in the tooltip: at sixteen pixels square, a dot can be read, a
count cannot.

The icon is composed on the fly, from the exact size requested rather than
from a single image that Windows would shrink. That requires reading the
PNG's pixels, which the standard library does not do: `win/images.py`
contains a minimal decoder, limited to what the app needs.

To change the icon, regenerate the set with `icongen_windows.py` and replace
the `windows-icons/` folder with the one it produces. The Windows
constraints are documented in
[docs/windows-icons.md](docs/windows-icons.md).

## Language

**Behaviour** tab → **Appearance** → **Language**: *Follow Windows*,
*English* or *Français*. English by default, like the rest of the app.

Changing the language **rebuilds the window**. Tk widgets read their text
once, when they are built; re-translating them afterwards would require
keeping a registry of every one of them, and the slightest omission would
leave a label in the old language. A half-translated window is worse than no
translation at all: it forces you to translate mentally at every glance.

The English text serves as the translation key. It therefore stays readable
in the code, and a missing entry falls back to English rather than showing
a bare identifier. The catalogue is in
[shelly_screens/locale_fr.py](shelly_screens/locale_fr.py); there is no file
to compile and no dependency.

Sentences with variable values use named fields rather than f-strings: an
f-string would be evaluated before translation, and the resulting string
would no longer work as a key.

## Themes

**Behaviour** tab → **Appearance**: *Follow Windows*, *Light* or *Dark*.
The change is immediate, without reopening the window, and in *Follow
Windows* mode a switch of the Windows theme is picked up within three
seconds.

The accent colour is taken from your Windows settings. It is not used as
is: on a dark background, the default blue would leave only 2.3:1 contrast
for white text. It is therefore lightened just as much as needed, without
touching its hue, and the colour of the text on top of it is chosen by WCAG
contrast calculation. In light mode the accent is kept as is, since it
already stands out.

The interface uses the ttk `clam` theme in all three cases. The native
`vista` theme looks nicer in light mode, but it draws its widgets with
system images: their backgrounds cannot be coloured and dark mode would stay
white in places. The title bar, for its part, does not belong to Tk — it
switches via `DwmSetWindowAttribute`.

## What the app does on its own

* **Windows sleep and shutdown** — switches off every outlet except the boot
  screen, critical outlets and the PC's. The switch-off is synchronous and
  without delay: Windows only grants a few moments before suspending the
  process.
* **Wake** — waits three seconds for the network to come back, then
  reapplies the last profile.
* **Profile change** — **switches on** the missing screens, waits for
  Windows to see them, **and only then** switches off the rest, and brings
  back windows left on a switched-off screen.

Switching on before switching off avoids ending up, even for an instant,
with no screen at all, and gives the panels their few seconds of
initialisation.

### Lost windows are brought back

At the end of a profile change, any window left outside the screens that are
on is moved to **the nearest screen that is on** — option *Behaviour* →
*Windows*, enabled by default.

* **Screen that is on** means listed by Windows **and** whose outlet is not
  switched off by the profile. A monitor powered from the PC's USB-C stays
  listed once its outlet is off: a window sitting on it is lost, even though
  Windows does not see it that way.
* **Lost** means its title bar cannot be grabbed on any screen that is on. A
  window straddling two screens, still grabbable, is left alone.
* The window keeps its size — shrunk only if it does not fit — and is placed
  as close as possible to where it was, without coming to the foreground.
  Minimised, it stays minimised and will come back in the right place;
  maximised, it is maximised again on its new screen.
* Only windows that were on a real screen are affected. Some programs
  deliberately park windows far away from the desktop: making them pop up
  would be a nuisance.

The log names every window brought back.

An unreachable device does not stop the others from responding: its outlets
are left as they are and flagged in the menu.

## Code layout

```
main.py                      entry point
build.cmd, build.py          builds the executable (PyInstaller)
windows-icons/               icon set, produced by icongen_windows.py
install-startup.ps1          console-free shortcuts, when running from the sources
run-console.cmd              diagnostic launch, with a console
shelly_screens/
  device.py                  Shelly Gen2+ JSON-RPC client
  device_services.py         optional device services (Matter, Cloud...)
  device_leds.py             Power Strip LED rings and buttons
  discovery.py               location: known address, mDNS, scan
  config.py                  configuration model, migration, persistence in three files
  paths.py                   where the program and its data live
  machine_admin.py           saving the hardware configuration through UAC
  installer.py               installation, update, removal
  controller.py              orchestration of devices / outlets / screens / windows
  sensing.py                 installing and monitoring the on-device scripts
  power_history.py           power consumption history (SQLite)
  screen_layout.py           outlet / screen agreement before a capture
  product.py                 author, links, validated devices (About tab)
  wifi_setup.py              first setup: access point, Wi-Fi
  app.py                     icon, menu, system events
  scripts/                   pc_sensing.js, pc_probe.js (run on the device)
  win/
    api.py                   shared ctypes, DPI awareness
    monitors.py              screen enumeration, stable key
    wlan.py                  the PC's Wi-Fi adapter (netsh): list, join, leave
    layout.py                windows left on a switched-off screen
    icon.py                  icon generation
    shell.py                 hidden window, notification area, messages
    hotkey.py                global shortcuts: reading, availability test
    session.py               which Windows session drives the devices
    elevation.py             running a short task through UAC
  ui/settings.py             settings window (tkinter)
  ui/setup_dialog.py         install, update and uninstall dialogs
  ui/history_window.py       power consumption history window
  ui/profile_picker.py       profile picker window (global shortcut)
  ui/screen_map.py           screen map in the Profiles tab
  ui/first_setup.py          first setup wizard
```

## Technical notes

**Outlet references.** An outlet is written `<device key>:<output>`, for
example `strip2:1`. The key is stable even if the IP address changes, and
renaming a key propagates to outlets and profiles.

**DPI awareness.** The process switches to Per-Monitor V2 as soon as
`win.api` is imported. Without it, Windows virtualises window coordinates on
scaled screens and the recorded positions are wrong.

**Screen identification.** `\.\DISPLAY1` is only an enumeration rank that
changes as soon as a screen turns on or off; the model cannot tell two
identical screens apart. We use the interface path returned by
`EnumDisplayDevices` with `EDD_GET_DEVICE_INTERFACE_NAME`, reduced to
`<hardware>#UID<output>`.

**Authentication.** SHA-256 Digest, with the user forced to `admin`. The
computation is the RFC 7616 one, with `ha2` derived from the method and the
URI. Shelly's documentation describes, for other firmwares, a constant `ha2`
computed over `dummy_method:dummy_uri`; **this firmware rejects it** —
verified on the device, only the standard computation works. The difference
matters: a constant `ha2` would make the response independent of the
request, and therefore replayable to trigger another command.

**IP addresses.** Resolution first tries the **mDNS name**
`<device-id>.local`, then the remembered address, then a scan of the local
subnets, checking the MAC address every time.

The name comes first even when the address still answers: a DHCP lease
renews without warning, and the remembered address ends up hard-coded
elsewhere — in the on-device script, which does not fix itself. The name, on
the other hand, follows the device: it survives a subnet change, a firmware
change, and even a factory reset, since it derives from the model and the
MAC address.

A first mDNS resolution queries the network over multicast and takes up to
three seconds, whereas a known address answers in a few milliseconds; it
therefore gets its own, more generous timeout.

Requests explicitly target **IPv4**: these devices also advertise a
link-local `fe80::` address, on which the connection fails for lack of a
scope identifier.

**Throttled reconnections.** A failed read no longer triggers a full
resolution: the device is left alone for 15 s, then 30, then 60, and the
counter resets as soon as it responds. The same resolution is not retried
more than once every half-minute. The old behaviour did the opposite — it
flooded an already struggling device with requests, which preceded two
watchdog resets.

**Configuration format.** Version 2 replaces version 1's single device with
a list. A version 1 configuration is migrated on the fly, with no
intervention.

## Power consumption history

Icon menu → **Consumption history...**, or **PC power** tab → **Open
history...**. A separate window, to review a day, a week or a month of power
consumption, **outlet by outlet**: an *Outlet* selector at the top left
switches from one to another. The PC's outlet opens first, then the window
remembers the last choice.

### What is recorded

**Every configured outlet** is tracked, each under its name, in a single
database. Nothing extra is queried: the history feeds on the readings the
app already takes every five seconds. It only keeps what tells something
new — a change of at least 3 % or 1 W, plus one anchor point per minute. The
points from a single reading are written in one transaction. Allow roughly
1 to 4 MB per outlet per month.

An outlet whose device does not respond leaves a gap, not a zero: we do not
invent a measurement we did not take.

**Only the PC's outlet is tracked during sleep.** The others are only
recorded live, while the app is running: during sleep, the screens are off
and only a few USB hubs draw power, which is not worth wearing out the power
strips' flash memory to log.

During sleep or shutdown, the app is no longer running, but the on-device
power logger in the power strip keeps measuring. At launch and on every
wake, its readings fill the gap. It now timestamps its last reading, which
locates a sleep period to the second rather than to the quarter hour.

Retrieval waits for the PC's power strip to respond, and keeps retrying as
long as it fails: on wake, the network often comes back in several stages.
Each logger reading is kept if it falls in a gap in the live readings —
none in the three minutes before it — whatever its date: the first readings
taken after wake therefore do not hide the night before them.

The measurements are stored in a **SQLite** database, a standard format
included with Python: `history\power_history.sqlite3`, in the machine's
data folder. How far back it goes is set in **PC power** → *Keep history
for*, from 1 to 365 days; beyond that, the oldest points are pruned.

The database can be read without the app — *DB Browser for SQLite*, Excel,
Grafana or any language — and describes itself:

| Object | Content |
| --- | --- |
| `sample` | The points: outlet, Unix UTC timestamp, watts, source |
| `outlet` | The outlets, identified by **MAC address** and output number |
| `info` | What each column contains, in plain words |
| `sample_readable` | The same points with local time as text |

An outlet is identified there by the power strip's MAC, not by the device
key, which changes with renames. Its **name** (`outlet.label`), on the other
hand, follows the outlet's renames: that is what you read when opening the
database without the app. An outlet removed from the configuration can still
be viewed, marked *(removed)*, until pruning takes its points away. The
schema version is in
`PRAGMA user_version`. The WAL journal lets the window read while the app
writes, and protects the database from a power cut.

A first, home-made binary format preceded SQLite. It is imported
automatically at launch, and each migrated file is kept, renamed to
`.bin.migrated`, until the database has been checked.

### Navigating

| Gesture | Effect |
| --- | --- |
| Mouse wheel | Zoom around the pointer |
| Drag | Scroll through time |
| ◀ ▶, arrow keys | Move back or forward by half a window |
| 1 h … 30 d | Displayed duration |
| **Live**, double-click, `End` | Back to the present, which then follows new measurements |
| Hover | Exact time and power, and their source |

Below the chart: minimum, time-weighted average, maximum and **energy
consumed** over the visible period, with the share actually measured. A
ten-second spike does not weigh as much as an hour of sleep.

A period with no measurement at all stays a **gap**: joining its two edges
would suggest a power draw that was never observed.

### Exporting

The window's **Export** menu. The export covers the **displayed period**:
what you see is what you export. Two variants:

| Variant | Separators | For |
| --- | --- | --- |
| **CSV, standard** | comma, decimal point, ISO 8601 time | any tool — the RFC 4180 standard |
| **CSV for Excel** | those from the Windows regional settings | a double-click in Excel |

French-locale Excel expects semicolons and decimal commas; a standard CSV
gets crammed into the first column there. The second variant reads the
separators from the Windows settings so that opening it works.

Each row carries `duration_s`, the time during which the value held. Energy
in watt-hours follows from a single formula —
`SUMPRODUCT(watts, duration_s) / 3600` —, without rebuilding the timeline.

### Log or linear

The logarithmic scale is fitted to what is visible. It is the right choice
as soon as sleep and activity share the screen: 2 W and 200 W both stay
readable. On a period of activity alone, the linear scale makes differences
proportional — the most honest reading of a power draw, since energy itself
is linear. Both are one click away.

### Limitations

The on-device power logger keeps **84 readings**. Sleep is flat, one point
per quarter hour is enough: that covers about 21 hours. A longer shutdown —
a weekend — only keeps its last 21 hours.

Only the PC's outlet benefits from this logger. Other outlets could be
tracked later, but only while the app is running.

## Version

Two numbers, and nothing more. The number is in
[shelly_screens/__init__.py](shelly_screens/__init__.py), shown in the
settings window title and on the first line of the log.

The **major** changes when the existing configuration is no longer enough as
is: a file format that evolves, a setting whose meaning changes, an
on-device script incompatible with the old one. In other words, when an
update requires checking something rather than simply restarting.

The **minor** changes on every iteration — fix, addition, robustness
measure — even for a detail. Its role is not to summarise the extent of the
work but to answer a single question, asked on a day something breaks:
*which version is running in front of me?* A log that does not say which
code it is about wastes more time than it saves.

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| Red icon | No device reachable. `Devices` → `Reconnect`, or check the power strips' power supply. |
| A device "offline" | The others keep working. Its outlets show a `-` state and are not operated. |
| An outlet stays "not identified" | Normal for a USB hub or the PC tower. For a screen: run the wizard again, the screen may have taken more than twelve seconds to disconnect. |
| A window stays on a switched-off screen | Check *Behaviour* → *Windows*, and that this screen's outlet is matched to it (**Identify displays**): otherwise, a screen powered from the PC's USB-C is still counted as on. |
| Very slow profile change | An expected screen does not come back: the wait runs to the maximum delay (20 s by default, adjustable in `Behaviour`). |
| Nothing at PC boot | Check that an outlet carries the **Boot screen** role, and that the keyboard's USB hub is **Critical**. |
| Sleep no longer switches anything off | The firmware's metering path can freeze: the app detects it and flags it in the icon menu, with a button to restart the power strip. A restart is harmless — bistable relays, and `initial_state` brings the PC's outlet back on. |
| Screens switch off while the PC is running | Switch-off threshold too high. Run a new measurement, or lower it in `PC power`. |
| Nothing switches back on at PC boot | Check in `PC power` that the script is `running`, and that an outlet carries the **Boot screen** role. |
| The keyboard does not respond in the BIOS | Its USB hub must be marked **Critical**, not just driven by the script. |
| A console opens at launch (sources) | The shortcut must target `pythonw.exe`, not `python.exe`. Reinstall it with `install-startup.ps1`. |
| No trace of what the app is doing | Icon menu → **Open log file**, or `%ProgramData%\Shelly PC Screens\logs`. |
| Blue icon, profiles refused | Another session is on the screen and drives the power strips. Switch to it. |
| A banner asks for an administrator | A hardware setting was changed from an ordinary account: **Save changes (administrator)** or **Discard**. |
| A device in `auth failed` | It responds but refuses the password. `Devices` → `Password...`, or a button reset if lost. |
| Light window while Windows is dark | The mode must be set to *Follow Windows* in `Behaviour` → `Appearance`. The setting read is `AppsUseLightTheme` in the registry. |

## Single instance

A second launch does not start: it asks the running instance to open its
settings, then exits. That is the expected behaviour of a tray-icon program,
whose window is often closed.

This is not a convenience but a **necessity**. Two instances each keep their
configuration in memory and write it out in full on every save: the last
one to write erases the other's work. A freshly measured calibration once
disappeared this way, replaced by an older copy.

The lock is a Windows named mutex. It belongs to the process and disappears
with it, even if it is killed abruptly — a lock file, by contrast, would
survive and block every later launch.

The lock holds within one Windows session: two accounts signed in side by
side each have their instance, and only the one on the screen drives the
devices (see *Several accounts signed in*).

## Logs

Everything is written to `%ProgramData%\Shelly PC Screens\logs\`, one file
per account (`shelly-screens-<account>.log`), rotating at 512 KB over three
files. The icon menu offers **Open log file**
to open it directly.

This is essential, not optional. Without a console, `sys.stdout` is `None`
and `print` raises no error: it simply writes nowhere. A program that relied
only on `print` would therefore run perfectly silent, with no way at all of
knowing what it is doing. Uncaught exceptions, including in threads, are
also redirected to this file.

`run-console.cmd` adds on-screen output on top of the file.

Each line is written **all the way to disk** (`fsync`), not just handed to
the system. Without it, the last lines are lost when the machine stops
abruptly — precisely the ones that would explain why.
