C10.1 isolated Android app visual acceptance
Local date: 2026-11-03
Evidence directory: G:\Myself\Adam2\.cache\c101-visual
Isolation: SDK .cache/c10-preflight/sdk, AVD .cache/android-avd/c10-preflight, serial emulator-5556, ADB server port 5041. No default ADB or real device used.
Start: 01:39:53 +08; cleanup: 01:46:55 +08 (about 421 seconds including parent adapter repairs/waiting).
Install succeeded. Cold activity launch TotalTime=758ms, WaitTime=762ms.
APK SHA256 BA44EC23F087B63475594FC6A27AC60D72F6F7A16D26488F31859B87E935935F.
Independent visual inspection passed for direct and formal API portrait/landscape PNGs: title Adam Practice - Local Record, Record name field, Record note field, SAVE RECORD button, No saved record result; all legible and unclipped.
Portrait 720x1280 rotation0: title [24,88][696,153], name [24,153][696,244], note [24,244][696,335], button [24,335][696,431], result [24,431][696,469].
Landscape 1280x720 rotation1: same y bounds, x right edge 1160; all five controls visible, navigation on right.
First formal API attempt failed: RuntimeError practice application is not foreground. window-windows.txt lacks mCurrentFocus; full window.txt contains correct practice app focus. Parent repaired adapter.
Second formal API attempt: portrait publish succeeded, rotated old observation rejected; fresh landscape observe failed RuntimeError device identity or rotation is unavailable. Later stable _state succeeded. Failure-instant raw boot/window outputs were not captured, so root cause is unconfirmed; window-landscape.txt was collected afterward and parses rotation1.
Third explicit acceptance run waited 5 seconds after each requested rotation, then succeeded without production auto-retry: portrait observe, old version rejected with device or page changed; observe again, fresh landscape observe. Session b6434b42, two real media_saved events, full source/ref/version evidence api-report.json. Timed API sequence 14.421 seconds (excludes initial 5-second portrait settle).
Formal API portrait/landscape independently inspected after final run: same five complete controls and geometry as direct captures.
Restored rotation0; stopped only owned emulator and ADB. cleanup.json confirms no owned PIDs and no 5041/5556/5557 listeners. AVD/APK and evidence retained. No pytest, production code edits, input/save behavior testing, or real apps involved.
