local InfoMessage = require("ui/widget/infomessage")
local ConfirmBox = require("ui/widget/confirmbox")
local UIManager = require("ui/uimanager")
local WidgetContainer = require("ui/widget/container/widgetcontainer")
local _ = require("gettext")

local base_path = "/mnt/us/kindle-voice-bridge"
local log_path = base_path .. "/diagnostics.txt"
local bluetooth_log_path = base_path .. "/bluetooth-inspection.txt"
local bumble_check_log_path = base_path .. "/bumble-check.txt"
local record_log_path = base_path .. "/record-send.log"
local end_marker = "\nDiagnostics complete.\n"
local bluetooth_end_marker = "\nBluetooth inspection complete.\n"
local bumble_check_end_marker = "\nBumble runtime check complete.\n"
local record_end_marker = "\nRecord and send finished.\n"
local record_wait_seconds = 200
local record_poll_seconds = 3

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
-- restores it in a finally path. The script deletes upload.json after it reads
-- the token. The result line is written to the log.
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
}

function VoiceBridge:init()
    self.ui.menu:registerToMainMenu(self)
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

function VoiceBridge:startRecordAndSend()
    if self.record_running then
        UIManager:show(InfoMessage:new{ text = _("A recording is already running.") })
        return
    end
    os.remove(record_log_path)
    local pipe = io.popen("(" .. record_command .. ") > " .. record_log_path .. " 2>&1 &", "r")
    if not pipe then
        UIManager:show(InfoMessage:new{ text = _("The plugin could not start the recording.") })
        return
    end
    pcall(pipe.close, pipe)
    self.record_running = true
    UIManager:show(InfoMessage:new{
        text = _("The recording started. Bluetooth stops for about one minute. The result appears when the run ends."),
    })
    UIManager:scheduleIn(record_poll_seconds, function()
        self:watchRecordLog(os.time() + record_wait_seconds)
    end)
end

function VoiceBridge:watchRecordLog(deadline)
    local text = read(record_log_path)
    local complete = text ~= nil and text:sub(-#record_end_marker) == record_end_marker
    if not complete and os.time() < deadline then
        UIManager:scheduleIn(record_poll_seconds, function()
            self:watchRecordLog(deadline)
        end)
        return
    end
    self.record_running = false
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

function VoiceBridge:confirmRecordAndSend()
    UIManager:show(ConfirmBox:new{
        text = _("This action records the headset microphone for about five seconds and sends the recording to the receiver. Bluetooth stops twice during the run. A crash can leave Bluetooth unusable. The run reads /mnt/us/kindle-voice-bridge/upload.json and deletes it. Start the recording?"),
        ok_text = _("Record"),
        ok_callback = function() self:startRecordAndSend() end,
    })
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
        sorting_hint = "more_tools",
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
                text = _("Record and send"),
                callback = function() self:confirmRecordAndSend() end,
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
