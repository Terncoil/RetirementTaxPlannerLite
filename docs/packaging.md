# App icon and packaging

## Where the icon comes from

The master art is `packaging/source-icon.ico` (256x256). The script
`packaging/make_icons.py` turns it into:

| File | Used for |
| --- | --- |
| `src/rtpl/data/icons/rtpl_16.png` ... `rtpl_256.png` | Title bar, taskbar, macOS Dock, About box, Linux menus |
| `src/rtpl/data/icons/rtpl.ico` (16-256 px inside) | Windows `.exe` icon and shortcuts |
| `packaging/rtpl.icns` | macOS `.app` bundle |

To change the icon, replace the master (a square PNG of 1024x1024 is best,
256x256 at least) and run:

```bash
uv run --with pillow python packaging/make_icons.py path/to/new-icon.png
```

## Running from source (`uv run rtpl`)

* **Title bar, taskbar, Dock:** the app sets its icon at start-up. On
  Windows it also sets an AppUserModelID, without which the taskbar would
  show Python's icon.
* **About box:** shows the icon.
* **Shortcut:** `uv run rtpl-shortcut` makes one with the icon.
  * Windows: on the Desktop and in the Start menu.
  * Linux: in the applications menu (and on `~/Desktop` if it exists).
  * macOS: not available from source; build the `.app` instead.

  Run it again if you move the project folder.
* **Linux on Wayland** shows the icon only once the `.desktop` file from
  `rtpl-shortcut` is installed, because Wayland ignores window icons.

## Stand-alone apps (PyInstaller)

Build on the OS you are building for (no cross-compiling):

```bash
uv run --with pyinstaller pyinstaller packaging/rtpl.spec --noconfirm
```

| OS | Result | Icon |
| --- | --- | --- |
| Windows | `dist/RetirementTaxPlannerLite/RetirementTaxPlannerLite.exe` | In the `.exe`, so Explorer, shortcuts and the taskbar show it |
| macOS | `dist/RetirementTaxPlannerLite.app` | In the bundle, so Finder, Dock and Launchpad show it |
| Linux | `dist/RetirementTaxPlannerLite/RetirementTaxPlannerLite` | Linux programs carry no icon; `sh packaging/linux/install.sh` installs the build with a menu entry and icons |

## File types

Profiles and plans are saved as `.rtpl` files (JSON inside); older `.json`
files still open. `src/rtpl/app/documents.py` lists the types the OS should
open with the app (`DOCUMENT_TYPES`) and which window opens each extension.
The Estimated Quarterly Tax Assistant's `.eqta` files are routed already
but show a "later version" message until its window exists; add an entry
to `DOCUMENT_TYPES` then to register them too.

A double-clicked file reaches the app as a command-line argument on Windows
and Linux, and as a file-open event on macOS. Registration:

| OS | How `.rtpl` gets registered |
| --- | --- |
| Windows | **Help > Open .rtpl Files With This App**, or `uv run rtpl-shortcut` from source. Both write per-user keys under `HKCU\Software\Classes` (no administrator rights) pointing at the copy of the app they run from; do it again after moving the app. A future installer can write the same keys. |
| macOS | `packaging/rtpl.spec` adds `CFBundleDocumentTypes` and `UTExportedTypeDeclarations` to the bundle's Info.plist; Finder picks them up when the app is in Applications. |
| Linux | `packaging/linux/install.sh` installs `packaging/linux/rtpl-mime.xml` (MIME type `application/x-rtpl`), the `.desktop` file with its `MimeType` line and `%f`, and makes the app the default with `xdg-mime`. From source, `uv run rtpl-shortcut` or the Help menu item does the same. |

The tests check that the Linux files match `documents.py`.

## Version

The version is set in one place, `__version__` in `src/rtpl/__init__.py`,
as MAJOR.MINOR.PATCH. Everything else reads it from there:

| Where | How it gets the version |
| --- | --- |
| Package metadata (`pip show`) | `pyproject.toml` reads `rtpl.__version__` |
| About box | `rtpl.__version__` |
| Windows `.exe` (Properties > Details) | `packaging/rtpl.spec` writes a version resource |
| macOS `.app` (Get Info) | `packaging/rtpl.spec` writes Info.plist |
| Release tag and download names | `packaging/version.py`, run by the build workflow |

Checks make sure they agree: the tests compare the package metadata with
`__version__`, and the build workflow fails if the metadata, the Windows
version resource or the macOS Info.plist differ from it, or if a hand-made
tag differs from it.

**Raise the version in every pull request that changes the app** (anything
under `src/`, `packaging/` or `pyproject.toml`). The *version-bump* check on
the pull request fails until it is higher than on `main`. Pull requests that
change only docs, tests or examples need no bump.

* PATCH (0.2.0 to 0.2.1): fixes and small changes.
* MINOR (0.2.1 to 0.3.0): new features.
* MAJOR (0.x to 1.0.0): when you decide it is ready for general use.

## Builds on GitHub

The **build apps** workflow builds the apps (macOS only when switched on, see
below), checks that each one starts and carries the right version, and zips them as
`RetirementTaxPlannerLite-<version>-<OS>.zip`. Each zip also holds a copy
of the `examples` folder (sample profiles and plans) next to the app, so
people can try it with File > Open. It runs:

* **On every push to `main`** (so after each merged pull request). The zips
  are on that run's page in the Actions tab, under *Artifacts*, for 30 days.
  You must be signed in to GitHub to download them.
* **Releases happen by themselves when switched on.** When the repository
  variable `AUTO_RELEASE` is `true` and a push to `main` carries a version
  that has no release yet, the workflow also tags that commit `v<version>`
  and publishes a GitHub Release with the three zips and notes listing the
  merged pull requests. Releases stay for good, and anyone who can see the
  repository can download them. Because every app change raises the version,
  every merged app change then becomes a release.
* On pull requests that change packaging, app code or the examples, and by hand
  (Actions tab, "build apps", "Run workflow"): builds only, no release.

### macOS builds are off for now

While the repository is private, GitHub counts each macOS minute as ten
against the monthly Actions allowance, so the Mac build and the Mac tests
are switched off. Releases then carry the Windows and Linux downloads only.

* **One Mac build:** Actions tab, *build apps*, *Run workflow*, tick
  "Also build for macOS", *Run workflow*. The zip is on that run's page.
* **Mac builds every time again:** Settings, *Secrets and variables*,
  *Actions*, *Variables* tab, *New repository variable*: name
  `MACOS_BUILDS`, value `true`. Delete it or set it to `false` to switch
  them off again. It turns on both the Mac build and the Mac tests.

Public repositories get standard runners, macOS included, without using
the allowance, so once the repository is public you can set
`MACOS_BUILDS` to `true` and leave it.

Other savings: draft pull requests skip the app builds (they run once
the pull request is marked ready), and a new push to a pull request
cancels its unfinished runs.

### Automatic releases are off for now

While a larger feature (such as the Estimated Quarterly Tax Assistant) is
merged in parts, pushes to `main` build only and publish no release, so no
half-finished version goes out as a download. Pull requests still raise
the version as usual; the builds are on each run's page as above.

* **Switch automatic releases on:** Settings, *Secrets and variables*,
  *Actions*, *Variables* tab, *New repository variable*: name
  `AUTO_RELEASE`, value `true`. The next push to `main` whose version has
  no release yet publishes one. Delete it or set it to `false` to switch
  them off again.
* **One release while they are off:** push the tag `v<version>` (below).

You can also push a tag `v<version>` yourself, or make a release by hand on
GitHub; the tag must match `__version__`, and the workflow attaches the zips.

### Opening a downloaded build

The builds are not code-signed, so each OS warns the first time:

* **Windows:** unzip, run `RetirementTaxPlannerLite.exe`. If SmartScreen
  says "Windows protected your PC", click *More info*, then *Run anyway*.
* **macOS** (Apple Silicon): unzip, open the `RetirementTaxPlannerLite`
  folder, drag the app to Applications (keep the `examples` folder wherever
  you like), then
  right-click it, *Open*, and *Open* again. On recent macOS, if that does
  not offer *Open*, go to System Settings, Privacy & Security, and click
  *Open Anyway*.
* **Linux** (x86-64): unzip and run
  `RetirementTaxPlannerLite/RetirementTaxPlannerLite`.

On every OS the sample files are in `RetirementTaxPlannerLite/examples`.

Next steps not done yet: an Intel Mac build, a Windows installer (for example Inno Setup,
which would also make the Start-menu shortcut), macOS code signing and
notarization, and a Linux AppImage or Flatpak.
