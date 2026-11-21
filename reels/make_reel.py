#!/usr/bin/env python3
"""사진·영상 폴더 → 인스타그램 릴스(1080x1920, 30fps, H.264) 자동 제작.

사용법
  python reels/make_reel.py <폴더>              # 폴더 안 reel.yaml 대로 제작
  python reels/make_reel.py <폴더> --init       # reel.yaml 틀 만들기
  python reels/make_reel.py <폴더> --theme food --title "창원 파스타 맛집"   # 설정 파일 없이 바로

자세한 내용은 reels/README.md 참고.
"""
from __future__ import annotations

import argparse
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None
try:
    from pillow_heif import register_heif_opener  # 아이폰 HEIC 사진

    register_heif_opener()
except ImportError:  # pragma: no cover
    pass

W, H, FPS = 1080, 1920, 30
HERE = Path(__file__).resolve().parent
FONT_DIR = HERE / "fonts"
IMG_EXT = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}
VID_EXT = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"}
AUD_EXT = {".mp3", ".m4a", ".aac", ".wav", ".ogg", ".flac"}

# 인스타 화면 안전 영역: 위쪽 ~160px(상단 UI), 아래쪽 ~480px(설명·버튼), 오른쪽 ~140px(좋아요 버튼)
SAFE_X = 64
SAFE_BOTTOM = 1420
TEXT_W = 880

THEMES = {  # 색은 쓰지 않고, 첫 화면 작은 글씨와 마지막 문구만 바뀐다
    "food": dict(label="맛집", cta="저장해두고 꼭 가보세요", numbered=False),
    "info": dict(label="알아두면 좋은 정보", cta="저장하고 필요할 때 꺼내보세요", numbered=True),
    "kids": dict(label="아이와 가볼 곳", cta="주말 나들이 리스트에 저장", numbered=False),
    "book": dict(label="오늘의 책", cta="읽고 싶은 책 목록에 저장", numbered=False),
}

DEFAULTS = dict(image_duration=2.6, video_duration=5.0, title_duration=3.2,
                outro_duration=3.6, crossfade=0.35, music_volume=0.8)

_EMOJI = re.compile("[\U00010000-\U0010FFFF☀-➿⬀-⯿️‍]")


def clean(s) -> str:
    """폰트에 없는 이모지는 네모(□)로 나오므로 지운다."""
    if s is None:
        return ""
    return _EMOJI.sub("", str(s)).strip()


# ───────────────────────── 폰트·글자 ─────────────────────────
_fonts: dict = {}


def font(weight: str, size: int) -> ImageFont.FreeTypeFont:
    key = (weight, size)
    if key not in _fonts:
        _fonts[key] = ImageFont.truetype(str(FONT_DIR / f"Pretendard-{weight}.otf"), size)
    return _fonts[key]


def wrap(text: str, f: ImageFont.FreeTypeFont, maxw: int) -> list[str]:
    out: list[str] = []
    for para in clean(text).split("\n"):
        line = ""
        for word in para.split(" "):
            cand = word if not line else f"{line} {word}"
            if f.getlength(cand) <= maxw:
                line = cand
                continue
            if line:
                out.append(line)
                line = ""
            while f.getlength(word) > maxw:  # 한 단어가 너무 길면 글자 단위로 자름
                i = len(word)
                while i > 1 and f.getlength(word[:i]) > maxw:
                    i -= 1
                out.append(word[:i])
                word = word[i:]
            line = word
        out.append(line)
    return [ln for ln in out if ln.strip()] or []


class TextBlock:
    def __init__(self, text, weight, size, fill, maxw=TEXT_W, lead=1.24, align="left"):
        self.f = font(weight, size)
        self.lines = wrap(text, self.f, maxw)
        self.fill = fill
        self.lh = int(size * lead)
        self.align = align
        asc, desc = self.f.getmetrics()
        self.h = 0 if not self.lines else (len(self.lines) - 1) * self.lh + asc + desc

    def draw(self, d: ImageDraw.ImageDraw, x: int, y: int, cx: int | None = None):
        for i, ln in enumerate(self.lines):
            if self.align == "center" and cx is not None:
                d.text((cx, y + i * self.lh), ln, font=self.f, fill=self.fill, anchor="ma")
            else:
                d.text((x, y + i * self.lh), ln, font=self.f, fill=self.fill, anchor="la")


def with_shadow(layer: Image.Image, radius=12, opacity=0.55, offset=(0, 4)) -> Image.Image:
    a = layer.getchannel("A").filter(ImageFilter.GaussianBlur(radius))
    a = a.point(lambda v: int(v * opacity))
    sh = Image.new("RGBA", layer.size, (0, 0, 0, 0))
    sh.putalpha(a)
    out = Image.new("RGBA", layer.size, (0, 0, 0, 0))
    out.alpha_composite(sh, offset)
    out.alpha_composite(layer)
    return out


def blank() -> Image.Image:
    return Image.new("RGBA", (W, H), (0, 0, 0, 0))


# ───────────────────────── 겹치는 그림(오버레이) ─────────────────────────
def ease_out(t: float) -> float:
    t = min(max(t, 0.0), 1.0)
    return 1 - (1 - t) ** 3


def ease_in_out(t: float) -> float:
    t = min(max(t, 0.0), 1.0)
    return 3 * t * t - 2 * t * t * t


class Overlay:
    """RGBA 그림을 프레임 위에 얹는다. start초에 나타나며 fade초 동안 rise px 올라오며 선명해진다."""

    def __init__(self, img: Image.Image, start=0.0, fade=0.45, rise=34):
        self.start, self.fade, self.rise = start, fade, rise
        bbox = img.getbbox()
        self.empty = bbox is None
        if self.empty:
            return
        x0, y0, x1, y1 = bbox
        arr = np.asarray(img.crop(bbox), dtype=np.float32) / 255.0
        self.a = arr[..., 3:4]
        self.prgb = arr[..., :3] * self.a * 255.0
        self.x, self.y = x0, y0

    def apply(self, frame: np.ndarray, t: float) -> None:
        if self.empty:
            return
        f = 1.0 if self.fade <= 0 else ease_out((t - self.start) / self.fade)
        if t < self.start or f <= 0:
            return
        dy = int(round((1 - f) * self.rise))
        h, w = self.a.shape[:2]
        y0, x0 = self.y + dy, self.x
        y1, x1 = min(y0 + h, H), min(x0 + w, W)
        if y0 >= H or y1 <= 0:
            return
        sy = max(0, -y0)
        y0 = max(0, y0)
        a = self.a[sy:sy + (y1 - y0), : x1 - x0] * f
        p = self.prgb[sy:sy + (y1 - y0), : x1 - x0] * f
        reg = frame[y0:y1, x0:x1].astype(np.float32)
        frame[y0:y1, x0:x1] = np.clip(reg * (1 - a) + p, 0, 255).astype(np.uint8)


_grad_cache: dict = {}


def gradient_overlay(y_from: float, max_alpha: float) -> Overlay:
    key = (y_from, max_alpha)
    if key not in _grad_cache:
        _grad_cache[key] = _gradient(y_from, max_alpha)
    return _grad_cache[key]


def _gradient(y_from: float, max_alpha: float) -> Overlay:
    """아래쪽을 어둡게 해 글자가 잘 보이게 한다."""
    ys = np.arange(H, dtype=np.float32)
    t = np.clip((ys - y_from) / (H - y_from), 0, 1) ** 1.4
    alpha = (t * max_alpha * 255).astype(np.uint8)
    arr = np.zeros((H, W, 4), np.uint8)
    arr[..., 3] = alpha[:, None]
    return Overlay(Image.fromarray(arr, "RGBA"), fade=0)


# ───────────────────────── 화면 구성 요소 (미니멀: 흰 글자 + 여백) ─────────────────────────
WHITE = (255, 255, 255, 255)
SOFT = (255, 255, 255, 205)
FAINT = (255, 255, 255, 150)


def soft_shadow(layer):
    return with_shadow(layer, radius=14, opacity=0.38, offset=(0, 2))


def dim_overlay(alpha: float) -> Overlay:
    return Overlay(Image.new("RGBA", (W, H), (0, 0, 0, int(alpha * 255))), fade=0)


def stack(blocks, y, layer):
    """[(TextBlock, 아래 간격)] 을 가운데 정렬로 위에서부터 그린다."""
    d = ImageDraw.Draw(layer)
    for b, g in blocks:
        b.draw(d, 0, y, cx=W // 2)
        y += b.h + g
    return y


def stack_h(blocks):
    return sum(b.h for b, _ in blocks) + sum(g for _, g in blocks[:-1])


def top_bar(theme: dict, handle: str) -> Overlay:
    return Overlay(blank())  # 상단 장식 없음


# 스타일: magazine(A·작은 글씨 왼쪽 아래) / vlog(B·자막 없이 빠르게) / bold(C·큰 글씨 가운데)
STYLE = "bold"
STYLE_DEFAULTS = {
    "magazine": dict(image_duration=2.4, title_duration=3.0, outro_duration=3.4, crossfade=0.45),
    "vlog": dict(image_duration=1.3, title_duration=1.6, outro_duration=3.0, crossfade=0.12),
    "bold": dict(image_duration=2.6, title_duration=3.0, outro_duration=3.4, crossfade=0.3),
}


def stack_left(blocks, y, layer, x=SAFE_X):
    d = ImageDraw.Draw(layer)
    for b, g in blocks:
        b.draw(d, x, y)
        y += b.h + g
    return y


def _keep(blocks):
    blocks = [(b, g) for b, g in blocks if b.lines]
    if blocks:
        blocks[-1] = (blocks[-1][0], 0)
    return blocks


def title_overlays(theme, title, subtitle) -> list[Overlay]:
    if STYLE == "vlog":
        return []
    if STYLE == "magazine":
        parts = _keep([(TextBlock(theme["label"], "Regular", 34, FAINT), 18),
                       (TextBlock(title, "SemiBold", 68, WHITE, lead=1.28, maxw=820), 22),
                       (TextBlock(subtitle, "Regular", 40, SOFT, maxw=820), 0)])
        layer = blank()
        stack_left(parts, SAFE_BOTTOM - stack_h(parts), layer)
        return [gradient_overlay(H * 0.5, 0.6), Overlay(soft_shadow(layer), start=0.2, fade=0.9, rise=10)]
    parts = _keep([(TextBlock(theme["label"], "Medium", 32, SOFT, align="center"), 26),
                   (TextBlock(title, "Bold", 96, WHITE, lead=1.2, maxw=920, align="center"), 30),
                   (TextBlock(subtitle, "Medium", 40, SOFT, maxw=900, align="center"), 0)])
    layer = blank()
    stack(parts, int(H * 0.47 - stack_h(parts) / 2), layer)
    return [dim_overlay(0.35), Overlay(soft_shadow(layer), start=0.08, fade=0.5, rise=20)]


def caption_overlays(theme, text, sub, number: str | None) -> list[Overlay]:
    if STYLE == "vlog" or (not clean(text) and not clean(sub)):
        return []
    if STYLE == "magazine":
        parts = _keep(([(TextBlock(number, "Regular", 32, FAINT), 14)] if number else []) +
                      [(TextBlock(text, "Medium", 52, WHITE, lead=1.32, maxw=820), 14),
                       (TextBlock(sub, "Regular", 38, SOFT, maxw=820), 0)])
        layer = blank()
        stack_left(parts, SAFE_BOTTOM - stack_h(parts), layer)
        return [gradient_overlay(H * 0.62, 0.5), Overlay(soft_shadow(layer), start=0.15, fade=0.8, rise=8)]
    parts = _keep(([(TextBlock(number, "Medium", 34, SOFT, align="center"), 20)] if number else []) +
                  [(TextBlock(text, "Bold", 78, WHITE, lead=1.22, maxw=920, align="center"), 24),
                   (TextBlock(sub, "Medium", 40, SOFT, maxw=900, align="center"), 0)])
    layer = blank()
    stack(parts, int(H * 0.5 - stack_h(parts) / 2), layer)
    return [dim_overlay(0.3), Overlay(soft_shadow(layer), start=0.08, fade=0.4, rise=18)]


def _details(outro):
    details = outro.get("details") or {}
    if isinstance(details, list):  # [[키, 값], ...] 형식도 허용
        details = {str(k): v for k, v in details}
    return details


def outro_overlays(theme, outro: dict, handle: str) -> list[Overlay]:
    name = clean(outro.get("name"))
    details = _details(outro)
    cta = outro.get("cta") or theme["cta"]
    if STYLE == "magazine":
        parts = [(TextBlock(theme["label"], "Regular", 34, FAINT), 18),
                 (TextBlock(name, "SemiBold", 68, WHITE, lead=1.25, maxw=900), 44)]
        for k, v in details.items():
            parts += [(TextBlock(k, "Regular", 32, FAINT), 8),
                      (TextBlock(v, "Regular", 42, SOFT, lead=1.35, maxw=900), 30)]
        parts = _keep(parts)
        layer = blank()
        stack_left(parts, int(H * 0.44 - stack_h(parts) / 2), layer)
        end = _keep([(TextBlock(cta, "Medium", 40, SOFT), 12), (TextBlock(handle, "Regular", 34, FAINT), 0)])
        l2 = blank()
        stack_left(end, SAFE_BOTTOM - stack_h(end), l2)
        return [Overlay(soft_shadow(layer), start=0.15, fade=0.9, rise=10),
                Overlay(soft_shadow(l2), start=0.6, fade=0.9, rise=8)]
    big = STYLE == "bold"
    info = [(TextBlock(name, "Bold" if big else "SemiBold", 76 if big else 60, WHITE, lead=1.2, maxw=920,
                       align="center"), 50)]
    for k, v in details.items():
        info += [(TextBlock(k, "Regular", 28, FAINT, align="center"), 10),
                 (TextBlock(v, "Medium" if big else "Regular", 40 if big else 36, SOFT, lead=1.35, maxw=900,
                            align="center"), 36)]
    info = _keep(info)
    ovs = []
    if info:
        layer = blank()
        stack(info, int(H * 0.44 - stack_h(info) / 2), layer)
        ovs.append(Overlay(soft_shadow(layer), start=0.1, fade=0.6, rise=16))
    end = _keep([(TextBlock(cta, "Medium", 40, WHITE, maxw=900, align="center"), 14),
                 (TextBlock(handle, "Regular", 30, FAINT, align="center"), 0)])
    layer = blank()
    stack(end, SAFE_BOTTOM - stack_h(end) if info else int(H * 0.47 - stack_h(end) / 2), layer)
    ovs.append(Overlay(soft_shadow(layer), start=0.5 if info else 0.15, fade=0.6, rise=12))
    return ovs


# ───────────────────────── 바탕 화면(사진·영상) ─────────────────────────
SW, SH = int(W * 1.3), int(H * 1.3)


def load_image(path: Path) -> Image.Image:
    img = Image.open(path)
    img = ImageOps.exif_transpose(img)
    return img.convert("RGB")


def build_scene(img: Image.Image, force_cover=False) -> Image.Image:
    """세로 사진은 꽉 채우고, 가로 사진은 검은 배경 가운데에 화면 폭으로 놓는다."""
    if force_cover or img.width / img.height <= 0.9:
        return ImageOps.fit(img, (SW, SH), Image.LANCZOS, centering=(0.5, 0.45))
    bg = Image.new("RGB", (SW, SH), (0, 0, 0))
    fh = int(img.height * SW / img.width)
    bg.paste(img.resize((SW, fh), Image.LANCZOS), (0, (SH - fh) // 2))
    return bg


class ImageBase:
    """사진을 천천히 확대/축소하고 살짝 옮긴다(켄 번스 효과)."""

    def __init__(self, scene: Image.Image, dur: float, idx: int, blur_dark=False):
        self.scene = scene
        self.n = max(1, round(dur * FPS))
        self.zoom_in = idx % 2 == 0
        self.pan = 1 if idx % 4 in (0, 3) else -1

    def frame(self, k: int) -> np.ndarray:
        p = ease_in_out(k / max(1, self.n - 1))
        amt = 0.08 if STYLE == "vlog" else 0.045
        z = 1.03 + amt * (p if self.zoom_in else 1 - p)
        cw, ch = SW / z, SH / z
        cx = SW / 2 + self.pan * (p - 0.5) * SW * 0.015
        cy = SH / 2
        box = (cx - cw / 2, cy - ch / 2, cx + cw / 2, cy + ch / 2)
        im = self.scene.transform((W, H), Image.EXTENT, box, Image.BICUBIC)
        return np.array(im)

    def close(self):
        pass


def probe(path: Path) -> dict:
    if not shutil.which("ffprobe"):
        return probe_without_ffprobe(path)
    out = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
                         capture_output=True, text=True, check=True).stdout
    info = json.loads(out)
    v = next((s for s in info["streams"] if s.get("codec_type") == "video"), None)
    if v is None:
        raise ValueError(f"{path.name}: 영상 트랙이 없습니다")
    w, h = int(v["width"]), int(v["height"])
    rot = 0
    for sd in v.get("side_data_list", []) or []:
        if "rotation" in sd:
            rot = int(float(sd["rotation"]))
    rot = int(v.get("tags", {}).get("rotate", rot))
    if abs(rot) % 180 == 90:
        w, h = h, w
    dur = float(info["format"].get("duration") or v.get("duration") or 0)
    has_audio = any(s.get("codec_type") == "audio" for s in info["streams"])
    return dict(w=w, h=h, duration=dur, has_audio=has_audio)


def probe_without_ffprobe(path: Path) -> dict:
    """ffprobe 가 없는 환경: ffmpeg 출력과 첫 장면으로 크기·길이·소리 유무를 알아낸다."""
    err = subprocess.run([FFMPEG, "-hide_banner", "-i", str(path)], capture_output=True, text=True).stderr
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", err)
    dur = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3]) if m else 0.0
    still = video_still(path, 0)  # 회전 정보가 적용된 실제 화면 크기
    return dict(w=still.width, h=still.height, duration=dur, has_audio=bool(re.search(r"Stream #.*Audio:", err)))


class VideoBase:
    def __init__(self, path: Path, start: float, dur: float, meta: dict):
        self.path, self.start, self.dur = path, start, dur
        self.n = max(1, round(dur * FPS))
        cover = meta["w"] / meta["h"] <= 0.9
        if cover:
            self.vf = (f"scale={W}:{H}:force_original_aspect_ratio=increase:flags=lanczos,"
                       f"crop={W}:{H},fps={FPS},format=rgb24")
        else:
            self.vf = (f"scale={W}:-2:flags=lanczos,pad={W}:{H}:0:(oh-ih)/2:black,"
                       f"fps={FPS},format=rgb24")
        self.proc = None
        self.k = -1
        self.last = None

    def frame(self, k: int) -> np.ndarray:
        if self.proc is None:
            self.proc = subprocess.Popen(
                [FFMPEG, "-v", "error", "-ss", f"{self.start:.3f}", "-t", f"{self.dur + 0.5:.3f}",
                 "-i", str(self.path), "-an", "-vf", self.vf, "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                stdout=subprocess.PIPE)
        size = W * H * 3
        while self.k < k:
            buf = self.proc.stdout.read(size) if self.proc.stdout else b""
            if len(buf) < size:  # 영상이 끝났으면 마지막 장면 유지
                if self.last is None:
                    self.last = np.zeros((H, W, 3), np.uint8)
                self.k = k
                break
            self.last = np.frombuffer(buf, np.uint8).reshape(H, W, 3)
            self.k += 1
        return self.last.copy()

    def close(self):
        if self.proc:
            self.proc.stdout.close()
            self.proc.kill()
            self.proc.wait()
            self.proc = None


def video_still(path: Path, t: float) -> Image.Image:
    data = subprocess.run([FFMPEG, "-v", "error", "-ss", f"{t:.3f}", "-i", str(path), "-frames:v", "1",
                           "-f", "image2pipe", "-vcodec", "png", "-"], capture_output=True, check=True).stdout
    return Image.open(io.BytesIO(data)).convert("RGB")


def outro_scene(still: Image.Image, theme: dict) -> Image.Image:
    bg = ImageOps.fit(still, (SW // 12, SH // 12), Image.BILINEAR).filter(ImageFilter.GaussianBlur(2.5))
    bg = bg.resize((SW, SH), Image.BICUBIC)
    return ImageEnhance.Brightness(bg).enhance(0.38)


# ───────────────────────── 설정 읽기 ─────────────────────────
def media_files(folder: Path) -> list[Path]:
    return sorted(p for p in folder.iterdir()
                  if p.suffix.lower() in IMG_EXT | VID_EXT and not p.name.startswith(("."))
                  and not p.stem.endswith(("_reel", "_reel_cover")))


def load_config(folder: Path, args) -> dict:
    cfg_path = args.config or folder / "reel.yaml"
    cfg: dict = {}
    if cfg_path.exists():
        if yaml is None:
            sys.exit("PyYAML 이 필요합니다: pip install pyyaml")
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    for k in ("theme", "style", "title", "subtitle", "handle", "music"):
        v = getattr(args, k, None)
        if v is not None:
            cfg[k] = v
    cfg.setdefault("theme", "info")
    cfg.setdefault("style", "bold")
    if cfg["style"] not in STYLE_DEFAULTS:
        sys.exit(f"style 은 {', '.join(STYLE_DEFAULTS)} 중 하나여야 합니다")
    if cfg["theme"] not in THEMES:
        sys.exit(f"theme 은 {', '.join(THEMES)} 중 하나여야 합니다 (지금: {cfg['theme']})")
    if not cfg.get("clips"):
        cfg["clips"] = [p.name for p in media_files(folder)]
    if not cfg["clips"]:
        sys.exit(f"{folder} 에 사진/영상이 없습니다")
    cfg["clips"] = [c if isinstance(c, dict) else {"file": c} for c in cfg["clips"]]
    if "title" not in cfg:
        cfg["title"] = folder.name
    return cfg


def write_template(folder: Path, theme: str):
    path = folder / "reel.yaml"
    if path.exists():
        sys.exit(f"{path} 가 이미 있습니다. 지우거나 직접 고쳐 주세요.")
    clips = media_files(folder)
    lines = [
        "# 릴스 설정 파일 — 값만 바꾸면 됩니다. 필요 없는 줄은 지워도 됩니다.",
        f"theme: {theme}            # food(맛집) / info(정보) / kids(아이와 가볼 곳) / book(책)",
        f'title: "{folder.name}"      # 첫 화면 큰 제목. 줄바꿈은 \\n',
        'subtitle: ""                # 제목 아래 작은 글',
        'handle: ""                  # 예: "@my_id" (위쪽 표시)',
        "# music: bgm.mp3            # 배경음악(생략 시 폴더 안 음악 파일 자동 사용)",
        "clips:",
    ]
    for p in clips:
        lines.append(f"  - file: {p.name}")
        lines.append('    text: ""                # 화면 자막')
        lines.append('    sub: ""                 # 자막 아래 작은 글')
        if p.suffix.lower() in VID_EXT:
            lines.append("    # start: 0              # 영상 시작 지점(초)")
            lines.append("    # duration: 5           # 쓸 길이(초)")
            lines.append("    # sound: true           # 영상 원래 소리 살리기")
    lines += [
        "outro:",
        '  name: ""                  # 가게·장소·책 이름',
        "  details:                  # 정보 카드 (키: 값). 필요 없으면 지우기",
        '    위치: ""',
        '    영업시간: ""',
        '  # cta: "저장해두고 꼭 가보세요"',
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"만들었습니다: {path}")


# ───────────────────────── 제작 ─────────────────────────
class Segment:
    def __init__(self, base, dur: float, overlays: list[Overlay]):
        self.base, self.dur, self.overlays = base, dur, overlays
        self.nf = max(1, round(dur * FPS))


def build_segments(folder: Path, cfg: dict):
    theme = THEMES[cfg["theme"]]
    d = {**DEFAULTS, **STYLE_DEFAULTS[STYLE], **{k: cfg[k] for k in DEFAULTS if k in cfg}}
    numbered = cfg.get("numbered", theme["numbered"])
    clips = cfg["clips"]
    segs: list[Segment] = []
    audio_parts = []  # (path, src_start, dur, timeline_index)
    first_still = None
    has_music = bool(find_music(folder, cfg))
    n_captioned = sum(1 for c in clips[1:] if clean(c.get("text")) or clean(c.get("sub")))
    num = 0
    for i, c in enumerate(clips):
        path = folder / c["file"]
        if not path.exists():
            sys.exit(f"파일이 없습니다: {path}")
        ext = path.suffix.lower()
        is_title = i == 0 and clean(cfg.get("title"))
        if ext in VID_EXT:
            meta = probe(path)
            start = float(c.get("start", 0))
            avail = max(0.5, meta["duration"] - start)
            dur = float(c.get("duration", min(avail, d["video_duration"])))
            base = VideoBase(path, start, dur, meta)
            still = video_still(path, start + min(dur, avail) / 2)
            sound = c.get("sound", not has_music)
            if sound and meta["has_audio"]:
                audio_parts.append((path, start, dur, i))
        elif ext in IMG_EXT:
            img = load_image(path)
            still = img
            if c.get("crop"):  # 한 사진의 일부를 확대해 별도 장면으로: [왼쪽, 위, 오른쪽, 아래] (0~1 비율)
                x0, y0, x1, y1 = c["crop"]
                img = img.crop((int(x0 * img.width), int(y0 * img.height),
                                int(x1 * img.width), int(y1 * img.height)))
            dflt = d["title_duration"] if is_title else d["image_duration"]
            dur = float(c.get("duration", dflt))
            base = ImageBase(build_scene(img, force_cover=bool(c.get("crop"))), dur, i)
        else:
            sys.exit(f"지원하지 않는 파일 형식: {path.name}")
        if first_still is None:
            first_still = still
        if is_title:
            ovs = title_overlays(theme, cfg["title"], cfg.get("subtitle"))
        else:
            label = None
            if numbered and (clean(c.get("text")) or clean(c.get("sub"))):
                num += 1
                label = f"{num:02d}" + (f" / {n_captioned:02d}" if n_captioned > 1 else "")
            ovs = caption_overlays(theme, c.get("text"), c.get("sub"), label)
        segs.append(Segment(base, dur, ovs))

    outro = cfg.get("outro", {})
    if outro is not False:
        outro = outro or {}
        dur = float(outro.get("duration", d["outro_duration"]))
        scene = outro_scene(first_still, theme)
        segs.append(Segment(ImageBase(scene, dur, 1), dur, outro_overlays(theme, outro, cfg.get("handle", ""))))
    return segs, audio_parts, d


def find_music(folder: Path, cfg: dict):
    m = cfg.get("music")
    if m is False:
        return None
    if m:
        p = Path(m) if Path(m).is_absolute() else folder / m
        if not p.exists():
            sys.exit(f"음악 파일이 없습니다: {p}")
        return p
    found = sorted(p for p in folder.iterdir() if p.suffix.lower() in AUD_EXT)
    return found[0] if found else None


def render(folder: Path, cfg: dict, out: Path, crf: int, preset: str):
    theme = THEMES[cfg["theme"]]
    segs, audio_parts, d = build_segments(folder, cfg)
    xf = max(0, round(d["crossfade"] * FPS))
    starts, s = [], 0
    for seg in segs:
        starts.append(s)
        s += seg.nf - xf
    total_f = starts[-1] + segs[-1].nf
    total = total_f / FPS
    bar = top_bar(theme, cfg.get("handle", ""))
    tmpdir = Path(tempfile.mkdtemp(prefix="reel_"))
    vid_tmp = tmpdir / "video.mp4"
    enc = subprocess.Popen(
        [FFMPEG, "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS),
         "-i", "-", "-vf", "scale=out_color_matrix=bt709:out_range=tv", "-c:v", "libx264", "-preset", preset,
         "-crf", str(crf), "-pix_fmt", "yuv420p", "-profile:v", "high", "-g", str(FPS * 2),
         "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", str(vid_tmp)],
        stdin=subprocess.PIPE)
    cover_f = min(segs[0].nf - xf - 1, int(1.4 * FPS))
    cover = None
    print(f"구간 {len(segs)}개, 길이 {total:.1f}초 제작 중…")
    try:
        for g in range(total_f):
            mixed = None
            for i, seg in enumerate(segs):
                k = g - starts[i]
                if k < 0 or k >= seg.nf:
                    if k >= seg.nf:
                        seg.base.close()
                    continue
                fr = seg.base.frame(k)
                for ov in seg.overlays:
                    ov.apply(fr, k / FPS)
                if mixed is None:
                    mixed = fr
                else:  # 앞 장면과 겹치는 구간 → 부드럽게 전환
                    a = (k + 1) / (xf + 1)
                    mixed = (mixed.astype(np.float32) * (1 - a) + fr.astype(np.float32) * a).astype(np.uint8)
            bar.apply(mixed, g / FPS)
            if g == cover_f:
                cover = mixed.copy()
            enc.stdin.write(mixed.tobytes())
            if g % (FPS * 2) == 0:
                print(f"  {g / FPS:5.1f}s / {total:.1f}s", flush=True)
    finally:
        for seg in segs:
            seg.base.close()
        enc.stdin.close()
        enc.wait()
    if enc.returncode != 0:
        sys.exit("영상 인코딩 실패")

    # 소리: 배경음악 + (원하면) 영상 원래 소리
    music = find_music(folder, cfg)
    inputs, filters, labels = [], [], []
    if music:
        inputs += ["-stream_loop", "-1", "-i", str(music)]
        vol = float(d["music_volume"]) * (0.45 if audio_parts else 1.0)
        filters.append(f"[1:a]atrim=0:{total:.3f},asetpts=PTS-STARTPTS,volume={vol:.2f},"
                       f"afade=t=in:d=0.6,afade=t=out:st={max(0, total - 1.8):.3f}:d=1.8[m]")
        labels.append("[m]")
    for j, (path, st, dur, idx) in enumerate(audio_parts):
        n = 1 + (1 if music else 0) + j
        inputs += ["-i", str(path)]
        delay = int(starts[idx] / FPS * 1000)
        filters.append(f"[{n}:a]atrim={st:.3f}:{st + dur:.3f},asetpts=PTS-STARTPTS,"
                       f"afade=t=in:d=0.25,afade=t=out:st={max(0, dur - 0.4):.3f}:d=0.4,"
                       f"adelay={delay}|{delay},aformat=channel_layouts=stereo[v{j}]")
        labels.append(f"[v{j}]")
    if labels:
        mix = f"{''.join(labels)}amix=inputs={len(labels)}:normalize=0:duration=longest," \
              f"apad,atrim=0:{total:.3f},alimiter=limit=0.95[aout]"
        fc = ";".join(filters + [mix])
        cmd = [FFMPEG, "-y", "-v", "error", "-i", str(vid_tmp), *inputs, "-filter_complex", fc,
               "-map", "0:v", "-map", "[aout]"]
    else:  # 소리가 없으면 무음 트랙을 넣어 둔다
        cmd = [FFMPEG, "-y", "-v", "error", "-i", str(vid_tmp), "-f", "lavfi", "-i",
               "anullsrc=r=48000:cl=stereo", "-map", "0:v", "-map", "1:a", "-shortest"]
    cmd += ["-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
            "-t", f"{total:.3f}", "-movflags", "+faststart", str(out)]
    subprocess.run(cmd, check=True)
    if cover is not None:
        Image.fromarray(cover).save(out.with_name(out.stem + "_cover.jpg"), quality=92)
    shutil.rmtree(tmpdir, ignore_errors=True)
    print(f"완성: {out}  ({total:.1f}초)")
    if cover is not None:
        print(f"커버: {out.with_name(out.stem + '_cover.jpg')}")


FFMPEG = "ffmpeg"


def main():
    global FFMPEG
    ap = argparse.ArgumentParser(description="사진·영상으로 인스타 릴스 만들기")
    ap.add_argument("folder", type=Path, help="사진·영상이 들어 있는 폴더")
    ap.add_argument("--init", action="store_true", help="reel.yaml 틀 만들기")
    ap.add_argument("--theme", choices=list(THEMES))
    ap.add_argument("--style", choices=["magazine", "vlog", "bold"],
                    help="magazine: 작은 글씨 왼쪽 아래 / vlog: 자막 없이 빠르게 / bold: 큰 글씨 가운데")
    ap.add_argument("--config", type=Path, help="설정 파일 (기본: <폴더>/reel.yaml)")
    ap.add_argument("--title")
    ap.add_argument("--subtitle")
    ap.add_argument("--handle")
    ap.add_argument("--music")
    ap.add_argument("--out", type=Path, help="결과 파일 (기본: <폴더>/<폴더이름>_reel.mp4)")
    ap.add_argument("--crf", type=int, default=18, help="화질 (낮을수록 고화질, 기본 18)")
    ap.add_argument("--preset", default="medium", help="x264 속도 (veryfast·medium·slow)")
    args = ap.parse_args()

    FFMPEG = shutil.which("ffmpeg")
    if not FFMPEG:
        try:  # pip install imageio-ffmpeg 로 받은 ffmpeg 사용
            import imageio_ffmpeg
            FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
        except ImportError:
            sys.exit("ffmpeg 가 필요합니다 (apt install ffmpeg 또는 pip install imageio-ffmpeg)")
    folder = args.folder.resolve()
    if not folder.is_dir():
        sys.exit(f"폴더가 아닙니다: {folder}")
    if args.init:
        write_template(folder, args.theme or "info")
        return
    cfg = load_config(folder, args)
    global STYLE
    STYLE = cfg["style"]
    out = (args.out or folder / f"{folder.name}_reel.mp4").resolve()
    render(folder, cfg, out, args.crf, args.preset)


if __name__ == "__main__":
    main()
