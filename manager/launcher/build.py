"""Build `Show Mod Manager.exe` (the manager's launcher) into the manager folder.

    python -I launcher/build.py [--toolchain <llvm-mingw>] [--out <exe>]     (from the manager folder)

Draws launcher/icon.ico (the manager's teal badge, drawn here, no game art) when it is missing, compiles launcher.rc
with llvm-windres and launcher.c with clang from llvm-mingw: --toolchain, else the setup's (settings.setup / llvm).
Writes --out (default: the manager folder's exe). The exe is a GUI program: no console.
"""
import argparse
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
MANAGER = HERE.parent
sys.path.insert(0, str(MANAGER))

from app import config  # noqa: E402

OUT = MANAGER / "Show Mod Manager.exe"
ICON = HERE / "icon.ico"
SIZES = [16, 20, 24, 32, 40, 48, 64, 128, 256]


def _font(size):
    from PIL import ImageFont
    try:
        font = ImageFont.truetype(r"C:\Windows\Fonts\bahnschrift.ttf", size)
        try:
            font.set_variation_by_name("Bold Condensed")
        except Exception:
            pass
        return font
    except OSError:
        return ImageFont.load_default()


def draw_icon(path: Path) -> None:
    """A dark teal disc with a mint-to-cyan ring and a white italic "MM", like the manager's header badge."""
    from PIL import Image, ImageDraw
    big = 1024
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    ring = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    grad = Image.new("RGBA", (big, big))
    gd = ImageDraw.Draw(grad)
    for y in range(big):  # mint (top) to cyan (bottom)
        t = y / (big - 1)
        gd.line([(0, y), (big, y)], fill=(int(94 + (64 - 94) * t), int(236 + (196 - 236) * t),
                                          int(181 + (232 - 181) * t), 255))
    mask = Image.new("L", (big, big), 0)
    ImageDraw.Draw(mask).ellipse([16, 16, big - 16, big - 16], fill=255)
    ring.paste(grad, (0, 0), mask)
    img = Image.alpha_composite(img, ring)
    d = ImageDraw.Draw(img)
    d.ellipse([88, 88, big - 88, big - 88], fill=(10, 52, 58, 255))
    font = _font(470)
    text = "MM"
    box = d.textbbox((0, 0), text, font=font)
    w, h = box[2] - box[0], box[3] - box[1]
    layer = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    ImageDraw.Draw(layer).text(((big - w) / 2 - box[0], (big - h) / 2 - box[1] - 10), text, font=font,
                               fill=(255, 255, 255, 255))
    layer = layer.transform(layer.size, Image.AFFINE, (1, 0.18, -90, 0, 1, 0), resample=Image.BICUBIC)  # italic
    img = Image.alpha_composite(img, layer)
    img.save(path, sizes=[(s, s) for s in SIZES])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Build the manager's launcher exe.")
    parser.add_argument("--toolchain", type=Path, help="an llvm-mingw folder (default: the setup's llvm)")
    parser.add_argument("--out", type=Path, default=OUT, help=f"the exe to write (default: {OUT.name} here)")
    args = parser.parse_args(argv)
    toolchain = args.toolchain
    if toolchain is None:
        settings = config.load(config.MANAGER / "state" / "settings.json")
        if settings.setup is None:
            print("no Fastball setup folder in the settings: pass --toolchain <llvm-mingw>")
            return 1
        toolchain = Path(settings.setup) / "llvm"
    llvm = toolchain / "bin"
    out = args.out.absolute()
    clang, windres = llvm / "clang.exe", llvm / "llvm-windres.exe"
    for tool in (clang, windres):
        if not tool.exists():
            print(f"missing {tool}: the setup folder's llvm-mingw is needed")
            return 1
    if not ICON.exists():
        draw_icon(ICON)
    out.parent.mkdir(parents=True, exist_ok=True)
    res = HERE / "launcher.res.o"
    subprocess.run([str(windres), "-i", str(HERE / "launcher.rc"), "-o", str(res), "--target=pe-x86-64",
                    "-I", str(HERE)], check=True, cwd=HERE)
    # no link timestamp: the same sources give the same exe, and so the same release zip
    subprocess.run([str(clang), "-O2", "-municode", "-mwindows", "-s", "-Wl,--no-insert-timestamp",
                    str(HERE / "launcher.c"), str(res), "-o", str(out)], check=True, cwd=HERE)
    res.unlink(missing_ok=True)
    print(f"built {out} ({out.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
