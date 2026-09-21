import io
import zipfile
from lxml import etree as ET
import pytest

from pptx_finder.native_clipboard import freeze_theme_colours, NS


def package(theme=True):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as z:
        if theme:
            z.writestr("clipboard/theme/theme1.xml", f'<a:clipboardTheme xmlns:a="{NS}"><a:themeElements><a:clrScheme name="company"><a:accent1><a:srgbClr val="558822"/></a:accent1><a:dk1><a:sysClr val="windowText" lastClr="112233"/></a:dk1></a:clrScheme></a:themeElements></a:clipboardTheme>')
        z.writestr("clipboard/drawings/drawing1.xml", f'<a:sp xmlns:a="{NS}"><a:solidFill><a:schemeClr val="accent1"><a:alpha val="60000"/><a:lumMod val="75000"/></a:schemeClr></a:solidFill><a:schemeClr val="tx1"/></a:sp>')
        z.writestr("clipboard/media/image1.png", b"original image bytes")
    return stream.getvalue()


def test_fixed_colour_keeps_transparency_transforms_and_embedded_images():
    result = freeze_theme_colours(package())
    with zipfile.ZipFile(io.BytesIO(result)) as z:
        root = ET.fromstring(z.read("clipboard/drawings/drawing1.xml"))
        colours = list(root.iter(f"{{{NS}}}srgbClr"))
        assert [c.get("val") for c in colours] == ["558822", "112233"]
        assert colours[0].find(f"{{{NS}}}alpha").get("val") == "60000"
        assert colours[0].find(f"{{{NS}}}lumMod").get("val") == "75000"
        assert z.read("clipboard/media/image1.png") == b"original image bytes"
        assert not list(root.iter(f"{{{NS}}}schemeClr"))


def test_missing_theme_is_an_explicit_error_not_a_raster_fallback():
    with pytest.raises(ValueError, match="原始主题"):
        freeze_theme_colours(package(theme=False))
