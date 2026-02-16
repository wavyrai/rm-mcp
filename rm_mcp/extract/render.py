"""
SVG/PNG rendering for reMarkable .rm files.

Uses rmc for .rm -> SVG conversion, then cairosvg for SVG -> PNG.
"""

import logging
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# reMarkable tablet screen dimensions (in pixels) - used as fallback
REMARKABLE_WIDTH = 1404
REMARKABLE_HEIGHT = 1872

# Standard reMarkable background color (light cream/gray)
# Can be overridden via REMARKABLE_BACKGROUND_COLOR environment variable
_DEFAULT_BACKGROUND_COLOR = "#FBFBFB"


def get_background_color() -> str:
    """Get the background color, checking env var for override."""
    return os.environ.get("REMARKABLE_BACKGROUND_COLOR", _DEFAULT_BACKGROUND_COLOR)


# For backwards compatibility, expose as module constant (evaluated at import)
# Use get_background_color() for runtime evaluation of env var
REMARKABLE_BACKGROUND_COLOR = get_background_color()


def _ensure_cairo_library_path() -> None:
    """Ensure the Cairo C library can be found by cairocffi.

    When running under uvx with uv-managed Python, Homebrew's library path
    (/opt/homebrew/lib on Apple Silicon, /usr/local/lib on Intel Mac) may not
    be in the dynamic linker search path. This causes cairocffi to fail with
    'no library called "cairo" was found' even when Cairo is installed.

    This function adds common library paths to DYLD_FALLBACK_LIBRARY_PATH
    so cairocffi can find libcairo.dylib.
    """
    if sys.platform != "darwin":
        return

    candidates = ["/opt/homebrew/lib", "/usr/local/lib"]
    existing = [p for p in candidates if Path(p, "libcairo.2.dylib").exists()]
    if not existing:
        return

    env_key = "DYLD_FALLBACK_LIBRARY_PATH"
    current = os.environ.get(env_key, "")
    current_paths = set(current.split(":")) if current else set()

    new_paths = [p for p in existing if p not in current_paths]
    if new_paths:
        updated = ":".join(filter(None, [current] + new_paths))
        os.environ[env_key] = updated
        logger.debug("Added %s to %s", new_paths, env_key)


# Auto-configure library path at import time
_ensure_cairo_library_path()


def _find_rmc() -> str:
    """Find the rmc binary, checking the current venv's bin dir first.

    When running under uvx, the venv's bin/ may not be on PATH,
    so subprocess.run(["rmc", ...]) would fail with FileNotFoundError.
    """
    # Check next to the current Python executable (same venv bin dir)
    venv_rmc = Path(sys.executable).parent / "rmc"
    if venv_rmc.is_file():
        return str(venv_rmc)
    # Fall back to PATH lookup
    found = shutil.which("rmc")
    if found:
        return found
    raise FileNotFoundError("rmc binary not found")

# Margin around content when using content-based bounding box (in pixels)
CONTENT_MARGIN = 50


def _parse_hex_color(hex_color: str) -> tuple:
    """Parse a hex color string to RGBA tuple.

    Supports #RRGGBB (RGB) and #RRGGBBAA (RGBA) formats.

    Args:
        hex_color: Hex color string (e.g., "#FFFFFF" or "#FFFFFF80")

    Returns:
        Tuple of (r, g, b, a) values (0-255)
    """
    if not hex_color.startswith("#"):
        return (255, 255, 255, 255)

    hex_str = hex_color.lstrip("#")
    if len(hex_str) == 6:
        r, g, b = tuple(int(hex_str[i : i + 2], 16) for i in (0, 2, 4))
        return (r, g, b, 255)
    elif len(hex_str) == 8:
        r, g, b, a = tuple(int(hex_str[i : i + 2], 16) for i in (0, 2, 4, 6))
        return (r, g, b, a)
    else:
        return (255, 255, 255, 255)


def _get_svg_content_bounds(svg_path: Path) -> Optional[tuple]:
    """
    Parse SVG file to get the content bounding box from viewBox.

    Args:
        svg_path: Path to the SVG file

    Returns:
        Tuple of (min_x, min_y, width, height) or None if not determinable
    """
    import xml.etree.ElementTree as ET

    try:
        tree = ET.parse(svg_path)
        root = tree.getroot()

        # Try to get viewBox attribute
        viewbox = root.get("viewBox")
        if viewbox:
            parts = viewbox.split()
            if len(parts) == 4:
                return (float(parts[0]), float(parts[1]), float(parts[2]), float(parts[3]))

        # Fallback to width/height attributes
        width = root.get("width")
        height = root.get("height")
        if width and height:
            # Remove 'px' suffix if present
            w = float(width.replace("px", ""))
            h = float(height.replace("px", ""))
            return (0, 0, w, h)

        return None
    except Exception:
        return None


def render_rm_file_to_png(
    rm_file_path: Path, background_color: Optional[str] = None
) -> Optional[bytes]:
    """
    Render a .rm file to PNG image bytes.

    Uses rmc to convert .rm to SVG, then cairosvg to convert to PNG.
    The output is sized based on the SVG content bounds with a margin.

    Args:
        rm_file_path: Path to the .rm file
        background_color: Background color (e.g., "#FFFFFF", "transparent", None).
                         None means transparent. Use REMARKABLE_BACKGROUND_COLOR
                         for the standard reMarkable paper color.

    Returns:
        PNG image bytes, or None if rendering failed
    """
    import subprocess
    import tempfile

    tmp_svg_path = None
    tmp_png_path = None
    tmp_raw_path = None

    try:
        # Create temp files
        with tempfile.NamedTemporaryFile(suffix=".svg", delete=False) as tmp_svg:
            tmp_svg_path = Path(tmp_svg.name)
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp_png:
            tmp_png_path = Path(tmp_png.name)

        # Convert .rm to SVG using rmc
        result = subprocess.run(
            [_find_rmc(), "-t", "svg", "-o", str(tmp_svg_path), str(rm_file_path)],
            capture_output=True,
            timeout=30,
        )
        if result.returncode != 0:
            return None

        # Get content bounds from SVG
        bounds = _get_svg_content_bounds(tmp_svg_path)
        if bounds:
            # Use content bounds with margin
            _, _, content_width, content_height = bounds
            output_width = int(content_width) + 2 * CONTENT_MARGIN
            output_height = int(content_height) + 2 * CONTENT_MARGIN
        else:
            # Fallback to standard reMarkable dimensions
            output_width = REMARKABLE_WIDTH
            output_height = REMARKABLE_HEIGHT

        # Convert SVG to PNG
        try:
            import cairosvg
            from PIL import Image as PILImage

            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp_raw:
                tmp_raw_path = Path(tmp_raw.name)

            # Use cairosvg with background_color if specified
            cairosvg.svg2png(
                url=str(tmp_svg_path),
                write_to=str(tmp_raw_path),
                output_width=output_width,
                output_height=output_height,
                background_color=background_color,
            )

            # If no background color specified (transparent), return as-is
            if background_color is None:
                with open(tmp_raw_path, "rb") as f:
                    return f.read()

            # If background color specified, ensure it's applied properly
            img = PILImage.open(tmp_raw_path)
            if img.mode == "RGBA" and background_color:
                # Parse hex color (supports #RRGGBB and #RRGGBBAA formats)
                r, g, b, a = _parse_hex_color(background_color)
                # Create background and composite foreground on top
                if a == 255:
                    # Fully opaque background - convert to RGB
                    bg = PILImage.new("RGB", img.size, (r, g, b))
                    bg.paste(img, mask=img.split()[3])
                    img = bg
                elif a > 0:
                    # Semi-transparent or transparent background
                    bg = PILImage.new("RGBA", img.size, (r, g, b, a))
                    img = PILImage.alpha_composite(bg, img)
                # If a == 0 (fully transparent), return as-is
            img.save(tmp_png_path)

            with open(tmp_png_path, "rb") as f:
                return f.read()

        except ImportError:
            raise RuntimeError(
                "cairosvg is required for PNG rendering. "
                "Install it with: pip install cairosvg"
            )

    except subprocess.TimeoutExpired:
        logger.warning("rmc timed out rendering %s", rm_file_path)
        return None
    except FileNotFoundError:
        logger.warning("rmc binary not found — is the 'rmc' package installed?")
        return None
    except Exception:
        logger.exception("Failed to render %s to PNG", rm_file_path)
        return None
    finally:
        if tmp_svg_path:
            tmp_svg_path.unlink(missing_ok=True)
        if tmp_png_path:
            tmp_png_path.unlink(missing_ok=True)
        if tmp_raw_path:
            tmp_raw_path.unlink(missing_ok=True)


def render_rm_file_to_svg(
    rm_file_path: Path, background_color: Optional[str] = None
) -> Optional[str]:
    """
    Render a .rm file to SVG string.

    Uses rmc to convert .rm to SVG, optionally adding a background.

    Args:
        rm_file_path: Path to the .rm file
        background_color: Background color (e.g., "#FFFFFF", None for transparent).
                         Use REMARKABLE_BACKGROUND_COLOR for the standard paper color.

    Returns:
        SVG content as string, or None if rendering failed
    """
    import subprocess
    import tempfile

    tmp_svg_path = None

    try:
        # Create temp file for SVG output
        with tempfile.NamedTemporaryFile(suffix=".svg", delete=False) as tmp_svg:
            tmp_svg_path = Path(tmp_svg.name)

        # Convert .rm to SVG using rmc
        result = subprocess.run(
            [_find_rmc(), "-t", "svg", "-o", str(tmp_svg_path), str(rm_file_path)],
            capture_output=True,
            timeout=30,
        )
        if result.returncode != 0:
            return None

        # Read SVG content
        svg_content = tmp_svg_path.read_text()

        # Add background rectangle if color specified
        if background_color:
            svg_content = _add_svg_background(svg_content, background_color)

        return svg_content

    except subprocess.TimeoutExpired:
        logger.warning("rmc timed out rendering %s to SVG", rm_file_path)
        return None
    except FileNotFoundError:
        logger.warning("rmc binary not found — is the 'rmc' package installed?")
        return None
    except Exception:
        logger.exception("Failed to render %s to SVG", rm_file_path)
        return None
    finally:
        if tmp_svg_path:
            tmp_svg_path.unlink(missing_ok=True)


def _add_svg_background(svg_content: str, background_color: str) -> str:
    """Add a background rectangle to an SVG.

    Inserts a rect element as the first child of the SVG to act as background.

    Args:
        svg_content: Original SVG content
        background_color: Background color (e.g., "#FFFFFF")

    Returns:
        SVG content with background added
    """
    import re

    # Find the opening <svg> tag and its attributes
    svg_match = re.search(r"(<svg[^>]*>)", svg_content, re.IGNORECASE)
    if not svg_match:
        return svg_content

    svg_tag = svg_match.group(1)

    # Extract viewBox or width/height for the background rect dimensions
    viewbox_match = re.search(r'viewBox="([^"]*)"', svg_tag)
    if viewbox_match:
        viewbox = viewbox_match.group(1)
        parts = viewbox.split()
        if len(parts) == 4:
            x, y, width, height = parts
            bg_rect = (
                f'<rect x="{x}" y="{y}" width="{width}" '
                f'height="{height}" fill="{background_color}"/>'
            )
        else:
            # Fallback to full page
            bg_rect = f'<rect x="0" y="0" width="100%" height="100%" fill="{background_color}"/>'
    else:
        # No viewBox, use 100% dimensions
        bg_rect = f'<rect x="0" y="0" width="100%" height="100%" fill="{background_color}"/>'

    # Insert background rect right after the opening svg tag
    insert_pos = svg_match.end()
    return svg_content[:insert_pos] + bg_rect + svg_content[insert_pos:]


def render_page_from_document_zip_svg(
    zip_path: Path, page: int = 1, background_color: Optional[str] = None
) -> Optional[str]:
    """
    Render a specific page from a reMarkable document zip to SVG.

    Args:
        zip_path: Path to the document zip file
        page: Page number (1-indexed)
        background_color: Background color (e.g., "#FFFFFF", None for transparent).
                         Use REMARKABLE_BACKGROUND_COLOR for the standard paper color.

    Returns:
        SVG content as string, or None if rendering failed or page doesn't exist
    """
    from rm_mcp.extract.notebook import _get_ordered_rm_files, _safe_extractall

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)

        with zipfile.ZipFile(zip_path, "r") as zf:
            _safe_extractall(zf, tmpdir_path)

        rm_files = _get_ordered_rm_files(tmpdir_path)

        # Validate page number
        if page < 1 or page > len(rm_files):
            return None

        # Render the requested page (None = blank page)
        target_rm_file = rm_files[page - 1]
        if target_rm_file is None:
            bg = background_color or "#FBFBFB"
            return (
                f'<svg xmlns="http://www.w3.org/2000/svg" '
                f'width="{REMARKABLE_WIDTH}" height="{REMARKABLE_HEIGHT}">'
                f'<rect width="100%" height="100%" fill="{bg}"/>'
                f"</svg>"
            )
        return render_rm_file_to_svg(target_rm_file, background_color=background_color)


def render_page_from_document_zip(
    zip_path: Path, page: int = 1, background_color: Optional[str] = None
) -> Optional[bytes]:
    """
    Render a specific page from a reMarkable document zip to PNG.

    Args:
        zip_path: Path to the document zip file
        page: Page number (1-indexed)
        background_color: Background color (e.g., "#FFFFFF", None for transparent).
                         Use REMARKABLE_BACKGROUND_COLOR for the standard paper color.

    Returns:
        PNG image bytes, or None if rendering failed or page doesn't exist
    """
    from rm_mcp.extract.notebook import _get_ordered_rm_files, _safe_extractall

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)

        with zipfile.ZipFile(zip_path, "r") as zf:
            _safe_extractall(zf, tmpdir_path)

        rm_files = _get_ordered_rm_files(tmpdir_path)

        # Validate page number
        if page < 1 or page > len(rm_files):
            return None

        # Render the requested page (None = blank page)
        target_rm_file = rm_files[page - 1]
        if target_rm_file is None:
            # Render blank page as PNG via cairosvg
            import cairosvg

            bg = background_color or "#FBFBFB"
            blank_svg = (
                f'<svg xmlns="http://www.w3.org/2000/svg" '
                f'width="{REMARKABLE_WIDTH}" height="{REMARKABLE_HEIGHT}">'
                f'<rect width="100%" height="100%" fill="{bg}"/>'
                f"</svg>"
            )
            return cairosvg.svg2png(
                bytestring=blank_svg.encode("utf-8"),
                output_width=REMARKABLE_WIDTH,
                output_height=REMARKABLE_HEIGHT,
            )
        return render_rm_file_to_png(target_rm_file, background_color=background_color)


