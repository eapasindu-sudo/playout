// cefcg: Playout's built-in web browser for web overlays (scoreboards, score bugs), like vMix's Web Browser input.
//
// Chromium Embedded (CEF) draws the page off screen, on a see-through background, straight into a shared
// memory block that Playout reads: no screenshots and no PNG pictures to make and decode, so the page can
// run at the output frame rate with little CPU.
//
//   cefcg.exe --cg-url=URL --cg-width=1920 --cg-height=1080 --cg-fps=25 --cg-shm=NAME
//             [--cg-js=FILE] [--cg-cache=FOLDER]
//
// Shared memory NAME (made by Playout, 64 + width * height * 4 bytes):
//   0  uint32 magic 'CEFG'   4 uint32 width   8 uint32 height   12 uint32 loaded (1 once the page loaded)
//   16 int64 picture number: odd while a picture is being written, even when it is complete
//   64 width * height BGRA pixels, premultiplied alpha, top row first
//
// Commands on stdin, one per line: reload, hide (stop drawing: the CG is off air), show, quit.
// stdin closing (Playout stopped) quits too. Lines on stdout: "loaded", "error <text>".

#include <windows.h>

#include <atomic>
#include <cstring>
#include <fstream>
#include <sstream>
#include <string>
#include <thread>

#include "include/base/cef_callback.h"
#include "include/cef_app.h"
#include "include/cef_browser.h"
#include "include/cef_client.h"
#include "include/cef_command_line.h"
#include "include/cef_render_handler.h"
#include "include/wrapper/cef_closure_task.h"
#include "include/wrapper/cef_helpers.h"

namespace {

const uint32_t kMagic = 0x47464543;  // "CEFG"
const size_t kHeader = 64;

struct Options {
  std::string url, js, cache;
  std::wstring shm;
  int width = 1920, height = 1080, fps = 25;
};
Options g_opt;

uint8_t* g_mem = nullptr;
CefRefPtr<CefBrowser> g_browser;
bool g_quit = false;

void Say(const std::string& line) {
  std::string s = line + "\n";
  DWORD n = 0;
  HANDLE h = ::GetStdHandle(STD_OUTPUT_HANDLE);
  if (h && h != INVALID_HANDLE_VALUE)
    ::WriteFile(h, s.data(), static_cast<DWORD>(s.size()), &n, nullptr);
}

class Client : public CefClient, public CefRenderHandler, public CefLifeSpanHandler, public CefLoadHandler {
 public:
  CefRefPtr<CefRenderHandler> GetRenderHandler() override { return this; }
  CefRefPtr<CefLifeSpanHandler> GetLifeSpanHandler() override { return this; }
  CefRefPtr<CefLoadHandler> GetLoadHandler() override { return this; }

  void GetViewRect(CefRefPtr<CefBrowser>, CefRect& rect) override { rect = CefRect(0, 0, g_opt.width, g_opt.height); }

  void OnPaint(CefRefPtr<CefBrowser>, PaintElementType type, const RectList&, const void* buffer, int width,
               int height) override {
    if (type != PET_VIEW || !g_mem)
      return;
    auto* seq = reinterpret_cast<volatile LONG64*>(g_mem + 16);
    ::InterlockedIncrement64(seq);  // odd: being written
    const int w = width < g_opt.width ? width : g_opt.width;
    const int h = height < g_opt.height ? height : g_opt.height;
    const auto* src = static_cast<const uint8_t*>(buffer);
    uint8_t* dst = g_mem + kHeader;
    if (w != g_opt.width || h != g_opt.height)
      std::memset(dst, 0, static_cast<size_t>(g_opt.width) * g_opt.height * 4);
    for (int y = 0; y < h; ++y)
      std::memcpy(dst + static_cast<size_t>(y) * g_opt.width * 4, src + static_cast<size_t>(y) * width * 4,
                  static_cast<size_t>(w) * 4);
    ::InterlockedIncrement64(seq);  // even: complete
  }

  void OnAfterCreated(CefRefPtr<CefBrowser> browser) override {
    g_browser = browser;
    if (g_quit)
      browser->GetHost()->CloseBrowser(true);
  }

  void OnBeforeClose(CefRefPtr<CefBrowser>) override {
    g_browser = nullptr;
    CefQuitMessageLoop();
  }

  void OnLoadEnd(CefRefPtr<CefBrowser>, CefRefPtr<CefFrame> frame, int) override {
    if (!frame->IsMain())
      return;
    if (!g_opt.js.empty())
      frame->ExecuteJavaScript(g_opt.js, frame->GetURL(), 0);
    if (g_mem)
      *reinterpret_cast<volatile uint32_t*>(g_mem + 12) = 1;
    Say("loaded");
  }

  void OnLoadError(CefRefPtr<CefBrowser>, CefRefPtr<CefFrame> frame, ErrorCode code, const CefString& text,
                   const CefString&) override {
    if (frame->IsMain() && code != ERR_ABORTED)
      Say("error Could not open the page: " + text.ToString());
  }

 private:
  IMPLEMENT_REFCOUNTING(Client);
};

class App : public CefApp {
 public:
  void OnBeforeCommandLineProcessing(const CefString& process_type, CefRefPtr<CefCommandLine> cl) override {
    if (!process_type.empty())
      return;
    cl->AppendSwitch("mute-audio");
    cl->AppendSwitchWithValue("force-device-scale-factor", "1");
    cl->AppendSwitchWithValue("autoplay-policy", "no-user-gesture-required");
    cl->AppendSwitch("disable-background-timer-throttling");
    cl->AppendSwitch("disable-renderer-backgrounding");
    cl->AppendSwitch("disable-extensions");
    cl->AppendSwitch("no-first-run");
  }

 private:
  IMPLEMENT_REFCOUNTING(App);
};

void Command(const std::string& cmd) {
  CEF_REQUIRE_UI_THREAD();
  if (cmd == "quit") {
    g_quit = true;
    if (g_browser)
      g_browser->GetHost()->CloseBrowser(true);
    return;
  }
  if (!g_browser)
    return;
  if (cmd == "reload")
    g_browser->ReloadIgnoreCache();
  else if (cmd == "hide")
    g_browser->GetHost()->WasHidden(true);
  else if (cmd == "show") {
    g_browser->GetHost()->WasHidden(false);
    g_browser->GetHost()->Invalidate(PET_VIEW);
  }
}

void ReadCommands() {
  HANDLE in = ::GetStdHandle(STD_INPUT_HANDLE);
  std::string buf;
  char chunk[256];
  DWORD n = 0;
  while (in && in != INVALID_HANDLE_VALUE && ::ReadFile(in, chunk, sizeof(chunk), &n, nullptr) && n > 0) {
    buf.append(chunk, n);
    size_t at;
    while ((at = buf.find('\n')) != std::string::npos) {
      std::string line = buf.substr(0, at);
      buf.erase(0, at + 1);
      while (!line.empty() && (line.back() == '\r' || line.back() == ' '))
        line.pop_back();
      if (!line.empty())
        CefPostTask(TID_UI, base::BindOnce(&Command, line));
    }
  }
  CefPostTask(TID_UI, base::BindOnce(&Command, std::string("quit")));   // Playout is gone
}

std::string ReadFileText(const std::wstring& path) {
  std::ifstream f(path, std::ios::binary);
  std::stringstream ss;
  ss << f.rdbuf();
  return ss.str();
}

int Num(CefRefPtr<CefCommandLine> cl, const char* name, int def, int lo, int hi) {
  std::string v = cl->GetSwitchValue(name).ToString();
  int n = v.empty() ? def : std::atoi(v.c_str());
  return n < lo ? lo : (n > hi ? hi : n);
}

}  // namespace

int APIENTRY wWinMain(HINSTANCE instance, HINSTANCE, LPWSTR, int) {
  ::SetErrorMode(::GetErrorMode() | SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX | SEM_NOOPENFILEERRORBOX);
  CefMainArgs main_args(instance);
  CefRefPtr<App> app(new App);
  int code = CefExecuteProcess(main_args, app, nullptr);   // the browser's own helper processes end here
  if (code >= 0)
    return code;

  CefRefPtr<CefCommandLine> cl = CefCommandLine::CreateCommandLine();
  cl->InitFromString(::GetCommandLineW());
  g_opt.url = cl->GetSwitchValue("cg-url").ToString();
  g_opt.width = Num(cl, "cg-width", 1920, 16, 7680);
  g_opt.height = Num(cl, "cg-height", 1080, 16, 4320);
  g_opt.fps = Num(cl, "cg-fps", 25, 1, 60);
  g_opt.shm = cl->GetSwitchValue("cg-shm").ToWString();
  g_opt.cache = cl->GetSwitchValue("cg-cache").ToString();
  std::wstring js = cl->GetSwitchValue("cg-js").ToWString();
  if (!js.empty())
    g_opt.js = ReadFileText(js);
  if (g_opt.url.empty() || g_opt.shm.empty()) {
    Say("error cefcg needs --cg-url and --cg-shm");
    return 2;
  }

  const size_t size = kHeader + static_cast<size_t>(g_opt.width) * g_opt.height * 4;
  HANDLE map = ::OpenFileMappingW(FILE_MAP_ALL_ACCESS, FALSE, g_opt.shm.c_str());
  if (!map)
    map = ::CreateFileMappingW(INVALID_HANDLE_VALUE, nullptr, PAGE_READWRITE, static_cast<DWORD>(size >> 32),
                               static_cast<DWORD>(size & 0xffffffff), g_opt.shm.c_str());
  if (map)
    g_mem = static_cast<uint8_t*>(::MapViewOfFile(map, FILE_MAP_ALL_ACCESS, 0, 0, size));
  if (!g_mem) {
    Say("error cefcg could not open the shared picture memory");
    return 3;
  }
  auto* hdr = reinterpret_cast<uint32_t*>(g_mem);
  hdr[1] = static_cast<uint32_t>(g_opt.width);
  hdr[2] = static_cast<uint32_t>(g_opt.height);
  hdr[3] = 0;
  hdr[0] = kMagic;

  CefSettings settings;
  settings.no_sandbox = true;
  settings.windowless_rendering_enabled = true;
  settings.log_severity = LOGSEVERITY_DISABLE;
  if (!g_opt.cache.empty())
    CefString(&settings.root_cache_path) = g_opt.cache;
  if (!CefInitialize(main_args, settings, app, nullptr)) {
    Say("error The built-in browser did not start");
    return 4;
  }

  CefWindowInfo window;
  window.SetAsWindowless(nullptr);
  CefBrowserSettings bs;
  bs.windowless_frame_rate = g_opt.fps;
  bs.background_color = 0;   // see-through: the page's own background shows only where it draws one
  CefBrowserHost::CreateBrowser(window, new Client, g_opt.url, bs, nullptr, nullptr);

  std::thread(ReadCommands).detach();
  CefRunMessageLoop();
  CefShutdown();
  ::UnmapViewOfFile(g_mem);
  ::CloseHandle(map);
  ::ExitProcess(0);   // the stdin thread may still wait on a read
}
