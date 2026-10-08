# AstraNova (public edition)

A friend and a helper on your Windows PC. **Nova** is the friend you talk to: she texts like a real person,
remembers what you tell her and sometimes texts you first. **Astra** gets things done in the background: email,
calendar, messages, research on the web, files and reminders. Ask Nova for something and Astra does it right in
the chat.

It picks a model that fits your graphics card (4 GB and up), lets you talk instead of type (microphone button),
filters out ads, newsletters and sign-in alerts from your email (Settings > Email, adjustable), and can run as a
Discord bot that chats with your friends in public mode, with no personal details.

## For people installing it

**Windows:** download `AstraNova-Setup.exe` from the latest release and run it.

**Mac (Apple Silicon, M1 and newer):** download `AstraNova-macOS.dmg`, open it and drag AstraNova into
Applications. The first time, macOS says it can't check the app (it isn't from the App Store): open
System Settings > Privacy & Security, scroll down and click **Open Anyway**. On the Mac, AstraNova chats, plans,
researches, handles mail, calendar, Spotify, files and Terminal commands; controlling other apps' windows is
Windows-only for now. To talk instead of typing, press the Fn (globe) key twice.

### Windows details

Run `AstraNova-Setup.exe` and press Install. No admin rights, no Python, nothing else to set up. The first start
installs a local AI engine and downloads the model once (or use a cloud model in Settings > Model).
Uninstall from Windows Settings > Apps.

AstraNova keeps itself up to date: a minute after it opens it looks for a new version, downloads it in the
background and puts it in place when you close the app (or right away with Settings > About > Restart to update).
Chats and settings are kept. Turn it off in Settings > About.

## Building the installer

1. Run **build_installer.bat**. It sets up Python if needed, builds the app, tests it, builds the installer and
   zips it.
2. Share `release\AstraNova-<version>.zip` (it contains `AstraNova-Setup.exe` and a short read-me).

`run_dev.bat` runs it from source after the first build.

## Releasing an update

Bump `__version__` in `astra/__init__.py` and push to `main`. That's all: GitHub Actions builds `AstraNova-Setup.exe` on Windows and attaches it to a release. Every installed copy picks it
up on its own the next time it's opened.

## Privacy

Everything stays on the PC that runs it: chats, memories and settings are in `%APPDATA%\AstraNova-Public`.
Nothing personal from the developer is included.

## License

Copyright (c) 2026 AIXENI. All rights reserved. AstraNova is **not open source**: the code is public so you can
see how it works, but copying, modifying, redistributing or reusing any part of it without written permission
is prohibited. See [LICENSE](LICENSE).
