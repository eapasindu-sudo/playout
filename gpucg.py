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
