import io
from pathlib import Path
import plistlib
import shutil

from PIL import Image

ICONS = Path(__file__).resolve().parents[1] / "desktop/library/icons"


def svg_image(name):
    import resvg_py
    return Image.open(io.BytesIO(resvg_py.svg_to_bytes(
        svg_path=str(ICONS / f"{name}.svg"), width=1024, height=1024,
        skip_system_fonts=True))).convert("RGBA")


def write_desktop_icons(image, dest, ico=None):
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    image = image.resize((1024, 1024), Image.Resampling.LANCZOS)
    image.resize((256, 256), Image.Resampling.LANCZOS).save(dest / "icon.png")
    if ico is None:
        image.save(dest / "icon.ico", sizes=[(n, n) for n in (16, 24, 32, 48, 64, 128, 256)])
    else:
        (dest / "icon.ico").write_bytes(ico)
    image.save(dest / "icon.icns")


def set_bundle_icon(bundle, icon):
    contents = Path(bundle) / "Contents"
    resource = contents / "Resources/icon.icns"
    plist = contents / "Info.plist"
    data = plistlib.loads(plist.read_bytes())
    if icon is None:
        resource.unlink(missing_ok=True)
    else:
        resource.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(icon, resource)
    if icon is not None and data.get("CFBundleIconFile") != resource.name:
        data["CFBundleIconFile"] = resource.name
        plist.write_bytes(plistlib.dumps(data))


def game_icons(exe, dest):
    from extract_icon import MissingIconError, load_icon, largest_frame
    if not Path(exe).is_file():
        print(f"icon: original executable unavailable: {exe}")
        return None
    try:
        ico = load_icon(exe)
    except MissingIconError as error:
        print(f"icon: {error}")
        return None
    write_desktop_icons(largest_frame(ico), dest, ico=ico)
    return Path(dest) / "icon.icns"
