"""CG blending on the graphics card (OpenGL, through moderngl).

The program picture goes to the GPU once per frame, every CG on air is drawn over it there, and the
result comes back. A CG picture that does not change (a still PNG, a score that has not moved) stays on
the GPU and is not sent again. The see-through parts, fades and the program's own alpha behave as in
cg.composite (the CPU blend), which stays in use when the PC has no usable OpenGL.

config.json "cg_gpu": "auto" (default), "off", or "force" (also on a PC without a graphics driver).
"""
import logging
import threading

import numpy as np

MODE = "auto"
STATE = {"gpu": None, "renderer": "", "error": ""}   # health page

_VS = """
#version 330
uniform vec4 rect;        // x0, y0, x1, y1 in clip space
in vec2 pos;
out vec2 uv;
void main() {
    uv = pos;
    gl_Position = vec4(mix(rect.xy, rect.zw, pos), 0.0, 1.0);
}
"""
_FS = """
#version 330
uniform sampler2D tex;
uniform float opacity;
in vec2 uv;
out vec4 color;
void main() {
    vec4 c = texture(tex, uv);
    color = vec4(c.rgb, c.a * opacity);
}
"""


class GLCompositor:
    """Lives on the thread that renders (an OpenGL context belongs to one thread)."""

    CACHE = 24          # CG pictures kept on the GPU

    def __init__(self):
        import moderngl
        self.mgl = moderngl
        try:
            self.ctx = moderngl.create_standalone_context()
        except Exception:
            self.ctx = moderngl.create_standalone_context(backend="egl")
        if self.ctx.version_code < 330:
            raise RuntimeError(f"OpenGL {self.ctx.version_code} is too old (3.3 needed)")
        r = self.ctx.info.get("GL_RENDERER", "")
        if any(k in r.lower() for k in ("gdi generic", "llvmpipe", "softpipe", "basic render")) and MODE != "force":
            raise RuntimeError(f"no graphics card driver ({r})")
        self.prog = self.ctx.program(vertex_shader=_VS, fragment_shader=_FS)
        quad = np.array([0, 0, 1, 0, 0, 1, 1, 1], "f4")
        self.vao = self.ctx.vertex_array(self.prog, [(self.ctx.buffer(quad.tobytes()), "2f", "pos")])
        self.size = None
        self._tex = {}       # id(array) -> (array, texture)
        self.renderer = self.ctx.info.get("GL_RENDERER", "")

    def _target(self, w, h):
        if self.size != (w, h):
            self.size = (w, h)
            self.base = self.ctx.texture((w, h), 4)
            self.fbo = self.ctx.framebuffer(color_attachments=[self.base])

    def _texture(self, img):
        hit = self._tex.get(id(img))
        if hit is not None and hit[0] is img:
            return hit[1]
        h, w = img.shape[:2]
        t = self.ctx.texture((w, h), 4, np.ascontiguousarray(img).tobytes())
        t.filter = (self.mgl.NEAREST, self.mgl.NEAREST)
        if len(self._tex) >= self.CACHE:
            old = next(iter(self._tex))
            self._tex.pop(old)[1].release()
        self._tex[id(img)] = (img, t)
        return t

    def render(self, frame, layers):
        """frame: (H, W, 4) uint8 BGRA. layers: [(img, x, y, opacity 0-255)]. Returns a new (H, W, 4) array."""
        H, W = frame.shape[:2]
        self._target(W, H)
        self.base.write(np.ascontiguousarray(frame).tobytes())
        self.fbo.use()
        self.ctx.viewport = (0, 0, W, H)
        self.ctx.enable(self.mgl.BLEND)
        # colour: over; alpha: the program's own alpha stays as it is
        self.ctx.blend_func = (self.mgl.SRC_ALPHA, self.mgl.ONE_MINUS_SRC_ALPHA, self.mgl.ZERO, self.mgl.ONE)
        used = set()
        for img, x, y, op in layers:
            if op <= 0:
                continue
            h, w = img.shape[:2]
            tex = self._texture(img)
            used.add(id(img))
            tex.use(0)
            # row 0 of the picture is row 0 of the texture; the framebuffer is read back the same way
            self.prog["rect"].value = (x / W * 2 - 1, y / H * 2 - 1, (x + w) / W * 2 - 1, (y + h) / H * 2 - 1)
            self.prog["opacity"].value = min(255, op) / 255.0
            self.vao.render(self.mgl.TRIANGLE_STRIP)
        out = np.frombuffer(self.fbo.read(components=4, alignment=1), np.uint8).reshape(H, W, 4).copy()
        for k in [k for k in self._tex if k not in used]:   # pictures no longer on air leave the GPU
            self._tex.pop(k)[1].release()
        return out


_local = threading.local()


def compositor():
    """The GPU compositor for this thread, or None (off, no OpenGL, or it failed once)."""
    if (MODE or "auto").lower() in ("off", "cpu", "none"):
        STATE["gpu"] = False
        return None
    c = getattr(_local, "c", 0)
    if c == 0:
        try:
            c = GLCompositor()
            STATE.update(gpu=True, renderer=c.renderer, error="")
            logging.info("CG blending on the GPU: %s", c.renderer)
        except Exception as e:
            c = None
            STATE.update(gpu=False, error=str(e)[:200] or type(e).__name__)
            logging.info("CG blending on the CPU (no OpenGL: %s)", STATE["error"])
        _local.c = c
    return c


def failed(e):
    """A GPU blend went wrong: this thread goes back to the CPU blend."""
    _local.c = None
    STATE.update(gpu=False, error=str(e)[:200] or type(e).__name__)
    logging.warning("CG GPU blend failed, using the CPU: %s", STATE["error"])


# ---- video pictures to BGRA on the GPU ------------------------------------------------------------
# A decoded clip / USB picture comes as YUV (nv12 from GPU decoding, yuv420p / yuyv422 otherwise).
# Turning it into the program's BGRA at the output size (colour conversion + scaling) is the heaviest
# CPU job of playout; here the YUV planes go to the graphics card and a shader does both.

_YUV_FS = """
#version 330
uniform sampler2D ty;
uniform sampler2D tu;
uniform sampler2D tv;
uniform int kind;           // 0 planar (Y, U, V)  1 nv12 (Y, UV)  2 yuyv422  3 uyvy422
uniform vec2 sy;            // visible part of the Y texture (the rest is row padding)
uniform vec2 sc;            // same for the colour textures
uniform ivec2 src;          // picture size in pixels (packed formats)
uniform int full;           // 1 = full range (0-255), 0 = TV range (16-235)
uniform vec3 kr;            // R = Y + kr.x V;  G = Y - kr.y U - kr.z V
uniform float kb;           // B = Y + kb U
in vec2 uv;
out vec4 color;
void main() {
    float y, u, v;
    if (kind == 0) {
        y = texture(ty, uv * sy).r; u = texture(tu, uv * sc).r; v = texture(tv, uv * sc).r;
    } else if (kind == 1) {
        y = texture(ty, uv * sy).r; vec2 c = texture(tu, uv * sc).rg; u = c.x; v = c.y;
    } else {
        ivec2 p = min(ivec2(uv * vec2(src)), src - 1);
        vec4 t = texelFetch(ty, ivec2(p.x / 2, p.y), 0);
        if (kind == 2) { y = (p.x % 2 == 0) ? t.r : t.b; u = t.g; v = t.a; }
        else           { y = (p.x % 2 == 0) ? t.g : t.a; u = t.r; v = t.b; }
    }
    if (full == 1) { u -= 0.50196; v -= 0.50196; }
    else { y = (y - 0.06275) * 1.16438; u = (u - 0.50196) * 1.13839; v = (v - 0.50196) * 1.13839; }
    vec3 rgb = clamp(vec3(y + kr.x * v, y - kr.y * u - kr.z * v, y + kb * u), 0.0, 1.0);
    color = vec4(rgb.b, rgb.g, rgb.r, 1.0);     // stored as B G R A
}
"""
_KINDS = {"yuv420p": 0, "yuvj420p": 0, "yuv422p": 0, "yuvj422p": 0, "yuv444p": 0, "yuvj444p": 0,
          "nv12": 1, "yuyv422": 2, "uyvy422": 3}
_BT709 = ((1.5748, 0.1873, 0.4681), 1.8556)
_BT601 = ((1.402, 0.344136, 0.714136), 1.772)
CONVERT = {"used": None, "error": ""}      # health page
MAX_FAILS = 3               # failures in a row before the CPU takes over for good


class YUVConverter:
    def __init__(self, ctx):
        import moderngl
        self.mgl, self.ctx = moderngl, ctx
        self.prog = ctx.program(vertex_shader=_VS, fragment_shader=_YUV_FS)
        self.prog["rect"].value = (-1.0, -1.0, 1.0, 1.0)
        for i, n in enumerate(("ty", "tu", "tv")):
            self.prog[n].value = i
        quad = np.array([0, 0, 1, 0, 0, 1, 1, 1], "f4")
        self.vao = ctx.vertex_array(self.prog, [(ctx.buffer(quad.tobytes()), "2f", "pos")])
        self.tex = {}
        self.out = None

    def _upload(self, slot, size, comps, data):
        t = self.tex.get(slot)
        if t is None or t.size != size or t.components != comps:
            if t is not None:
                t.release()
            t = self.tex[slot] = self.ctx.texture(size, comps, alignment=1)
            t.filter = (self.mgl.LINEAR, self.mgl.LINEAR)
            t.repeat_x = t.repeat_y = False
        t.write(data, alignment=1)
        t.use(slot)

    def convert(self, frame, W, H):
        kind = _KINDS[frame.format.name]
        w, h = frame.width, frame.height
        planes = frame.planes
        ls = planes[0].line_size
        if kind == 0:
            pw, ph = planes[1].width, planes[1].height
            self._upload(0, (ls, h), 1, planes[0])
            self._upload(1, (planes[1].line_size, ph), 1, planes[1])
            self._upload(2, (planes[2].line_size, ph), 1, planes[2])
            self.prog["sc"].value = (pw / planes[1].line_size, 1.0)
        elif kind == 1:
            cw, ch = (w + 1) // 2, (h + 1) // 2
            self._upload(0, (ls, h), 1, planes[0])
            self._upload(1, (planes[1].line_size // 2, ch), 2, planes[1])
            self.prog["sc"].value = (cw / (planes[1].line_size // 2), 1.0)
        else:
            self._upload(0, (ls // 4, h), 4, planes[0])
            self.prog["src"].value = (w, h)
        self.prog["sy"].value = (w / ls, 1.0)
        self.prog["kind"].value = kind
        full = frame.format.name.startswith("yuvj") or int(getattr(frame, "color_range", 0) or 0) == 2
        self.prog["full"].value = 1 if full else 0
        cs = int(getattr(frame, "colorspace", 2) or 2)
        m = _BT709 if cs == 1 or (cs not in (5, 6) and h >= 720 and not full) else _BT601   # MJPEG: BT.601
        self.prog["kr"].value, self.prog["kb"].value = m
        if self.out is None or self.out.size != (W, H):
            self.out = self.ctx.texture((W, H), 4)
            self.fbo = self.ctx.framebuffer(color_attachments=[self.out])
        self.fbo.use()
        self.ctx.viewport = (0, 0, W, H)
        self.ctx.disable(self.mgl.BLEND)
        self.vao.render(self.mgl.TRIANGLE_STRIP)
        return np.frombuffer(self.fbo.read(components=4, alignment=1), np.uint8)


_conv = None
_conv_lock = threading.Lock()
_fails = 0


def _converter():
    global _conv
    if _conv is None:
        g = GLCompositor()          # same checks: real graphics card, OpenGL 3.3
        _conv = (g.ctx, YUVConverter(g.ctx), g)
        logging.info("Video pictures converted on the GPU: %s", g.renderer)
    return _conv


def to_bgra(frame, W, H):
    """A decoded VideoFrame as a flat BGRA array of W x H, made on the graphics card.
    The CPU (swscale) does it when the GPU cannot: this never fails. One failed picture
    only sends that picture to the CPU; the GPU is given up after MAX_FAILS in a row."""
    global _fails
    if CONVERT["used"] is not False and (MODE or "auto").lower() not in ("off", "cpu", "none") \
            and frame.format.name in _KINDS:
        try:
            with _conv_lock:        # one OpenGL context, used by each decoding thread in turn
                ctx, conv, _ = _converter()
                with ctx:
                    img = conv.convert(frame, W, H)
            CONVERT["used"] = True
            _fails = 0
            return img
        except Exception as e:
            _fails += 1
            err = f"{str(e)[:150] or type(e).__name__} ({frame.format.name} {frame.width}x{frame.height} to {W}x{H})"
            if _conv is not None:
                with _conv_lock:
                    ctx, conv, _ = _conv
                    try:
                        with ctx:
                            for t in list(conv.tex.values()) + ([conv.out] if conv.out is not None else []):
                                t.release()
                    except Exception:
                        pass
                    conv.tex, conv.out = {}, None     # made fresh on the next picture
            if _fails >= MAX_FAILS:
                CONVERT.update(used=False, error=err)
                logging.info("Video pictures converted on the CPU: %s", err)
            else:
                logging.info("One video picture converted on the CPU: %s", err)
    img = frame.reformat(width=W, height=H, format="bgra", interpolation="FAST_BILINEAR").to_ndarray()
    return np.ascontiguousarray(img).reshape(-1)
