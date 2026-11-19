-- Offline test for the Voice Bridge record page.
--
-- The page is driven by /tmp/voicebridge-bt-session.state, which the daemon
-- rewrites every 100 ms. This spec stubs the KOReader modules that main.lua
-- requires, loads the plugin, and drives RecordPage through the state machine
-- with a state file it writes itself. It never touches a Kindle, Bluetooth, or
-- the real control socket. The second half covers the reading context
-- collector, its JSON payload and the highlight dialog entry, with a fake ui
-- and a context file in the same temporary directory.
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
    "read_state = read_state, encode_context = encode_context, " ..
    "fit_context = fit_context, context_path = context_path, " ..
    "context_log_path = context_log_path }")
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
local function publish(phase, elapsed_ms, remaining_ms, link, result, answer)
    heartbeat = heartbeat + 1
    local file = assert(io.open(STATE, "w"))
    file:write(string.format("v1 %d %s %d %d %s %s\nanswer %s\n",
        heartbeat, phase, elapsed_ms, remaining_ms, link or "connected",
        result or "-", answer or "-"))
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
check(not page.closed, "the bar stays open for the answer")
check(page.waiting, "the bar is waiting for the answer")

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

-- 15. The answer turns the bar into a prompt, and closing it closes the bar.
publish("recording", 100, 14900)
page = new_page()
page:tick()
page:onTapStop()
publish("idle", -1, -1, "connected", "ok sent rec_11 3.00s")
page:tick()
equal(page.phase, "done", "the capture ended")
check(page.waiting, "the bar waits for the answer")
equal(page:hint_word(), "Waiting for the answer…", "the hint says it is waiting")
publish("idle", -1, -1, "connected", "ok sent rec_11 3.00s", "It is a cloud.")
page:tick()
equal(page.phase, "answer", "the answer phase is shown")
equal(page:state_word(), "RESPONSE READY", "the answer word")
page:onTapStop()
local message = UIManager.shown[#UIManager.shown].widget
equal(message.text, "It is a cloud.", "the popup shows the answer")
message.dismiss_callback()
check(page.closed, "closing the popup closes the bar")

-- Reading context ----------------------------------------------------------

local CONTEXT = STATE .. ".context.json"
local CONTEXT_LOG = STATE .. ".context.log"
local ASK_COMMAND = STATE .. ".ask.cmd"
local BOOK = STATE .. ".book.epub"

local function new_ui(options)
    options = options or {}
    local ui = {
        document = options.document,
        doc_settings = options.doc_settings,
        toc = options.toc,
        highlight = options.highlight,
        page = options.page,
        menu = {
            registerToMainMenu = function(self, plugin) self.plugin = plugin end,
        },
    }
    ui.getCurrentPage = function() return ui.page end
    return ui
end

-- Each getter exists only when the test supplies it, so a missing API and a
-- raising API are both reachable.
local function new_document(options)
    options = options or {}
    return {
        file = options.file,
        getProps = options.props and function() return options.props end,
        getPageCount = options.page_count and function() return options.page_count end,
        getXPointer = options.xpointer and function() return options.xpointer end,
        getTOC = options.toc and function() return options.toc end,
    }
end

local function read_text(path)
    local handle = io.open(path, "r")
    if not handle then return nil end
    local text = handle:read("*a")
    handle:close()
    return text
end

local function contains(haystack, needle)
    return haystack ~= nil and haystack:find(needle, 1, true) ~= nil
end

-- 16. No open document means no context, and no error.
local no_book = exports.VoiceBridge:new{ ui = new_ui{} }
check(no_book:collectReadingContext() == nil,
    "collectReadingContext returns nil without a document")
check(no_book:writeReadingContext(nil) == false,
    "writeReadingContext refuses a missing context")

-- 17. Every getter feeds one context, and the payload is the daemon shape.
local book = assert(io.open(BOOK, "w"))
book:write("0123456")
book:close()

local reader_ui = new_ui{
    document = new_document{
        file = BOOK,
        props = {
            title = 'Rain "and" \\ Snow',
            authors = "Ursula K. Le Guin; Kim Stanley Robinson",
        },
        page_count = 10,
        xpointer = "!/body/DocFragment[3]/body/div/p[1]",
    },
    doc_settings = {
        readSetting = function(_, key)
            if key == "partial_md5_checksum" then return "abc123" end
        end,
    },
    toc = { getTocTitleOfCurrentPage = function() return "Chapter\nTwo" end },
    page = 3,
}
local ask = exports.VoiceBridge:new{
    ui = reader_ui,
    context_path = CONTEXT,
    context_log_path = CONTEXT_LOG,
}
local context = ask:collectReadingContext()
check(context ~= nil, "a document yields a context")
equal(context.book.title, 'Rain "and" \\ Snow', "the title comes from the props")
equal(context.book.authors[1], "Ursula K. Le Guin", "authors are split on the semicolon")
equal(context.book.authors[2], "Kim Stanley Robinson", "the second author is kept")
equal(context.book.size_bytes, 7, "the size comes from the book file")
equal(context.book.partial_md5, "abc123", "the partial md5 comes from the settings")
equal(context.book.format, "epub", "the format comes from the suffix")
equal(context.pos.page, 3, "the page comes from the ui")
equal(context.pos.total_pages, 10, "the total comes from the document")
equal(context.pos.fraction, 0.3, "the fraction is page over total")
equal(context.pos.chapter, "Chapter\nTwo", "the chapter comes from the toc")
equal(context.locator.xpointer, "!/body/DocFragment[3]/body/div/p[1]",
    "the xpointer comes from the document")
check(context.highlight == nil, "no selection means no highlight")

local payload = exports.encode_context(context)
check(contains(payload, '"v":1'), "the payload carries the version")
check(contains(payload, '"title":"Rain \\"and\\" \\\\ Snow"'),
    "quotes and backslashes are escaped")
check(contains(payload, '"chapter":"Chapter\\nTwo"'), "the newline is escaped")
check(contains(payload, '"page":3'), "the page is a number")
check(contains(payload, '"fraction":0.3'), "the fraction is a number")
check(contains(payload, '"highlight":null'), "an absent highlight is null")
check(contains(payload, '"locator":{"xpointer":'),
    "the locator section is present")

-- 18. Control characters are escaped, not written raw.
local escaped = exports.encode_context{
    v = 1,
    book = { title = "T", authors = {}, size_bytes = 0, partial_md5 = "", format = "epub" },
    pos = { page = 1, total_pages = 2, fraction = 0.5, chapter = "" },
    locator = { xpointer = "", href = "" },
    highlight = 'a\1b\tc"d\\e',
}
check(contains(escaped, "\\u0001"), "a control character is escaped")
check(contains(escaped, "\\t"), "a tab is escaped")
check(contains(escaped, '\\"d'), "an inner quote is escaped")
check(contains(escaped, "\\\\e"), "an inner backslash is escaped")

-- 19. A long selection is cropped so the file stays under the byte budget.
local long_text = string.rep("\195\169", 2000)  -- 4000 bytes of two-byte characters
local long_context = {
    v = 1,
    book = { title = "Book", authors = {}, size_bytes = 0, partial_md5 = "", format = "epub" },
    pos = { page = 1, total_pages = 2, fraction = 0.5, chapter = "" },
    locator = { xpointer = "", href = "" },
    highlight = long_text,
}
local fitted = exports.fit_context(long_context, 2500)
check(#fitted <= 2500, "the fitted payload fits the byte budget")
check(#long_context.highlight > 0, "the highlight keeps a prefix")
equal(long_text:sub(1, #long_context.highlight), long_context.highlight,
    "the kept text is a prefix of the selection")
-- A cut is clean when the next byte starts a character, not continues one.
local after = long_text:byte(#long_context.highlight + 1)
check(after == nil or after < 0x80 or after > 0xBF,
    "the cut falls on a UTF-8 boundary")
equal(#long_context.highlight % 2, 0, "the kept text holds whole characters")
check(ask:writeReadingContext(long_context), "writeReadingContext reports success")
local stored = read_text(CONTEXT)
check(stored ~= nil, "the context file is written")
check(#stored <= 2500, "the context file stays under the byte budget")
check(contains(stored, '"highlight":"'), "the cropped highlight is kept")

-- 20. The ask writes the context before the start command, and the bar opens.
publish("idle", -1, -1, "connected", "-")
os.remove(ASK_COMMAND)
ask.state_path = STATE
ask.command_path = ASK_COMMAND
ask:startManualRecording()
local ask_payload = read_text(CONTEXT)
check(contains(ask_payload, '"title":"Rain'),
    "startManualRecording writes the context file")
check(contains(ask_payload, '"page":3'), "the context file carries the page")
local command = io.open(ASK_COMMAND, "r")
check(command ~= nil, "startManualRecording writes the start command")
if command then
equal(command:read("*l"), "start_recording", "the start command is written")
command:close()
end
check(ask:manualPageOpen(), "the recording bar is shown")

-- 21. A context file that cannot be written does not block the ask.
os.remove(ASK_COMMAND)
local blocked = exports.VoiceBridge:new{
    ui = reader_ui,
    state_path = STATE,
    command_path = ASK_COMMAND,
    context_path = STATE .. ".missing-dir/context.json",
}
blocked:startManualRecording()
check(read_text(ASK_COMMAND) ~= nil, "the ask still starts without a context file")

-- 22. The probe shows the fields and appends one log line.
local shown = #UIManager.shown
ask:showReadingContext()
equal(#UIManager.shown, shown + 1, "the probe shows one message")
local probe = UIManager.shown[#UIManager.shown].widget
check(contains(probe.text, "Title: Rain"), "the probe shows the title")
check(contains(probe.text, "Authors: Ursula K. Le Guin"), "the probe shows the authors")
check(contains(probe.text, "Page: 3 of 10"), "the probe shows the page")
check(contains(probe.text, "Fraction: 0.300"), "the probe shows the fraction")
check(contains(probe.text, "Chapter: Chapter"), "the probe shows the chapter")
check(contains(probe.text, "XPointer: !/body"), "the probe shows the xpointer")
check(contains(probe.text, "Highlight: no"), "the probe reports no highlight")
local log = read_text(CONTEXT_LOG)
check(log ~= nil, "the probe appends to the context log")
check(contains(log, "page=3/10"), "the log line carries the page")
check(contains(log, "highlight=no"), "the log line carries the highlight flag")
check(not contains(log, "\n\n"), "the log line is one line per probe")

-- 23. Each getter is optional: a raising or missing API drops one field.
local broken_ui = new_ui{
    document = { getProps = function() error("no props on this build") end },
    page = 5,
}
local degraded = exports.VoiceBridge:new{ ui = broken_ui }
local partial = degraded:collectReadingContext()
check(partial ~= nil, "a partial document still yields a context")
equal(partial.book.title, "", "a raising getProps drops only the title")
equal(partial.pos.page, 5, "the page still comes from the ui")
check(partial.pos.total_pages == nil, "a missing getPageCount drops the total")
check(partial.pos.fraction == nil, "no fraction without a total")
equal(partial.locator.xpointer, "", "a missing getXPointer drops the xpointer")
equal(partial.book.size_bytes, 0, "a missing file drops the size")
equal(partial.book.format, "", "a missing file drops the format")
check(contains(exports.encode_context(partial), '"total_pages":null'),
    "an unknown total encodes as null")

degraded.view = { state = { page = 7 } }
local fallback = new_ui{ document = broken_ui.document }
check(fallback:getCurrentPage() == nil, "the fallback ui has no page getter")
degraded.ui = fallback
equal(degraded:collectReadingContext().pos.page, 7,
    "the view state supplies the page when the ui does not")

-- 24. The highlight entry stashes the text, closes the dialog and asks.
local highlight = {
    selected_text = { text = "It was a bright cold day in April." },
    cleared = 0,
}
highlight.clear = function(self) self.cleared = self.cleared + 1 end
highlight.addToHighlightDialog = function(self, id, get_button)
    self.entry_id = id
    self.button = get_button(self)
end
reader_ui.highlight = highlight
local asked = 0
local entry = exports.VoiceBridge:new{ ui = reader_ui, context_path = CONTEXT .. ".hl" }
entry.startManualRecording = function() asked = asked + 1 end
equal(highlight.entry_id, "voicebridge_ask", "the entry is registered once")
check(highlight.button ~= nil, "the dialog receives one button")
equal(highlight.button.text, "Ask Voice Bridge", "the button names the action")
highlight.button.callback()
equal(entry.selected_text, "It was a bright cold day in April.",
    "the selection is stashed for the ask")
equal(highlight.cleared, 1, "the dialog is closed")
equal(asked, 1, "the normal ask starts")
equal(entry:collectReadingContext().highlight, "It was a bright cold day in April.",
    "the stashed selection becomes the highlight")

-- 25. A build without addToHighlightDialog gets the wrapped opener instead.
local legacy = {
    selected_text = { text = "legacy selection" },
    onShowHighlightDialog = function()
        return { { text = "Highlight", callback = function() end } }
    end,
}
local legacy_asked = 0
local legacy_ui = new_ui{ document = reader_ui.document, page = 3, highlight = legacy }
local legacy_entry = exports.VoiceBridge:new{ ui = legacy_ui }
legacy_entry.startManualRecording = function() legacy_asked = legacy_asked + 1 end
local buttons = legacy.onShowHighlightDialog(legacy)
equal(#buttons, 2, "the wrapped opener appends one entry")
equal(buttons[2].text, "Ask Voice Bridge", "the appended entry names the action")
buttons[2].callback()
equal(legacy_entry.selected_text, "legacy selection",
    "the wrapped entry stashes the selection")
equal(legacy_asked, 1, "the wrapped entry starts the ask")

-- 26. A build with no highlight hooks is a supported configuration.
local plain = exports.VoiceBridge:new{ ui = new_ui{ document = reader_ui.document, page = 3 } }
check(plain:registerHighlightEntry() == false, "no hooks is not an error")
local menu = {}
plain:addToMainMenu(menu)
local texts = {}
for _, item in ipairs(menu.voicebridge.sub_item_table) do
    texts[#texts + 1] = item.text
end
local joined = table.concat(texts, "|")
check(contains(joined, "Ask about this page"), "the page entry is always offered")
check(contains(joined, "Show reading context"), "the probe entry is always offered")

local page_asked = 0
local page_entry = exports.VoiceBridge:new{ ui = new_ui{ document = reader_ui.document, page = 3 } }
page_entry.startManualRecording = function() page_asked = page_asked + 1 end
page_entry.selected_text = "stale selection from an earlier ask"
page_entry:askAboutPage()
equal(page_asked, 1, "the page entry starts the ask")
check(page_entry.selected_text == nil,
    "a page-only ask drops a stale selection")

os.remove(CONTEXT)
os.remove(CONTEXT .. ".hl")
os.remove(CONTEXT_LOG)
os.remove(ASK_COMMAND)
os.remove(BOOK)
os.remove(STATE)
os.remove(STATE .. ".bad")

if failures > 0 then
    io.stderr:write(string.format("%d of %d checks failed\n", failures, checks))
end
print(string.format("%d checks passed", checks))

-- Under pytest the wrapper reads this table and asserts on it, so a failing
-- check produces a normal test failure instead of aborting the whole run.
-- A standalone `lua tests/koreader/voicebridge_spec.lua` still exits non-zero.
local summary = { failures = failures, checks = checks }
if os.getenv("VOICEBRIDGE_SPEC_REPORT") ~= "1" and failures > 0 then
    os.exit(1)
end
return summary
