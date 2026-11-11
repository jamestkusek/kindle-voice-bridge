-- Offline test for the Voice Bridge record page.
--
-- The page is driven by /tmp/voicebridge-bt-session.state, which the daemon
-- rewrites every 100 ms. This spec stubs the KOReader modules that main.lua
-- requires, loads the plugin, and drives RecordPage through the state machine
-- with a state file it writes itself. It never touches a Kindle, Bluetooth, or
-- the real control socket.
--
-- Run it with any Lua 5.1+ interpreter:
--
--   lua tests/koreader/voicebridge_spec.lua
--
-- The Python wrapper (test_record_page.py) runs it through lupa and skips when
-- lupa is unavailable.

local PLUGIN = os.getenv("VOICEBRIDGE_PLUGIN") or
    "koreader/voicebridge.koplugin/main.lua"
local STATE = os.getenv("VOICEBRIDGE_STATE") or
    os.tmpname()

-- Minimal KOReader stand-ins ------------------------------------------------

local function derive(parent, proto)
    proto = proto or {}
    proto.__index = proto
    if parent then
        setmetatable(proto, { __index = parent })
    end
    proto.new = function(self, o)
        o = o or {}
        setmetatable(o, self)
        if o._init then o:_init() end
        if o.init then o:init() end
        return o
    end
    proto.extend = function(self, o)
        return derive(self, o)
    end
    return proto
end

local Widget = derive(nil, {})
Widget.free = function() end
Widget.paintTo = function() end
Widget.getSize = function() return { w = 0, h = 0 } end

local WidgetContainer = derive(Widget, {})
WidgetContainer.clear = function(self)
    for index = #self, 1, -1 do
        self[index] = nil
    end
end
WidgetContainer.free = function(self)
    for _, child in ipairs(self) do
        if child.free then child:free() end
    end
end

local InputContainer = derive(WidgetContainer, {})
function InputContainer:_init()
    if not self.key_events then self.key_events = {} end
    if not self.ges_events then self.ges_events = {} end
end

local TextWidget = derive(Widget, {})
function TextWidget:setText(text) self.text = text end

local VerticalGroup = derive(WidgetContainer, {})
function VerticalGroup:resetLayout() self._size = nil end

local HorizontalGroup = derive(WidgetContainer, {})
function HorizontalGroup:resetLayout() self._size = nil end

local VerticalSpan = derive(Widget, {})
local HorizontalSpan = derive(Widget, {})
local FrameContainer = derive(WidgetContainer, {})

local Screen = {
    width = 1072,
    height = 1448,
}
function Screen:getWidth() return self.width end
function Screen:getHeight() return self.height end
function Screen:scaleBySize(px) return px * 2 end

local UIManager = {
    shown = {},
    dirty = {},
    scheduled = {},
}
function UIManager:show(widget, refreshtype)
    table.insert(self.shown, { widget = widget, refreshtype = refreshtype })
end
function UIManager:close(widget) self.closed_widget = widget end
function UIManager:setDirty(widget, refreshtype)
    table.insert(self.dirty, refreshtype)
end
function UIManager:scheduleIn(seconds, action)
    table.insert(self.scheduled, action)
end
function UIManager:runScheduled()
    local pending = self.scheduled
    self.scheduled = {}
    for _, action in ipairs(pending) do action() end
end

local Geom = { new = function(_, o) return o end }
local GestureRange = { new = function(_, o) return o end }
local Font = { getFace = function(_, name, size) return { name = name, size = size } end }
local Blitbuffer = { COLOR_BLACK = "black", COLOR_WHITE = "white" }
local InfoMessage = { new = function(_, o) return o end }
local ConfirmBox = { new = function(_, o) return o end }
local Dispatcher = { registerAction = function() end }

local function install(name, value)
    package.preload[name] = function() return value end
end

install("ui/widget/infomessage", InfoMessage)
install("ui/widget/confirmbox", ConfirmBox)
install("ui/uimanager", UIManager)
install("ui/widget/container/widgetcontainer", WidgetContainer)
install("ui/widget/container/inputcontainer", InputContainer)
install("ui/widget/container/framecontainer", FrameContainer)
install("ui/widget/textwidget", TextWidget)
install("ui/widget/verticalgroup", VerticalGroup)
install("ui/widget/verticalspan", VerticalSpan)
install("ui/widget/horizontalgroup", HorizontalGroup)
install("ui/widget/horizontalspan", HorizontalSpan)
install("ui/geometry", Geom)
install("ui/gesturerange", GestureRange)
install("ui/font", Font)
install("ffi/blitbuffer", Blitbuffer)
install("device", { screen = Screen })
install("dispatcher", Dispatcher)
install("gettext", function(text) return text end)

-- Load the plugin ----------------------------------------------------------

local source = assert(io.open(PLUGIN, "r"), "cannot open " .. PLUGIN):read("*a")
-- The plugin ends with `return VoiceBridge`. Replace that with a table so the
-- spec can reach the locals it needs without changing the shipped file.
local patched, replaced = source:gsub("\nreturn VoiceBridge%s*$",
    "\nreturn { VoiceBridge = VoiceBridge, RecordPage = RecordPage, " ..
    "read_state = read_state }")
assert(replaced == 1, "the plugin does not end with `return VoiceBridge`")
local load_chunk = loadstring or load
local chunk = assert(load_chunk(patched, PLUGIN))
local exports = chunk()

-- Assertions ---------------------------------------------------------------

local failures = 0
local checks = 0

local function check(condition, message)
    checks = checks + 1
    if not condition then
        failures = failures + 1
        io.stderr:write("FAIL: " .. message .. "\n")
    end
end

local function equal(actual, expected, message)
    check(actual == expected,
        string.format("%s (expected %s, got %s)", message,
            tostring(expected), tostring(actual)))
end

-- Drive the page -----------------------------------------------------------

local heartbeat = 0
local function publish(phase, elapsed_ms, remaining_ms, link, result)
    heartbeat = heartbeat + 1
    local file = assert(io.open(STATE, "w"))
    file:write(string.format("v1 %d %s %d %d %s %s\n",
        heartbeat, phase, elapsed_ms, remaining_ms, link or "connected",
        result or "-"))
    file:close()
end

local function new_page()
    local page = exports.RecordPage:new{ state_path = STATE }
    page.spawn_stop = function() return true end
    return page
end

-- 1. The parser reads the documented shape.
publish("recording", 1000, 14000, "connected", "ok sent rec_1 5.00s")
local parsed = exports.read_state(STATE)
equal(parsed.phase, "recording", "read_state parses the phase")
equal(parsed.elapsed_ms, 1000, "read_state parses the elapsed time")
equal(parsed.remaining_ms, 14000, "read_state parses the remaining time")
equal(parsed.link, "connected", "read_state parses the link state")
equal(parsed.result, "ok sent rec_1 5.00s", "read_state keeps the result text")

-- 2. A malformed or missing file reads as nil.
local missing = exports.read_state(STATE .. ".absent")
check(missing == nil, "read_state returns nil for a missing file")
local bad = assert(io.open(STATE .. ".bad", "w"))
bad:write("not a state line\n")
bad:close()
check(exports.read_state(STATE .. ".bad") == nil,
    "read_state returns nil for a malformed line")

-- 3. Preparing shows PREPARING and no countdown.
publish("preparing", -1, -1)
local page = new_page()
page:tick()
equal(page.phase, "preparing", "preparing state is shown")
equal(page:state_word(), "PREPARING", "preparing word")
equal(page:count_word(), "", "no countdown before SCO is ready")

-- 4. Recording shows SPEAK NOW and a countdown in whole seconds.
publish("recording", 100, 14900)
page:tick()
equal(page.phase, "recording", "recording state is shown")
equal(page:state_word(), "SPEAK NOW", "recording word is the speak cue")
equal(page:count_word(), "15", "countdown starts at 15")
check(page:is_live(), "the recording page is live")

publish("recording", 2100, 12900)
page:tick()
equal(page:count_word(), "13", "countdown counts down")

-- 5. A tap asks the daemon to stop and shows SENDING.
page:onTapStop()
check(page.stop_requested, "a tap requests the stop")
equal(page.phase, "stopping", "a tap shows the stopping state")

publish("recording", 2200, 12800)
page:tick()
equal(page.phase, "stopping", "the page stays in stopping until the daemon agrees")

publish("sending", 2200, 0)
page:tick()
equal(page.phase, "sending", "the daemon sending phase is shown")
equal(page:state_word(), "SENDING", "sending word")

-- 6. The result closes the recording.
publish("idle", -1, -1, "connected", "ok sent rec_9 2.20s")
page:tick()
equal(page.phase, "done", "a successful result ends the page")
equal(page:state_word(), "SENT", "done word")
equal(page:result_word(), "sent rec_9 2.20s", "the result text is shown")
check(page.closed, "the page stops ticking after the result")

-- 7. The 15-second cap ends the recording without a tap.
publish("recording", 14900, 100)
page = new_page()
page:tick()
equal(page.phase, "recording", "the capped page starts recording")
publish("idle", -1, -1, "connected", "ok sent rec_10 15.00s")
page:tick()
equal(page.phase, "done", "the cap ends the page without a tap")
equal(page:result_word(), "sent rec_10 15.00s", "the capped result is shown")

-- 8. A failed upload is reported, not hidden.
publish("recording", 100, 14900)
page = new_page()
page:tick()
page:onTapStop()
publish("idle", -1, -1, "connected", "error not sent. The upload failed: timeout")
page:tick()
equal(page.phase, "failed", "a failed upload ends the page")
equal(page:state_word(), "NOT SENT", "failed word")
equal(page:result_word(), "not sent. The upload failed: timeout",
    "the failure text is shown")

-- 9. A start that never leaves idle fails with a reason.
page = new_page()
for _ = 1, 21 do
    publish("idle", -1, -1)
    page:tick()
end
equal(page.phase, "failed", "a start that never leaves idle fails")
check(page:result_word() ~= "", "the failed start has a reason")

-- 10. A session that stops writing the state file is reported as stale.
publish("recording", 100, 14900)
page = new_page()
page:tick()
equal(page.phase, "recording", "the stale page starts recording")
os.remove(STATE)
for _ = 1, 9 do
    page:tick()
end
equal(page.phase, "stale", "a missing state file ends the page")
equal(page:state_word(), "NO SESSION", "stale word")

-- 11. The page is dismissed by a tap once it is finished.
page:onTapStop()
check(UIManager.closed_widget == page, "a tap closes a finished page")

-- 12. The bar inverts only while the microphone is live.
publish("preparing", -1, -1)
page = new_page()
page:tick()
equal(page[1].background, "white", "preparing is not inverted")
publish("recording", 100, 14900)
page:tick()
equal(page[1].background, "black", "recording inverts the page")

-- 13. The widget is a strip on the bottom edge, not a full-screen page.
page = new_page()
check(page.dimen.h < Screen:getHeight(), "the bar does not cover the page")
equal(page.dimen.y + page.dimen.h, Screen:getHeight(),
    "the bar sits on the bottom edge")
equal(page.dimen.w, Screen:getWidth(), "the bar spans the width")
check(page.covers_fullscreen ~= true, "the bar is not marked fullscreen")
equal(page.ges_events.TapStop[1].range.h, page.dimen.h,
    "the tap range is the bar height")

-- 14. The stop command is a file write, not a process spawn.
local command_file = STATE .. ".cmd"
page = new_page()
page.command_path = command_file
page.spawn_stop = nil
check(page:spawn_stop(), "spawn_stop reports success")
local handle = assert(io.open(command_file, "r"), "the command file was written")
equal(handle:read("*l"), "stop_recording", "the stop command is written")
handle:close()
os.remove(command_file)

os.remove(STATE)
os.remove(STATE .. ".bad")

if failures > 0 then
    io.stderr:write(string.format("%d of %d checks failed\n", failures, checks))
    os.exit(1)
end
print(string.format("%d checks passed", checks))
