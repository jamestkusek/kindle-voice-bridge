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




-- Read-only. This command imports Python modules and writes one report file.
-- It does not open the UART, start Bluetooth, or change any device state.

-- Manual start only. This command stops the stock Bluetooth daemon and
-- restores it in a finally path. The script reads upload.json and keeps it so
-- later runs reuse it. The result line is written to the log.

-- The session client uses only the Python standard library. It sends one line
-- to the daemon and prints one line. It does not import Bumble.

-- The phase command reports preparing, recording, sending, idle, or stopping.
-- It is additive. An older daemon replies with an error, and the plugin keeps
-- the preparing message until the result arrives.



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
    record_page = nil,
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

-- Show a short status message. The message closes itself, and the result
-- dialog replaces it. A failed run calls clearStatus, so no stale message
-- stays on the screen.
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
                self:finish("failed", _("The stop did not finish."))
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
        self:finish("failed", _("The plugin could not stop the recording."))
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
    if self:manualPageOpen() then
        UIManager:show(InfoMessage:new{
            text = _("A recording is already active. Tap the bar to stop it."),
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

    -- The daemon polls this file every 100 ms. Starting a client process takes
    -- several seconds on the device, which is longer than the bar will wait.
    if not write_command(command_path, "start_recording\n") then
        UIManager:show(InfoMessage:new{
            text = _("The plugin could not start the recording."),
        })
        return
    end
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
            text = _("The plugin could not stop the recording."),
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

-- Return the daemon capture phase, or nil when the daemon is old or absent.
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
    if self:manualPageOpen() then
        UIManager:show(InfoMessage:new{ text = _("A recording is already running.") })
        return
    end
    UIManager:show(ConfirmBox:new{
        text = _("This action starts a Bluetooth session. The session stops stock Bluetooth once. It keeps the Bluetooth controller for up to 30 minutes after the last recording. A crash can leave Bluetooth unusable. The session reads /mnt/us/kindle-voice-bridge/upload.json and keeps it for later sessions. Start the session?"),
        ok_text = _("Start"),
        ok_callback = function() self:startBluetoothSession() end,
    })
end

function VoiceBridge:addToMainMenu(menu_items)
    menu_items.voicebridge = {
        text = _("Voice Bridge"),
        sorting_hint = "tools",
        sub_item_table = {
            {
                text = _("Start Bluetooth session"),
                callback = function() self:confirmStartBluetoothSession() end,
            },
            {
                text = _("Stop Bluetooth session"),
                callback = function() self:stopBluetoothSession() end,
            },
            {
                text = _("Ask question"),
                callback = function() self:startManualRecording() end,
            },
        },
    }
end

return VoiceBridge
