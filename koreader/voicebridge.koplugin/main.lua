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

local VoiceBridge = WidgetContainer:extend{
    name = "voicebridge",
    is_doc_only = false,
    record_page = nil,
    selected_text = nil,
    state_path = nil,
    command_path = nil,
    context_path = nil,
    context_log_path = nil,
}

function VoiceBridge:init()
    self:onDispatcherRegisterActions()
    self.ui.menu:registerToMainMenu(self)
    self:registerHighlightEntry()
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

-- Reading context -----------------------------------------------------------
--
-- One optional JSON file per ask: /tmp/voicebridge-context.json, which the
-- daemon reads when the capture starts. Every getter runs in its own pcall, so
-- a build without one API loses only that field, and a missing document loses
-- the whole file.

local context_path = "/tmp/voicebridge-context.json"
local context_log_path = "/tmp/voicebridge-context.log"
-- The daemon's header cap is 4096 decoded bytes, after base64 expansion.
local context_max_bytes = 2500
local highlight_entry_id = "voicebridge_ask"

local function attempt(getter)
    local ok, value = pcall(getter)
    if ok then return value end
    return nil
end

local function as_text(value)
    if type(value) == "string" then return value end
    return nil
end

local function as_count(value)
    if type(value) == "number" and value >= 0 then
        return math.floor(value)
    end
    return nil
end

-- KOReader reports authors as one string on some builds and a list on others.
local function author_list(value)
    local names = {}
    if type(value) == "table" then
        for _, item in ipairs(value) do
            local name = as_text(item)
            if name and name ~= "" then names[#names + 1] = name end
        end
        return names
    end
    local text = as_text(value)
    if not text then return names end
    for part in text:gmatch("[^;\n]+") do
        local name = part:match("^%s*(.-)%s*$")
        if name and name ~= "" then names[#names + 1] = name end
    end
    return names
end

-- Read the book size without loading the file.
local function file_size(path)
    local text = as_text(path)
    if not text then return 0 end
    local file = io.open(text, "rb")
    if not file then return 0 end
    local size = file:seek("end")
    file:close()
    return as_count(size) or 0
end

local function file_format(path)
    local text = as_text(path)
    if not text then return "" end
    return (text:lower():match("%.([%w]+)$")) or ""
end

-- The deepest TOC entry at or before the page.
local function chapter_from_toc(toc, page)
    if type(toc) ~= "table" or not page then return nil end
    local best, best_page = nil, nil
    for _, entry in ipairs(toc) do
        if type(entry) == "table" then
            local title = as_text(entry.title)
            local entry_page = as_count(entry.page)
            if title and entry_page and entry_page <= page
                    and (best_page == nil or entry_page >= best_page) then
                best, best_page = title, entry_page
            end
        end
    end
    return best
end

-- One line of text, so a log entry cannot break into several lines.
local function one_line(text)
    if type(text) ~= "string" or text == "" then return "-" end
    return (text:gsub("%s+", " "))
end

local function json_escape(text)
    return (text:gsub('[%c"\\]', function(char)
        if char == '"' then return '\\"' end
        if char == '\\' then return '\\\\' end
        if char == '\n' then return '\\n' end
        if char == '\r' then return '\\r' end
        if char == '\t' then return '\\t' end
        return string.format('\\u%04x', char:byte())
    end))
end

local function json_string(text)
    return '"' .. json_escape(as_text(text) or "") .. '"'
end

local function json_number(value)
    if type(value) ~= "number" then return "null" end
    if value == math.floor(value) and math.abs(value) < 1e15 then
        return string.format("%d", value)
    end
    local text = string.format("%.6f", value)
    text = text:gsub("0+$", ""):gsub("%\\.$", "")
    return text
end

-- Hand-written JSON for the one shape the daemon reads. A general table walker
-- would be more code than the format needs.
local function encode_context(context)
    local book = context.book or {}
    local pos = context.pos or {}
    local locator = context.locator or {}
    local authors = {}
    for _, name in ipairs(book.authors or {}) do
        authors[#authors + 1] = json_string(name)
    end
    local highlight = "null"
    if as_text(context.highlight) and context.highlight ~= "" then
        highlight = json_string(context.highlight)
    end
    return '{"v":1,"book":{' ..
        '"title":' .. json_string(book.title) ..
        ',"authors":[' .. table.concat(authors, ",") .. ']' ..
        ',"size_bytes":' .. json_number(book.size_bytes or 0) ..
        ',"partial_md5":' .. json_string(book.partial_md5) ..
        ',"format":' .. json_string(book.format) ..
        '},"pos":{' ..
        '"page":' .. json_number(pos.page) ..
        ',"total_pages":' .. json_number(pos.total_pages) ..
        ',"fraction":' .. json_number(pos.fraction) ..
        ',"chapter":' .. json_string(pos.chapter) ..
        '},"locator":{' ..
        '"xpointer":' .. json_string(locator.xpointer) ..
        ',"href":' .. json_string(locator.href) ..
        '},"highlight":' .. highlight .. '}'
end

-- The text fields that yield space when the payload is too long, in order.
local function crop_fields(context)
    return {
        {
            read = function() return context.highlight end,
            write = function(text) context.highlight = text end,
        },
        {
            read = function() return context.pos.chapter end,
            write = function(text) context.pos.chapter = text end,
        },
        {
            read = function() return context.book.title end,
            write = function(text) context.book.title = text end,
        },
    }
end

-- Cut at or below limit bytes without splitting a UTF-8 sequence.
local function utf8_prefix(text, limit)
    if #text <= limit then return text end
    local cut = math.max(0, limit)
    while cut > 0 do
        local byte = text:byte(cut + 1)
        if not byte or byte < 0x80 or byte > 0xBF then break end
        cut = cut - 1
    end
    return text:sub(1, cut)
end

-- Keep the encoded payload under max_bytes by shortening one field at a time.
-- The highlight goes first, so a long selection never pushes the file over the
-- header cap.
local function fit_context(context, max_bytes)
    local payload = encode_context(context)
    if #payload <= max_bytes then return payload end
    for _, field in ipairs(crop_fields(context)) do
        local text = field.read()
        if type(text) == "string" and text ~= "" then
            field.write("")
            if #encode_context(context) <= max_bytes then
                -- Longest prefix that still fits. Escaping only adds bytes, so
                -- the encoded length grows with the prefix.
                local low, high = 0, #text
                while low < high do
                    local middle = math.floor((low + high + 1) / 2)
                    field.write(utf8_prefix(text, middle))
                    if #encode_context(context) <= max_bytes then
                        low = middle
                    else
                        high = middle - 1
                    end
                end
                field.write(utf8_prefix(text, low))
                return encode_context(context)
            end
        end
    end
    return encode_context(context)
end

-- Text for the "Show reading context" probe.
local function describe_context(context)
    local book = context.book or {}
    local pos = context.pos or {}
    local locator = context.locator or {}
    local authors = table.concat(book.authors or {}, ", ")
    local fraction = "-"
    if type(pos.fraction) == "number" then
        fraction = string.format("%.3f", pos.fraction)
    end
    return table.concat({
        _("Title: ") .. (book.title ~= "" and book.title or _("(none)")),
        _("Authors: ") .. (authors ~= "" and authors or _("(none)")),
        _("Page: ") .. tostring(pos.page or "-") .. _(" of ") ..
            tostring(pos.total_pages or "-"),
        _("Fraction: ") .. fraction,
        _("Chapter: ") .. (pos.chapter ~= "" and pos.chapter or _("(none)")),
        _("XPointer: ") ..
            (locator.xpointer ~= "" and locator.xpointer or _("(none)")),
        _("Highlight: ") .. (context.highlight and _("yes") or _("no")),
    }, "\n")
end

-- One log line for the probe. The title is the only book text it carries.
local function context_log_line(context)
    local book = context.book or {}
    local pos = context.pos or {}
    local locator = context.locator or {}
    local fraction = "-"
    if type(pos.fraction) == "number" then
        fraction = string.format("%.4f", pos.fraction)
    end
    return string.format("%s page=%s/%s fraction=%s chapter=%s xpointer=%s highlight=%s title=%s\n",
        os.date("!%Y-%m-%dT%H:%M:%SZ"),
        tostring(pos.page or "-"), tostring(pos.total_pages or "-"), fraction,
        one_line(pos.chapter), one_line(locator.xpointer),
        context.highlight and "yes" or "no", one_line(book.title))
end

-- Close the highlight dialog through whichever hook this build has.
local function close_highlight_dialog(highlight)
    for _, name in ipairs({ "closeHighlightDialog", "clear" }) do
        local close = highlight[name]
        if type(close) == "function" then
            pcall(close, highlight)
            return true
        end
    end
    return false
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

-- Reading context for one ask. Returns nil when no document is open. Every
-- getter is optional: a missing or raising API drops only its own field.
function VoiceBridge:collectReadingContext()
    local ui = self.ui
    local document = ui and ui.document
    if not document then return nil end

    local page = as_count(attempt(function() return ui:getCurrentPage() end))
    if not page then
        page = as_count(attempt(function() return self.view.state.page end))
    end
    local total = as_count(attempt(function() return document:getPageCount() end))
    local fraction = nil
    if page and total and total > 0 then
        fraction = page / total
    end

    local props = attempt(function() return document:getProps() end)
    if type(props) ~= "table" then props = {} end
    local path = attempt(function() return document.file end)

    local chapter = as_text(attempt(function()
        return ui.toc:getTocTitleOfCurrentPage()
    end))
    if not chapter then
        chapter = as_text(attempt(function()
            return chapter_from_toc(document:getTOC(), page)
        end))
    end

    return {
        v = 1,
        book = {
            title = as_text(props.title) or "",
            authors = author_list(props.authors),
            size_bytes = file_size(path),
            partial_md5 = as_text(attempt(function()
                return ui.doc_settings:readSetting("partial_md5_checksum")
            end)) or "",
            format = file_format(path),
        },
        pos = {
            page = page,
            total_pages = total,
            fraction = fraction,
            chapter = chapter or "",
        },
        locator = {
            xpointer = as_text(attempt(function()
                return document:getXPointer()
            end)) or "",
            -- Reserved: the daemon resolves the fraction, not the href.
            href = "",
        },
        highlight = self:currentHighlight(),
    }
end

-- The selection the highlight dialog holds right now.
function VoiceBridge:liveSelection()
    local text = as_text(attempt(function()
        return self.ui.highlight.selected_text.text
    end))
    if text == "" then return nil end
    return text
end

-- The text stashed by the highlight entry, else the live selection.
function VoiceBridge:currentHighlight()
    return as_text(self.selected_text) or self:liveSelection()
end

-- Write the context file the daemon reads. A failure returns false and must
-- not block the ask. The context is cropped in place to fit the header cap.
function VoiceBridge:writeReadingContext(context)
    if type(context) ~= "table" then return false end
    local payload = fit_context(context, context_max_bytes)
    local file = io.open(self.context_path or context_path, "wb")
    if not file then return false end
    local written = file:write(payload)
    local closed = file:close()
    return written and closed and true or false
end

-- Probe: show what the daemon would receive and log one line.
function VoiceBridge:showReadingContext()
    local context = self:collectReadingContext()
    if not context then
        UIManager:show(InfoMessage:new{ text = _("Open a book first.") })
        return
    end
    local file = io.open(self.context_log_path or context_log_path, "a")
    if file then
        file:write(context_log_line(context))
        file:close()
    end
    UIManager:show(InfoMessage:new{ text = describe_context(context) })
end

-- The action behind the highlight entry: keep the quoted text for the ask,
-- dismiss the dialog, then run the normal spoken ask.
function VoiceBridge:askAboutSelection(highlight)
    local text = as_text(attempt(function()
        return highlight.selected_text.text
    end))
    self.selected_text = text or self:currentHighlight()
    if highlight then close_highlight_dialog(highlight) end
    self:startManualRecording()
end

-- Builds without addToHighlightDialog open the dialog themselves. Wrap the
-- opener so the entry is appended to the list it returns.
function VoiceBridge:wrapHighlightDialog(highlight)
    local original = highlight.onShowHighlightDialog
    highlight.onShowHighlightDialog = function(this, ...)
        local dialog = original(this, ...)
        if type(dialog) ~= "table" or dialog.text ~= nil then
            -- Unknown shape: leave the dialog as the build made it.
            return dialog
        end
        table.insert(dialog, {
            text = _("Ask Voice Bridge"),
            callback = function() self:askAboutSelection(this) end,
        })
        return dialog
    end
    return true
end

-- Register the highlight dialog entry. Both hooks are optional, so a build
-- without them keeps the menu item and loses only the shortcut.
function VoiceBridge:registerHighlightEntry()
    local highlight = self.ui and self.ui.highlight
    if type(highlight) ~= "table" then return false end
    if type(highlight.addToHighlightDialog) == "function" then
        return pcall(highlight.addToHighlightDialog, highlight,
            highlight_entry_id, function(this)
                return {
                    text = _("Ask Voice Bridge"),
                    callback = function() self:askAboutSelection(this) end,
                }
            end)
    end
    if type(highlight.onShowHighlightDialog) == "function" then
        return self:wrapHighlightDialog(highlight)
    end
    return false
end

-- Menu entry: ask about the page, with the live selection when there is one
-- and page-only context otherwise. A stale stashed selection is dropped.
function VoiceBridge:askAboutPage()
    self.selected_text = self:liveSelection()
    self:startManualRecording()
end

function VoiceBridge:startManualRecording()
    if self:manualPageOpen() then
        UIManager:show(InfoMessage:new{
            text = _("A recording is already active. Tap the bar to stop it."),
        })
        return
    end
    local state = read_state(self.state_path)
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

    -- The context file is optional, so a write failure never blocks the ask.
    local context = self:collectReadingContext()
    if context then
        pcall(self.writeReadingContext, self, context)
    end
    -- One ask uses one selection.
    self.selected_text = nil

    -- The daemon polls this file every 100 ms. Starting a client process takes
    -- several seconds on the device, which is longer than the bar will wait.
    if not write_command(self.command_path or command_path, "start_recording\n") then
        UIManager:show(InfoMessage:new{
            text = _("The plugin could not start the recording."),
        })
        return
    end
    self.record_page = RecordPage:new{
        phase = "waiting",
        state_path = self.state_path,
        command_path = self.command_path,
    }
    self.record_page:start()
end

function VoiceBridge:stopManualRecording()
    if self:manualPageOpen() then
        self.record_page:onTapStop()
        return
    end
    local state = read_state(self.state_path)
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
    self.record_page = RecordPage:new{
        phase = "stopping",
        stop_requested = true,
        state_path = self.state_path,
        command_path = self.command_path,
    }
    self.record_page:start()
    if not write_command(self.command_path or command_path, "stop_recording\n") then
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
    local state = read_state(self.state_path)
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
            {
                text = _("Ask about this page"),
                callback = function() self:askAboutPage() end,
            },
            {
                text = _("Show reading context"),
                callback = function() self:showReadingContext() end,
            },
        },
    }
end

return VoiceBridge
