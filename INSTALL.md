# Installing The Edge

For people who downloaded the app. If you want to run it from source, the
[README](README.md) covers that instead.

---

## Not betting advice

The Edge is a projection tool. It is not advice and it does not tell you to
stake money.

Its own measured record is in the app, on the Performance page: **about 51%
against the spread, where 52.4% is break-even at standard juice.** An "edge"
shown here is inside the noise more often than not. Read the numbers as what
a model thinks, not as a prediction of what will happen.

Never stake money you cannot afford to lose. If gambling stops being fun,
stop. In the US: call or text **1-800-GAMBLER**.

---

## What you need

- **macOS** on Apple silicon (M1 or later), or **Windows 10/11** on a 64-bit PC.
  `uname -m` says `arm64` on a Mac that will run it.
- About **1 GB** of disk. Twice that if you use the assistant.
- Nothing else. No Python, no accounts, no cloud services. Everything runs on
  your machine and the data stays there.

---

## macOS

1. Download `TheEdge-macos-arm.tar.gz`.
2. **Open Terminal** and run these, one line at a time:

   ```bash
   cd ~/Downloads
   tar -xzf TheEdge-macos-arm.tar.gz
   xattr -dr com.apple.quarantine TheEdge.app
   open TheEdge.app
   ```

3. Drag `TheEdge.app` to your Applications folder whenever you like.

**Why the Terminal.** The build is not signed by Apple, so macOS quarantines
it and double-clicking gives you "TheEdge is damaged and can't be opened".
The app is not damaged — that is the message macOS uses for anything
unsigned. The `xattr` line removes the quarantine flag. Extracting in Finder
instead of Terminal re-applies it, which is why step 2 does both.

---

## Windows

1. Download `TheEdge-windows-setup.exe`.
2. Double-click it. Windows will warn you it is from an unknown publisher:
   **More info → Run anyway.**
3. Follow the installer.

**Use the installer, not the zip.** Windows tags anything downloaded from the
internet, Explorer copies that tag onto every file it extracts from a zip, and
Windows then refuses to load the tagged parts — which stops the app opening its
own window. The installer sidesteps it: the tag lands on `setup.exe` and the
files it writes carry none.

If you take `TheEdge-windows.zip` anyway, unblock it after extracting:

```powershell
Get-ChildItem -Recurse "$HOME\Downloads\TheEdge-windows" | Unblock-File
```

---

## First run

The app opens on the current week with everything it can work out on its own —
schedules, scores, the model's projections, power rankings, pick'em and
survivor. That all works with no setup at all.

Press **the refresh button beside LIVE**, top left, to pull everything fresh.
It also refreshes itself once a minute while open.

### Betting lines (optional)

Without a sportsbook feed the book columns read "–" and nothing else changes.
To fill them in you need a free key from [the-odds-api.com](https://the-odds-api.com):

1. Sign up. The free tier is 500 requests a month.
2. **Settings → Data sources → Odds API key.** Paste it and press **Save**.
3. Press **Update odds**, bottom left.

The lines have their own button on purpose. Each press spends three requests,
so it is never bundled into an ordinary refresh — nothing else in the app can
spend your allowance. The line under the button says how much is left.

### The assistant (optional)

Ask questions about the season in plain English. It runs **entirely on your
machine** — nothing is sent anywhere — so it needs a model downloaded first.
Open the **Assistant** tab and press the setup button; it handles the rest.
About 3 GB, once.

---

## Keeping it up to date

The app tells you. When a newer version is published, a bar appears at the top
of the window with a link. The build you are running is always in the bottom
left corner, under "Updated": `build a1b2c3d · 2026-09-18`.

To update, download the new file and install it over the old one. **Your data
is kept** — picks, history and settings live in a separate folder and the app
upgrades its database in place, taking a backup first.

---

## Where your data lives

- **macOS**: `~/Library/Application Support/nflpicker/`
- **Windows**: `%LOCALAPPDATA%\nflpicker\`

One database file, plus your settings. **Settings → Backup** makes a copy you
can keep somewhere else — worth doing before an update if a season's picks
matter to you.

---

## Pick sharing

The first time you open The Edge after activating it, you will be asked about
this. It is on unless you turn it off, and nothing is sent until you have
answered.

**What is shared.** The picks you make — who you took, and the line and price
showing when you took them — along with an id: sixteen characters worked out
from your licence key.

**What is not.** Your name, your email and your licence key. None of the three
is sent.

**What it is for.** Picks are graded and pickers ranked, which is what makes
the model better over a season; The Edge may follow picks from people who turn
out to be consistently right. The id is not your name, but The Edge can work
out which customer it belongs to — that is how a strong picker gets followed,
and it is why the app does not call the id "anonymous".

**Turning it off.** **Settings → Sharing** at any time. Switching it off stops
it immediately, and there is a button beside the switch that deletes everything
already shared. While it is on, the Picks page carries a small "Sharing picks ·
change" line that takes you straight to the switch.

---

## If something goes wrong

**The board is full of dashes.** That is always "we do not have this", never
zero. Usually the betting lines, which need the key above.

**Nothing is updating.** Check the connections list on the Settings page — it
says which feed last answered and when. Then press refresh.

**The power rankings have not changed.** They are cut once a week, when the
previous week's last game goes final, and then left alone until the next one.
That is deliberate: a ranking that moves three times on a Tuesday cannot be
compared against anything.

**The assistant is slow.** The first answer after opening the app is the slow
one while the model loads; it stays warm for an hour after that.

**Anything else.** The **Desk** tab inside the app has the current list of
known issues and what is being worked on.
