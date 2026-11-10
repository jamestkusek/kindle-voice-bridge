local InfoMessage = require("ui/widget/infomessage")
local ConfirmBox = require("ui/widget/confirmbox")
local UIManager = require("ui/uimanager")
local WidgetContainer = require("ui/widget/container/widgetcontainer")
local _ = require("gettext")

local base_path = "/mnt/us/kindle-voice-bridge"
local log_path = base_path .. "/diagnostics.txt"
local bluetooth_log_path = base_path .. "/bluetooth-inspection.txt"
local end_marker = "\nDiagnostics complete.\n"
local bluetooth_end_marker = "\nBluetooth inspection complete.\n"

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
