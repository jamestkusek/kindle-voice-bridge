local Blitbuffer = require("ffi/blitbuffer")
local ConfirmBox = require("ui/widget/confirmbox")
local Dispatcher = require("dispatcher")
local Font = require("ui/font")
local FrameContainer = require("ui/widget/container/framecontainer")
local Geom = require("ui/geometry")
local GestureRange = require("ui/gesturerange")
local HorizontalGroup = require("ui/widget/horizontalgroup")
local HorizontalSpan = require("ui/widget/horizontalspan")
local InfoMessage = require("ui/widget/infomessage")
local InputContainer = require("ui/widget/container/inputcontainer")
local Screen = require("device").screen
local TextWidget = require("ui/widget/textwidget")
local UIManager = require("ui/uimanager")
local VerticalGroup = require("ui/widget/verticalgroup")
local VerticalSpan = require("ui/widget/verticalspan")
local WidgetContainer = require("ui/widget/container/widgetcontainer")
local _ = require("gettext")

local base_path = "/mnt/us/kindle-voice-bridge"
local state_path = "/tmp/voicebridge-bt-session.state"
local command_path = "/tmp/voicebridge-bt-session.cmd"
local log_path = base_path .. "/diagnostics.txt"
local bluetooth_log_path = base_path .. "/bluetooth-inspection.txt"
local bumble_check_log_path = base_path .. "/bumble-check.txt"
local record_log_path = base_path .. "/record-send.log"
local manual_record_log_path = base_path .. "/manual-record.log"
local manual_start_log_path = base_path .. "/manual-record-start.log"
local end_marker = "\nDiagnostics complete.\n"
local bluetooth_end_marker = "\nBluetooth inspection complete.\n"
local bumble_check_end_marker = "\nBumble runtime check complete.\n"
local record_end_marker = "\nRecord and send finished.\n"
local record_wait_seconds = 200
local record_poll_seconds = 1
local status_timeout_seconds = 6
local record_status_timeout_seconds = 20

local hci_command = [[
PYTHONHOME=/mnt/us/kindle-voice-bridge/python31117
PYTHONNOUSERSITE=1
PYTHONDONTWRITEBYTECODE=1
LD_LIBRARY_PATH="$PYTHONHOME/lib"
export PYTHONHOME PYTHONNOUSERSITE PYTHONDONTWRITEBYTECODE LD_LIBRARY_PATH
unset PYTHONPATH PYTHONSTARTUP PYTHONUSERBASE VIRTUAL_ENV
"$PYTHONHOME/bin/python3.11" -B /mnt/us/kindle-voice-bridge/launch_hci.py
]]

local diagnostics_command = [[
printf 'Voice Bridge diagnostics\n\nFirmware:\n'
if [ -r /etc/prettyversion.txt ]; then
    cat /etc/prettyversion.txt 2>/dev/null || printf 'Cannot read firmware text.\n'
elif [ -e /etc/prettyversion.txt ]; then
    printf 'Cannot read firmware text.\n'
else
    printf 'Not present.\n'
fi
printf '\n\nArchitecture: '
if architecture=$(uname -m 2>/dev/null) && [ -n "$architecture" ]; then
    printf '%s\n' "$architecture"
else
    printf 'Unavailable.\n'
fi

for python in python3 python; do
    if path=$(command -v "$python" 2>/dev/null); then
        printf '%s available: yes\n' "$python"
        printf '%s path: %s\n' "$python" "$path"
        if version=$("$python" --version 2>&1) && [ -n "$version" ]; then
            printf '%s version: %s\n' "$python" "$version"
        else
            printf '%s version: unavailable\n' "$python"
        fi
    else
        printf '%s available: no\n' "$python"
        printf '%s version: unavailable\n' "$python"
    fi
done

printf '\nKernel: '
uname -r 2>/dev/null || printf 'Unavailable.\n'
printf 'CPU features: '
awk -F: '$1 ~ /^Features/ { print $2; found=1; exit } END { if (!found) print "Unavailable." }' /proc/cpuinfo 2>/dev/null
printf 'C library: '
getconf GNU_LIBC_VERSION 2>/dev/null || printf 'Unavailable.\n'
for library in /lib/ld-linux-armhf.so.3 /lib/libgcc_s.so.1 /usr/lib/libgcc_s.so.1; do
    if [ -e "$library" ]; then
        printf '%s present: yes\n' "$library"
    else
        printf '%s present: no\n' "$library"
    fi
done

runtime_home=/mnt/us/kindle-voice-bridge/python31117
runtime="$runtime_home/bin/python3.11"
printf '\nBundled Python runtime:\n'
if [ -f "$runtime" ]; then
    (
        unset PYTHONPATH PYTHONSTARTUP PYTHONUSERBASE VIRTUAL_ENV
        PYTHONHOME="$runtime_home"
        PYTHONNOUSERSITE=1
        PYTHONDONTWRITEBYTECODE=1
        LD_LIBRARY_PATH="$PYTHONHOME/lib"
        export PYTHONHOME PYTHONNOUSERSITE PYTHONDONTWRITEBYTECODE LD_LIBRARY_PATH
        ulimit -c 0 2>/dev/null || :
        "$runtime" --version 2>&1
        result=$?
        printf 'Version check exit status: %s\n' "$result"
        if [ "$result" -eq 0 ]; then
            "$runtime" -u -B -c 'import sys; print("Python started", flush=True); import ssl; print("ssl OK", flush=True); import socket; print("socket OK", flush=True); import ctypes; print("ctypes OK", flush=True); import asyncio; print("asyncio OK", flush=True)' 2>&1
            printf 'Import check exit status: %s\n' "$?"
        else
            printf 'Loader diagnostics, last 20 lines:\n'
            LD_DEBUG=libs "$runtime" --version 2>&1 | tail -20
        fi
    )
else
    printf 'Not installed.\n'
fi

printf '\nUSB storage mount:\n'
awk '$2 == "/mnt/us" { print "Filesystem: " $3 ", options: " $4; found=1 } END { if (!found) print "No separate /mnt/us mount found." }' /proc/mounts 2>/dev/null

for device in /dev/ttymxc2 /dev/stpbt; do
    if [ -e "$device" ]; then
        printf '%s present: yes\n' "$device"
        ls -ld "$device" 2>/dev/null
    else
        printf '%s present: no\n' "$device"
    fi
done

btfd=no
btd=no
bsa_server=no
btmanagerd=no
ace_bt=no
proc_seen=no
proc_error=no
for entry in /proc/[0-9]*/comm; do
    if [ ! -e "$entry" ]; then
        continue
    fi
    proc_seen=yes
    if { read -r name < "$entry"; } 2>/dev/null; then
        case "$name" in
            btfd) btfd=yes ;;
            btd) btd=yes ;;
            bsa_server) bsa_server=yes ;;
            btmanagerd) btmanagerd=yes ;;
            ace_bt) ace_bt=yes ;;
        esac
    else
        proc_error=yes
    fi
done
printf '\nBluetooth processes running:\n'
for process in btfd btd bsa_server btmanagerd ace_bt; do
    case "$process" in
        btfd) status=$btfd ;;
        btd) status=$btd ;;
        bsa_server) status=$bsa_server ;;
        btmanagerd) status=$btmanagerd ;;
        ace_bt) status=$ace_bt ;;
    esac
    if [ "$status" = no ] && { [ "$proc_seen" = no ] || [ "$proc_error" = yes ]; }; then
        status=unknown
    fi
    printf '%s: %s\n' "$process" "$status"
done
printf '\nDiagnostics complete.\n'
]]

local bluetooth_command = [[
printf 'Voice Bridge Bluetooth inspection\n\nFirmware:\n'
if [ -r /etc/prettyversion.txt ]; then
    cat /etc/prettyversion.txt 2>/dev/null || printf 'Cannot read firmware text.\n'
elif [ -e /etc/prettyversion.txt ]; then
    printf 'Cannot read firmware text.\n'
else
    printf 'Not present.\n'
fi
printf '\n\nDevice permissions:\n'
if ! ls -ldnL /dev/ttymxc2 2>/dev/null; then
    printf 'Cannot read /dev/ttymxc2 permissions. Check the device path and access.\n'
fi

btfd=no
btd=no
bsa_server=no
bluetoothd=no
btmanagerd=no
ace_bt=no
proc_seen=no
comm_incomplete=no
fd_incomplete=no
holder_found=no
printf '\nProcesses with a file descriptor pointing exactly to /dev/ttymxc2:\n'
for proc in /proc/[0-9]*; do
    [ -d "$proc" ] || continue
    pid=${proc#/proc/}
    case "$pid" in ''|*[!0-9]*) continue ;; esac
    proc_seen=yes
    if { read -r name < "$proc/comm"; } 2>/dev/null && [ -n "$name" ]; then
        case "$name" in
            btfd) btfd=yes ;;
            btd) btd=yes ;;
            bsa_server) bsa_server=yes ;;
            bluetoothd) bluetoothd=yes ;;
            btmanagerd) btmanagerd=yes ;;
            ace_bt) ace_bt=yes ;;
        esac
    else
        name='Process name unavailable.'
        comm_incomplete=yes
    fi
    for fd in "$proc"/fd/*; do
        [ -e "$fd" ] || continue
        if target=$(readlink "$fd" 2>/dev/null); then
            if [ "$target" = /dev/ttymxc2 ]; then
                printf '%s\n' "$name"
                holder_found=yes
                break
            fi
        else
            fd_incomplete=yes
        fi
    done
done
if [ "$proc_seen" = no ]; then
    comm_incomplete=yes
    fd_incomplete=yes
fi
if [ "$holder_found" = no ]; then
    printf 'None found among readable file descriptors.\n'
fi
if [ "$fd_incomplete" = yes ] || [ "$comm_incomplete" = yes ]; then
    printf 'Some process data was unavailable. The results may be incomplete.\n'
fi
printf 'Only processes visible to KOReader are included. Processes can change during the scan.\n'

printf '\nBluetooth processes running:\n'
for process in btfd btd bsa_server bluetoothd btmanagerd ace_bt; do
    case "$process" in
        btfd) status=$btfd ;;
        btd) status=$btd ;;
        bsa_server) status=$bsa_server ;;
        bluetoothd) status=$bluetoothd ;;
        btmanagerd) status=$btmanagerd ;;
        ace_bt) status=$ace_bt ;;
    esac
    if [ "$status" = no ] && [ "$comm_incomplete" = yes ]; then
        status=unknown
    fi
    printf '%s: %s\n' "$process" "$status"
done

printf '\nrfkill entries:\n'
rfkill_root=/sys/class/rfkill
if [ ! -d "$rfkill_root" ]; then
    printf 'The rfkill directory is unavailable.\n'
elif [ ! -r "$rfkill_root" ] || [ ! -x "$rfkill_root" ]; then
    printf 'Cannot read the rfkill directory.\n'
else
    rfkill_seen=no
    for entry in "$rfkill_root"/*; do
        [ -d "$entry" ] || continue
        rfkill_seen=yes
        printf '%s:\n' "${entry##*/}"
        for field in name type state; do
            if { read -r value < "$entry/$field"; } 2>/dev/null; then
                printf '  %s: %s\n' "$field" "$value"
            else
                printf '  %s: unavailable\n' "$field"
            fi
        done
    done
    if [ "$rfkill_seen" = no ]; then
        printf 'No rfkill entries found.\n'
    fi
fi

printf '\nBluetooth proc files:\n'
for node in /proc/bluetooth/btenable /proc/bluetooth/sleep/proto \
    /proc/bluetooth/sleep/btwake /proc/bluetooth/sleep/asleep \
    /proc/bluetooth/sleep/hostwake; do
    printf '%s:\n' "$node"
    if cat "$node" 2>/dev/null; then
        printf '\n'
    else
        printf '\nCannot read this file.\n'
    fi
done

printf '\nBluetooth service status:\n'
if command -v initctl >/dev/null 2>&1; then
    for service in btd btfd; do
        printf '%s:\n' "$service"
        if initctl status "$service" 2>&1; then
            :
        else
            printf 'Status check failed. Exit status: %s\n' "$?"
        fi
    done
else
    printf 'initctl is unavailable.\n'
fi

printf '\n/dev/cpu_dma_latency present: '
if [ -e /dev/cpu_dma_latency ]; then
    printf 'yes\n'
elif [ -x /dev ]; then
    printf 'no\n'
else
    printf 'unknown\n'
fi
printf '\nBluetooth inspection complete.\n'
]]

-- Read-only. This command imports Python modules and writes one report file.
-- It does not open the UART, start Bluetooth, or change any device state.
local bumble_check_command = [[
exec 2>&1
printf 'Voice Bridge Bumble runtime check\n\n'
runtime_home=/mnt/us/kindle-voice-bridge/python31117
runtime="$runtime_home/bin/python3.11"
bundle=/mnt/us/kindle-voice-bridge/bumble
printf 'Python runtime: %s\n' "$runtime"
if [ -f "$runtime" ]; then
    printf 'Python runtime present: yes\n'
else
    printf 'Python runtime present: no\n'
fi
printf 'Bundle directory: %s\n' "$bundle"
if [ -d "$bundle" ]; then
    printf 'Bundle directory present: yes\n'
else
    printf 'Bundle directory present: no\n'
fi
printf '\n'
if [ ! -f "$runtime" ]; then
    printf 'The check did not run. The bundled Python is missing.\n'
    printf '\nBumble runtime check complete.\n'
    exit 1
fi
(
    unset PYTHONSTARTUP PYTHONUSERBASE VIRTUAL_ENV
    PYTHONHOME="$runtime_home"
    PYTHONPATH="$bundle"
    PYTHONNOUSERSITE=1
    PYTHONDONTWRITEBYTECODE=1
    LD_LIBRARY_PATH="$runtime_home/lib"
    export PYTHONHOME PYTHONPATH PYTHONNOUSERSITE PYTHONDONTWRITEBYTECODE LD_LIBRARY_PATH
    ulimit -c 0 2>/dev/null || :
    "$runtime" -u -B -c '
import os
import sys
print("Python version: " + sys.version.split()[0])
print("Python executable: " + sys.executable)
print("Python path: " + os.environ.get("PYTHONPATH", "unset"))
print("")
try:
    import bumble
    print("Bumble location: " + bumble.__file__)
except Exception as error:
    print("Bumble location: unavailable: " + type(error).__name__ + ": " + str(error))
print("")
modules = ["bumble.core", "bumble.hci", "bumble.keys", "bumble.device", "bumble.rfcomm", "bumble.hfp", "bumble.transport.common", "bumble.transport.serial"]
print("Module imports:")
failed = 0
for name in modules:
    try:
        __import__(name)
    except Exception as error:
        failed = failed + 1
        print("  " + name + ": FAIL: " + type(error).__name__ + ": " + str(error))
    else:
        print("  " + name + ": OK")
print("")
print("Module failures: " + str(failed))
print("")
try:
    from bumble import crypto
    print("Crypto backend: " + crypto.EccKey.__module__)
    print("Crypto AES-CMAC backend: " + crypto.aes_cmac.__module__)
    from bumble.crypto.builtin import aes_cmac
    expected = "763cbcde81df9131bf897712c088edad"
    actual = aes_cmac(bytes(16), bytes(16)).hex()
    if actual == expected:
        print("Crypto AES-CMAC self-test: pass")
    else:
        print("Crypto AES-CMAC self-test: fail: got " + actual)
except Exception as error:
    print("Crypto backend: unavailable: " + type(error).__name__ + ": " + str(error))
print("")
try:
    from bumble._version import version
    print("Bumble version: " + version)
except Exception as error:
    print("Bumble version: unavailable: " + type(error).__name__ + ": " + str(error))
print("")
bundle = os.environ.get("PYTHONPATH", "")
count = 0
if bundle and os.path.isdir(bundle):
    for root, dirs, files in os.walk(bundle):
        count = count + len(files)
    print("Bundle file count: " + str(count))
else:
    print("Bundle file count: unavailable")
'
)
printf '\nPython exit status: %s\n' "$?"
printf '\nBumble runtime check complete.\n'
]]

-- Manual start only. This command stops the stock Bluetooth daemon and
-- restores it in a finally path. The script reads upload.json and keeps it so
-- later runs reuse it. The result line is written to the log.
local record_command = [[
exec 2>&1
printf 'Voice Bridge record and send\n\n'
runtime_home=/mnt/us/kindle-voice-bridge/python31117
runtime="$runtime_home/bin/python3.11"
bundle=/mnt/us/kindle-voice-bridge/bumble
if [ ! -f "$runtime" ]; then
    printf 'The bundled Python is missing.\n'
    printf '\nResult: not sent. The bundled Python is missing.\n'
    printf '\nRecord and send finished.\n'
    exit 1
fi
(
    unset PYTHONPATH PYTHONSTARTUP PYTHONUSERBASE VIRTUAL_ENV
    PYTHONHOME="$runtime_home"
    PYTHONPATH="$bundle"
    PYTHONNOUSERSITE=1
    PYTHONDONTWRITEBYTECODE=1
    LD_LIBRARY_PATH="$runtime_home/lib"
    export PYTHONHOME PYTHONPATH PYTHONNOUSERSITE PYTHONDONTWRITEBYTECODE LD_LIBRARY_PATH
    ulimit -c 0 2>/dev/null || :
    "$runtime" -u -B /mnt/us/kindle-voice-bridge/capture_local.py \
        --config /mnt/us/kindle-voice-bridge/upload.json \
        --authorize-bluetooth-handoff \
        --authorize-microphone-recording \
        --confirm-bluetooth-enabled-in-ui
)
printf '\nExit status: %s\n' "$?"
printf '\nRecord and send finished.\n'
]]

-- The session client uses only the Python standard library. It sends one line
-- to the daemon and prints one line. It does not import Bumble.
local session_status_command = [[
exec 2>&1
runtime_home=/mnt/us/kindle-voice-bridge/python31117
runtime="$runtime_home/bin/python3.11"
if [ ! -f "$runtime" ]; then
    printf 'error The bundled Python is missing.\n'
    exit 1
fi
(
    unset PYTHONPATH PYTHONSTARTUP PYTHONUSERBASE VIRTUAL_ENV
    PYTHONHOME="$runtime_home"
    PYTHONNOUSERSITE=1
    PYTHONDONTWRITEBYTECODE=1
    LD_LIBRARY_PATH="$runtime_home/lib"
    export PYTHONHOME PYTHONNOUSERSITE PYTHONDONTWRITEBYTECODE LD_LIBRARY_PATH
    ulimit -c 0 2>/dev/null || :
    "$runtime" -B /mnt/us/kindle-voice-bridge/bt_session.py --client status
)
]]

-- The phase command reports preparing, recording, sending, idle, or stopping.
-- It is additive. An older daemon replies with an error, and the plugin keeps
-- the preparing message until the result arrives.
local session_phase_command = [[
exec 2>&1
runtime_home=/mnt/us/kindle-voice-bridge/python31117
runtime="$runtime_home/bin/python3.11"
if [ ! -f "$runtime" ]; then
    printf 'error The bundled Python is missing.\n'
    exit 1
fi
(
    unset PYTHONPATH PYTHONSTARTUP PYTHONUSERBASE VIRTUAL_ENV
    PYTHONHOME="$runtime_home"
    PYTHONNOUSERSITE=1
    PYTHONDONTWRITEBYTECODE=1
    LD_LIBRARY_PATH="$runtime_home/lib"
    export PYTHONHOME PYTHONNOUSERSITE PYTHONDONTWRITEBYTECODE LD_LIBRARY_PATH
    ulimit -c 0 2>/dev/null || :
    "$runtime" -B /mnt/us/kindle-voice-bridge/bt_session.py --client phase --timeout 2
)
]]

local session_start_recording_command = [[
exec 2>&1
runtime_home=/mnt/us/kindle-voice-bridge/python31117
runtime="$runtime_home/bin/python3.11"
if [ ! -f "$runtime" ]; then
    printf 'error The bundled Python is missing.\n'
    printf '\nManual recording start finished.\n'
    exit 1
fi
(
    unset PYTHONPATH PYTHONSTARTUP PYTHONUSERBASE VIRTUAL_ENV
    PYTHONHOME="$runtime_home"
    PYTHONNOUSERSITE=1
    PYTHONDONTWRITEBYTECODE=1
    LD_LIBRARY_PATH="$runtime_home/lib"
    export PYTHONHOME PYTHONNOUSERSITE PYTHONDONTWRITEBYTECODE LD_LIBRARY_PATH
    ulimit -c 0 2>/dev/null || :
    "$runtime" -B /mnt/us/kindle-voice-bridge/bt_session.py --client start_recording --timeout 200
)
printf '\nManual recording start finished.\n'
]]

local session_stop_recording_command = [[
exec 2>&1
runtime_home=/mnt/us/kindle-voice-bridge/python31117
runtime="$runtime_home/bin/python3.11"
if [ ! -f "$runtime" ]; then
    printf 'error The bundled Python is missing.\n'
    printf '\nManual Stop and send finished.\n'
    exit 1
fi
(
    unset PYTHONPATH PYTHONSTARTUP PYTHONUSERBASE VIRTUAL_ENV
    PYTHONHOME="$runtime_home"
    PYTHONNOUSERSITE=1
    PYTHONDONTWRITEBYTECODE=1
    LD_LIBRARY_PATH="$runtime_home/lib"
    export PYTHONHOME PYTHONNOUSERSITE PYTHONDONTWRITEBYTECODE LD_LIBRARY_PATH
    ulimit -c 0 2>/dev/null || :
    "$runtime" -B /mnt/us/kindle-voice-bridge/bt_session.py --client stop_recording --timeout 200
)
printf '\nManual Stop and send finished.\n'
]]

-- Manual start only. The daemon performs the Bluetooth handoff once and
-- restores Bluetooth when it stops. The daemon reads upload.json and keeps it
-- for later sessions.
local session_start_command = [[
exec 2>&1
printf 'Voice Bridge Bluetooth session\n\n'
runtime_home=/mnt/us/kindle-voice-bridge/python31117
runtime="$runtime_home/bin/python3.11"
bundle=/mnt/us/kindle-voice-bridge/bumble
config=/mnt/us/kindle-voice-bridge/upload.json
log=/mnt/us/kindle-voice-bridge/bt-session.log
if [ ! -f "$runtime" ]; then
    printf 'The bundled Python is missing.\n'
    exit 1
fi
if [ ! -r "$config" ]; then
    printf 'The upload configuration is missing. Write /mnt/us/kindle-voice-bridge/upload.json first.\n'
    exit 1
fi
(
    unset PYTHONPATH PYTHONSTARTUP PYTHONUSERBASE VIRTUAL_ENV
    PYTHONHOME="$runtime_home"
    PYTHONPATH="$bundle"
    PYTHONNOUSERSITE=1
    PYTHONDONTWRITEBYTECODE=1
    LD_LIBRARY_PATH="$runtime_home/lib"
    export PYTHONHOME PYTHONPATH PYTHONNOUSERSITE PYTHONDONTWRITEBYTECODE LD_LIBRARY_PATH
    ulimit -c 0 2>/dev/null || :
    exec "$runtime" -u -B /mnt/us/kindle-voice-bridge/bt_session.py \
        --config "$config" \
        --authorize-bluetooth-handoff \
        --authorize-microphone-recording \
        --confirm-bluetooth-enabled-in-ui
) >> "$log" 2>&1 &
daemon_pid=$!
count=0
while [ "$count" -lt 30 ]; do
    reply=$(
        (
            unset PYTHONPATH PYTHONSTARTUP PYTHONUSERBASE VIRTUAL_ENV
            PYTHONHOME="$runtime_home"
            PYTHONNOUSERSITE=1
            PYTHONDONTWRITEBYTECODE=1
            LD_LIBRARY_PATH="$runtime_home/lib"
            export PYTHONHOME PYTHONNOUSERSITE PYTHONDONTWRITEBYTECODE LD_LIBRARY_PATH
            ulimit -c 0 2>/dev/null || :
            "$runtime" -B /mnt/us/kindle-voice-bridge/bt_session.py --client status 2>/dev/null
        )
    )
    case "$reply" in
        ok*)
            printf '%s\n' "$reply"
            exit 0
            ;;
    esac
    if ! kill -0 "$daemon_pid" 2>/dev/null; then
        printf 'The session exited before it was ready.\n'
        printf 'Open kindle-voice-bridge/bt-session.log to check.\n'
        exit 1
    fi
    sleep 1
    count=$((count + 1))
done
printf 'The session did not become ready.\n'
printf 'Open kindle-voice-bridge/bt-session.log to check.\n'
exit 1
]]

local session_stop_command = [[
exec 2>&1
runtime_home=/mnt/us/kindle-voice-bridge/python31117
runtime="$runtime_home/bin/python3.11"
if [ ! -f "$runtime" ]; then
    printf 'The bundled Python is missing.\n'
    exit 1
fi
reply=$(
    (
        unset PYTHONPATH PYTHONSTARTUP PYTHONUSERBASE VIRTUAL_ENV
        PYTHONHOME="$runtime_home"
        PYTHONNOUSERSITE=1
        PYTHONDONTWRITEBYTECODE=1
        LD_LIBRARY_PATH="$runtime_home/lib"
        export PYTHONHOME PYTHONNOUSERSITE PYTHONDONTWRITEBYTECODE LD_LIBRARY_PATH
        ulimit -c 0 2>/dev/null || :
        "$runtime" -B /mnt/us/kindle-voice-bridge/bt_session.py --client stop --timeout 40 2>&1
    )
)
printf '%s\n' "$reply"
case "$reply" in
    ok*)
        printf 'Bluetooth returns in a few seconds.\n'
        ;;
esac
]]

-- The daemon stays connected. This command only opens and closes SCO.
local session_record_command = [[
exec 2>&1
printf 'Voice Bridge record and send (Bluetooth session)\n\n'
runtime_home=/mnt/us/kindle-voice-bridge/python31117
runtime="$runtime_home/bin/python3.11"
if [ ! -f "$runtime" ]; then
    printf 'The bundled Python is missing.\n'
    printf '\nResult: not sent. The bundled Python is missing.\n'
    printf '\nRecord and send finished.\n'
    exit 1
fi
reply=$(
    (
        unset PYTHONPATH PYTHONSTARTUP PYTHONUSERBASE VIRTUAL_ENV
        PYTHONHOME="$runtime_home"
        PYTHONNOUSERSITE=1
        PYTHONDONTWRITEBYTECODE=1
        LD_LIBRARY_PATH="$runtime_home/lib"
        export PYTHONHOME PYTHONNOUSERSITE PYTHONDONTWRITEBYTECODE LD_LIBRARY_PATH
        ulimit -c 0 2>/dev/null || :
        "$runtime" -B /mnt/us/kindle-voice-bridge/bt_session.py --client record 2>&1
    )
)
printf '%s\n' "$reply"
printf '\nResult: %s\n' "$reply"
printf '\nRecord and send finished.\n'
]]

local export_keys_command = [[
exec 2>&1
keys_source=/var/local/zbluetooth/bt_devices.xml
keys_target=/mnt/us/kindle-voice-bridge/bt-keys.txt
if [ ! -r "$keys_source" ]; then
    printf 'Cannot read %s. No link keys were exported.\n' "$keys_source"
    exit 1
fi
if ! cat "$keys_source" > "$keys_target"; then
    printf 'Cannot write %s. Check the folder, the permissions and the free space.\n' "$keys_target"
    exit 1
fi
printf 'Exported the Bluetooth link keys to %s.\n' "$keys_target"
printf 'Connect USB storage to copy the file to your computer. Delete the file after the test.\n'
]]

local function run(command)
    local pipe = io.popen(command, "r")
    if not pipe then return nil end
    local read_ok, text = pcall(pipe.read, pipe, "*a")
    pcall(pipe.close, pipe)
    if read_ok then return text end
end

local function read(path)
    local file = io.open(path, "r")
    if not file then return nil end
    local read_ok, text = pcall(file.read, file, "*a")
    pcall(file.close, file)
    if read_ok then return text end
end

-- The device script prints one line that starts with "Result: ".
local function last_result(text)
    if not text then return nil end
    local result = nil
    for line in text:gmatch("[^\n]+") do
        if line:sub(1, 8) == "Result: " then
            result = line:sub(9)
        end
    end
    return result
end

local function save(path, text)
    local file = io.open(path, "w")
    if not file then return false end
    local written = file:write(text)
    local closed = file:close()
    return written and closed
end

local VoiceBridge = WidgetContainer:extend{
    name = "voicebridge",
    is_doc_only = false,
    record_running = false,
    record_phase = nil,
    record_page = nil,
    status_message = nil,
}

function VoiceBridge:init()
    self:onDispatcherRegisterActions()
    self.ui.menu:registerToMainMenu(self)
end

-- One gesture toggles manual capture in an active resident session.
function VoiceBridge:onDispatcherRegisterActions()
    Dispatcher:registerAction("voicebridge_record", {
        category = "none",
        event = "VoiceBridgeRecord",
        title = _("Voice Bridge: start/stop recording"),
        general = true,
    })
end

function VoiceBridge:onVoiceBridgeRecord()
    self:toggleManualRecording()
    return true
end

function VoiceBridge:runDiagnostics()
    local output = run(diagnostics_command) or ""
    local complete = output:sub(-#end_marker) == end_marker
    if not complete then
        output = output .. "\nCould not complete diagnostics.\n"
    end
    local message
    if not save(log_path, output) then
        message = _("The plugin cannot write diagnostics.txt. Check that /mnt/us/kindle-voice-bridge exists and is writable. Check the free space.")
    elseif not complete then
        message = _("Diagnostics did not finish. The plugin saved the log. Reconnect USB storage. Open kindle-voice-bridge/diagnostics.txt on your computer.")
    else
        message = _("The plugin saved the log. Reconnect USB storage. Open kindle-voice-bridge/diagnostics.txt on your computer.")
    end
    UIManager:show(InfoMessage:new{ text = message })
end

function VoiceBridge:inspectBluetooth()
    local output = run(bluetooth_command) or ""
    local complete = output:sub(-#bluetooth_end_marker) == bluetooth_end_marker
    if not complete then
        output = output .. "\nBluetooth inspection did not finish.\n"
    end
    local message
    if not save(bluetooth_log_path, output) then
        message = _("The plugin cannot save bluetooth-inspection.txt. Check that /mnt/us/kindle-voice-bridge exists and is writable. Check the free space.")
    elseif not complete then
        message = _("The Bluetooth inspection did not finish. The plugin saved a partial report. Restart KOReader and try again.")
    else
        message = _("The plugin saved bluetooth-inspection.txt. Connect USB storage. Open kindle-voice-bridge/bluetooth-inspection.txt on your computer.")
    end
    UIManager:show(InfoMessage:new{ text = message })
end

function VoiceBridge:checkBumbleRuntime()
    local output = run(bumble_check_command) or ""
    local complete = output:sub(-#bumble_check_end_marker) == bumble_check_end_marker
    if not complete then
        output = output .. "\nThe Bumble runtime check did not finish.\n"
    end
    local message
    if not save(bumble_check_log_path, output) then
        message = _("The plugin cannot save bumble-check.txt. Check that /mnt/us/kindle-voice-bridge exists and is writable. Check the free space.")
    elseif not complete then
        message = _("The Bumble runtime check did not finish. The plugin saved a partial report. Restart KOReader and try again.")
    else
        message = _("The plugin saved bumble-check.txt. Connect USB storage. Open kindle-voice-bridge/bumble-check.txt on your computer.")
    end
    UIManager:show(InfoMessage:new{ text = message })
end

function VoiceBridge:exportBluetoothKeys()
    local output = run(export_keys_command)
    if not output or output == "" then
        output = _("The export did not start. Check that /mnt/us/kindle-voice-bridge exists and is writable.")
    end
    UIManager:show(InfoMessage:new{ text = output })
end

function VoiceBridge:confirmExportBluetoothKeys()
    UIManager:show(ConfirmBox:new{
        text = _("The exported file contains secret Bluetooth link keys. Another device can use these keys to impersonate the Bluetooth identity of this Kindle. The plugin writes the keys to /mnt/us/kindle-voice-bridge/bt-keys.txt. Delete this file after the test. Export the keys?"),
        ok_text = _("Export"),
        ok_callback = function() self:exportBluetoothKeys() end,
    })
end

-- Show a short status message. The message closes itself, and the result
-- dialog replaces it. A failed run calls clearStatus, so no stale message
-- stays on the screen.
function VoiceBridge:showStatus(text, timeout)
    self:clearStatus()
    local message
    message = InfoMessage:new{
        text = text,
        timeout = timeout or status_timeout_seconds,
        dismiss_callback = function()
            if self.status_message == message then
                self.status_message = nil
            end
        end,
    }
    self.status_message = message
    UIManager:show(message)
end

function VoiceBridge:clearStatus()
    local message = self.status_message
    self.status_message = nil
    if message then
        pcall(UIManager.close, UIManager, message)
    end
end

-- Live recording page -------------------------------------------------------
--
-- The daemon rewrites /tmp/voicebridge-bt-session.state every 100 ms:
--
--   v1 <heartbeat> <phase> <elapsed_ms> <remaining_ms> <link> <result>
--
-- The page reads that file on a timer, so the state word and the countdown
-- come from the daemon and never from the plugin's own guess. SPEAK NOW
-- appears only after the daemon reports that SCO is open and its audio
-- callback is installed.

local page_tick_seconds = 0.25
local page_stale_ticks = 6   -- 1.5 s without a heartbeat change
local page_start_ticks = 20  -- 5 s to leave idle after Start
local page_stop_ticks = 80   -- 20 s for a stop to finish
local page_answer_ticks = 260 -- 65 s to wait for the answer

local function write_command(path, text)
    local file = io.open(path, "w")
    if not file then return false end
    local written = file:write(text)
    local closed = file:close()
    return written and closed
end

local function read_state(path)
    local file = io.open(path or state_path, "r")
    if not file then return nil end
    local line = file:read("*l")
    local answer_line = file:read("*l")
    file:close()
    if not line then return nil end
    local heartbeat, phase, elapsed, remaining, link, result =
        line:match("^v1 (%d+) (%a+) (%-?%d+) (%-?%d+) (%a+) (.*)$")
    if not heartbeat then return nil end
    local answer = nil
    if answer_line then
        answer = answer_line:match("^answer%s+(.*)$")
    end
    return {
        heartbeat = tonumber(heartbeat),
        phase = phase,
        elapsed_ms = tonumber(elapsed),
        remaining_ms = tonumber(remaining),
        link = link,
        result = result,
        answer = answer,
    }
end

-- A strip along the bottom edge. It does not cover the page, so the reader
-- stays usable and taps outside the strip still turn pages.
local RecordPage = InputContainer:extend{
    name = "voicebridge_record_page",
    phase = "waiting",
    remaining_ms = -1,
    result = nil,
    heartbeat = nil,
    stale_ticks = 0,
    start_ticks = 0,
    stop_ticks = 0,
    stop_requested = false,
    closed = false,
    waiting = false,
    answer = nil,
    answer_ticks = 0,
    state_path = nil,
    command_path = nil,
}

function RecordPage:init()
    local height = Screen:scaleBySize(64)
    self.dimen = Geom:new{
        x = 0,
        y = Screen:getHeight() - height,
        w = Screen:getWidth(),
        h = height,
    }
    self.ges_events = {
        TapStop = {
            GestureRange:new{ ges = "tap", range = self.dimen },
        },
    }
    self:build()
end

function RecordPage:paintTo(bb, _x, _y)
    -- The strip always sits on the bottom edge, whatever position the window
    -- manager passes, so the layout does not depend on show() position support.
    InputContainer.paintTo(self, bb, self.dimen.x, self.dimen.y)
end

function RecordPage:is_live()
    return self.phase == "recording"
end

function RecordPage:is_finished()
    return self.phase == "done" or self.phase == "failed" or
        self.phase == "stale"
end

function RecordPage:state_word()
    local phase = self.phase
    if phase == "recording" then return _("SPEAK NOW") end
    if phase == "preparing" then return _("PREPARING") end
    if phase == "sending" or phase == "stopping" then return _("SENDING") end
    if phase == "done" then return _("SENT") end
    if phase == "answer" then return _("RESPONSE READY") end
    if phase == "failed" then return _("NOT SENT") end
    if phase == "stale" then return _("NO SESSION") end
    return _("WAITING")
end

function RecordPage:count_word()
    if self.phase ~= "recording" then return "" end
    local remaining = self.remaining_ms
    if remaining == nil or remaining < 0 then return "" end
    return tostring(math.max(0, math.ceil(remaining / 1000)))
end

function RecordPage:hint_word()
    local phase = self.phase
    if phase == "recording" then
        return _("Tap the bar to stop and send")
    elseif phase == "preparing" then
        return _("Opening the microphone…")
    elseif phase == "sending" or phase == "stopping" then
        return _("Uploading the recording…")
    elseif phase == "answer" then
        return _("Tap the bar to read it")
    elseif phase == "done" then
        if self.waiting then return _("Waiting for the answer…") end
        return _("Tap the bar to close")
    elseif self:is_finished() then
        return _("Tap the bar to close")
    end
    return _("Waiting for the session…")
end

function RecordPage:result_word()
    if not self:is_finished() then return "" end
    local text = self.result
    if not text or text == "-" then return "" end
    text = text:gsub("^ok%s+", "")
    text = text:gsub("^error%s+", "")
    return text
end

function RecordPage:build()
    local width = self.dimen.w
    local height = self.dimen.h
    local padding = Screen:scaleBySize(10)
    local gap = Screen:scaleBySize(14)
    local max_width = width - padding * 2
    local live = self:is_live()
    local background = live and Blitbuffer.COLOR_BLACK or Blitbuffer.COLOR_WHITE
    local foreground = live and Blitbuffer.COLOR_WHITE or Blitbuffer.COLOR_BLACK

    self.state_text = TextWidget:new{
        text = self:state_word(),
        face = Font:getFace("cfont", 20),
        bold = true,
        fgcolor = foreground,
        max_width = max_width,
    }
    self.count_text = TextWidget:new{
        text = self:count_word(),
        face = Font:getFace("cfont", 30),
        bold = true,
        fgcolor = foreground,
        max_width = max_width,
    }
    self.hint_text = TextWidget:new{
        text = self:hint_word(),
        face = Font:getFace("cfont", 12),
        fgcolor = foreground,
        max_width = max_width,
    }
    self.result_text = TextWidget:new{
        text = self:result_word(),
        face = Font:getFace("cfont", 12),
        fgcolor = foreground,
        max_width = max_width,
    }

    self.top_group = HorizontalGroup:new{
        align = "center",
        self.state_text,
        HorizontalSpan:new{ width = gap },
        self.count_text,
    }
    self.bottom_group = HorizontalGroup:new{
        align = "center",
        self.hint_text,
        HorizontalSpan:new{ width = gap },
        self.result_text,
    }
    self.group = VerticalGroup:new{
        align = "center",
        self.top_group,
        VerticalSpan:new{ width = Screen:scaleBySize(4) },
        self.bottom_group,
    }
    self[1] = FrameContainer:new{
        background = background,
        bordersize = 0,
        padding = padding,
        margin = 0,
        width = width,
        height = height,
        self.group,
    }
end

function RecordPage:rebuild()
    if self[1] then
        self[1]:free()
        self[1] = nil
    end
    self:build()
end

function RecordPage:start()
    UIManager:show(self, "ui", self.dimen, self.dimen.x, self.dimen.y)
    self:scheduleTick()
end

function RecordPage:scheduleTick()
    UIManager:scheduleIn(page_tick_seconds, function()
        self:tick()
    end)
end

function RecordPage:tick()
    if self.closed then return end
    local state = read_state(self.state_path)
    if not state then
        self.stale_ticks = self.stale_ticks + 1
        if self.stale_ticks * page_tick_seconds >= 2.0 then
            self:finish("stale", _("The Bluetooth session stopped."))
            return
        end
        self:scheduleTick()
        return
    end
    if state.heartbeat == self.heartbeat then
        self.stale_ticks = self.stale_ticks + 1
        if self.stale_ticks >= page_stale_ticks then
            self:finish("stale", _("The Bluetooth session stopped responding."))
            return
        end
    else
        self.heartbeat = state.heartbeat
        self.stale_ticks = 0
    end
    self:apply(state)
    if self.waiting then
        if state.answer and state.answer ~= "-" then
            self.answer = state.answer
            self.waiting = false
            self:set_phase("answer")
        else
            self.answer_ticks = self.answer_ticks + 1
            if self.answer_ticks >= page_answer_ticks then
                -- No answer is coming. Keep the result and let a tap close.
                self.waiting = false
                self:rebuild()
                UIManager:setDirty(self, "ui", self.dimen)
            end
        end
    end
    if not self.closed then
        self:scheduleTick()
    end
end

function RecordPage:apply(state)
    local phase = state.phase

    if phase == "idle" then
        if self.stop_requested then
            self:finish_result(state.result)
            return
        end
        if self.phase == "waiting" or self.phase == "preparing" then
            -- The daemon never left idle, so the start command failed.
            self.start_ticks = self.start_ticks + 1
            if self.start_ticks >= page_start_ticks then
                self:finish("failed", self:start_error())
                return
            end
            self:set_phase("waiting")
            return
        end
        -- The 15-second cap ended the capture without a stop request.
        self:finish_result(state.result)
        return
    end

    if phase == "sending" or phase == "stopping" then
        self:set_phase("sending")
        return
    end

    if phase == "recording" then
        if self.stop_requested then
            self.stop_ticks = self.stop_ticks + 1
            if self.stop_ticks >= page_stop_ticks then
                self:finish("failed", _("Stop & send did not finish."))
                return
            end
            self:set_phase("stopping")
            return
        end
        self.remaining_ms = state.remaining_ms
        self:set_phase("recording")
        return
    end

    if phase == "preparing" then
        self:set_phase("preparing")
        return
    end

    self:set_phase("waiting")
end

function RecordPage:start_error()
    local text = read(manual_start_log_path)
    if text then
        for line in text:gmatch("[^\n]+") do
            if line:match("^error%s") then
                return (line:gsub("^error%s+", ""))
            end
        end
    end
    return _("The recording did not start. Start the Bluetooth session first.")
end

function RecordPage:set_phase(phase)
    if phase == self.phase then
        self:refresh_count()
        return
    end
    self.phase = phase
    self:rebuild()
    UIManager:setDirty(self, "ui", self.dimen)
end

function RecordPage:refresh_count()
    if self.phase ~= "recording" then return end
    local text = self:count_word()
    if text == self.count_text.text then return end
    self.count_text:setText(text)
    self.top_group:resetLayout()
    self.group:resetLayout()
    UIManager:setDirty(self, "ui", self.dimen)
end

function RecordPage:finish(kind, result)
    self.phase = kind
    self.result = result
    if kind == "done" then
        -- The receiver may still send an answer, so keep polling the state.
        self.waiting = true
        self.answer_ticks = 0
        self:rebuild()
        UIManager:setDirty(self, "ui", self.dimen)
        return
    end
    self.closed = true
    self:rebuild()
    UIManager:setDirty(self, "ui", self.dimen)
end

function RecordPage:finish_result(reply)
    if reply and reply:sub(1, 3) == "ok " then
        self:finish("done", reply)
    else
        self:finish("failed", reply)
    end
end

function RecordPage:spawn_stop()
    -- The daemon polls this file every 100 ms. Starting a client process takes
    -- several seconds on the device, which is longer than the capture can wait.
    return write_command(self.command_path or command_path, "stop_recording\n")
end

function RecordPage:request_stop()
    if self.stop_requested then return end
    self.stop_requested = true
    self.stop_ticks = 0
    self.phase = "stopping"
    self:rebuild()
    UIManager:setDirty(self, "ui", self.dimen)
    if not self:spawn_stop() then
        self:finish("failed", _("The plugin could not start Stop & send."))
    end
end

function RecordPage:close()
    self.closed = true
    UIManager:close(self, "ui", self.dimen)
end

function RecordPage:show_answer()
    local text = self.answer
    if not text or text == "-" then return end
    UIManager:show(InfoMessage:new{
        text = text,
        dismiss_callback = function() self:close() end,
    })
end

function RecordPage:onTapStop()
    if self.phase == "answer" then
        self:show_answer()
        return
    end
    if self:is_finished() then
        self:close()
        return
    end
    if self.stop_requested then return end
    if self.phase == "recording" or self.phase == "preparing" or
            self.phase == "waiting" then
        self:request_stop()
    end
end

function RecordPage:onCloseWidget()
    self.closed = true
end

function VoiceBridge:manualPageOpen()
    return self.record_page ~= nil and not self.record_page.closed
end

function VoiceBridge:startManualRecording()
    if self.record_running then
        UIManager:show(InfoMessage:new{
            text = _("Another recording action is still running. Wait for it to finish."),
        })
        return
    end
    if self:manualPageOpen() then
        UIManager:show(InfoMessage:new{
            text = _("A manual recording is already active. Use Stop & send."),
        })
        return
    end
    local state = read_state()
    if not state then
        UIManager:show(InfoMessage:new{
            text = _("Start the Bluetooth session first. A session started by an older daemon has no state file; stop it and start it again."),
        })
        return
    end
    if state.phase ~= "idle" then
        UIManager:show(InfoMessage:new{
            text = _("The Bluetooth session is busy. Wait for the current recording to finish."),
        })
        return
    end

    os.remove(manual_start_log_path)
    local pipe = io.popen("(" .. session_start_recording_command .. ") > " ..
        manual_start_log_path .. " 2>&1 &", "r")
    if not pipe then
        UIManager:show(InfoMessage:new{
            text = _("The plugin could not start manual recording."),
        })
        return
    end
    pcall(pipe.close, pipe)
    self.record_page = RecordPage:new{ phase = "waiting" }
    self.record_page:start()
end

function VoiceBridge:stopManualRecording()
    if self:manualPageOpen() then
        self.record_page:onTapStop()
        return
    end
    local state = read_state()
    if not state then
        UIManager:show(InfoMessage:new{
            text = _("Start the Bluetooth session first. A session started by an older daemon has no state file; stop it and start it again."),
        })
        return
    end
    if state.phase == "idle" then
        local result = state.result
        if result and result ~= "-" then
            UIManager:show(InfoMessage:new{
                text = _("Result: ") .. (result:gsub("^ok%s+", "")),
            })
        else
            UIManager:show(InfoMessage:new{ text = _("No manual recording is active.") })
        end
        return
    end
    -- A recording is running that this plugin did not start. Take it over.
    self.record_page = RecordPage:new{ phase = "stopping", stop_requested = true }
    self.record_page:start()
    if not write_command(command_path, "stop_recording\n") then
        UIManager:show(InfoMessage:new{
            text = _("The plugin could not start Stop & send."),
        })
        return
    end
end

function VoiceBridge:toggleManualRecording()
    if self:manualPageOpen() then
        self.record_page:onTapStop()
        return
    end
    local state = read_state()
    if state and state.phase ~= "idle" then
        self:stopManualRecording()
        return
    end
    self:startManualRecording()
end

function VoiceBridge:startRecordAndSend(command, message, poll_phase)
    if self.record_running or self:manualPageOpen() then
        UIManager:show(InfoMessage:new{ text = _("A recording is already running. Wait for it to finish.") })
        return
    end
    os.remove(record_log_path)
    local pipe = io.popen("(" .. command .. ") > " .. record_log_path .. " 2>&1 &", "r")
    if not pipe then
        UIManager:show(InfoMessage:new{ text = _("The plugin could not start the recording.") })
        return
    end
    pcall(pipe.close, pipe)
    self.record_running = true
    self.record_phase = poll_phase and "preparing" or nil
    self:showStatus(message, record_status_timeout_seconds)
    UIManager:scheduleIn(record_poll_seconds, function()
        self:watchRecordLog(os.time() + record_wait_seconds, poll_phase)
    end)
end

function VoiceBridge:watchRecordLog(deadline, poll_phase)
    local text = read(record_log_path)
    local complete = text ~= nil and text:sub(-#record_end_marker) == record_end_marker
    if not complete and os.time() < deadline then
        -- Ask the daemon for the phase only while the microphone prepares.
        -- The daemon can block during the upload, so stop polling after settle.
        if poll_phase and self.record_phase == "preparing" then
            local phase = self:sessionPhase()
            if phase == "recording" then
                self.record_phase = "recording"
                self:showStatus(_("Recording — speak now"), record_status_timeout_seconds)
            elseif phase == nil then
                -- An older daemon or a busy daemon. Keep the preparing
                -- message and stop polling.
                self.record_phase = "unavailable"
            elseif phase == "sending" or phase == "stopping" then
                -- The capture already ended. Stop polling so the synchronous
                -- upload cannot block the plugin.
                self.record_phase = phase
            end
        end
        UIManager:scheduleIn(record_poll_seconds, function()
            self:watchRecordLog(deadline, poll_phase)
        end)
        return
    end
    self.record_running = false
    self.record_phase = nil
    self:clearStatus()
    local result = last_result(text)
    local message
    if not complete then
        message = _("The run did not finish. It may still be running. Open kindle-voice-bridge/record-send.log to check.")
    elseif result then
        message = _("Result: ") .. result
    else
        message = _("The run finished without a result. Open kindle-voice-bridge/record-send.log to check.")
    end
    UIManager:show(InfoMessage:new{ text = message })
end

function VoiceBridge:sessionStatus()
    local output = run(session_status_command)
    if output and output:sub(1, 3) == "ok " then
        return (output:gsub("%s+$", ""))
    end
    return nil
end

-- Return the daemon capture phase, or nil when the daemon is old or absent.
function VoiceBridge:sessionPhase()
    local output = run(session_phase_command)
    if output then
        return output:match("^ok%s+(%a+)")
    end
    return nil
end

function VoiceBridge:startBluetoothSession()
    local output = run(session_start_command)
    if not output or output == "" then
        output = _("The session did not start. Open kindle-voice-bridge/bt-session.log to check.")
    end
    UIManager:show(InfoMessage:new{ text = output })
end

function VoiceBridge:stopBluetoothSession()
    local output = run(session_stop_command)
    if not output or output == "" then
        output = _("The stop request did not complete. Open kindle-voice-bridge/bt-session.log to check.")
    end
    local page = self.record_page
    if page and not page.closed then
        page.closed = true
        pcall(UIManager.close, UIManager, page)
    end
    UIManager:show(InfoMessage:new{ text = output })
end

function VoiceBridge:confirmStartBluetoothSession()
    if self.record_running or self:manualPageOpen() then
        UIManager:show(InfoMessage:new{ text = _("A recording is already running.") })
        return
    end
    UIManager:show(ConfirmBox:new{
        text = _("This action starts a Bluetooth session. The session stops stock Bluetooth once. It keeps the Bluetooth controller for up to 30 minutes after the last recording. A crash can leave Bluetooth unusable. The session reads /mnt/us/kindle-voice-bridge/upload.json and keeps it for later sessions. Stop the HCI probe before you start a session. Start the session?"),
        ok_text = _("Start"),
        ok_callback = function() self:startBluetoothSession() end,
    })
end

-- Legacy fixed-duration convenience action. Manual duration selection uses
-- Start recording and Stop & send, or the single-gesture toggle.
function VoiceBridge:recordAndSend()
    if self.record_running or self:manualPageOpen() then
        UIManager:show(InfoMessage:new{ text = _("A recording is already running. Wait for it to finish.") })
        return
    end
    local session = self:sessionStatus()
    if session then
        local phase = self:sessionPhase()
        if phase and phase ~= "idle" then
            UIManager:show(InfoMessage:new{
                text = _("The Bluetooth session is busy. Finish the active recording first."),
            })
            return
        end
        self:startRecordAndSend(session_record_command, _("Preparing the microphone…\n\nThe Bluetooth session stays connected. A crash can leave Bluetooth unusable. The result appears when the run ends."), true)
        return
    end
    UIManager:show(ConfirmBox:new{
        text = _("This action records the headset microphone for about five seconds and sends the recording to the receiver. Bluetooth stops twice during the run. A crash can leave Bluetooth unusable. The run reads /mnt/us/kindle-voice-bridge/upload.json and keeps it for later runs. Start the recording?"),
        ok_text = _("Record"),
        ok_callback = function()
            self:startRecordAndSend(record_command, _("Preparing the microphone…\n\nBluetooth stops for about one minute. The result appears when the run ends."), false)
        end,
    })
end

-- Keep the old name for callers outside this file.
function VoiceBridge:confirmRecordAndSend()
    self:recordAndSend()
end

function VoiceBridge:runHciCommand(action)
    local arguments = action == "start" and "start --confirm-bluetooth-enabled-in-ui" or "stop"
    local command = hci_command:gsub("\n$", "") .. " " .. arguments .. [[ 2>&1
printf '\nExit status: %s\n' "$?"
]]
    local output = run(command)
    if not output then
        output = _("The HCI launcher could not start.")
    end
    UIManager:show(InfoMessage:new{ text = output })
end

function VoiceBridge:confirmHciStart()
    UIManager:show(ConfirmBox:new{
        text = _("Is Bluetooth enabled in the Kindle UI? Select Start only if Bluetooth is enabled. Stock Bluetooth will be interrupted only after the laptop connects. Raw HCI traffic is plaintext. Bluetooth restoration is not guaranteed. USB recovery is not verified."),
        ok_text = _("Start"),
        ok_callback = function() self:runHciCommand("start") end,
    })
end

function VoiceBridge:addToMainMenu(menu_items)
    menu_items.voicebridge = {
        text = _("Voice Bridge"),
        sorting_hint = "tools",
        sub_item_table = {
            {
                text = _("Run diagnostics"),
                callback = function() self:runDiagnostics() end,
            },
            {
                text = _("Inspect Bluetooth"),
                callback = function() self:inspectBluetooth() end,
            },
            {
                text = _("Check Bumble runtime"),
                callback = function() self:checkBumbleRuntime() end,
            },
            {
                text = _("Start Bluetooth session"),
                callback = function() self:confirmStartBluetoothSession() end,
            },
            {
                text = _("Stop Bluetooth session"),
                callback = function() self:stopBluetoothSession() end,
            },
            {
                text = _("Start recording"),
                callback = function() self:startManualRecording() end,
            },
            {
                text = _("Stop & send"),
                callback = function() self:stopManualRecording() end,
            },
            {
                text = _("Record and send"),
                callback = function() self:recordAndSend() end,
            },
            {
                text = _("Start HCI probe"),
                callback = function() self:confirmHciStart() end,
            },
            {
                text = _("Stop HCI probe"),
                callback = function() self:runHciCommand("stop") end,
            },
            {
                text = _("Export Bluetooth link keys"),
                callback = function() self:confirmExportBluetoothKeys() end,
            },
        },
    }
end

return VoiceBridge
