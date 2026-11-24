# OS-24 Local .deb Installer - Verification

## Environment
- OS: Ubuntu Server 26.04.1 LTS
- Shell: Bash
- Project: os-lab
- Task: OS-24 Local .deb Installer

## Purpose
This script installs a local Debian (.deb) package on Ubuntu.

## Verification Plan
1. Check Bash syntax.
2. Test missing arguments.
3. Test nonexistent package files.
4. Test invalid .deb packages.
5. Test installation preview and confirmation.
6. Verify successful package installation.

## Test Results

### Test 1 - Bash Syntax
Command: bash -n linux-lab/scripts/deb_install.sh
Result: PASS - No syntax errors.

### Test 2 - Missing Argument
Command: bash linux-lab/scripts/deb_install.sh
Result: PASS - Usage message displayed.

### Test 3 - Missing Package File
Command: bash linux-lab/scripts/deb_install.sh olmayan-paket.deb
Result: PASS - Missing package was rejected.

### Test 4 - Invalid Package
Command: touch sahte.deb && bash linux-lab/scripts/deb_install.sh sahte.deb
Result: PASS - Invalid package was rejected.

### Test 5 - Real Package Installation
Package: hello
Command: bash linux-lab/scripts/deb_install.sh hello_*.deb
Result: PASS - Package installation completed.

### Test 6 - Installation Verification
Command: dpkg -s hello | grep Status
Output: Status: install ok installed
Result: PASS - Package installation verified.

## Faz 2 - Windows UI Automation and Safe Control

### Live services
Console command: `.venv-test\Scripts\python.exe android-lab\lab\web_ui.py 5050`
Result: PASS - `http://127.0.0.1:5050/api/health` returned `status=ok`.

Dashboard command (from `dashboard/`, using the installed Node 24.20.0 executable because Node was not on PowerShell `PATH`): `& "$env:LOCALAPPDATA\Microsoft\WinGet\Packages\OpenJS.NodeJS.LTS_Microsoft.Winget.Source_8wekyb3d8bbwe\node-v24.20.0-win-x64\node.exe" node_modules\astro\bin\astro.mjs dev --host 127.0.0.1 --port 4321 --strictPort`
Result: PASS - `/labs/winapps/` returned HTTP 200 and its `/api` proxy reached the live console.

### Live UI Automation
Target: a locally created, non-sensitive `OSLab UIA Verification` window.

Command: `GET /api/labs/winapps/apps/3214182/elements`
Result: PASS - HTTP 200, `engine=uia`, 9 visible elements, and a revision token.

Command: `GET /api/labs/winapps/apps/3214182/search?q=Safe%20action`
Result: PASS - HTTP 200, one `Button` match labelled `Safe action`.

Playwright against the live dashboard and console:
- PASS - the UIA hotspot and search displayed the live `Safe action` result.
- PASS - arm and disarm state transitions reached the live `/control` endpoint.
- PASS - the preview dialog and enabled confirmation were displayed.
- PASS - no `/click` or `/input` request was sent; the preview was cancelled.
- PASS - mirrored window pixels were blurred in the evidence image.

Evidence: [winapps-uia-controls.png](docs/media/screenshots/winapps-uia-controls.png)

### Automated Test Results
Command: `python -m pytest -q tests/test_winapps.py tests/test_web_ui.py` (from `android-lab/lab/`)
Result: PASS - 66 passed.

Command: `python -m pytest tests` (from `android-lab/lab/`)
Result: PARTIAL - 607 passed, 83 skipped, 5 failed. The failures were outside WinApps: a Docker availability assertion, two Windows content/encoding checks, and two macOS parity checks (including an Accessibility-permission check).

Command: `node scripts/check-translations.mjs` (from `dashboard/`)
Result: PASS - 2,867 keys match across 7 locales; no new untranslated TSX strings.

Command: `node_modules/.bin/tsc --noEmit` (from `dashboard/`)
Result: PASS.

Command: `node_modules/.bin/astro build --outDir .phase2-build` (from `dashboard/`)
Result: PASS - build completed; Vite reported a large-chunk warning.

Command: `scripts/e2e.sh --changed`
Result: WinApps scenarios passed. One full run had 75 passed and 1 skipped; the latest run had 74 passed, 1 skipped, and 1 unrelated `lab-switcher` locator failure.

Command: `lat check`
Result: PASS.

## PR #316 review remediation verification

The results below supersede the earlier WinApps counts above where they differ.

Command: `.\.venv-test\Scripts\python.exe -m pytest android-lab\lab\tests\test_winapps.py android-lab\lab\tests\test_web_ui.py -q`
Result: PASS - 77 passed, including a regression check that UIA focus does not call `SetForegroundWindow` or `BringWindowToTop`.

Command: `.\.venv-test\Scripts\python.exe -m pytest android-lab\lab\tests -q`
Result: PARTIAL - 576 passed, 80 skipped, 5 failed, and 45 errors. The WinApps tests pass. Remaining failures are outside WinApps: Source Lab fixture setup hits Windows `charmap` encoding errors, and the five failures concern cleanup, content/encoding, and macOS parity.

Command: `npm.cmd run test:i18n` (from `dashboard/`)
Result: PASS - 2,872 keys match across 7 locales.

Command: `npm.cmd run check` (from `dashboard/`)
Result: PASS - TypeScript check completed.

Command: `npm.cmd run build` (from `dashboard/`)
Result: PASS - production build completed; Vite reported the existing large-chunk warning. Generated `dashboard/dist` output was restored and is not part of the changes.

Command: `lat check`
Result: PASS - graph checks completed; the tool noted that no init version is recorded.

Command: `scripts/e2e.sh --changed`
Result: NOT FULLY PASSING. With two workers, one WinApps stream test and one unrelated health test failed under load. A one-worker rerun completed 80 passed and 1 skipped; its sole failure was the unrelated `lab-switcher` locator test. All WinApps cases passed in the serial run.

Command: `npm.cmd run test:e2e -- e2e/winapps.spec.ts` (from `dashboard/`, with the generated E2E build and one worker)
Result: PASS - 12 passed, including large and invalid frame handling, stalled-stream fallback, stale-stream ordering, and narrow-viewport behavior.

Command: `npm.cmd run test:e2e` (from `dashboard/`, with the generated E2E build and one worker)
Result: PARTIAL - 100 passed, 1 skipped, and 2 failed out of 103 tests. All 12 WinApps tests passed. The unrelated `how-to-use` failures are a stale embedded-command reference and a copied-prompt clipboard comparison.
