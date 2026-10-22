// pgpu: Playout's native GPU engine, built the way vMix's engine works.
//
// The program picture is mixed on the graphics card (Direct3D 11): the clip / live picture, every CG on air and
// the web overlays (scoreboards) are textures drawn over each other there. Web overlays come from Chromium
// Embedded drawing on the GPU itself (shared textures: the page picture never goes through the CPU), and the
// stream and the recording are encoded by the graphics card's encoder (NVENC) straight from the mixed texture.
// The CPU only gets the sound (AAC) and sending (MPEG-TS to Playout, which passes it to Facebook / YouTube).
// Without a usable GPU path each step falls back by itself: the CPU picture copy for web pages, the CPU (or
// another GPU) encoder fed from a copy of the picture.
//
//   pgpu.exe --pg-shm=NAME --pg-width=1920 --pg-height=1080 --pg-fps-num=25 --pg-fps-den=1 [--pg-cache=DIR]
//
// Shared memory NAME (made by Playout, see gpuengine.py for the layout): the program picture, the sound of the
// frame, CG pictures to upload, and the mixed picture read back for NDI / preview.
//
// Commands: one JSON object per line on stdin ("op": frame, drop, web_open, web_reload, web_show, web_close,
// stream_start, stream_stop, rec_start, rec_stop, quit). Events: one line each on stdout ("ready ...",
// "ack K", "web ID loaded|error|mode ...", "stream ...", "rec ...", "stats ..."). The stream's MPEG-TS goes
// through a named pipe that pgpu makes and Playout reads.

#include <windows.h>
#include <d3d11_4.h>
#include <d3dcompiler.h>
#include <dxgi1_2.h>
#include <wrl/client.h>

#include <atomic>
#include <chrono>
#include <cmath>
#include <condition_variable>
#include <cstdio>
#include <cstring>
#include <deque>
#include <map>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include "include/base/cef_callback.h"
#include "include/cef_app.h"
#include "include/cef_browser.h"
#include "include/cef_client.h"
#include "include/cef_command_line.h"
#include "include/cef_render_handler.h"
#include "include/wrapper/cef_closure_task.h"
#include "include/wrapper/cef_helpers.h"

extern "C" {
#include <libavcodec/avcodec.h>
#include <libavformat/avformat.h>
#include <libavutil/audio_fifo.h>
#include <libavutil/channel_layout.h>
#include <libavutil/hwcontext.h>
#include <libavutil/hwcontext_d3d11va.h>
#include <libavutil/imgutils.h>
#include <libavutil/opt.h>
#include <libswscale/swscale.h>
}

using Microsoft::WRL::ComPtr;

namespace {

// ---------------------------------------------------------------- shared memory layout (keep with gpuengine.py)
const uint32_t kMagic = 0x55504750;  // "PGPU"
const size_t kHeader = 256;
const int kMaxSamples = 8192;        // sound samples per channel in one frame, at most
// 0 magic, 4 width, 8 height, then from kHeader: base picture (w*h*4), sound (2 * kMaxSamples float32, one
// channel after the other), read-back picture (w*h*4), upload area (2 * w*h*4)
size_t BaseOff() { return kHeader; }

struct Options {
  std::wstring shm;
  std::string cache;
  int width = 1920, height = 1080, fps_num = 25, fps_den = 1;
};
Options g_opt;
uint8_t* g_mem = nullptr;
size_t PicBytes() { return static_cast<size_t>(g_opt.width) * g_opt.height * 4; }
size_t AudioOff() { return BaseOff() + PicBytes(); }
size_t ReadbackOff() { return AudioOff() + 2 * kMaxSamples * sizeof(float); }
size_t UploadOff() { return ReadbackOff() + PicBytes(); }
size_t UploadBytes() { return 2 * PicBytes(); }
size_t ShmBytes() { return UploadOff() + UploadBytes(); }

std::mutex g_say_lock;
void Say(const std::string& line) {
  std::lock_guard<std::mutex> l(g_say_lock);
  std::string s = line + "\n";
  DWORD n = 0;
  HANDLE h = ::GetStdHandle(STD_OUTPUT_HANDLE);
  if (h && h != INVALID_HANDLE_VALUE)
    ::WriteFile(h, s.data(), static_cast<DWORD>(s.size()), &n, nullptr);
}

std::string OneLine(std::string s) {
  for (auto& c : s)
    if (c == '\n' || c == '\r')
      c = ' ';
  return s.substr(0, 300);
}

std::string AvErr(int err) {
  char buf[AV_ERROR_MAX_STRING_SIZE] = {0};
  av_strerror(err, buf, sizeof(buf));
  return buf;
}

double Now() {
  using namespace std::chrono;
  return duration<double>(steady_clock::now().time_since_epoch()).count();
}

// ---------------------------------------------------------------- a small JSON reader (the commands)
struct Json {
  enum Kind { Null, Bool, Num, Str, Arr, Obj } kind = Null;
  double num = 0;
  bool b = false;
  std::string str;
  std::vector<Json> arr;
  std::vector<std::pair<std::string, Json>> obj;

  const Json* Get(const char* key) const {
    for (const auto& kv : obj)
      if (kv.first == key)
        return &kv.second;
    return nullptr;
  }
  double N(const char* key, double def = 0) const {
    const Json* j = Get(key);
    return j && j->kind == Num ? j->num : (j && j->kind == Bool ? (j->b ? 1 : 0) : def);
  }
  std::string S(const char* key, const std::string& def = "") const {
    const Json* j = Get(key);
    return j && j->kind == Str ? j->str : def;
  }
};

class JsonReader {
 public:
  explicit JsonReader(const std::string& s) : s_(s) {}
  bool Parse(Json& out) {
    bool ok = Value(out);
    Ws();
    return ok && i_ == s_.size();
  }

 private:
  void Ws() {
    while (i_ < s_.size() && (s_[i_] == ' ' || s_[i_] == '\t' || s_[i_] == '\r' || s_[i_] == '\n'))
      ++i_;
  }
  bool Lit(const char* w) {
    size_t n = std::strlen(w);
    if (s_.compare(i_, n, w) != 0)
      return false;
    i_ += n;
    return true;
  }
  static void Utf8(std::string& o, unsigned cp) {
    if (cp < 0x80) {
      o += static_cast<char>(cp);
    } else if (cp < 0x800) {
      o += static_cast<char>(0xC0 | (cp >> 6));
      o += static_cast<char>(0x80 | (cp & 0x3F));
    } else if (cp < 0x10000) {
      o += static_cast<char>(0xE0 | (cp >> 12));
      o += static_cast<char>(0x80 | ((cp >> 6) & 0x3F));
      o += static_cast<char>(0x80 | (cp & 0x3F));
    } else {
      o += static_cast<char>(0xF0 | (cp >> 18));
      o += static_cast<char>(0x80 | ((cp >> 12) & 0x3F));
      o += static_cast<char>(0x80 | ((cp >> 6) & 0x3F));
      o += static_cast<char>(0x80 | (cp & 0x3F));
    }
  }
  bool Hex4(unsigned& v) {
    if (i_ + 4 > s_.size())
      return false;
    v = 0;
    for (int k = 0; k < 4; ++k) {
      char c = s_[i_++];
      v <<= 4;
      if (c >= '0' && c <= '9')
        v |= c - '0';
      else if (c >= 'a' && c <= 'f')
        v |= c - 'a' + 10;
      else if (c >= 'A' && c <= 'F')
        v |= c - 'A' + 10;
      else
        return false;
    }
    return true;
  }
  bool String(std::string& o) {
    if (i_ >= s_.size() || s_[i_] != '"')
      return false;
    ++i_;
    while (i_ < s_.size()) {
      char c = s_[i_++];
      if (c == '"')
        return true;
      if (c != '\\') {
        o += c;
        continue;
      }
      if (i_ >= s_.size())
        return false;
      char e = s_[i_++];
      switch (e) {
        case 'n': o += '\n'; break;
        case 't': o += '\t'; break;
        case 'r': o += '\r'; break;
        case 'b': o += '\b'; break;
        case 'f': o += '\f'; break;
        case 'u': {
          unsigned cp;
          if (!Hex4(cp))
            return false;
          if (cp >= 0xD800 && cp < 0xDC00 && i_ + 6 <= s_.size() && s_[i_] == '\\' && s_[i_ + 1] == 'u') {
            i_ += 2;
            unsigned lo;
            if (!Hex4(lo))
              return false;
            cp = 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00);
          }
          Utf8(o, cp);
          break;
        }
        default: o += e;
      }
    }
    return false;
  }
  bool Value(Json& v) {
    Ws();
    if (i_ >= s_.size())
      return false;
    char c = s_[i_];
    if (c == '{') {
      v.kind = Json::Obj;
      ++i_;
      Ws();
      if (i_ < s_.size() && s_[i_] == '}') {
        ++i_;
        return true;
      }
      while (true) {
        Ws();
        std::string k;
        if (!String(k))
          return false;
        Ws();
        if (i_ >= s_.size() || s_[i_] != ':')
          return false;
        ++i_;
        Json item;
        if (!Value(item))
          return false;
        v.obj.emplace_back(std::move(k), std::move(item));
        Ws();
        if (i_ < s_.size() && s_[i_] == ',') {
          ++i_;
          continue;
        }
        if (i_ < s_.size() && s_[i_] == '}') {
          ++i_;
          return true;
        }
        return false;
      }
    }
    if (c == '[') {
      v.kind = Json::Arr;
      ++i_;
      Ws();
      if (i_ < s_.size() && s_[i_] == ']') {
        ++i_;
        return true;
      }
      while (true) {
        Json item;
        if (!Value(item))
          return false;
        v.arr.push_back(std::move(item));
        Ws();
        if (i_ < s_.size() && s_[i_] == ',') {
          ++i_;
          continue;
        }
        if (i_ < s_.size() && s_[i_] == ']') {
          ++i_;
          return true;
        }
        return false;
      }
    }
    if (c == '"') {
      v.kind = Json::Str;
      return String(v.str);
    }
    if (Lit("true")) {
      v.kind = Json::Bool;
      v.b = true;
      return true;
    }
    if (Lit("false")) {
      v.kind = Json::Bool;
      return true;
    }
    if (Lit("null"))
      return true;
    size_t start = i_;
    while (i_ < s_.size() && (std::strchr("+-.eE", s_[i_]) || (s_[i_] >= '0' && s_[i_] <= '9')))
      ++i_;
    if (i_ == start)
      return false;
    v.kind = Json::Num;
    v.num = std::strtod(s_.substr(start, i_ - start).c_str(), nullptr);
    return true;
  }
  const std::string& s_;
  size_t i_ = 0;
};

// ---------------------------------------------------------------- Direct3D 11
ComPtr<ID3D11Device> g_dev;
ComPtr<ID3D11Device1> g_dev1;
ComPtr<ID3D11DeviceContext> g_ctx;
std::recursive_mutex g_gpu;   // every use of the immediate context (render thread, browser pictures, encoders)
std::string g_adapter;
bool g_nvidia = false, g_warp = false;

void GpuLock(void*) { g_gpu.lock(); }
void GpuUnlock(void*) { g_gpu.unlock(); }

struct Tex {
  ComPtr<ID3D11Texture2D> tex;
  ComPtr<ID3D11ShaderResourceView> srv;
  ComPtr<ID3D11RenderTargetView> rtv;
  int w = 0, h = 0;
};

bool MakeTex(Tex& t, int w, int h, bool target, const void* data = nullptr, UINT pitch = 0) {
  D3D11_TEXTURE2D_DESC d = {};
  d.Width = w;
  d.Height = h;
  d.MipLevels = 1;
  d.ArraySize = 1;
  d.Format = DXGI_FORMAT_B8G8R8A8_UNORM;
  d.SampleDesc.Count = 1;
  d.Usage = D3D11_USAGE_DEFAULT;
  d.BindFlags = D3D11_BIND_SHADER_RESOURCE | (target ? D3D11_BIND_RENDER_TARGET : 0);
  D3D11_SUBRESOURCE_DATA init = {data, pitch, 0};
  t = Tex();
  if (FAILED(g_dev->CreateTexture2D(&d, data ? &init : nullptr, &t.tex)))
    return false;
  if (FAILED(g_dev->CreateShaderResourceView(t.tex.Get(), nullptr, &t.srv)))
    return false;
  if (target && FAILED(g_dev->CreateRenderTargetView(t.tex.Get(), nullptr, &t.rtv)))
    return false;
  t.w = w;
  t.h = h;
  return true;
}

bool PickDevice() {
  ComPtr<IDXGIFactory1> factory;
  if (FAILED(CreateDXGIFactory1(IID_PPV_ARGS(&factory))))
    return false;
  ComPtr<IDXGIAdapter1> pick;
  DXGI_ADAPTER_DESC1 pick_desc = {};
  // the NVIDIA card first (NVENC), then any other real graphics card
  for (int pass = 0; pass < 2 && !pick; ++pass) {
    ComPtr<IDXGIAdapter1> a;
    for (UINT i = 0; factory->EnumAdapters1(i, &a) != DXGI_ERROR_NOT_FOUND; ++i) {
      DXGI_ADAPTER_DESC1 d;
      a->GetDesc1(&d);
      if (d.Flags & DXGI_ADAPTER_FLAG_SOFTWARE)
        continue;
      if (pass == 0 && d.VendorId != 0x10DE)
        continue;
      pick = a;
      pick_desc = d;
      break;
    }
  }
  const D3D_FEATURE_LEVEL levels[] = {D3D_FEATURE_LEVEL_11_1, D3D_FEATURE_LEVEL_11_0, D3D_FEATURE_LEVEL_10_1};
  const UINT flags = D3D11_CREATE_DEVICE_BGRA_SUPPORT | D3D11_CREATE_DEVICE_VIDEO_SUPPORT;
  HRESULT hr = E_FAIL;
  if (pick) {
    hr = D3D11CreateDevice(pick.Get(), D3D_DRIVER_TYPE_UNKNOWN, nullptr, flags, levels, 3, D3D11_SDK_VERSION,
                           &g_dev, nullptr, &g_ctx);
    if (FAILED(hr))
      hr = D3D11CreateDevice(pick.Get(), D3D_DRIVER_TYPE_UNKNOWN, nullptr, D3D11_CREATE_DEVICE_BGRA_SUPPORT,
                             levels, 3, D3D11_SDK_VERSION, &g_dev, nullptr, &g_ctx);
  }
  if (SUCCEEDED(hr)) {
    char name[256] = {0};
    WideCharToMultiByte(CP_UTF8, 0, pick_desc.Description, -1, name, sizeof(name) - 1, nullptr, nullptr);
    g_adapter = name;
    g_nvidia = pick_desc.VendorId == 0x10DE;
  } else {
    // no graphics card driver (a server, a test machine): Windows' own software renderer keeps it working
    hr = D3D11CreateDevice(nullptr, D3D_DRIVER_TYPE_WARP, nullptr, D3D11_CREATE_DEVICE_BGRA_SUPPORT, levels, 3,
                           D3D11_SDK_VERSION, &g_dev, nullptr, &g_ctx);
    if (FAILED(hr))
      return false;
    g_adapter = "Microsoft WARP (software)";
    g_warp = true;
  }
  ComPtr<ID3D11Multithread> mt;
  if (SUCCEEDED(g_ctx.As(&mt)))
    mt->SetMultithreadProtected(TRUE);
  g_dev.As(&g_dev1);
  return true;
}

// ---------------------------------------------------------------- the mixer
const char* kShader = R"(
cbuffer P : register(b0) { float4 rect; float4 uv; float4 prm; };
Texture2D tex : register(t0);
SamplerState smp : register(s0);
struct V { float4 pos : SV_Position; float2 uv : TEXCOORD0; };
V vs(uint id : SV_VertexID) {
  float2 t = float2(id & 1, id >> 1);
  V o;
  o.pos = float4(lerp(rect.x, rect.z, t.x), lerp(rect.y, rect.w, t.y), 0, 1);
  o.uv = lerp(uv.xy, uv.zw, t);
  return o;
}
float4 ps(V i) : SV_Target {
  float4 c = tex.Sample(smp, i.uv);
  if (prm.y < 0.5) c.rgb *= c.a;       // straight alpha (CG pictures) -> premultiplied
  if (prm.z > 0.5) return float4(saturate(c.rgb + prm.w), 1);   // plain copy (scaling, layouts), brightness
  return c * prm.x;                    // fade
}
)";

struct Params {
  float rect[4];
  float uv[4];
  float prm[4];
};

class Mixer {
 public:
  bool Init(int w, int h) {
    ComPtr<ID3DBlob> vsb, psb, err;
    if (FAILED(D3DCompile(kShader, std::strlen(kShader), "mix", nullptr, nullptr, "vs", "vs_4_0", 0, 0, &vsb, &err)) ||
        FAILED(D3DCompile(kShader, std::strlen(kShader), "mix", nullptr, nullptr, "ps", "ps_4_0", 0, 0, &psb, &err)))
      return false;
    if (FAILED(g_dev->CreateVertexShader(vsb->GetBufferPointer(), vsb->GetBufferSize(), nullptr, &vs_)) ||
        FAILED(g_dev->CreatePixelShader(psb->GetBufferPointer(), psb->GetBufferSize(), nullptr, &ps_)))
      return false;
    D3D11_BUFFER_DESC bd = {};
    bd.ByteWidth = sizeof(Params);
    bd.Usage = D3D11_USAGE_DEFAULT;
    bd.BindFlags = D3D11_BIND_CONSTANT_BUFFER;
    if (FAILED(g_dev->CreateBuffer(&bd, nullptr, &cb_)))
      return false;
    // over: colour = src + dst * (1 - src alpha); the program's own alpha stays as it is (as cg.composite)
    D3D11_BLEND_DESC b = {};
    b.RenderTarget[0].BlendEnable = TRUE;
    b.RenderTarget[0].SrcBlend = D3D11_BLEND_ONE;
    b.RenderTarget[0].DestBlend = D3D11_BLEND_INV_SRC_ALPHA;
    b.RenderTarget[0].BlendOp = D3D11_BLEND_OP_ADD;
    b.RenderTarget[0].SrcBlendAlpha = D3D11_BLEND_ZERO;
    b.RenderTarget[0].DestBlendAlpha = D3D11_BLEND_ONE;
    b.RenderTarget[0].BlendOpAlpha = D3D11_BLEND_OP_ADD;
    b.RenderTarget[0].RenderTargetWriteMask = D3D11_COLOR_WRITE_ENABLE_ALL;
    if (FAILED(g_dev->CreateBlendState(&b, &over_)))
      return false;
    b.RenderTarget[0].BlendEnable = FALSE;
    if (FAILED(g_dev->CreateBlendState(&b, &copy_)))
      return false;
    D3D11_SAMPLER_DESC s = {};
    s.Filter = D3D11_FILTER_MIN_MAG_MIP_POINT;
    s.AddressU = s.AddressV = s.AddressW = D3D11_TEXTURE_ADDRESS_CLAMP;
    s.MaxLOD = D3D11_FLOAT32_MAX;
    if (FAILED(g_dev->CreateSamplerState(&s, &point_)))
      return false;
    s.Filter = D3D11_FILTER_MIN_MAG_MIP_LINEAR;
    if (FAILED(g_dev->CreateSamplerState(&s, &linear_)))
      return false;
    D3D11_RASTERIZER_DESC r = {};
    r.FillMode = D3D11_FILL_SOLID;
    r.CullMode = D3D11_CULL_NONE;
    r.ScissorEnable = FALSE;
    r.DepthClipEnable = TRUE;
    if (FAILED(g_dev->CreateRasterizerState(&r, &rs_)))
      return false;
    if (!MakeTex(pgm_, w, h, true))
      return false;
    D3D11_TEXTURE2D_DESC sd = {};
    sd.Width = w;
    sd.Height = h;
    sd.MipLevels = sd.ArraySize = 1;
    sd.Format = DXGI_FORMAT_B8G8R8A8_UNORM;
    sd.SampleDesc.Count = 1;
    sd.Usage = D3D11_USAGE_STAGING;
    sd.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
    return SUCCEEDED(g_dev->CreateTexture2D(&sd, nullptr, &staging_));
  }

  Tex& Program() { return pgm_; }

  void Base(const uint8_t* bgra) {
    g_ctx->UpdateSubresource(pgm_.tex.Get(), 0, nullptr, bgra, pgm_.w * 4, 0);
  }

  // squeeze: the program goes into box over black (and any picture under it): its own texture first
  Tex* BaseTex(const uint8_t* bgra) {
    if (!base_.tex && !MakeTex(base_, pgm_.w, pgm_.h, false))
      return nullptr;
    g_ctx->UpdateSubresource(base_.tex.Get(), 0, nullptr, bgra, pgm_.w * 4, 0);
    return &base_;
  }

  void Black() {
    const float black[4] = {0, 0, 0, 1};
    g_ctx->ClearRenderTargetView(pgm_.rtv.Get(), black);
  }

  void Begin(Tex& target) {
    D3D11_VIEWPORT vp = {0, 0, static_cast<float>(target.w), static_cast<float>(target.h), 0, 1};
    ID3D11RenderTargetView* rtv = target.rtv.Get();
    g_ctx->OMSetRenderTargets(1, &rtv, nullptr);
    g_ctx->RSSetViewports(1, &vp);
    g_ctx->RSSetState(rs_.Get());
    g_ctx->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLESTRIP);
    g_ctx->IASetInputLayout(nullptr);
    g_ctx->VSSetShader(vs_.Get(), nullptr, 0);
    g_ctx->PSSetShader(ps_.Get(), nullptr, 0);
    ID3D11Buffer* cb = cb_.Get();
    g_ctx->VSSetConstantBuffers(0, 1, &cb);
    g_ctx->PSSetConstantBuffers(0, 1, &cb);
    tw_ = target.w;
    th_ = target.h;
  }

  // draws src (its part u0..u1, v0..v1) at x, y (w x h pixels) over the target
  void Draw(Tex& src, float x, float y, float w, float h, float opacity, bool premultiplied, bool scale,
            bool plain_copy = false, float u0 = 0, float v0 = 0, float u1 = 1, float v1 = 1, float bright = 0) {
    Params p;
    p.rect[0] = x / tw_ * 2 - 1;
    p.rect[1] = 1 - y / th_ * 2;
    p.rect[2] = (x + w) / tw_ * 2 - 1;
    p.rect[3] = 1 - (y + h) / th_ * 2;
    p.uv[0] = u0;
    p.uv[1] = v0;
    p.uv[2] = u1;
    p.uv[3] = v1;
    p.prm[0] = opacity;
    p.prm[1] = premultiplied ? 1.f : 0.f;
    p.prm[2] = plain_copy ? 1.f : 0.f;
    p.prm[3] = bright;
    g_ctx->UpdateSubresource(cb_.Get(), 0, nullptr, &p, 0, 0);
    ID3D11ShaderResourceView* srv = src.srv.Get();
    g_ctx->PSSetShaderResources(0, 1, &srv);
    ID3D11SamplerState* smp = scale ? linear_.Get() : point_.Get();
    g_ctx->PSSetSamplers(0, 1, &smp);
    float f[4] = {0, 0, 0, 0};
    g_ctx->OMSetBlendState(plain_copy ? copy_.Get() : over_.Get(), f, 0xffffffff);
    g_ctx->Draw(4, 0);
    ID3D11ShaderResourceView* none = nullptr;
    g_ctx->PSSetShaderResources(0, 1, &none);
  }

  void End() {
    ID3D11RenderTargetView* none = nullptr;
    g_ctx->OMSetRenderTargets(1, &none, nullptr);
  }

  // the mixed picture, scaled to w x h when the encoder wants another size
  Tex* Sized(int w, int h) {
    if (w == pgm_.w && h == pgm_.h)
      return &pgm_;
    Tex& t = sized_[(static_cast<int64_t>(w) << 32) | h];
    if (!t.tex && !MakeTex(t, w, h, true))
      return nullptr;
    Begin(t);
    Draw(pgm_, 0, 0, static_cast<float>(w), static_cast<float>(h), 1, true, true, true);
    End();
    return &t;
  }

  // a vertical (9:16) picture for one destination, made from the mixed picture on the GPU
  Tex* Layout(int w, int h, const std::string& layout, float pos) {
    if (layout != "crop" && layout != "blur")
      return Sized(w, h);
    const float f = (pgm_.h * 9.f / 16.f) / pgm_.w;   // part of the width a 9:16 crop takes
    Tex& t = sized_[(static_cast<int64_t>(w) << 32) | (static_cast<int64_t>(h) << 1) | 1];
    if (!t.tex && !MakeTex(t, w, h, true))
      return nullptr;
    if (layout == "crop") {
      const float u0 = (1 - f) * pos;
      Begin(t);
      Draw(pgm_, 0, 0, static_cast<float>(w), static_cast<float>(h), 1, true, true, true, u0, 0, u0 + f, 1);
      End();
      return &t;
    }
    // blur: the centre of the picture, made very tiny and blown up again (soft), a little darker; the whole
    // picture over it across the width, in the middle
    Tex& tiny = sized_[(static_cast<int64_t>(w / 8) << 32) | (static_cast<int64_t>(h / 8) << 1) | 1];
    if (!tiny.tex && !MakeTex(tiny, w / 8 > 0 ? w / 8 : 1, h / 8 > 0 ? h / 8 : 1, true))
      return nullptr;
    const float u0 = (1 - f) * 0.5f;
    Begin(tiny);
    Draw(pgm_, 0, 0, static_cast<float>(tiny.w), static_cast<float>(tiny.h), 1, true, true, true, u0, 0, u0 + f, 1);
    End();
    Begin(t);
    Draw(tiny, 0, 0, static_cast<float>(w), static_cast<float>(h), 1, true, true, true, 0, 0, 1, 1, -0.12f);
    const float fh = w * 9.f / 16.f;
    Draw(pgm_, 0, (h - fh) / 2, static_cast<float>(w), fh, 1, true, true, true);
    End();
    return &t;
  }

  bool Readback(uint8_t* dst) {
    g_ctx->CopyResource(staging_.Get(), pgm_.tex.Get());
    D3D11_MAPPED_SUBRESOURCE m;
    if (FAILED(g_ctx->Map(staging_.Get(), 0, D3D11_MAP_READ, 0, &m)))
      return false;
    const size_t row = static_cast<size_t>(pgm_.w) * 4;
    for (int y = 0; y < pgm_.h; ++y)
      std::memcpy(dst + y * row, static_cast<const uint8_t*>(m.pData) + static_cast<size_t>(y) * m.RowPitch, row);
    g_ctx->Unmap(staging_.Get(), 0);
    return true;
  }

 private:
  ComPtr<ID3D11VertexShader> vs_;
  ComPtr<ID3D11PixelShader> ps_;
  ComPtr<ID3D11Buffer> cb_;
  ComPtr<ID3D11BlendState> over_, copy_;
  ComPtr<ID3D11SamplerState> point_, linear_;
  ComPtr<ID3D11RasterizerState> rs_;
  ComPtr<ID3D11Texture2D> staging_;
  Tex pgm_, base_;
  std::map<int64_t, Tex> sized_;
  float tw_ = 1, th_ = 1;
};

Mixer g_mixer;

// ---------------------------------------------------------------- web overlays (Chromium on the GPU)
class Page;
std::mutex g_pages_lock;
std::map<int, CefRefPtr<Page>> g_pages;

class Page : public CefClient, public CefRenderHandler, public CefLifeSpanHandler, public CefLoadHandler {
 public:
  Page(int id, std::string url, int w, int h, int fps, std::string js, bool gpu)
      : id_(id), url_(std::move(url)), js_(std::move(js)), w_(w), h_(h), fps_(fps), gpu_(gpu) {}

  CefRefPtr<CefRenderHandler> GetRenderHandler() override { return this; }
  CefRefPtr<CefLifeSpanHandler> GetLifeSpanHandler() override { return this; }
  CefRefPtr<CefLoadHandler> GetLoadHandler() override { return this; }

  void Open() {
    CEF_REQUIRE_UI_THREAD();
    CefWindowInfo window;
    window.SetAsWindowless(nullptr);
    window.shared_texture_enabled = gpu_ ? 1 : 0;
    CefBrowserSettings bs;
    bs.windowless_frame_rate = fps_;
    bs.background_color = 0;   // see-through where the page draws no background
    opened_at_ = Now();
    CefBrowserHost::CreateBrowser(window, this, url_, bs, nullptr, nullptr);
    Say("web " + std::to_string(id_) + " mode " + (gpu_ ? "gpu" : "cpu"));
  }

  void GetViewRect(CefRefPtr<CefBrowser>, CefRect& rect) override { rect = CefRect(0, 0, w_, h_); }

  // Chromium drew the page on the GPU: copy its texture (valid only during this call) into ours, GPU to GPU
  void OnAcceleratedPaint(CefRefPtr<CefBrowser>, PaintElementType type, const RectList&,
                          const CefAcceleratedPaintInfo& info) override {
    if (type != PET_VIEW || !info.shared_texture_handle)
      return;
    std::lock_guard<std::recursive_mutex> l(g_gpu);
    ComPtr<ID3D11Texture2D> shared;
    HRESULT hr = E_FAIL;
    if (g_dev1)
      hr = g_dev1->OpenSharedResource1(info.shared_texture_handle, IID_PPV_ARGS(&shared));
    if (FAILED(hr))
      hr = g_dev->OpenSharedResource(info.shared_texture_handle, IID_PPV_ARGS(&shared));
    if (FAILED(hr)) {
      if (++share_fails_ == 5)
        Say("web " + std::to_string(id_) + " note the page's GPU picture could not be opened (" +
            std::to_string(static_cast<unsigned>(hr)) + ")");
      return;
    }
    D3D11_TEXTURE2D_DESC d;
    shared->GetDesc(&d);
    if (!tex_.tex || tex_.w != static_cast<int>(d.Width) || tex_.h != static_cast<int>(d.Height)) {
      Tex t;
      if (!MakeTex(t, d.Width, d.Height, false))
        return;
      std::lock_guard<std::mutex> pl(lock_);
      tex_ = t;
    }
    g_ctx->CopyResource(tex_.tex.Get(), shared.Get());
    Drawn();
  }

  // no GPU path on this PC: Chromium gives the picture in memory, sent to the GPU once
  void OnPaint(CefRefPtr<CefBrowser>, PaintElementType type, const RectList&, const void* buffer, int width,
               int height) override {
    if (type != PET_VIEW)
      return;
    std::lock_guard<std::recursive_mutex> l(g_gpu);
    if (!tex_.tex || tex_.w != width || tex_.h != height) {
      Tex t;
      if (!MakeTex(t, width, height, false))
        return;
      std::lock_guard<std::mutex> pl(lock_);
      tex_ = t;
    }
    g_ctx->UpdateSubresource(tex_.tex.Get(), 0, nullptr, buffer, width * 4, 0);
    Drawn();
  }

  void OnAfterCreated(CefRefPtr<CefBrowser> browser) override {
    browser_ = browser;
    if (closing_)
      browser->GetHost()->CloseBrowser(true);
  }

  void OnBeforeClose(CefRefPtr<CefBrowser>) override { browser_ = nullptr; }

  void OnLoadEnd(CefRefPtr<CefBrowser>, CefRefPtr<CefFrame> frame, int) override {
    if (!frame->IsMain())
      return;
    if (!js_.empty())
      frame->ExecuteJavaScript(js_, frame->GetURL(), 0);
    Say("web " + std::to_string(id_) + " loaded");
  }

  void OnLoadError(CefRefPtr<CefBrowser>, CefRefPtr<CefFrame> frame, ErrorCode code, const CefString& text,
                   const CefString&) override {
    if (frame->IsMain() && code != ERR_ABORTED)
      Say("web " + std::to_string(id_) + " error Could not open the page: " + OneLine(text.ToString()));
  }

  void Reload() {
    if (browser_)
      browser_->ReloadIgnoreCache();
  }
  void Show(bool on) {
    if (on && hidden_)
      opened_at_ = Now();   // CheckPages: give the page time to draw again
    hidden_ = !on;
    // the page keeps running off air, as in cefcg: a hidden page (document.hidden, no animation frames) can
    // leave parts undrawn when it comes back, e.g. a score bar that slides in on an animation frame
    if (browser_ && on)
      browser_->GetHost()->Invalidate(PET_VIEW);
  }
  void Close() {
    closing_ = true;
    if (browser_)
      browser_->GetHost()->CloseBrowser(true);
  }

  // a copy of the texture reference for the mixer (the browser may replace it on resize)
  Tex Picture() {
    std::lock_guard<std::mutex> pl(lock_);
    return frames_ ? tex_ : Tex();
  }
  int id() const { return id_; }
  bool gpu() const { return gpu_; }
  int frames() const { return frames_; }
  double opened_at() const { return opened_at_; }
  double last() const { return last_; }
  bool hidden() const { return hidden_; }
  const std::string& url() const { return url_; }
  const std::string& js() const { return js_; }
  int w() const { return w_; }
  int h() const { return h_; }
  int fps() const { return fps_; }

 private:
  void Drawn() {
    last_ = Now();
    if (++frames_ == 1)
      Say("web " + std::to_string(id_) + " drawn");
  }
  int id_;
  std::string url_, js_;
  int w_, h_, fps_;
  bool gpu_;
  CefRefPtr<CefBrowser> browser_;
  bool closing_ = false, hidden_ = false;
  std::mutex lock_;
  Tex tex_;
  std::atomic<int> frames_{0};
  int share_fails_ = 0;
  double opened_at_ = 0, last_ = 0;
  IMPLEMENT_REFCOUNTING(Page);
};

void OpenPage(int id, std::string url, int w, int h, int fps, std::string js, bool gpu) {
  CefRefPtr<Page> old;
  CefRefPtr<Page> p = new Page(id, std::move(url), w, h, fps, std::move(js), gpu);
  {
    std::lock_guard<std::mutex> l(g_pages_lock);
    old = g_pages[id];
    g_pages[id] = p;
  }
  if (old)
    old->Close();
  p->Open();
}

// the page never drew on the GPU: open it again with the picture in memory (works on every PC)
void CheckPages() {
  CEF_REQUIRE_UI_THREAD();
  std::vector<CefRefPtr<Page>> retry;
  {
    std::lock_guard<std::mutex> l(g_pages_lock);
    for (auto& kv : g_pages)
      if (kv.second->gpu() && kv.second->frames() == 0 && !kv.second->hidden() && Now() - kv.second->opened_at() > 8)
        retry.push_back(kv.second);
  }
  for (auto& p : retry) {
    Say("web " + std::to_string(p->id()) + " note no GPU picture from the page: drawing it in memory instead");
    OpenPage(p->id(), p->url(), p->w(), p->h(), p->fps(), p->js(), false);
  }
  CefPostDelayedTask(TID_UI, base::BindOnce(&CheckPages), 2000);
}

// ---------------------------------------------------------------- encoders (stream / recording)
class Output {
 public:
  virtual ~Output() {}
  virtual bool Write(const uint8_t* data, int size) = 0;
};

// the stream's MPEG-TS goes to Playout through a named pipe (Playout sends it to every destination)
class PipeOutput : public Output {
 public:
  explicit PipeOutput(const std::wstring& name) {
    pipe_ = ::CreateNamedPipeW(name.c_str(), PIPE_ACCESS_OUTBOUND, PIPE_TYPE_BYTE | PIPE_WAIT, 1, 1 << 20, 0, 0,
                               nullptr);
    if (pipe_ != INVALID_HANDLE_VALUE)
      thread_ = std::thread(&PipeOutput::Run, this);
  }
  ~PipeOutput() override {
    {
      std::lock_guard<std::mutex> l(lock_);
      stop_ = true;
    }
    cv_.notify_all();
    if (pipe_ != INVALID_HANDLE_VALUE) {
      ::CancelIoEx(pipe_, nullptr);
      // unblock a ConnectNamedPipe that still waits for Playout
      HANDLE h = ::CreateFileW(name_.c_str(), GENERIC_READ, 0, nullptr, OPEN_EXISTING, 0, nullptr);
      if (thread_.joinable())
        thread_.join();
      if (h != INVALID_HANDLE_VALUE)
        ::CloseHandle(h);
      ::CloseHandle(pipe_);
    }
  }
  bool ok() const { return pipe_ != INVALID_HANDLE_VALUE; }
  void set_name(const std::wstring& n) { name_ = n; }
  bool Write(const uint8_t* data, int size) override {
    std::lock_guard<std::mutex> l(lock_);
    if (queued_ > (64 << 20))   // Playout is not reading: drop rather than eat memory
      return false;
    q_.emplace_back(data, data + size);
    queued_ += size;
    cv_.notify_one();
    return true;
  }

 private:
  void Run() {
    BOOL ok = ::ConnectNamedPipe(pipe_, nullptr) || ::GetLastError() == ERROR_PIPE_CONNECTED;
    while (true) {
      std::vector<uint8_t> chunk;
      {
        std::unique_lock<std::mutex> l(lock_);
        cv_.wait(l, [&] { return stop_ || !q_.empty(); });
        if (q_.empty())
          return;
        chunk.swap(q_.front());
        q_.pop_front();
        queued_ -= chunk.size();
      }
      if (!ok)
        continue;
      DWORD n = 0;
      if (!::WriteFile(pipe_, chunk.data(), static_cast<DWORD>(chunk.size()), &n, nullptr))
        ok = FALSE;
    }
  }
  HANDLE pipe_ = INVALID_HANDLE_VALUE;
  std::wstring name_;
  std::thread thread_;
  std::mutex lock_;
  std::condition_variable cv_;
  std::deque<std::vector<uint8_t>> q_;
  size_t queued_ = 0;
  bool stop_ = false;
};

struct EncConfig {
  std::string name;     // "stream", "rec", "v0".. (a vertical destination's own encode)
  std::string kind;     // "stream" (MPEG-TS to a pipe) or "rec" (MP4 file)
  std::string layout;   // "" as mixed, "crop" 9:16 crop, "blur" 9:16 whole picture on a blurred background
  float pos = 0.5f;     // crop: 0 left .. 1 right
  std::string encoder;  // auto, h264_nvenc, h264_qsv, h264_amf, libx264
  std::string path;     // rec: the file
  std::wstring pipe;    // stream: the pipe name
  int w = 1920, h = 1080, kbps = 6000, akbps = 160, gop = 50;
  std::string preset = "veryfast";
};

struct Job {
  AVFrame* video = nullptr;
  std::vector<float> audio;   // left samples then right samples
  int n = 0;
};

class Encoder {
 public:
  explicit Encoder(EncConfig c) : c_(std::move(c)) {}
  ~Encoder() { Close(); }

  bool Open(std::string& err) {
    const char* order_auto[] = {"h264_nvenc", "h264_qsv", "h264_amf", "libx264"};
    std::vector<std::string> order;
    if (c_.encoder.empty() || c_.encoder == "auto")
      order.assign(order_auto, order_auto + 4);
    else {
      order.push_back(c_.encoder);
      if (c_.encoder != "libx264")
        order.push_back("libx264");
    }
    for (const auto& name : order) {
      // NVIDIA: frames straight from the mixer's texture; then the same encoder fed from a picture copy
      if (name == "h264_nvenc" && g_nvidia && OpenVideo(name, true, err))
        break;
      if (OpenVideo(name, false, err))
        break;
    }
    if (!venc_)
      return false;
    if (!OpenAudio(err) || !OpenMux(err)) {
      Close();
      return false;
    }
    stop_ = false;
    thread_ = std::thread(&Encoder::Run, this);
    return true;
  }

  std::string name() const { return name_ + (hw_ ? " (GPU frames)" : " (picture copy)"); }
  bool hw() const { return hw_; }
  int dropped() const { return dropped_; }
  int64_t frames() const { return vpts_; }
  int w() const { return c_.w; }
  int h() const { return c_.h; }
  const EncConfig& config() const { return c_; }

  // render thread, GPU lock held: hand the mixed picture (and the frame's sound) to the encoder
  void Submit(Tex* src, const float* audio, int n) {
    if (!src)
      return;
    {
      std::lock_guard<std::mutex> l(lock_);
      if (q_.size() >= 8) {
        ++dropped_;
        return;
      }
    }
    Job j;
    j.n = n;
    j.audio.assign(audio, audio + 2 * static_cast<size_t>(n));
    AVFrame* f = av_frame_alloc();
    if (hw_) {
      if (av_hwframe_get_buffer(frames_ctx_, f, 0) < 0) {
        av_frame_free(&f);
        ++dropped_;
        return;
      }
      auto* dst = reinterpret_cast<ID3D11Texture2D*>(f->data[0]);
      UINT index = static_cast<UINT>(reinterpret_cast<intptr_t>(f->data[1]));
      g_ctx->CopySubresourceRegion(dst, index, 0, 0, 0, src->tex.Get(), 0, nullptr);
    } else {
      if (!staging_) {
        D3D11_TEXTURE2D_DESC sd = {};
        sd.Width = c_.w;
        sd.Height = c_.h;
        sd.MipLevels = sd.ArraySize = 1;
        sd.Format = DXGI_FORMAT_B8G8R8A8_UNORM;
        sd.SampleDesc.Count = 1;
        sd.Usage = D3D11_USAGE_STAGING;
        sd.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
        if (FAILED(g_dev->CreateTexture2D(&sd, nullptr, &staging_))) {
          av_frame_free(&f);
          return;
        }
      }
      g_ctx->CopyResource(staging_.Get(), src->tex.Get());
      D3D11_MAPPED_SUBRESOURCE m;
      f->format = AV_PIX_FMT_BGRA;
      f->width = c_.w;
      f->height = c_.h;
      if (av_frame_get_buffer(f, 32) < 0 || FAILED(g_ctx->Map(staging_.Get(), 0, D3D11_MAP_READ, 0, &m))) {
        av_frame_free(&f);
        return;
      }
      av_image_copy_plane(f->data[0], f->linesize[0], static_cast<const uint8_t*>(m.pData), m.RowPitch, c_.w * 4,
                          c_.h);
      g_ctx->Unmap(staging_.Get(), 0);
    }
    j.video = f;
    {
      std::lock_guard<std::mutex> l(lock_);
      q_.push_back(std::move(j));
    }
    cv_.notify_one();
  }

  void Close() {
    if (thread_.joinable()) {
      {
        std::lock_guard<std::mutex> l(lock_);
        stop_ = true;
      }
      cv_.notify_all();
      thread_.join();
    }
    for (auto& j : q_)
      av_frame_free(&j.video);
    q_.clear();
    if (mux_) {
      if (header_)
        av_write_trailer(mux_);
      if (mux_->pb) {
        if (custom_io_) {
          av_freep(&mux_->pb->buffer);
          avio_context_free(&mux_->pb);
        } else {
          avio_closep(&mux_->pb);
        }
      }
      avformat_free_context(mux_);
      mux_ = nullptr;
    }
    avcodec_free_context(&venc_);
    avcodec_free_context(&aenc_);
    if (fifo_) {
      av_audio_fifo_free(fifo_);
      fifo_ = nullptr;
    }
    av_frame_free(&conv_);
    sws_freeContext(sws_);
    sws_ = nullptr;
    av_buffer_unref(&frames_ctx_);
    av_buffer_unref(&device_ctx_);
    pipe_.reset();
  }

  void Key() { key_ = true; }

 private:
  std::atomic<bool> key_{false};
  bool OpenVideo(const std::string& name, bool hw, std::string& err) {
    const AVCodec* codec = avcodec_find_encoder_by_name(name.c_str());
    if (!codec) {
      err = name + " not in this FFmpeg";
      return false;
    }
    AVCodecContext* ctx = avcodec_alloc_context3(codec);
    ctx->width = c_.w;
    ctx->height = c_.h;
    ctx->time_base = AVRational{g_opt.fps_den, g_opt.fps_num};
    ctx->framerate = AVRational{g_opt.fps_num, g_opt.fps_den};
    ctx->gop_size = c_.gop;
    ctx->max_b_frames = 0;
    ctx->bit_rate = static_cast<int64_t>(c_.kbps) * 1000;
    ctx->rc_max_rate = ctx->bit_rate;
    ctx->rc_buffer_size = static_cast<int>(ctx->bit_rate * 2);
    if (c_.kind == "rec")
      ctx->flags |= AV_CODEC_FLAG_GLOBAL_HEADER;
    AVBufferRef* dev = nullptr;
    AVBufferRef* frames = nullptr;
    if (hw) {
      dev = av_hwdevice_ctx_alloc(AV_HWDEVICE_TYPE_D3D11VA);
      auto* dc = reinterpret_cast<AVHWDeviceContext*>(dev->data);
      auto* d3d = reinterpret_cast<AVD3D11VADeviceContext*>(dc->hwctx);
      d3d->device = g_dev.Get();
      d3d->device->AddRef();
      d3d->lock = GpuLock;
      d3d->unlock = GpuUnlock;
      d3d->lock_ctx = nullptr;
      int r = av_hwdevice_ctx_init(dev);
      if (r >= 0) {
        frames = av_hwframe_ctx_alloc(dev);
        auto* fc = reinterpret_cast<AVHWFramesContext*>(frames->data);
        fc->format = AV_PIX_FMT_D3D11;
        fc->sw_format = AV_PIX_FMT_BGRA;
        fc->width = c_.w;
        fc->height = c_.h;
        fc->initial_pool_size = 20;
        reinterpret_cast<AVD3D11VAFramesContext*>(fc->hwctx)->BindFlags = D3D11_BIND_RENDER_TARGET;
        r = av_hwframe_ctx_init(frames);
      }
      if (r < 0) {
        err = name + " GPU frames: " + AvErr(r);
        av_buffer_unref(&frames);
        av_buffer_unref(&dev);
        avcodec_free_context(&ctx);
        return false;
      }
      ctx->pix_fmt = AV_PIX_FMT_D3D11;
      ctx->sw_pix_fmt = AV_PIX_FMT_BGRA;
      ctx->hw_frames_ctx = av_buffer_ref(frames);
    } else {
      ctx->pix_fmt = (name == "h264_nvenc" || name == "h264_amf") ? AV_PIX_FMT_BGRA
                     : name == "h264_qsv"                         ? AV_PIX_FMT_NV12
                                                                  : AV_PIX_FMT_YUV420P;
    }
    AVDictionary* o = nullptr;
    std::string k = std::to_string(c_.kbps) + "k", k2 = std::to_string(c_.kbps * 2) + "k";
    if (name == "h264_nvenc") {
      av_dict_set(&o, "preset", "p4", 0);
      av_dict_set(&o, "tune", "ll", 0);
      av_dict_set(&o, "rc", "cbr", 0);
      av_dict_set(&o, "forced-idr", "1", 0);
    } else if (name == "h264_qsv") {
      av_dict_set(&o, "preset", "veryfast", 0);
      av_dict_set(&o, "look_ahead", "0", 0);
    } else if (name == "h264_amf") {
      av_dict_set(&o, "usage", "lowlatency", 0);
      av_dict_set(&o, "rc", "cbr", 0);
      av_dict_set(&o, "quality", "speed", 0);
    } else {
      av_dict_set(&o, "preset", c_.preset.c_str(), 0);
      av_dict_set(&o, "tune", "zerolatency", 0);
      std::string g = std::to_string(c_.gop);
      av_dict_set(&o, "x264-params", ("keyint=" + g + ":min-keyint=" + g + ":scenecut=0:nal-hrd=cbr").c_str(), 0);
    }
    int r = avcodec_open2(ctx, codec, &o);
    av_dict_free(&o);
    if (r < 0) {
      err = name + (hw ? " (GPU frames)" : "") + ": " + AvErr(r);
      av_buffer_unref(&frames);
      av_buffer_unref(&dev);
      avcodec_free_context(&ctx);
      return false;
    }
    venc_ = ctx;
    device_ctx_ = dev;
    frames_ctx_ = frames;
    hw_ = hw;
    name_ = name;
    if (!hw && ctx->pix_fmt != AV_PIX_FMT_BGRA) {
      sws_ = sws_getContext(c_.w, c_.h, AV_PIX_FMT_BGRA, c_.w, c_.h, ctx->pix_fmt, SWS_FAST_BILINEAR, nullptr,
                            nullptr, nullptr);
      conv_ = av_frame_alloc();
      conv_->format = ctx->pix_fmt;
      conv_->width = c_.w;
      conv_->height = c_.h;
      av_frame_get_buffer(conv_, 32);
    }
    return true;
  }

  bool OpenAudio(std::string& err) {
    const AVCodec* codec = avcodec_find_encoder(AV_CODEC_ID_AAC);
    if (!codec) {
      err = "no AAC encoder";
      return false;
    }
    aenc_ = avcodec_alloc_context3(codec);
    aenc_->sample_fmt = AV_SAMPLE_FMT_FLTP;
    aenc_->sample_rate = 48000;
    av_channel_layout_default(&aenc_->ch_layout, 2);
    aenc_->bit_rate = static_cast<int64_t>(c_.akbps) * 1000;
    aenc_->time_base = AVRational{1, 48000};
    if (c_.kind == "rec")
      aenc_->flags |= AV_CODEC_FLAG_GLOBAL_HEADER;
    int r = avcodec_open2(aenc_, codec, nullptr);
    if (r < 0) {
      err = "AAC: " + AvErr(r);
      return false;
    }
    fifo_ = av_audio_fifo_alloc(AV_SAMPLE_FMT_FLTP, 2, 48000);
    return fifo_ != nullptr;
  }

  static int WritePacket(void* opaque, const uint8_t* buf, int size) {
    auto* self = static_cast<Encoder*>(opaque);
    if (self->pipe_)
      self->pipe_->Write(buf, size);
    return size;
  }
#if LIBAVFORMAT_VERSION_MAJOR < 61
  static int WritePacketOld(void* opaque, uint8_t* buf, int size) { return WritePacket(opaque, buf, size); }
#endif

  bool OpenMux(std::string& err) {
    int r;
    if (c_.kind == "stream") {
      r = avformat_alloc_output_context2(&mux_, nullptr, "mpegts", nullptr);
      if (r < 0) {
        err = "mpegts: " + AvErr(r);
        return false;
      }
      pipe_.reset(new PipeOutput(c_.pipe));
      pipe_->set_name(c_.pipe);
      if (!pipe_->ok()) {
        err = "could not make the stream pipe";
        return false;
      }
      auto* buf = static_cast<unsigned char*>(av_malloc(65536));
#if LIBAVFORMAT_VERSION_MAJOR < 61
      mux_->pb = avio_alloc_context(buf, 65536, 1, this, nullptr, &Encoder::WritePacketOld, nullptr);
#else
      mux_->pb = avio_alloc_context(buf, 65536, 1, this, nullptr, &Encoder::WritePacket, nullptr);
#endif
      mux_->flags |= AVFMT_FLAG_CUSTOM_IO;
      custom_io_ = true;
    } else {
      r = avformat_alloc_output_context2(&mux_, nullptr, "mp4", c_.path.c_str());
      if (r < 0) {
        err = "mp4: " + AvErr(r);
        return false;
      }
      r = avio_open(&mux_->pb, c_.path.c_str(), AVIO_FLAG_WRITE);
      if (r < 0) {
        err = "could not open the file: " + AvErr(r);
        return false;
      }
    }
    vs_ = avformat_new_stream(mux_, nullptr);
    avcodec_parameters_from_context(vs_->codecpar, venc_);
    vs_->time_base = venc_->time_base;
    vs_->avg_frame_rate = venc_->framerate;
    as_ = avformat_new_stream(mux_, nullptr);
    avcodec_parameters_from_context(as_->codecpar, aenc_);
    as_->time_base = aenc_->time_base;
    AVDictionary* o = nullptr;
    if (c_.kind == "rec")
      av_dict_set(&o, "movflags", "frag_keyframe+empty_moov+default_base_moof", 0);
    r = avformat_write_header(mux_, &o);
    av_dict_free(&o);
    if (r < 0) {
      err = "header: " + AvErr(r);
      return false;
    }
    header_ = true;
    return true;
  }

  void Drain(AVCodecContext* ctx, AVStream* st) {
    AVPacket* pkt = av_packet_alloc();
    while (avcodec_receive_packet(ctx, pkt) >= 0) {
      av_packet_rescale_ts(pkt, ctx->time_base, st->time_base);
      pkt->stream_index = st->index;
      av_interleaved_write_frame(mux_, pkt);
    }
    av_packet_free(&pkt);
  }

  void EncodeAudio(bool flush) {
    while (av_audio_fifo_size(fifo_) >= aenc_->frame_size || (flush && av_audio_fifo_size(fifo_) > 0)) {
      int n = av_audio_fifo_size(fifo_);
      if (n > aenc_->frame_size)
        n = aenc_->frame_size;
      AVFrame* f = av_frame_alloc();
      f->nb_samples = n;
      f->format = AV_SAMPLE_FMT_FLTP;
      f->sample_rate = 48000;
      av_channel_layout_copy(&f->ch_layout, &aenc_->ch_layout);
      av_frame_get_buffer(f, 0);
      av_audio_fifo_read(fifo_, reinterpret_cast<void**>(f->data), n);
      f->pts = apts_;
      apts_ += n;
      if (avcodec_send_frame(aenc_, f) >= 0)
        Drain(aenc_, as_);
      av_frame_free(&f);
    }
  }

  void Run() {
    while (true) {
      Job j;
      {
        std::unique_lock<std::mutex> l(lock_);
        cv_.wait(l, [&] { return stop_ || !q_.empty(); });
        if (q_.empty())
          break;
        j = std::move(q_.front());
        q_.pop_front();
      }
      AVFrame* f = j.video;
      if (sws_ && conv_) {
        av_frame_make_writable(conv_);
        sws_scale(sws_, f->data, f->linesize, 0, c_.h, conv_->data, conv_->linesize);
        f = conv_;
      }
      f->pts = vpts_++;
      // a destination joined mid-stream: a keyframe now (with its SPS/PPS) so it can start at once
      f->pict_type = key_.exchange(false) ? AV_PICTURE_TYPE_I : AV_PICTURE_TYPE_NONE;
      if (avcodec_send_frame(venc_, f) >= 0)
        Drain(venc_, vs_);
      av_frame_free(&j.video);
      if (j.n > 0) {
        void* planes[2] = {j.audio.data(), j.audio.data() + j.n};
        av_audio_fifo_write(fifo_, planes, j.n);
        EncodeAudio(false);
      }
    }
    avcodec_send_frame(venc_, nullptr);
    Drain(venc_, vs_);
    EncodeAudio(true);
    avcodec_send_frame(aenc_, nullptr);
    Drain(aenc_, as_);
  }

  EncConfig c_;
  std::string name_;
  bool hw_ = false;
  AVCodecContext* venc_ = nullptr;
  AVCodecContext* aenc_ = nullptr;
  AVFormatContext* mux_ = nullptr;
  AVStream* vs_ = nullptr;
  AVStream* as_ = nullptr;
  bool header_ = false, custom_io_ = false;
  AVBufferRef* device_ctx_ = nullptr;
  AVBufferRef* frames_ctx_ = nullptr;
  AVAudioFifo* fifo_ = nullptr;
  SwsContext* sws_ = nullptr;
  AVFrame* conv_ = nullptr;
  ComPtr<ID3D11Texture2D> staging_;
  std::unique_ptr<PipeOutput> pipe_;
  std::thread thread_;
  std::mutex lock_;
  std::condition_variable cv_;
  std::deque<Job> q_;
  bool stop_ = false;
  std::atomic<int> dropped_{0};
  int64_t vpts_ = 0, apts_ = 0;
};

// an encoder that stopped finishes its file in the background, so the output never waits for it
void Retire(std::unique_ptr<Encoder> e) {
  if (e)
    std::thread([](Encoder* p) { delete p; }, e.release()).detach();
}

// ---------------------------------------------------------------- clips decoded on the GPU (d3d11va)
// A program clip is decoded by the graphics card's video decoder, its NV12 / P010 picture turned into BGRA and
// sized to the output by a shader, and the result handed to Playout in shared memory (Playout keeps the
// timing, the sound and every other part of playing it). Without a GPU decoder the CPU decodes and converts.
//
// Clip shared memory (made by Playout): 64-byte header, then kClipSlots slot headers of 16 bytes
// (int32 state: 0 free / 1 picture ready, int32 unused, double time in seconds from the in point), then
// at kClipPixels the slots' pictures (w * h * 4 BGRA each).
const int kClipSlots = 4;
const size_t kClipPixels = 4096;

const char* kYuvShader = R"(
cbuffer P : register(b0) { float4 m0; float4 m1; float4 m2; float4 off; };
Texture2D<float> lum : register(t0);
Texture2D<float2> chroma : register(t1);
SamplerState smp : register(s0);
struct V { float4 pos : SV_Position; float2 uv : TEXCOORD0; };
V vs(uint id : SV_VertexID) {
  float2 t = float2(id & 1, id >> 1);
  V o;
  o.pos = float4(t.x * 2 - 1, 1 - t.y * 2, 0, 1);
  o.uv = t * off.zw;
  return o;
}
float4 ps(V i) : SV_Target {
  float3 yuv = float3(lum.Sample(smp, i.uv), chroma.Sample(smp, i.uv)) - float3(off.x, 0.5, 0.5);
  float3 rgb = float3(dot(m0.xyz, yuv), dot(m1.xyz, yuv), dot(m2.xyz, yuv));
  return float4(saturate(rgb), 1);
}
)";

struct YuvParams {
  float m0[4], m1[4], m2[4], off[4];
};

class YuvConverter {
 public:
  bool Init() {
    ComPtr<ID3DBlob> vsb, psb, err;
    if (FAILED(D3DCompile(kYuvShader, std::strlen(kYuvShader), "yuv", nullptr, nullptr, "vs", "vs_4_0", 0, 0, &vsb, &err)) ||
        FAILED(D3DCompile(kYuvShader, std::strlen(kYuvShader), "yuv", nullptr, nullptr, "ps", "ps_4_0", 0, 0, &psb, &err)))
      return false;
    if (FAILED(g_dev->CreateVertexShader(vsb->GetBufferPointer(), vsb->GetBufferSize(), nullptr, &vs_)) ||
        FAILED(g_dev->CreatePixelShader(psb->GetBufferPointer(), psb->GetBufferSize(), nullptr, &ps_)))
      return false;
    D3D11_BUFFER_DESC bd = {};
    bd.ByteWidth = sizeof(YuvParams);
    bd.Usage = D3D11_USAGE_DEFAULT;
    bd.BindFlags = D3D11_BIND_CONSTANT_BUFFER;
    D3D11_SAMPLER_DESC s = {};
    s.Filter = D3D11_FILTER_MIN_MAG_MIP_LINEAR;
    s.AddressU = s.AddressV = s.AddressW = D3D11_TEXTURE_ADDRESS_CLAMP;
    s.MaxLOD = D3D11_FLOAT32_MAX;
    D3D11_RASTERIZER_DESC r = {};
    r.FillMode = D3D11_FILL_SOLID;
    r.CullMode = D3D11_CULL_NONE;
    r.DepthClipEnable = TRUE;
    D3D11_BLEND_DESC b = {};
    b.RenderTarget[0].RenderTargetWriteMask = D3D11_COLOR_WRITE_ENABLE_ALL;
    return SUCCEEDED(g_dev->CreateBuffer(&bd, nullptr, &cb_)) && SUCCEEDED(g_dev->CreateSamplerState(&s, &smp_)) &&
           SUCCEEDED(g_dev->CreateRasterizerState(&r, &rs_)) && SUCCEEDED(g_dev->CreateBlendState(&b, &blend_));
  }

  // GPU lock held. frame: a d3d11va picture. Writes w x h BGRA to dst.
  bool Convert(const AVFrame* frame, int w, int h, uint8_t* dst) {
    auto* tex = reinterpret_cast<ID3D11Texture2D*>(frame->data[0]);
    const UINT index = static_cast<UINT>(reinterpret_cast<intptr_t>(frame->data[1]));
    D3D11_TEXTURE2D_DESC d;
    tex->GetDesc(&d);
    const bool p010 = d.Format == DXGI_FORMAT_P010 || d.Format == DXGI_FORMAT_P016;
    if (!src_ || src_w_ != static_cast<int>(d.Width) || src_h_ != static_cast<int>(d.Height) || src_fmt_ != d.Format) {
      D3D11_TEXTURE2D_DESC sd = {};
      sd.Width = d.Width;
      sd.Height = d.Height;
      sd.MipLevels = sd.ArraySize = 1;
      sd.Format = d.Format;
      sd.SampleDesc.Count = 1;
      sd.Usage = D3D11_USAGE_DEFAULT;
      sd.BindFlags = D3D11_BIND_SHADER_RESOURCE;
      src_.Reset();
      lum_.Reset();
      chroma_.Reset();
      if (FAILED(g_dev->CreateTexture2D(&sd, nullptr, &src_)))
        return false;
      D3D11_SHADER_RESOURCE_VIEW_DESC v = {};
      v.ViewDimension = D3D11_SRV_DIMENSION_TEXTURE2D;
      v.Texture2D.MipLevels = 1;
      v.Format = p010 ? DXGI_FORMAT_R16_UNORM : DXGI_FORMAT_R8_UNORM;
      if (FAILED(g_dev->CreateShaderResourceView(src_.Get(), &v, &lum_)))
        return false;
      v.Format = p010 ? DXGI_FORMAT_R16G16_UNORM : DXGI_FORMAT_R8G8_UNORM;
      if (FAILED(g_dev->CreateShaderResourceView(src_.Get(), &v, &chroma_)))
        return false;
      src_w_ = d.Width;
      src_h_ = d.Height;
      src_fmt_ = d.Format;
    }
    if (out_.w != w || out_.h != h) {
      if (!MakeTex(out_, w, h, true))
        return false;
      D3D11_TEXTURE2D_DESC sd = {};
      sd.Width = w;
      sd.Height = h;
      sd.MipLevels = sd.ArraySize = 1;
      sd.Format = DXGI_FORMAT_B8G8R8A8_UNORM;
      sd.SampleDesc.Count = 1;
      sd.Usage = D3D11_USAGE_STAGING;
      sd.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
      staging_.Reset();
      if (FAILED(g_dev->CreateTexture2D(&sd, nullptr, &staging_)))
        return false;
    }
    g_ctx->CopySubresourceRegion(src_.Get(), 0, 0, 0, 0, tex, index, nullptr);
    // colour matrix: BT.709 for HD, BT.601 for SD; limited (TV) range unless the file says full range
    const bool full = frame->color_range == AVCOL_RANGE_JPEG;
    const bool sd = frame->colorspace == AVCOL_SPC_BT470BG || frame->colorspace == AVCOL_SPC_SMPTE170M ||
                    (frame->colorspace == AVCOL_SPC_UNSPECIFIED && frame->height < 720);
    const float ys = full ? 1.f : 255.f / 219.f, cs = full ? 1.f : 255.f / 224.f;
    const float kr = sd ? 0.299f : 0.2126f, kb = sd ? 0.114f : 0.0722f, kg = 1 - kr - kb;
    YuvParams p = {};
    p.m0[0] = ys; p.m0[1] = 0; p.m0[2] = cs * 2 * (1 - kr);
    p.m1[0] = ys; p.m1[1] = -cs * 2 * (1 - kb) * kb / kg; p.m1[2] = -cs * 2 * (1 - kr) * kr / kg;
    p.m2[0] = ys; p.m2[1] = cs * 2 * (1 - kb); p.m2[2] = 0;
    p.off[0] = full ? 0.f : 16.f / 255.f;
    // the picture may be padded (coded size): sample only the shown part
    p.off[2] = static_cast<float>(frame->width) / d.Width;
    p.off[3] = static_cast<float>(frame->height) / d.Height;
    g_ctx->UpdateSubresource(cb_.Get(), 0, nullptr, &p, 0, 0);
    D3D11_VIEWPORT vp = {0, 0, static_cast<float>(w), static_cast<float>(h), 0, 1};
    ID3D11RenderTargetView* rtv = out_.rtv.Get();
    g_ctx->OMSetRenderTargets(1, &rtv, nullptr);
    g_ctx->RSSetViewports(1, &vp);
    g_ctx->RSSetState(rs_.Get());
    float f[4] = {0, 0, 0, 0};
    g_ctx->OMSetBlendState(blend_.Get(), f, 0xffffffff);
    g_ctx->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLESTRIP);
    g_ctx->IASetInputLayout(nullptr);
    g_ctx->VSSetShader(vs_.Get(), nullptr, 0);
    g_ctx->PSSetShader(ps_.Get(), nullptr, 0);
    ID3D11Buffer* cb = cb_.Get();
    g_ctx->VSSetConstantBuffers(0, 1, &cb);
    g_ctx->PSSetConstantBuffers(0, 1, &cb);
    ID3D11ShaderResourceView* srv[2] = {lum_.Get(), chroma_.Get()};
    g_ctx->PSSetShaderResources(0, 2, srv);
    ID3D11SamplerState* smp = smp_.Get();
    g_ctx->PSSetSamplers(0, 1, &smp);
    g_ctx->Draw(4, 0);
    ID3D11ShaderResourceView* none[2] = {nullptr, nullptr};
    g_ctx->PSSetShaderResources(0, 2, none);
    ID3D11RenderTargetView* no_rtv = nullptr;
    g_ctx->OMSetRenderTargets(1, &no_rtv, nullptr);
    g_ctx->CopyResource(staging_.Get(), out_.tex.Get());
    D3D11_MAPPED_SUBRESOURCE m;
    if (FAILED(g_ctx->Map(staging_.Get(), 0, D3D11_MAP_READ, 0, &m)))
      return false;
    const size_t row = static_cast<size_t>(w) * 4;
    for (int y = 0; y < h; ++y)
      std::memcpy(dst + y * row, static_cast<const uint8_t*>(m.pData) + static_cast<size_t>(y) * m.RowPitch, row);
    g_ctx->Unmap(staging_.Get(), 0);
    return true;
  }

 private:
  ComPtr<ID3D11VertexShader> vs_;
  ComPtr<ID3D11PixelShader> ps_;
  ComPtr<ID3D11Buffer> cb_;
  ComPtr<ID3D11SamplerState> smp_;
  ComPtr<ID3D11RasterizerState> rs_;
  ComPtr<ID3D11BlendState> blend_;
  ComPtr<ID3D11Texture2D> src_, staging_;
  ComPtr<ID3D11ShaderResourceView> lum_, chroma_;
  int src_w_ = 0, src_h_ = 0;
  DXGI_FORMAT src_fmt_ = DXGI_FORMAT_UNKNOWN;
  Tex out_;
};

// one D3D11 device context for every GPU decoder (FFmpeg uses our GPU lock with it)
AVBufferRef* g_decode_dev = nullptr;
std::mutex g_decode_dev_lock;

AVBufferRef* DecodeDevice() {
  std::lock_guard<std::mutex> l(g_decode_dev_lock);
  if (!g_decode_dev && !g_warp) {
    AVBufferRef* dev = av_hwdevice_ctx_alloc(AV_HWDEVICE_TYPE_D3D11VA);
    auto* d3d = reinterpret_cast<AVD3D11VADeviceContext*>(reinterpret_cast<AVHWDeviceContext*>(dev->data)->hwctx);
    d3d->device = g_dev.Get();
    d3d->device->AddRef();
    d3d->lock = GpuLock;
    d3d->unlock = GpuUnlock;
    if (av_hwdevice_ctx_init(dev) < 0)
      av_buffer_unref(&dev);
    g_decode_dev = dev;
  }
  return g_decode_dev ? av_buffer_ref(g_decode_dev) : nullptr;
}

enum AVPixelFormat PickD3D11(AVCodecContext*, const enum AVPixelFormat* fmts) {
  for (const enum AVPixelFormat* p = fmts; *p != AV_PIX_FMT_NONE; ++p)
    if (*p == AV_PIX_FMT_D3D11)
      return *p;
  return fmts[0];   // this codec / size has no GPU decoder: the first software format
}

class Clip {
 public:
  Clip(int id, std::string path, std::wstring shm, int w, int h, double start, double end, double frame_dur, bool gpu)
      : id_(id), path_(std::move(path)), shm_(std::move(shm)), w_(w), h_(h), start_(start), end_(end),
        dur_(frame_dur), gpu_(gpu) {}
  ~Clip() { Stop(); }
  void Start() { thread_ = std::thread(&Clip::Run, this); }
  void Stop() {
    stop_ = true;
    if (thread_.joinable())
      thread_.join();
  }

 private:
  void Tell(const std::string& s) { Say("clip " + std::to_string(id_) + " " + s); }

  // waits for a free slot; nullptr when the clip is closed
  uint8_t* FreeSlot(int& slot) {
    while (!stop_) {
      auto* st = reinterpret_cast<volatile LONG*>(mem_ + 64 + next_ * 16);
      if (*st == 0) {
        slot = next_;
        return mem_ + kClipPixels + static_cast<size_t>(next_) * w_ * h_ * 4;
      }
      ::Sleep(2);
    }
    return nullptr;
  }
  void Publish(int slot, double t) {
    *reinterpret_cast<volatile double*>(mem_ + 64 + slot * 16 + 8) = t;
    ::MemoryBarrier();
    ::InterlockedExchange(reinterpret_cast<volatile LONG*>(mem_ + 64 + slot * 16), 1);
    next_ = (next_ + 1) % kClipSlots;
  }

  void Run() {
    const size_t size = kClipPixels + static_cast<size_t>(kClipSlots) * w_ * h_ * 4;
    HANDLE map = ::OpenFileMappingW(FILE_MAP_ALL_ACCESS, FALSE, shm_.c_str());
    if (map)
      mem_ = static_cast<uint8_t*>(::MapViewOfFile(map, FILE_MAP_ALL_ACCESS, 0, 0, size));
    if (!mem_) {
      Tell("error could not open the clip's shared memory");
      if (map)
        ::CloseHandle(map);
      return;
    }
    std::string err = Decode();
    if (!err.empty())
      Tell("error " + OneLine(err));
    else
      Tell("end");
    ::UnmapViewOfFile(mem_);
    ::CloseHandle(map);
  }

  std::string Decode() {
    AVFormatContext* fmt = nullptr;
    int r = avformat_open_input(&fmt, path_.c_str(), nullptr, nullptr);
    if (r < 0)
      return "open: " + AvErr(r);
    std::unique_ptr<AVFormatContext, void (*)(AVFormatContext*)> fmt_guard(fmt, [](AVFormatContext* f) {
      avformat_close_input(&f);
    });
    avformat_find_stream_info(fmt, nullptr);
    const AVCodec* codec = nullptr;
    const int vi = av_find_best_stream(fmt, AVMEDIA_TYPE_VIDEO, -1, -1, &codec, 0);
    if (vi < 0 || !codec)
      return "no video stream";
    AVStream* vs = fmt->streams[vi];
    for (unsigned i = 0; i < fmt->nb_streams; ++i)
      if (static_cast<int>(i) != vi)
        fmt->streams[i]->discard = AVDISCARD_ALL;
    AVCodecContext* dec = avcodec_alloc_context3(codec);
    std::unique_ptr<AVCodecContext, void (*)(AVCodecContext*)> dec_guard(dec, [](AVCodecContext* c) {
      avcodec_free_context(&c);
    });
    avcodec_parameters_to_context(dec, vs->codecpar);
    dec->pkt_timebase = vs->time_base;
    AVBufferRef* hw = gpu_ ? DecodeDevice() : nullptr;
    if (hw) {
      dec->hw_device_ctx = hw;
      dec->get_format = PickD3D11;
      dec->extra_hw_frames = kClipSlots + 2;
    } else {
      dec->thread_count = 0;   // all CPU cores
    }
    r = avcodec_open2(dec, codec, nullptr);
    if (r < 0)
      return std::string(codec->name) + ": " + AvErr(r);
    const double tb = av_q2d(vs->time_base);
    const double base = vs->start_time != AV_NOPTS_VALUE ? vs->start_time * tb : 0.0;
    double t0 = -1;
    if (start_ > 0) {
      t0 = base + start_;
      av_seek_frame(fmt, vi, static_cast<int64_t>(t0 / tb), AVSEEK_FLAG_BACKWARD);
    }
    const double end_t = end_ > 0 ? base + end_ : -1;
    int64_t last_slot = INT64_MIN;
    bool told = false;
    SwsContext* sws = nullptr;
    AVPacket* pkt = av_packet_alloc();
    AVFrame* frame = av_frame_alloc();
    std::string result;
    bool eof = false;
    while (!stop_ && result.empty()) {
      if (!eof) {
        r = av_read_frame(fmt, pkt);
        if (r < 0) {
          eof = true;
          avcodec_send_packet(dec, nullptr);
        } else {
          if (pkt->stream_index == vi)
            avcodec_send_packet(dec, pkt);
          av_packet_unref(pkt);
        }
      }
      bool out_point = false;
      while (!stop_ && (r = avcodec_receive_frame(dec, frame)) >= 0) {
        const int64_t ts = frame->best_effort_timestamp != AV_NOPTS_VALUE ? frame->best_effort_timestamp : frame->pts;
        const double t = ts != AV_NOPTS_VALUE ? ts * tb : 0.0;
        if (t0 < 0)
          t0 = t;
        if (t < t0 - dur_ * 0.5) {          // before the in point (after a seek)
          av_frame_unref(frame);
          continue;
        }
        if (end_t >= 0 && t >= end_t - dur_ * 0.5) {
          out_point = true;
          av_frame_unref(frame);
          break;
        }
        // one picture per output frame (a 50p clip at 25p converts only the pictures shown)
        const int64_t slot_no = static_cast<int64_t>(std::ceil((t - t0) / dur_ - 0.5 - 1e-6));
        if (slot_no == last_slot) {
          av_frame_unref(frame);
          continue;
        }
        last_slot = slot_no;
        int slot;
        uint8_t* dst = FreeSlot(slot);
        if (!dst) {
          av_frame_unref(frame);
          break;
        }
        bool ok = false;
        if (frame->format == AV_PIX_FMT_D3D11) {
          if (!told) {
            Tell("gpu " + std::string(codec->name));
            told = true;
          }
          std::lock_guard<std::recursive_mutex> l(g_gpu);
          if (!conv_ready_) {
            conv_ready_ = true;
            conv_ok_ = conv_.Init();
          }
          ok = conv_ok_ && conv_.Convert(frame, w_, h_, dst);
        } else {
          if (!told) {
            Tell("cpu " + std::string(codec->name));
            told = true;
          }
          sws = sws_getCachedContext(sws, frame->width, frame->height, static_cast<AVPixelFormat>(frame->format), w_, h_,
                                     AV_PIX_FMT_BGRA, SWS_FAST_BILINEAR, nullptr, nullptr, nullptr);
          if (sws) {
            uint8_t* planes[4] = {dst, nullptr, nullptr, nullptr};
            int lines[4] = {w_ * 4, 0, 0, 0};
            sws_scale(sws, frame->data, frame->linesize, 0, frame->height, planes, lines);
            ok = true;
          }
        }
        av_frame_unref(frame);
        if (!ok) {
          result = "the picture could not be converted";
          break;
        }
        Publish(slot, t - t0);
      }
      if (out_point || (eof && r == AVERROR_EOF))
        break;
    }
    sws_freeContext(sws);
    av_frame_free(&frame);
    av_packet_free(&pkt);
    return result;
  }

  int id_;
  std::string path_;
  std::wstring shm_;
  int w_, h_;
  double start_, end_, dur_;
  bool gpu_;
  uint8_t* mem_ = nullptr;
  int next_ = 0;
  std::atomic<bool> stop_{false};
  std::thread thread_;
  YuvConverter conv_;
  bool conv_ready_ = false, conv_ok_ = false;
};

std::mutex g_clips_lock;
std::map<int, std::unique_ptr<Clip>> g_clips;

void ClipCommand(const Json& j) {
  const std::string op = j.S("op");
  const int id = static_cast<int>(j.N("id"));
  std::unique_ptr<Clip> old;
  {
    std::lock_guard<std::mutex> l(g_clips_lock);
    auto it = g_clips.find(id);
    if (it != g_clips.end()) {
      old = std::move(it->second);
      g_clips.erase(it);
    }
  }
  if (old)
    std::thread([](Clip* c) { delete c; }, old.release()).detach();   // stops without holding up the commands
  if (op != "clip_open")
    return;
  std::string shm = j.S("shm");
  std::unique_ptr<Clip> c(new Clip(id, j.S("path"), std::wstring(shm.begin(), shm.end()), static_cast<int>(j.N("w")),
                                   static_cast<int>(j.N("h")), j.N("start"), j.N("end"), j.N("dur", 0.04),
                                   j.N("gpu", 1) != 0));
  c->Start();
  std::lock_guard<std::mutex> l(g_clips_lock);
  g_clips[id] = std::move(c);
}

// ---------------------------------------------------------------- the layout's preview, drawn by the GPU itself
// A child window over the control screen's preview box (Playout's window gives its handle and the box's place):
// the mixed picture is drawn into it straight from the GPU every frame, so the control screen no longer makes,
// sends and shows JPEG pictures of the program (as vMix's own preview).
struct PreviewWant {
  HWND parent = nullptr;
  int x = 0, y = 0, w = 0, h = 0;
  bool on = false;
};
std::mutex g_pv_lock;
PreviewWant g_pv_want;
HWND g_pv_hwnd = nullptr;          // UI thread makes and moves it
std::atomic<HWND> g_pv_ready{nullptr};
std::atomic<int> g_pv_w{0}, g_pv_h{0};
ComPtr<IDXGISwapChain> g_pv_chain;    // render thread
int g_pv_cw = 0, g_pv_ch = 0;
std::atomic<int64_t> g_pv_shown{0};   // pictures presented (diagnostics)
std::string g_pv_err;                  // the last step that failed (render thread)
HWND g_pv_chain_hwnd = nullptr;

LRESULT CALLBACK PreviewProc(HWND hwnd, UINT msg, WPARAM wp, LPARAM lp) {
  if (msg == WM_NCHITTEST)
    return HTTRANSPARENT;
  if (msg == WM_ERASEBKGND)
    return 1;
  return ::DefWindowProcW(hwnd, msg, wp, lp);
}

// The preview window lives on its own thread (with its own message loop), never on Chromium's UI thread and
// never waiting for the GPU lock: Present() on the render thread can wait for the window's thread to answer,
// and with Chromium's thread (which also takes the GPU lock for every page picture) that locked both up.
DWORD g_pv_thread_id = 0;
std::atomic<bool> g_pv_thread_up{false};

void PreviewApply() {
  PreviewWant w;
  {
    std::lock_guard<std::mutex> l(g_pv_lock);
    w = g_pv_want;
  }
  if (g_pv_hwnd && (!w.parent || ::GetParent(g_pv_hwnd) != w.parent || !::IsWindow(w.parent))) {
    // Playout's window changed (rare): leave the old one hidden; the render thread lets go of it by itself
    g_pv_ready = nullptr;
    ::ShowWindow(g_pv_hwnd, SW_HIDE);
    g_pv_hwnd = nullptr;
  }
  if (!w.parent || !::IsWindow(w.parent))
    return;
  if (!g_pv_hwnd) {
    static bool registered = false;
    if (!registered) {
      WNDCLASSW wc = {};
      wc.lpfnWndProc = PreviewProc;
      wc.hInstance = ::GetModuleHandleW(nullptr);
      wc.lpszClassName = L"PlayoutGpuPreview";
      wc.hbrBackground = static_cast<HBRUSH>(::GetStockObject(BLACK_BRUSH));
      ::RegisterClassW(&wc);
      registered = true;
    }
    g_pv_hwnd = ::CreateWindowExW(WS_EX_NOPARENTNOTIFY | WS_EX_TRANSPARENT, L"PlayoutGpuPreview", L"", WS_CHILD | WS_DISABLED,
                                  w.x, w.y, w.w > 0 ? w.w : 16, w.h > 0 ? w.h : 9, w.parent, nullptr,
                                  ::GetModuleHandleW(nullptr), nullptr);
    if (!g_pv_hwnd)
      return;
  }
  const bool show = w.on && w.w > 8 && w.h > 8;
  if (show) {
    g_pv_w = w.w;
    g_pv_h = w.h;
  } else {
    g_pv_ready = nullptr;
  }
  ::SetWindowPos(g_pv_hwnd, HWND_TOP, w.x, w.y, show ? w.w : 1, show ? w.h : 1,
                 SWP_NOACTIVATE | SWP_ASYNCWINDOWPOS | (show ? SWP_SHOWWINDOW : SWP_HIDEWINDOW));
  if (show)
    g_pv_ready = g_pv_hwnd;
}

void PreviewThread() {
  MSG m;
  ::PeekMessageW(&m, nullptr, 0, 0, PM_NOREMOVE);   // makes this thread's message queue
  g_pv_thread_id = ::GetCurrentThreadId();
  g_pv_thread_up = true;
  while (::GetMessageW(&m, nullptr, 0, 0) > 0) {
    if (m.hwnd == nullptr && m.message == WM_APP) {
      PreviewApply();
      continue;
    }
    ::TranslateMessage(&m);
    ::DispatchMessageW(&m);
  }
}

void PreviewChanged() {
  static std::once_flag once;
  std::call_once(once, [] { std::thread(PreviewThread).detach(); });
  while (!g_pv_thread_up)
    ::Sleep(1);
  ::PostThreadMessageW(g_pv_thread_id, WM_APP, 0, 0);
}

// render thread, GPU lock held: the mixed picture into the preview window
void PresentPreview() {
  HWND hwnd = g_pv_ready.load();
  if (!hwnd) {
    return;
  }
  const int w = g_pv_w, h = g_pv_h;
  if (!g_pv_chain || g_pv_chain_hwnd != hwnd) {
    g_pv_chain.Reset();
    ComPtr<IDXGIDevice> dxgi;
    ComPtr<IDXGIAdapter> adapter;
    ComPtr<IDXGIFactory> factory;
    if (FAILED(g_dev.As(&dxgi)) || FAILED(dxgi->GetAdapter(&adapter)) || FAILED(adapter->GetParent(IID_PPV_ARGS(&factory)))) {
      g_pv_err = "factory";
      return;
    }
    DXGI_SWAP_CHAIN_DESC d = {};
    d.BufferDesc.Width = w;
    d.BufferDesc.Height = h;
    d.BufferDesc.Format = DXGI_FORMAT_B8G8R8A8_UNORM;
    d.SampleDesc.Count = 1;
    d.BufferUsage = DXGI_USAGE_RENDER_TARGET_OUTPUT;
    d.BufferCount = 1;
    d.OutputWindow = hwnd;
    d.Windowed = TRUE;
    d.SwapEffect = DXGI_SWAP_EFFECT_DISCARD;
    HRESULT hr = factory->CreateSwapChain(g_dev.Get(), &d, &g_pv_chain);
    if (FAILED(hr)) {
      g_pv_err = "swapchain-" + std::to_string(static_cast<unsigned>(hr));
      return;
    }
    factory->MakeWindowAssociation(hwnd, DXGI_MWA_NO_ALT_ENTER | DXGI_MWA_NO_WINDOW_CHANGES);
    g_pv_chain_hwnd = hwnd;
    g_pv_cw = w;
    g_pv_ch = h;
  } else if (g_pv_cw != w || g_pv_ch != h) {
    if (FAILED(g_pv_chain->ResizeBuffers(1, w, h, DXGI_FORMAT_B8G8R8A8_UNORM, 0))) {
      g_pv_err = "resize";
      g_pv_chain.Reset();
      return;
    }
    g_pv_cw = w;
    g_pv_ch = h;
  }
  ComPtr<ID3D11Texture2D> back;
  if (FAILED(g_pv_chain->GetBuffer(0, IID_PPV_ARGS(&back)))) {
    g_pv_err = "buffer";
    return;
  }
  Tex t;
  t.tex = back;
  t.w = w;
  t.h = h;
  if (FAILED(g_dev->CreateRenderTargetView(back.Get(), nullptr, &t.rtv))) {
    g_pv_err = "target";
    return;
  }
  g_mixer.Begin(t);
  g_mixer.Draw(g_mixer.Program(), 0, 0, static_cast<float>(w), static_cast<float>(h), 1, true, true, true);
  g_mixer.End();
  const HRESULT hr = g_pv_chain->Present(0, 0);
  if (FAILED(hr)) {
    g_pv_err = "present-" + std::to_string(static_cast<unsigned>(hr));
    g_pv_chain.Reset();   // made again on the next picture
    return;
  }
  ++g_pv_shown;
}

// ---------------------------------------------------------------- the render thread
std::mutex g_cmd_lock;
std::condition_variable g_cmd_cv;
std::deque<Json> g_cmds;
bool g_cmd_stop = false;

std::map<std::string, std::unique_ptr<Encoder>> g_encs;   // by name: stream, rec, v0..
std::map<int64_t, Tex> g_images;   // CG pictures sent by Playout, by its id
std::atomic<int64_t> g_frames{0};

EncConfig ConfigFrom(const Json& j) {
  EncConfig c;
  c.name = j.S("name", "stream");
  c.kind = j.S("kind", "stream");
  c.layout = j.S("layout");
  c.pos = static_cast<float>(j.N("pos", 50)) / 100.f;
  c.encoder = j.S("enc", "auto");
  c.path = j.S("path");
  std::string pipe = j.S("pipe");
  c.pipe.assign(pipe.begin(), pipe.end());
  c.w = static_cast<int>(j.N("w", g_opt.width)) & ~1;
  c.h = static_cast<int>(j.N("h", g_opt.height)) & ~1;
  c.kbps = static_cast<int>(j.N("kbps", 6000));
  c.akbps = static_cast<int>(j.N("akbps", 160));
  c.gop = static_cast<int>(j.N("gop", 50));
  c.preset = j.S("preset", "veryfast");
  return c;
}

void StartEncoder(const Json& j) {
  EncConfig c = ConfigFrom(j);
  const std::string name = c.name;
  auto it = g_encs.find(name);
  if (it != g_encs.end()) {
    Retire(std::move(it->second));
    g_encs.erase(it);
  }
  std::unique_ptr<Encoder> e(new Encoder(c));
  std::string err;
  if (!e->Open(err)) {
    Say("enc " + name + " error " + OneLine(err));
    Retire(std::move(e));
    return;
  }
  Say("enc " + name + " started " + e->name());
  g_encs[name] = std::move(e);
}

void Frame(const Json& j) {
  const int64_t k = static_cast<int64_t>(j.N("k"));
  int n = static_cast<int>(j.N("n"));
  if (n < 0 || n > kMaxSamples)
    n = 0;
  std::lock_guard<std::recursive_mutex> l(g_gpu);
  // CG pictures that changed
  if (const Json* up = j.Get("up")) {
    for (const auto& u : up->arr) {
      const int w = static_cast<int>(u.N("w")), h = static_cast<int>(u.N("h"));
      const size_t off = static_cast<size_t>(u.N("off"));
      if (w <= 0 || h <= 0 || off + static_cast<size_t>(w) * h * 4 > UploadBytes())
        continue;
      Tex t;
      if (MakeTex(t, w, h, false, g_mem + UploadOff() + off, w * 4))
        g_images[static_cast<int64_t>(u.N("id"))] = t;
    }
  }
  const Json* box = j.Get("box");
  Tex* squeezed = nullptr;
  if (box && box->arr.size() == 4) {
    squeezed = g_mixer.BaseTex(g_mem + BaseOff());
    g_mixer.Black();
  } else {
    g_mixer.Base(g_mem + BaseOff());
  }
  g_mixer.Begin(g_mixer.Program());
  std::vector<const Json*> todo;
  if (const Json* under = j.Get("under"))
    for (const auto& L : under->arr)
      todo.push_back(&L);
  const size_t under_n = todo.size();
  if (const Json* layers = j.Get("layers"))
    for (const auto& L : layers->arr)
      todo.push_back(&L);
  if (squeezed && under_n == 0)
    g_mixer.Draw(*squeezed, static_cast<float>(box->arr[0].num), static_cast<float>(box->arr[1].num),
                 static_cast<float>(box->arr[2].num), static_cast<float>(box->arr[3].num), 1, true, true, true);
  {
    for (size_t li = 0; li < todo.size(); ++li) {
      const Json& L = *todo[li];
      if (squeezed && under_n > 0 && li == under_n)   // the program over the pictures under it
        g_mixer.Draw(*squeezed, static_cast<float>(box->arr[0].num), static_cast<float>(box->arr[1].num),
                     static_cast<float>(box->arr[2].num), static_cast<float>(box->arr[3].num), 1, true, true, true);
      const float x = static_cast<float>(L.N("x")), y = static_cast<float>(L.N("y"));
      const float op = static_cast<float>(L.N("o", 255)) / 255.f;
      if (L.Get("web")) {
        CefRefPtr<Page> p;
        {
          std::lock_guard<std::mutex> pl(g_pages_lock);
          auto it = g_pages.find(static_cast<int>(L.N("web")));
          if (it != g_pages.end())
            p = it->second;
        }
        if (!p)
          continue;
        Tex t = p->Picture();
        if (!t.tex)
          continue;
        const float z = static_cast<float>(L.N("z", 1));
        g_mixer.Draw(t, x, y, t.w * z, t.h * z, op, true, z < 0.999f || z > 1.001f);
      } else {
        auto it = g_images.find(static_cast<int64_t>(L.N("img")));
        if (it == g_images.end())
          continue;
        Tex& t = it->second;
        g_mixer.Draw(t, x, y, static_cast<float>(t.w), static_cast<float>(t.h), op, false, false);
      }
    }
    if (squeezed && under_n > 0 && todo.size() == under_n)
      g_mixer.Draw(*squeezed, static_cast<float>(box->arr[0].num), static_cast<float>(box->arr[1].num),
                   static_cast<float>(box->arr[2].num), static_cast<float>(box->arr[3].num), 1, true, true, true);
  }
  g_mixer.End();
  const float* audio = reinterpret_cast<const float*>(g_mem + AudioOff());
  std::vector<float> sound(2 * static_cast<size_t>(n));
  if (n) {
    std::memcpy(sound.data(), audio, n * sizeof(float));
    std::memcpy(sound.data() + n, audio + kMaxSamples, n * sizeof(float));
  }
  for (auto& kv : g_encs) {
    const EncConfig& c = kv.second->config();
    kv.second->Submit(g_mixer.Layout(c.w, c.h, c.layout, c.pos), sound.data(), n);
  }
  PresentPreview();
  const bool rb = j.N("rb", 1) != 0 && g_mixer.Readback(g_mem + ReadbackOff());
  ++g_frames;
  Say("ack " + std::to_string(k) + (rb ? " rb" : ""));
}

void Command(const Json& j);

void RenderThread() {
  double next_stats = Now() + 2;
  int64_t frames_at = 0;
  while (true) {
    Json j;
    {
      std::unique_lock<std::mutex> l(g_cmd_lock);
      g_cmd_cv.wait_for(l, std::chrono::milliseconds(500), [] { return g_cmd_stop || !g_cmds.empty(); });
      if (g_cmd_stop && g_cmds.empty())
        break;
      if (g_cmds.empty())
        j.kind = Json::Null;
      else {
        j = std::move(g_cmds.front());
        g_cmds.pop_front();
      }
    }
    if (j.kind == Json::Obj)
      Command(j);
    const double now = Now();
    if (now >= next_stats) {
      const int64_t f = g_frames.load();
      std::string s = "stats fps=" + std::to_string(static_cast<int>((f - frames_at) / (now - next_stats + 2) + 0.5));
      frames_at = f;
      next_stats = now + 2;
      for (auto& kv : g_encs)
        s += " " + kv.first + "_drop=" + std::to_string(kv.second->dropped()) + " " + kv.first +
             "_frames=" + std::to_string(kv.second->frames());
      {
        std::lock_guard<std::mutex> l(g_pages_lock);
        for (auto& kv : g_pages)
          s += " web" + std::to_string(kv.first) + "=" + std::to_string(kv.second->frames()) +
               (kv.second->gpu() ? "g" : "c");
      }
      s += " preview=" + std::to_string(g_pv_shown.load()) + (g_pv_ready.load() ? "on" : "off");
      {
        std::lock_guard<std::recursive_mutex> l(g_gpu);
        if (!g_pv_err.empty())
          s += " preview_error=" + g_pv_err;
      }
      Say(s);
    }
  }
  std::lock_guard<std::recursive_mutex> l(g_gpu);
  for (auto& kv : g_encs)
    Retire(std::move(kv.second));
  g_encs.clear();
}

void UiCommand(std::string op, int id, std::string url, int w, int h, int fps, std::string js, bool on) {
  CEF_REQUIRE_UI_THREAD();
  if (op == "web_open") {
    OpenPage(id, url, w, h, fps, js, true);
    return;
  }
  if (op == "quit") {
    std::vector<CefRefPtr<Page>> all;
    {
      std::lock_guard<std::mutex> l(g_pages_lock);
      for (auto& kv : g_pages)
        all.push_back(kv.second);
      g_pages.clear();
    }
    for (auto& p : all)
      p->Close();
    CefPostDelayedTask(TID_UI, base::BindOnce(&CefQuitMessageLoop), 300);
    return;
  }
  CefRefPtr<Page> p;
  {
    std::lock_guard<std::mutex> l(g_pages_lock);
    auto it = g_pages.find(id);
    if (it == g_pages.end())
      return;
    p = it->second;
    if (op == "web_close")
      g_pages.erase(it);
  }
  if (op == "web_close")
    p->Close();
  else if (op == "web_reload")
    OpenPage(id, p->url(), p->w(), p->h(), p->fps(), p->js(), true);   // a fresh page, as cefcg did
  else if (op == "web_show")
    p->Show(on);
}

void Command(const Json& j) {
  const std::string op = j.S("op");
  if (op == "frame") {
    Frame(j);
  } else if (op == "drop") {
    std::lock_guard<std::recursive_mutex> l(g_gpu);
    if (const Json* ids = j.Get("ids"))
      for (const auto& i : ids->arr)
        g_images.erase(static_cast<int64_t>(i.num));
  } else if (op == "enc_start") {
    std::lock_guard<std::recursive_mutex> l(g_gpu);
    StartEncoder(j);
  } else if (op == "enc_key") {
    std::lock_guard<std::recursive_mutex> l(g_gpu);
    auto it = g_encs.find(j.S("name"));
    if (it != g_encs.end())
      it->second->Key();
  } else if (op == "enc_stop") {
    std::lock_guard<std::recursive_mutex> l(g_gpu);
    const std::string name = j.S("name");
    auto it = g_encs.find(name);
    if (it != g_encs.end()) {
      Retire(std::move(it->second));
      g_encs.erase(it);
    }
    Say("enc " + name + " stopped");
  }
}

void ReadCommands() {
  HANDLE in = ::GetStdHandle(STD_INPUT_HANDLE);
  std::string buf;
  std::vector<char> chunk(1 << 16);
  DWORD n = 0;
  while (in && in != INVALID_HANDLE_VALUE && ::ReadFile(in, chunk.data(), static_cast<DWORD>(chunk.size()), &n, nullptr) &&
         n > 0) {
    buf.append(chunk.data(), n);
    size_t at;
    while ((at = buf.find('\n')) != std::string::npos) {
      std::string line = buf.substr(0, at);
      buf.erase(0, at + 1);
      Json j;
      if (!JsonReader(line).Parse(j) || j.kind != Json::Obj)
        continue;
      const std::string op = j.S("op");
      if (op == "quit")
        goto done;
      if (op == "preview") {
        {
          std::lock_guard<std::mutex> l(g_pv_lock);
          g_pv_want.parent = reinterpret_cast<HWND>(static_cast<intptr_t>(j.N("hwnd")));
          g_pv_want.x = static_cast<int>(j.N("x"));
          g_pv_want.y = static_cast<int>(j.N("y"));
          g_pv_want.w = static_cast<int>(j.N("w"));
          g_pv_want.h = static_cast<int>(j.N("h"));
          g_pv_want.on = j.N("on") != 0;
        }
        PreviewChanged();
        continue;
      }
      if (op.compare(0, 5, "clip_") == 0) {
        ClipCommand(j);
        continue;
      }
      if (op.compare(0, 4, "web_") == 0) {
        CefPostTask(TID_UI, base::BindOnce(&UiCommand, op, static_cast<int>(j.N("id")), j.S("url"),
                                           static_cast<int>(j.N("w", 1920)), static_cast<int>(j.N("h", 1080)),
                                           static_cast<int>(j.N("fps", 25)), j.S("js"), j.N("on", 1) != 0));
        continue;
      }
      {
        std::lock_guard<std::mutex> l(g_cmd_lock);
        g_cmds.push_back(std::move(j));
      }
      g_cmd_cv.notify_one();
    }
  }
done:
  // Playout stopped or asked to quit: finish the files, close the pages, end
  {
    std::lock_guard<std::mutex> l(g_cmd_lock);
    g_cmd_stop = true;
  }
  g_cmd_cv.notify_all();
  CefPostTask(TID_UI, base::BindOnce(&UiCommand, std::string("quit"), 0, std::string(), 0, 0, 0, std::string(), false));
}

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
    cl->AppendSwitch("disable-backgrounding-occluded-windows");
    cl->AppendSwitchWithValue("disable-features", "OptimizationGuideModelDownloading,OptimizationHints,OnDeviceModelPerformanceParams");
  }

 private:
  IMPLEMENT_REFCOUNTING(App);
};

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
  int code = CefExecuteProcess(main_args, app, nullptr);   // Chromium's own helper processes end here
  if (code >= 0)
    return code;

  CefRefPtr<CefCommandLine> cl = CefCommandLine::CreateCommandLine();
  cl->InitFromString(::GetCommandLineW());
  g_opt.shm = cl->GetSwitchValue("pg-shm").ToWString();
  g_opt.width = Num(cl, "pg-width", 1920, 16, 7680) & ~1;
  g_opt.height = Num(cl, "pg-height", 1080, 16, 4320) & ~1;
  g_opt.fps_num = Num(cl, "pg-fps-num", 25, 1, 240000);
  g_opt.fps_den = Num(cl, "pg-fps-den", 1, 1, 100000);
  g_opt.cache = cl->GetSwitchValue("pg-cache").ToString();
  if (g_opt.shm.empty()) {
    Say("error pgpu needs --pg-shm");
    return 2;
  }
  const size_t size = ShmBytes();
  HANDLE map = ::OpenFileMappingW(FILE_MAP_ALL_ACCESS, FALSE, g_opt.shm.c_str());
  if (map)
    g_mem = static_cast<uint8_t*>(::MapViewOfFile(map, FILE_MAP_ALL_ACCESS, 0, 0, size));
  if (!g_mem) {
    Say("error could not open the shared picture memory");
    return 3;
  }
  if (!PickDevice()) {
    Say("error no Direct3D 11 on this PC");
    return 5;
  }
  if (!g_mixer.Init(g_opt.width, g_opt.height)) {
    Say("error the GPU mixer did not start");
    return 6;
  }
  auto* hdr = reinterpret_cast<uint32_t*>(g_mem);
  hdr[1] = static_cast<uint32_t>(g_opt.width);
  hdr[2] = static_cast<uint32_t>(g_opt.height);
  hdr[0] = kMagic;

  CefSettings settings;
  settings.no_sandbox = true;
  settings.windowless_rendering_enabled = true;
  settings.log_severity = LOGSEVERITY_DISABLE;
  if (!g_opt.cache.empty())
    CefString(&settings.root_cache_path) = g_opt.cache;
  if (!CefInitialize(main_args, settings, app, nullptr)) {
    Say("error the built-in browser did not start");
    return 4;
  }
  Say(std::string("ready ") + (g_nvidia ? "nvidia" : g_warp ? "software" : "gpu") + " " + g_adapter);
  std::thread render(RenderThread);
  std::thread(ReadCommands).detach();
  CefPostDelayedTask(TID_UI, base::BindOnce(&CheckPages), 2000);
  CefRunMessageLoop();
  {
    std::lock_guard<std::mutex> l(g_cmd_lock);
    g_cmd_stop = true;
  }
  g_cmd_cv.notify_all();
  render.join();
  CefShutdown();
  // let the retired encoders finish their files
  ::Sleep(1500);
  ::UnmapViewOfFile(g_mem);
  ::CloseHandle(map);
  ::ExitProcess(0);   // the stdin thread may still wait on a read
}
